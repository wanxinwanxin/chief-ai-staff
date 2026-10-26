"""Build one snapshot of the whole org: departments, teams, agents, spend, agenda.

A department is one project folder under the portfolio roots (default ~/Github). The
folder name is its key, so the same project on two machines (nodes) is one department.
A team is one cmux workspace or one tmux session. A department can have zero, one, or
more teams. The team identity is its workspace, and its folder connects it to a
department, because cmux changes workspace titles as agents work.
"""

import os
import re
import time
from datetime import datetime

from . import agenda, nodes, portfolio, pricing, state
from .sources.logs import usage_by_hour, usage_since

RANGES = nodes.RANGES
CHIEF_TITLE = "Chief of Staff"
CHIEF_SESSION = "chief"  # The tmux session of the chief on a tmux node.
STATUS_ORDER = ["needs_input", "working", "idle", "ended"]
COLOR_SLOTS = 8
RUNNING_WINDOW = 120  # A sub-agent log that changed in the last 2 minutes is running.
ASK_WINDOW = 7 * 86400  # An unanswered question older than this is stale.
STATUS_RANK = {s: i for i, s in enumerate(state.STATUSES)}
FOCUS_RANK = {"today": 0, "week": 1}

# The end of a reply that asks the user something. Agents finish a turn with a
# question and then show as idle, so the hook status alone misses them.
_ASKS = re.compile(
    r"\?|\b(should i|shall i|do you want|would you like|want me to|let me know|"
    r"please (confirm|approve|review|decide|choose)|your (call|decision|approval)|"
    r"which (option|one|approach)|waiting for (you|your))\b", re.I)


def asks_user(last_user, tail):
    """True when the agent spoke last and the end of its reply asks the user something."""
    if not tail or not tail[1]:
        return False
    if last_user and last_user[0] and tail[0] and last_user[0] > tail[0]:
        return False
    return bool(_ASKS.search(tail[1][-220:]))


_TITLE_NOISE = re.compile(r"^[\s✳◐◑◒◓⠁-⣿·•*]+")


def _clean_title(title):
    return _TITLE_NOISE.sub("", title or "").strip()


def _pretty_folder(cwd):
    if not cwd:
        return "Unknown"
    name = os.path.basename(cwd.rstrip("/")) or cwd
    return name.replace("-", " ").replace("_", " ").strip().capitalize()


def _within(child, parent):
    if not child or not parent:
        return False
    child, parent = child.rstrip("/"), parent.rstrip("/")
    return child == parent or child.startswith(parent + "/")


def _empty_tokens():
    return {"input": 0, "output": 0, "cacheWrite": 0, "cacheRead": 0}


class _Meter:
    """Accumulate tokens and API-equivalent cost across models."""

    def __init__(self):
        self.tokens = _empty_tokens()
        self.cost = 0.0
        self.unpriced_tokens = 0
        self.models = {}

    def add(self, model, vec):
        t = self.tokens
        t["input"] += vec[0]
        t["output"] += vec[1]
        t["cacheWrite"] += vec[2] + vec[3]
        t["cacheRead"] += vec[4]
        c = pricing.cost(model, vec)
        if c is None:
            self.unpriced_tokens += sum(vec)
        else:
            self.cost += c
            self.models[model] = self.models.get(model, 0.0) + c

    def merge(self, other):
        for k in self.tokens:
            self.tokens[k] += other.tokens[k]
        self.cost += other.cost
        self.unpriced_tokens += other.unpriced_tokens
        for m, c in other.models.items():
            self.models[m] = self.models.get(m, 0.0) + c

    def as_dict(self):
        t = self.tokens
        prompt = t["input"] + t["cacheWrite"] + t["cacheRead"]
        return {
            "costUsd": round(self.cost, 4),
            "tokens": dict(t, total=sum(t.values())),
            "unpricedTokens": self.unpriced_tokens,
            "cacheHitRate": round(t["cacheRead"] / prompt, 3) if prompt else None,
            "models": {m: round(c, 4) for m, c in sorted(self.models.items(), key=lambda x: -x[1])},
        }


def _meter_for(entry, since):
    meter = _Meter()
    for model, vec in usage_since(entry, since).items():
        meter.add(model, vec)
    return meter


def _bucket_key(ts, bucket):
    lt = time.localtime(ts)
    if bucket == "hour":
        return time.strftime("%Y-%m-%d %H:00", lt)
    return time.strftime("%Y-%m-%d", lt)


def _bucket_labels(since, now, bucket):
    step = 3600 if bucket == "hour" else 86400
    labels, ts = [], since
    while ts <= now + step:
        key = _bucket_key(ts, bucket)
        if key not in labels:
            labels.append(key)
        ts += step
    return [k for k in labels if k <= _bucket_key(now, bucket)]


def _add_series(series, entry, since, bucket):
    for hour, models in usage_by_hour(entry, since).items():
        key = _bucket_key(hour, bucket)
        for model, vec in models.items():
            c = pricing.cost(model, vec)
            if c:
                series[key] = series.get(key, 0.0) + c


def _rollup(statuses):
    for s in STATUS_ORDER:
        if s in statuses:
            return s
    return "none"


def _entry_cwd(entry, files, by_session):
    if entry.get("parentSessionId"):
        parent_path = by_session.get(entry["parentSessionId"])
        if parent_path:
            return files[parent_path].get("cwd")
    return entry.get("cwd")


def _day(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def build(range_key="7d", node_list=None):
    now = time.time()
    span = RANGES.get(range_key, RANGES["7d"])
    since = now - span
    bucket = "hour" if span <= 86400 else "day"
    ctx = {"now": now, "since": since, "bucket": bucket, "range": range_key, "org": state.load_org()}
    org = ctx["org"]
    node_list = nodes.gather() if node_list is None else node_list

    # Each node adds its teams, and the folders, infos, and logs of its departments.
    # A department key is the folder name, so one project on two machines is one department.
    acc = {"paths": {}, "infos": {}, "entries": {}, "outside": {}}
    teams, chiefs, node_rows = [], [], []
    for n in node_list:
        n_teams, n_chiefs = _node(n, ctx, acc)
        teams += n_teams
        chiefs += n_chiefs
        agents = [a for t in n_teams + n_chiefs for a in t["agents"]]
        node_rows.append({"node": n["node"], "label": n.get("label") or n["node"],
                          "terminal": n.get("terminal"), "online": bool(n.get("online")),
                          "local": bool(n.get("local")), "at": n.get("receivedAt") or n.get("at"),
                          "teams": len(n_teams), "agents": len(agents),
                          "working": sum(1 for a in agents if a["status"] == "working")})

    # A cmux workspace that only shows a tmux session of another node is the same team.
    by_session = {t["workspaceId"]: t for t in teams + chiefs if t["terminal"] == "tmux"}
    kept = []
    for t in teams:
        twin = by_session.get(t["task"]) if t["terminal"] == "cmux" and not t["agents"] else None
        if twin:
            twin["mirror"] = {"node": t["node"], "workspaceId": t["workspaceId"], "workspaceRef": t["workspaceRef"]}
        else:
            kept.append(t)
    teams = kept
    chief = chiefs[0] if chiefs else None

    series_total = {}
    for c in chiefs:
        for k, v in c["series"].items():
            series_total[k] = series_total.get(k, 0.0) + v

    day_keys = [_day(now - i * 86400) for i in range(portfolio.DAYS - 1, -1, -1)]
    departments = []
    for key, by_node in acc["paths"].items():
        departments.append(_department(key, by_node, _merge_info(acc["infos"].get(key, [])),
                                       org["projects"].get(key, {}),
                                       [t for t in teams if t["department"] == key],
                                       acc["entries"].get(key, []), since, bucket, now, day_keys))
    for d in departments:
        for k, v in d["series"].items():
            series_total[k] = series_total.get(k, 0.0) + v

    # Stable colors: the color follows the department, never its rank.
    slotted = sorted((d for d in departments if d["status"] == "active"), key=lambda d: d["key"])
    slots = {d["key"]: i for i, d in enumerate(slotted[:COLOR_SLOTS])}
    for d in departments:
        d["colorSlot"] = slots.get(d["key"])
    by_key = {d["key"]: d for d in departments}
    for t in teams:
        d = by_key.get(t["department"])
        t["colorSlot"] = d["colorSlot"] if d else None
        t["departmentName"] = d["name"] if d else _pretty_folder(t["cwd"])
        t["name"] = t["departmentName"]
        t["meta"] = {k: (d or {}).get(k) for k in ("goal", "deadline", "focus", "next", "notes")}

    other = []
    other_meter = _Meter()
    other_series = {}
    for (label, cwd), entries in acc["outside"].items():
        m = _Meter()
        last = 0
        for entry in entries:
            m.merge(_meter_for(entry, since))
            _add_series(other_series, entry, since, bucket)
            last = max(last, entry.get("lastTs") or 0)
        if m.cost or m.unpriced_tokens:
            other.append({"cwd": cwd, "node": label, "name": _pretty_folder(cwd), "lastActivity": last,
                          **m.as_dict()})
            other_meter.merge(m)
    other.sort(key=lambda o: -o["costUsd"])
    for k, v in other_series.items():
        series_total[k] = series_total.get(k, 0.0) + v

    teams.sort(key=lambda t: (STATUS_ORDER.index(t["status"]) if t["status"] in STATUS_ORDER else 9,
                              -(t["lastActivity"] or 0)))
    departments.sort(key=lambda d: (STATUS_RANK.get(d["status"], 9), FOCUS_RANK.get(d["focus"], 9),
                                    -(d["lastActivity"] or 0)))

    total = _Meter()
    for d in departments:
        total.cost += d["costUsd"]
        total.unpriced_tokens += d["unpricedTokens"]
        for k in total.tokens:
            total.tokens[k] += d["tokens"][k]
    for c in chiefs:
        total.cost += c["costUsd"]
        for k in total.tokens:
            total.tokens[k] += c["tokens"][k]
    total.merge(other_meter)

    agents_all = [a for t in teams for a in t["agents"]]
    counts = {s: sum(1 for a in agents_all if a["status"] == s) for s in STATUS_ORDER}
    needs = []
    for t in teams:
        for a in t["agents"]:
            if a["status"] == "needs_input":
                needs.append({"project": t["name"], "task": t["task"], "agent": a["title"], "node": t["nodeLabel"],
                              "teamKey": t["key"], "workspaceId": t["workspaceId"], "surfaceId": a["surfaceId"],
                              "surfaceRef": a["surfaceRef"], "reason": a.get("waitReason"),
                              "since": a["lastActivity"], "lastAssistant": a["lastAssistant"],
                              "lastAssistantTail": a.get("lastAssistantTail")})

    snap = {
        "generatedAt": now,
        "range": {"key": range_key, "since": since, "bucket": bucket,
                  "buckets": _bucket_labels(since, now, bucket)},
        "cmux": any(r["terminal"] == "cmux" for r in node_rows),
        "nodes": node_rows,
        "roots": state.load_config()["roots"],
        "chief": chief,
        "departments": departments,
        "teams": teams,
        "outside": {"projects": other, "series": {k: round(v, 4) for k, v in other_series.items()},
                    **other_meter.as_dict()},
        "totals": dict(total.as_dict(), agents=counts,
                       departments={s: sum(1 for d in departments if d["status"] == s)
                                    for s in state.STATUSES},
                       series={k: round(v, 4) for k, v in series_total.items()}),
        "needsYou": needs,
        "note": "Dollar values are API-equivalent estimates from list prices. "
                "Subscription plans bill differently.",
    }
    snap["agenda"] = agenda.build(snap)
    return snap


def _node(n, ctx, acc):
    """Teams and chief teams of one node. Add its departments and logs to `acc`."""
    name, label = n["node"], n.get("label") or n["node"]
    since, now, bucket, org = ctx["since"], ctx["now"], ctx["bucket"], ctx["org"]
    files = n.get("logs") or {}
    by_session, children = {}, {}
    for path, entry in files.items():
        parent = entry.get("parentSessionId")
        if parent:
            children.setdefault(parent, []).append(path)
        elif entry.get("sessionId"):
            by_session[entry["sessionId"]] = path

    port = n.get("portfolio") or {}
    folders = list(port)
    for f in folders:
        key = portfolio.key(f)
        acc["paths"].setdefault(key, {}).setdefault(name, f)
        acc["infos"].setdefault(key, []).append(port[f])

    agent_dir = n.get("agentDir")
    assigned = set()
    teams, chiefs = [], []
    for raw in n.get("teams") or []:
        agents = [_agent(ra, files, by_session, children, since, now, assigned)
                  for ra in raw["agents"] if org["roles"].get(ra["surfaceId"]) != "ignore"]
        cwd = raw.get("cwd") or (agents[0]["cwd"] if agents else None)
        for i, a in enumerate(agents):
            a["role"] = org["roles"].get(a["surfaceId"]) or ("coordinator" if i == 0 else "worker")
        agents.sort(key=lambda a: 0 if a["role"] == "coordinator" else 1)
        is_chief = raw["title"] == CHIEF_TITLE or raw["workspaceId"] == CHIEF_SESSION or _within(cwd, agent_dir)
        team = {
            "key": raw["key"],
            "node": name,
            "nodeLabel": label,
            "terminal": n.get("terminal"),
            "online": bool(n.get("online")),
            "workspaceId": raw["workspaceId"],
            "workspaceRef": raw["workspaceRef"],
            "task": _clean_title(raw["title"]),
            "cwd": cwd,
            "folder": os.path.basename((cwd or "").rstrip("/")),
            "branch": raw.get("branch"),
            "dirty": raw.get("dirty"),
            "selected": raw.get("selected"),
            "agents": agents,
            "isChief": is_chief,
        }
        if is_chief:
            team["name"] = CHIEF_TITLE
            chiefs.append(team)
        else:
            folder = portfolio.match(cwd, folders) or cwd
            if folder and folder not in folders:
                folders.append(folder)
                acc["paths"].setdefault(portfolio.key(folder), {}).setdefault(name, folder)
            team["department"] = portfolio.key(folder) if folder else None
            teams.append(team)

    # Logs that no live agent owns go to the team whose folder contains them.
    owners = sorted([t for t in teams + chiefs if t.get("cwd")], key=lambda t: -len(t["cwd"]))
    earlier = {t["key"]: [] for t in owners}
    for path, entry in files.items():
        cwd = _entry_cwd(entry, files, by_session)
        chief_log = _within(cwd, agent_dir)
        folder = None if chief_log else portfolio.match(cwd, folders)
        if folder:
            acc["entries"].setdefault(portfolio.key(folder), []).append(entry)
        if path in assigned or (entry.get("lastTs") or 0) < since:
            continue
        owner = next((t for t in owners if _within(cwd, t["cwd"])), None)
        if owner:
            earlier[owner["key"]].append(entry)
        elif not folder and not chief_log:
            acc["outside"].setdefault((label, cwd or "?"), []).append(entry)

    git_fn = n.get("gitFn")
    git_data = n.get("git") or {}
    for t in teams + chiefs:
        meter = _Meter()
        series = {}
        statuses = set()
        last = 0
        for a in t["agents"]:
            meter.merge(a.pop("_meter"))
            for path in a.pop("_paths"):
                _add_series(series, files[path], since, bucket)
            statuses.add(a["status"])
            last = max(last, a["lastActivity"] or 0)
        e_meter, e_sessions = _Meter(), set()
        for entry in earlier.get(t["key"], []):
            m = _meter_for(entry, since)
            if not (m.cost or m.unpriced_tokens):
                continue
            e_meter.merge(m)
            e_sessions.add(entry.get("parentSessionId") or entry.get("sessionId"))
            _add_series(series, entry, since, bucket)
            last = max(last, entry.get("lastTs") or 0)
        meter.merge(e_meter)
        t["earlier"] = dict(e_meter.as_dict(), sessions=len(e_sessions))
        t.update(meter.as_dict())
        t["series"] = {k: round(v, 4) for k, v in series.items()}
        t["status"] = _rollup(statuses)
        t["lastActivity"] = last or None
        if git_fn:
            t["output"] = git_fn(t["cwd"], since)
        else:
            t["output"] = (git_data.get(t["cwd"]) or {}).get(ctx["range"])
    return teams, chiefs


def _merge_info(items):
    """Join the portfolio data of one department from several machines."""
    if len(items) <= 1:
        return items[0] if items else {}
    gits = [i["git"] for i in items if i.get("git")]
    g = None
    if gits:
        lasts = [x["lastCommit"] for x in gits if x.get("lastCommit")]
        days = {}
        for x in gits:  # The same commits can be on both machines, so take the larger count.
            for d, c in (x.get("commitDays") or {}).items():
                days[d] = max(days.get(d, 0), c)
        g = {"lastCommit": max(lasts, key=lambda c: c["at"]) if lasts else None, "commitDays": days,
             "repos": max(x.get("repos") or 0 for x in gits)}
    return {"git": g, "mtime": max((i.get("mtime") or 0) for i in items) or None,
            "description": next((i["description"] for i in items if i.get("description")), None)}


def _department(key, by_node, info, meta, teams, entries, since, bucket, now, day_keys):
    g = info.get("git") or {}
    meter = _Meter()
    series = {}
    hours_by_day, cost_by_day = {}, {}
    last_agent = 0
    for entry in entries:
        meter.merge(_meter_for(entry, since))
        _add_series(series, entry, since, bucket)
        last_agent = max(last_agent, entry.get("lastTs") or 0)
        for hour, models in entry.get("hours", {}).items():
            d = _day(int(hour))
            hours_by_day.setdefault(d, set()).add(int(hour))
            c = sum(pricing.cost(m, v) or 0 for m, v in models.items())
            cost_by_day[d] = cost_by_day.get(d, 0.0) + c
    last_commit = (g.get("lastCommit") or {}).get("at")
    team_last = max([t["lastActivity"] or 0 for t in teams] or [0])
    signals = [last_commit or 0, last_agent, team_last]
    if not g:
        signals.append(info.get("mtime") or 0)
    last = max(signals) or None
    suggested = portfolio.suggest(last, bool(teams), now)
    commit_days = g.get("commitDays") or {}
    daily = [{"d": k, "commits": commit_days.get(k, 0), "agentHours": len(hours_by_day.get(k, ())),
              "costUsd": round(cost_by_day.get(k, 0.0), 2)} for k in day_keys]
    prev = [cost_by_day.get(k, 0.0) for k in day_keys[-7:-1]]
    team_statuses = {t["status"] for t in teams}
    return {
        "key": key,
        "path": next(iter(by_node.values())),
        "paths": by_node,
        "nodes": sorted(by_node),
        "name": meta.get("name") or _pretty_folder(key),
        "folder": key,
        "description": info.get("description"),
        "inRoots": bool(info),
        "status": meta.get("status") or suggested,
        "statusSource": "you" if meta.get("status") else "suggested",
        "suggested": suggested,
        "focus": meta.get("focus"),
        "goal": meta.get("goal"),
        "deadline": meta.get("deadline"),
        "next": meta.get("next"),
        "notes": meta.get("notes"),
        "lastActivity": last,
        "lastCommit": g.get("lastCommit"),
        "lastAgent": last_agent or None,
        "isRepo": bool(g),
        "teams": [t["key"] for t in teams],
        "teamStatus": _rollup(team_statuses) if teams else "none",
        "uncommitted": sum((t["output"] or {}).get("uncommitted", 0) for t in teams),
        "daily": daily,
        "cost24h": round(cost_by_day.get(day_keys[-1], 0.0), 2),
        "costPrevAvg": round(sum(prev) / len(prev), 2) if prev else 0.0,
        "series": {k: round(v, 4) for k, v in series.items()},
        **meter.as_dict(),
    }


def _agent(ra, files, by_session, children, since, now, assigned):
    sess = ra["session"]
    path = sess.get("transcriptPath")
    if not path or path not in files:
        path = by_session.get(sess.get("sessionId"))
    own = files.get(path) if path else None
    meter = _meter_for(own, since) if own else _Meter()
    paths_used = [path] if own else []
    if own:
        assigned.add(path)
    subs = []
    sub_meter = _Meter()
    for sub_path in children.get(sess.get("sessionId"), []):
        entry = files[sub_path]
        assigned.add(sub_path)
        if (entry.get("lastTs") or 0) < since:
            continue
        m = _meter_for(entry, since)
        sub_meter.merge(m)
        paths_used.append(sub_path)
        subs.append({
            "id": entry.get("sessionId"),
            "description": entry.get("description") or "Sub-agent",
            "agentType": entry.get("agentType"),
            "lastActivity": entry.get("lastTs"),
            "running": (now - (entry.get("mtime") or 0)) < RUNNING_WINDOW,
            **m.as_dict(),
        })
    subs.sort(key=lambda s: -(s["lastActivity"] or 0))
    meter.merge(sub_meter)
    last_user = (own or {}).get("lastUser")
    last_asst = (own or {}).get("lastAssistant")
    last_tail = (own or {}).get("lastAssistantTail")
    last = max([t for t in [(own or {}).get("lastTs"), sess.get("updatedAt")] if t] or [0])
    status = sess["status"]
    reason = sess.get("waitReason") or ("prompt" if status == "needs_input" else None)
    if status == "idle" and last_tail and now - (last_tail[0] or 0) < ASK_WINDOW and asks_user(last_user, last_tail):
        status, reason = "needs_input", "question"
    agent = {
        "id": sess.get("sessionId"),
        "kind": sess["kind"],
        "surfaceId": ra["surfaceId"],
        "surfaceRef": ra["surfaceRef"],
        "paneId": ra.get("paneId"),
        "cwd": sess.get("cwd"),
        "title": _clean_title(ra["title"]) or ("Claude" if sess["kind"] == "claude" else "Codex"),
        "remoteControl": bool(sess.get("remoteControl")),
        "status": status,
        "waitReason": reason,
        "lastActivity": last or None,
        "lastUser": last_user,
        "lastAssistant": last_asst,
        "lastAssistantTail": last_tail,
        "subagents": subs,
        "subagentCostUsd": round(sub_meter.cost, 4),
        "_meter": meter,
        "_paths": paths_used,
    }
    agent.update(meter.as_dict())
    return agent


def ago(ts, now=None):
    if not ts:
        return "never"
    secs = max(0, int((now or time.time()) - ts))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def local_time(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")
