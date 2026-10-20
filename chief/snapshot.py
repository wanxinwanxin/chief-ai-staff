"""Build one snapshot of the whole org: departments, teams, agents, spend, agenda.

A department is one project folder under the portfolio roots (default ~/Github).
A team is one cmux workspace (one tab group in the left sidebar). A department can
have zero, one, or more teams. The team identity is its workspace, and its folder
connects it to a department, because cmux changes workspace titles as agents work.
"""

import os
import re
import time
from datetime import datetime

from . import agenda, paths, portfolio, pricing, state
from .sources import cmux, git
from .sources.logs import LogIndex, usage_by_hour, usage_since

RANGES = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
CHIEF_TITLE = "Chief of Staff"
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


def build(range_key="7d"):
    now = time.time()
    span = RANGES.get(range_key, RANGES["7d"])
    since = now - span
    bucket = "hour" if span <= 86400 else "day"
    org = state.load_org()

    index = LogIndex()
    files = index.refresh()
    by_session = {}
    for path, entry in files.items():
        if entry.get("kind") == "claude" and entry.get("parentSessionId"):
            continue
        by_session[entry.get("sessionId")] = path
    children = {}
    for path, entry in files.items():
        parent = entry.get("parentSessionId")
        if parent:
            children.setdefault(parent, []).append(path)

    in_cmux = cmux.available()
    sessions = cmux.agent_sessions() if in_cmux else {}
    workspaces = cmux.workspaces() if in_cmux else []

    assigned = set()
    teams, chief = [], None
    for ws in workspaces:
        side = cmux.sidebar_state(ws["ref"])
        cwd = side.get("cwd")
        branch_raw = (side.get("git_branch") or "none").split()
        branch = None if branch_raw[0] == "none" else branch_raw[0]
        dirty = len(branch_raw) > 1 and branch_raw[1] == "dirty"
        agents = []
        for surf in sorted(ws["surfaces"], key=lambda s: s["order"]):
            sess = sessions.get(surf["id"])
            if not sess or org["roles"].get(surf["id"]) == "ignore":
                continue
            cwd = cwd or sess.get("cwd")
            agents.append(_agent(surf, sess, files, by_session, children, since, now, assigned))
        is_chief = ws["title"] == CHIEF_TITLE or _within(cwd, str(paths.AGENT_DIR))
        for i, a in enumerate(agents):
            a["role"] = org["roles"].get(a["surfaceId"]) or ("coordinator" if i == 0 else "worker")
        agents.sort(key=lambda a: 0 if a["role"] == "coordinator" else 1)
        team = {
            "key": ws["id"],
            "workspaceId": ws["id"],
            "workspaceRef": ws["ref"],
            "task": _clean_title(ws["title"]),
            "cwd": cwd,
            "folder": os.path.basename((cwd or "").rstrip("/")),
            "branch": branch,
            "dirty": dirty,
            "selected": ws["selected"],
            "agents": agents,
            "isChief": is_chief,
        }
        if is_chief:
            team["name"] = CHIEF_TITLE
            chief = team
        else:
            teams.append(team)

    # Departments: every folder under the roots, plus team folders outside the roots.
    port = portfolio.scan()
    folders = list(port)
    for t in teams:
        t["department"] = portfolio.match(t["cwd"], folders) or t["cwd"]
        if t["department"] and t["department"] not in folders:
            folders.append(t["department"])

    # Logs that no live agent owns go to the team whose folder contains them.
    owners = sorted([t for t in teams + ([chief] if chief else []) if t.get("cwd")],
                    key=lambda t: -len(t["cwd"]))
    earlier = {t["key"]: [] for t in owners}
    agent_dir = str(paths.AGENT_DIR)
    dept_logs, outside = {}, {}
    for path, entry in files.items():
        cwd = _entry_cwd(entry, files, by_session)
        chief_log = _within(cwd, agent_dir)
        dept = None if chief_log else portfolio.match(cwd, folders)
        if dept:
            dept_logs.setdefault(dept, []).append(path)
        if path in assigned or (entry.get("lastTs") or 0) < since:
            continue
        owner = next((t for t in owners if _within(cwd, t["cwd"])), None)
        if owner:
            earlier[owner["key"]].append(path)
        elif not dept and not chief_log:
            outside.setdefault(cwd or "?", []).append(path)

    series_total = {}
    for t in teams + ([chief] if chief else []):
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
        for path in earlier.get(t["key"], []):
            entry = files[path]
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
        t["output"] = git.activity(t["cwd"], since)
    if chief:
        for k, v in chief["series"].items():
            series_total[k] = series_total.get(k, 0.0) + v

    departments = []
    day_keys = [_day(now - i * 86400) for i in range(portfolio.DAYS - 1, -1, -1)]
    for folder in folders:
        departments.append(_department(folder, port.get(folder) or {}, org["projects"].get(folder, {}),
                                       [t for t in teams if t["department"] == folder],
                                       [files[p] for p in dept_logs.get(folder, [])],
                                       since, bucket, now, day_keys))
    for d in departments:
        for k, v in d["series"].items():
            series_total[k] = series_total.get(k, 0.0) + v

    # Stable colors: the color follows the department folder, never its rank.
    slotted = sorted((d for d in departments if d["status"] == "active"), key=lambda d: d["path"])
    slots = {d["path"]: i for i, d in enumerate(slotted[:COLOR_SLOTS])}
    for d in departments:
        d["colorSlot"] = slots.get(d["path"])
    by_path = {d["path"]: d for d in departments}
    for t in teams:
        d = by_path.get(t["department"])
        t["colorSlot"] = d["colorSlot"] if d else None
        t["departmentName"] = d["name"] if d else _pretty_folder(t["cwd"])
        t["name"] = t["departmentName"]
        t["meta"] = {k: (d or {}).get(k) for k in ("goal", "deadline", "focus", "next", "notes")}

    other = []
    other_meter = _Meter()
    other_series = {}
    for cwd, plist in outside.items():
        m = _Meter()
        last = 0
        for path in plist:
            m.merge(_meter_for(files[path], since))
            _add_series(other_series, files[path], since, bucket)
            last = max(last, files[path].get("lastTs") or 0)
        if m.cost or m.unpriced_tokens:
            other.append({"cwd": cwd, "name": _pretty_folder(cwd), "lastActivity": last, **m.as_dict()})
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
    if chief:
        total.cost += chief["costUsd"]
        for k in total.tokens:
            total.tokens[k] += chief["tokens"][k]
    total.merge(other_meter)

    agents_all = [a for t in teams for a in t["agents"]]
    counts = {s: sum(1 for a in agents_all if a["status"] == s) for s in STATUS_ORDER}
    needs = []
    for t in teams:
        for a in t["agents"]:
            if a["status"] == "needs_input":
                needs.append({"project": t["name"], "task": t["task"], "agent": a["title"],
                              "workspaceId": t["workspaceId"], "surfaceId": a["surfaceId"],
                              "surfaceRef": a["surfaceRef"], "reason": a.get("waitReason"),
                              "since": a["lastActivity"], "lastAssistant": a["lastAssistant"],
                              "lastAssistantTail": a.get("lastAssistantTail")})

    snap = {
        "generatedAt": now,
        "range": {"key": range_key, "since": since, "bucket": bucket,
                  "buckets": _bucket_labels(since, now, bucket)},
        "cmux": in_cmux,
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


def _department(folder, info, meta, teams, entries, since, bucket, now, day_keys):
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
        "key": folder,
        "path": folder,
        "name": meta.get("name") or _pretty_folder(folder),
        "folder": os.path.basename(folder.rstrip("/")),
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


def _agent(surf, sess, files, by_session, children, since, now, assigned):
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
    reason = "prompt" if status == "needs_input" else None
    if status == "idle" and last_tail and now - (last_tail[0] or 0) < ASK_WINDOW and asks_user(last_user, last_tail):
        status, reason = "needs_input", "question"
    agent = {
        "id": sess.get("sessionId"),
        "kind": sess["kind"],
        "surfaceId": surf["id"],
        "surfaceRef": surf["ref"],
        "title": _clean_title(surf["title"]) or ("Claude" if sess["kind"] == "claude" else "Codex"),
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
