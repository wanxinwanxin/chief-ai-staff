"""Build one snapshot of the whole org: projects, agents, spend, output, focus.

A project is one cmux workspace (one tab group in the left sidebar). The project
identity is its folder, because cmux changes workspace titles as agents work.
"""

import os
import re
import time
from datetime import datetime

from . import paths, pricing, state
from .sources import cmux, git
from .sources.logs import LogIndex, usage_by_hour, usage_since

RANGES = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
CHIEF_TITLE = "Chief of Staff"
STATUS_ORDER = ["needs_input", "working", "idle", "ended"]
COLOR_SLOTS = 8
RUNNING_WINDOW = 120  # A sub-agent log that changed in the last 2 minutes is running.

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
    projects, chief = [], None
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
        project = {
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
        meta = org["projects"].get(cwd or "", {})
        project["name"] = meta.get("name") or (CHIEF_TITLE if is_chief else _pretty_folder(cwd))
        project["meta"] = {k: meta.get(k) for k in ("priority", "goal", "notes")}
        if is_chief:
            chief = project
        else:
            projects.append(project)

    # Logs that no live agent owns go to the project whose folder contains them.
    owners = sorted([p for p in projects + ([chief] if chief else []) if p.get("cwd")],
                    key=lambda p: -len(p["cwd"]))
    earlier = {p["key"]: [] for p in owners}
    elsewhere = {}
    for path, entry in files.items():
        if path in assigned:
            continue
        if entry.get("parentSessionId"):
            parent_path = by_session.get(entry["parentSessionId"])
            cwd = files[parent_path].get("cwd") if parent_path else entry.get("cwd")
        else:
            cwd = entry.get("cwd")
        if (entry.get("lastTs") or 0) < since:
            continue
        owner = next((p for p in owners if _within(cwd, p["cwd"])), None)
        if owner:
            earlier[owner["key"]].append(path)
        else:
            elsewhere.setdefault(cwd or "?", []).append(path)

    series_total = {}
    for p in projects + ([chief] if chief else []):
        meter = _Meter()
        series = {}
        statuses = set()
        last = 0
        for a in p["agents"]:
            meter.merge(a.pop("_meter"))
            for path in a.pop("_paths"):
                _add_series(series, files[path], since, bucket)
            statuses.add(a["status"])
            last = max(last, a["lastActivity"] or 0)
        e_meter, e_sessions = _Meter(), set()
        for path in earlier.get(p["key"], []):
            entry = files[path]
            m = _meter_for(entry, since)
            if not (m.cost or m.unpriced_tokens):
                continue
            e_meter.merge(m)
            e_sessions.add(entry.get("parentSessionId") or entry.get("sessionId"))
            _add_series(series, entry, since, bucket)
            last = max(last, entry.get("lastTs") or 0)
        meter.merge(e_meter)
        p["earlier"] = dict(e_meter.as_dict(), sessions=len(e_sessions))
        p.update(meter.as_dict())
        p["series"] = {k: round(v, 4) for k, v in series.items()}
        for k, v in series.items():
            series_total[k] = series_total.get(k, 0.0) + v
        p["status"] = _rollup(statuses)
        p["lastActivity"] = last or None
        p["output"] = git.activity(p["cwd"], since)

    # Stable colors: the color follows the project folder, never its rank.
    for slot, p in enumerate(sorted(projects, key=lambda p: (p["cwd"] or "", p["key"]))):
        p["colorSlot"] = slot if slot < COLOR_SLOTS else None

    other = []
    other_meter = _Meter()
    other_series = {}
    for cwd, plist in elsewhere.items():
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

    projects.sort(key=lambda p: (STATUS_ORDER.index(p["status"]) if p["status"] in STATUS_ORDER else 9,
                                 -(p["lastActivity"] or 0)))

    total = _Meter()
    for p in projects + ([chief] if chief else []):
        total.cost += p["costUsd"]
        total.unpriced_tokens += p["unpricedTokens"]
        for k in total.tokens:
            total.tokens[k] += p["tokens"][k]
    total.merge(other_meter)

    agents_all = [a for p in projects for a in p["agents"]]
    counts = {s: sum(1 for a in agents_all if a["status"] == s) for s in STATUS_ORDER}
    needs = []
    for p in projects:
        for a in p["agents"]:
            if a["status"] == "needs_input":
                needs.append({"project": p["name"], "task": p["task"], "agent": a["title"],
                              "workspaceId": p["workspaceId"], "surfaceId": a["surfaceId"],
                              "since": a["lastActivity"], "lastAssistant": a["lastAssistant"]})

    commits = sum((p["output"] or {}).get("commits", 0) for p in projects)
    return {
        "generatedAt": now,
        "range": {"key": range_key, "since": since, "bucket": bucket,
                  "buckets": _bucket_labels(since, now, bucket)},
        "cmux": in_cmux,
        "chief": chief,
        "projects": projects,
        "elsewhere": {"projects": other, "series": {k: round(v, 4) for k, v in other_series.items()},
                      **other_meter.as_dict()},
        "totals": dict(total.as_dict(), commits=commits, agents=counts,
                       series={k: round(v, 4) for k, v in series_total.items()}),
        "needsYou": needs,
        "focus": state.load_focus(),
        "note": "Dollar values are API-equivalent estimates from list prices. "
                "Subscription plans bill differently.",
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
    last = max([t for t in [(own or {}).get("lastTs"), sess.get("updatedAt")] if t] or [0])
    agent = {
        "id": sess.get("sessionId"),
        "kind": sess["kind"],
        "surfaceId": surf["id"],
        "surfaceRef": surf["ref"],
        "title": _clean_title(surf["title"]) or ("Claude" if sess["kind"] == "claude" else "Codex"),
        "status": sess["status"],
        "lastActivity": last or None,
        "lastUser": last_user,
        "lastAssistant": last_asst,
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
