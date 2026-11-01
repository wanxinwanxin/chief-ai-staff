"""The meeting agenda: what needs the user, and what the chief can do alone.

Each item has a label:
  needs-you   Only the user can decide or answer.
  chief-can   The chief can do it with a message to a coordinator, after one approval.
  info        Information for the user. No action is necessary.

The sections come in this order: blocked, scheduled tasks, ideas, deadlines, idle teams,
spend, drift, housekeeping, questions, focus.
"""

import time
from datetime import date

from . import state, tasks

SECTIONS = [
    ("blocked", "Blocked by you"),
    ("task", "Scheduled tasks"),
    ("idea", "Ideas"),
    ("deadline", "Deadlines"),
    ("idle", "Teams with no work"),
    ("spend", "Spend"),
    ("drift", "Drift"),
    ("housekeeping", "Housekeeping"),
    ("question", "Questions for you"),
    ("focus", "Focus"),
]
DEADLINE_DAYS = 14
IDLE_AFTER = 30 * 60
STALE_TAB = 3 * 86400
SPEND_MIN = 25.0
SPEND_RATIO = 2.0
DRIFT_DAYS = 7
REVIVE_DAYS = 3
UNCOMMITTED_MIN = 25
MAX_QUESTIONS = 3


def _ago(ts, now):
    if not ts:
        return "never"
    secs = max(0, int(now - ts))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _item(kind, label, dept, title, detail=None, **extra):
    return {"kind": kind, "label": label, "department": dept["name"] if dept else None,
            "departmentKey": dept["key"] if dept else None, "title": title, "detail": detail, **extra}


def _where(t, multi):
    return f"{t['name']} ({t['nodeLabel']})" if multi else t["name"]


IDEA_DETAIL = 400


def _ideas(view, depts):
    """One item for each review to discuss and each run that waits, one for the memos, one for the queue."""
    ready, waits, problems = [], [], []
    rows = view.get("items") or []

    def where(x):
        d = depts.get(x.get("dept") or "")
        return d["name"] if d else x.get("department") or x.get("folder")

    for x in rows:
        title = x.get("title") or "A note"
        if x["status"] == "ready" and x.get("kind") != "memo":
            kind = "new project" if x["kind"] == "explore" else "project"
            detail = " ".join((x.get("result") or "").split())
            if len(detail) > IDEA_DETAIL:
                detail = detail[:IDEA_DETAIL - 1] + "…"
            ready.append(_item("idea", "needs-you", depts.get(x.get("dept") or ""),
                             f"{title}: the review is ready ({kind} {where(x)})", detail or None,
                             idea=x["id"], hint="Pursue, park, or drop it. Then close the idea."))
        elif x["status"] == "running" and (x.get("run") or {}).get("needsYou"):
            waits.append(_item("idea", "needs-you", depts.get(x.get("dept") or ""),
                             f"{title}: the review run {x['run']['text']}", None, idea=x["id"],
                             teamKey=x["run"].get("teamKey")))
        elif x.get("stuck") or (x["status"] == "queued" and x.get("error")):
            problems.append(_item("idea", "needs-you", None, f"{title}: {x.get('error') or 'the triage failed'}",
                             "Tell the chief the kind of the idea, or try again.", idea=x["id"]))
    out = ready + waits + problems
    memos = [x for x in rows if x["status"] == "ready" and x.get("kind") == "memo"]
    if memos:
        out.append(_item("idea", "info", None, f"{len(memos)} {'memo' if len(memos) == 1 else 'memos'}: "
                         + "; ".join(x.get("title") or "" for x in memos[:6]) + (" …" if len(memos) > 6 else ""),
                         None, ideas=[x["id"] for x in memos]))
    queued = [x for x in rows if x["status"] in ("new", "sorting", "queued") and not x.get("stuck")
              and not x.get("error")]
    if queued:
        when = view.get("nextReviewText")
        out.append(_item("idea", "info", None,
                         f"{len(queued)} {'idea waits' if len(queued) == 1 else 'ideas wait'} for "
                         + (f"the review at {when}" if when else "a review (the schedule is off)"),
                         "; ".join(x.get("title") or "A note that is not sorted yet" for x in queued[:6]),
                         ideas=[x["id"] for x in queued]))
    return out


def build(snap):
    now = snap["generatedAt"]
    depts = {d["key"]: d for d in snap["departments"]}
    teams = snap["teams"]
    multi = len(snap.get("nodes") or []) > 1
    items = []

    # 1. Blocked by you: a permission prompt, or a reply that ends with a question.
    for t in teams:
        d = depts.get(t["department"])
        for a in t["agents"]:
            if a["status"] != "needs_input":
                continue
            what = "waits at a prompt" if a.get("waitReason") == "prompt" else "asked you a question"
            tail = (a.get("lastAssistantTail") or [None, None])[1]
            items.append(_item("blocked", "needs-you", d, f"{_where(t, multi)}: {a['title']} {what} "
                               f"({_ago(a['lastActivity'], now)} ago)", tail, teamKey=t["key"], node=t["node"],
                               workspaceId=t["workspaceId"], surfaceId=a["surfaceId"],
                               surfaceRef=a["surfaceRef"], since=a["lastActivity"]))

    # 2. Scheduled tasks: the last run of each task in each of its departments.
    runs = tasks.load_runs()
    by_pane = {a["paneId"]: t for t in teams for a in t["agents"] if a.get("paneId")}
    for task in tasks.load():
        if task["error"]:
            items.append(_item("task", "needs-you", None, f"{task['name']}: the task file has a problem",
                               task["error"], task=task["name"], runs=[]))
            continue
        if not task["enabled"]:
            continue
        last = tasks.latest(runs, task)
        rows = [{"department": dept, "status": r["status"], "text": tasks.run_line(r, now),
                 "needsYou": tasks.needs_you(r, now), "run": r["id"],
                 "teamKey": (by_pane.get(r.get("pane")) or {}).get("key")}
                for dept, r in last.items()]
        waiting = sum(1 for r in rows if r["needsYou"])
        nxt = tasks.when(tasks.next_slot(task, now), now)
        if not rows:
            what = f"the first run is {nxt}"
        elif waiting:
            what = f"{waiting} {'run needs' if waiting == 1 else 'runs need'} you"
        elif any(r["status"] in ("starting", "running") for r in rows):
            what = "runs now"
        else:
            what = f"ran at {tasks.when(min(r['startedAt'] for r in last.values()), now)}"
        items.append(_item("task", "needs-you" if waiting else "info", None, f"{task['name']}: {what}",
                           "\n".join(f"{r['department']}: {r['text']}" for r in rows) or None,
                           hint=f"Next run: {nxt}." if rows else None, task=task["name"], runs=rows))

    # 3. Ideas from the inbox (ideas.py): reviews to discuss, runs that wait, and memos.
    items += _ideas(snap.get("ideas") or {}, depts)

    # 4. Deadlines in the next 14 days, and late ones.
    today = date.fromtimestamp(now)
    for d in snap["departments"]:
        if not d.get("deadline") or d["status"] == "archived":
            continue
        try:
            due = date.fromisoformat(d["deadline"])
        except ValueError:
            continue
        days = (due - today).days
        if days > DEADLINE_DAYS:
            continue
        when = f"{-days} days late" if days < 0 else "today" if days == 0 else f"in {days} days"
        working = d["teamStatus"] in ("working", "needs_input", "idle")
        label = "chief-can" if working else "needs-you"
        detail = ("The chief can ask the coordinator for a status against the deadline." if working
                  else "No team works on it now. Start a team or move the deadline.")
        items.append(_item("deadline", label, d, f"{d['name']}: due {d['deadline']} ({when})", detail,
                           due=d["deadline"]))

    # 5. Teams with no work: every agent is idle, and no agent asks a question.
    # The agent of a finished scheduled run is not a team with no work.
    finished = tasks.finished_panes(runs)
    for t in teams:
        agents = [a for a in t["agents"] if a.get("paneId") not in finished]
        if not agents or t["status"] not in ("idle", "ended"):
            continue
        idle_for = now - (t["lastActivity"] or 0)
        if idle_for < IDLE_AFTER:
            continue
        d = depts.get(t["department"])
        coord = next((a for a in agents if a["role"] == "coordinator"), agents[0])
        last = (coord.get("lastAssistant") or [None, None])[1]
        if idle_for > STALE_TAB:
            title = f"{_where(t, multi)}: the tab \"{t['task']}\" has no activity for {_ago(t['lastActivity'], now)}"
            items.append(_item("idle", "needs-you", d, title, "Close the tab, or give the team a new task.",
                               teamKey=t["key"], node=t["node"],
                               workspaceId=t["workspaceId"], surfaceId=coord["surfaceId"]))
            continue
        has_goal = bool(d and d.get("goal"))
        label = "chief-can" if has_goal else "needs-you"
        detail = (f"Last reply: {last}" if last else None)
        title = f"{_where(t, multi)}: the team \"{t['task']}\" is idle for {_ago(t['lastActivity'], now)}"
        items.append(_item("idle", label, d, title, detail, teamKey=t["key"], node=t["node"],
                           workspaceId=t["workspaceId"],
                           surfaceId=coord["surfaceId"], surfaceRef=coord["surfaceRef"],
                           hint=("The chief can ask the coordinator for the next step to the goal." if has_goal
                                 else "Give the team a next task, or tell the chief the goal.")))

    # 6. Spend that goes up quickly (API-equivalent).
    for d in snap["departments"]:
        if d["cost24h"] >= SPEND_MIN and d["cost24h"] >= SPEND_RATIO * max(d["costPrevAvg"], 1.0):
            items.append(_item("spend", "info", d,
                               f"{d['name']}: ${d['cost24h']:,.0f} today, against ${d['costPrevAvg']:,.0f} "
                               "on an average day this week",
                               "API-equivalent estimate. Check that the work is worth the spend."))

    # 7. Drift between the status and the activity.
    for d in snap["departments"]:
        idle_days = (now - (d["lastActivity"] or 0)) / 86400
        if (d["focus"] or (d["status"] == "active" and d["statusSource"] == "you")) and idle_days > DRIFT_DAYS:
            where = f"focus of the {'day' if d['focus'] == 'today' else 'week'}" if d["focus"] else "active"
            items.append(_item("drift", "needs-you", d,
                               f"{d['name']} is {where}, but has no activity for {int(idle_days)} days",
                               "Keep it, or move it to semi-active or backlog."))
        elif (d["statusSource"] == "you" and d["status"] in ("backlog", "archived")
              and idle_days <= REVIVE_DAYS):
            items.append(_item("drift", "needs-you", d,
                               f"{d['name']} is {d['status']}, but had activity {_ago(d['lastActivity'], now)} ago",
                               "Reactivate it?"))
    suggested = [d for d in snap["departments"] if d["statusSource"] == "suggested" and d["inRoots"]]
    if suggested:
        counts = {s: sum(1 for d in suggested if d["status"] == s) for s in state.STATUSES}
        summary = ", ".join(f"{n} {s}" for s, n in counts.items() if n)
        items.append(_item("drift", "needs-you", None,
                           f"{len(suggested)} departments have a suggested status that you did not confirm",
                           f"Suggestion from the last activity: {summary}. Confirm all, or change some first."))

    # 8. Housekeeping that the chief can ask a coordinator to do.
    for t in teams:
        unc = (t.get("output") or {}).get("uncommitted") or 0
        if unc >= UNCOMMITTED_MIN and t["status"] != "working":
            items.append(_item("housekeeping", "chief-can", depts.get(t["department"]),
                               f"{_where(t, multi)}: {unc} uncommitted files",
                               "The chief can ask the coordinator to commit the finished work.",
                               teamKey=t["key"], node=t["node"], workspaceId=t["workspaceId"]))

    # 9. Questions about missing goals and deadlines. Focus departments first.
    asked = 0
    for d in sorted(snap["departments"], key=lambda d: ({"today": 0, "week": 1}.get(d["focus"], 2),
                                                        -(d["lastActivity"] or 0))):
        if asked >= MAX_QUESTIONS:
            break
        if d["status"] != "active":
            continue
        missing = [k for k in ("goal", "deadline") if not d.get(k) and (k == "goal" or d["focus"])]
        if missing:
            items.append(_item("question", "needs-you", d,
                               f"{d['name']}: what is the {' and the '.join(missing)}?",
                               d.get("description")))
            asked += 1

    # 10. Focus limits.
    for level, limit in state.FOCUS_LIMITS.items():
        names = [d["name"] for d in snap["departments"] if d["focus"] == level]
        word = "day" if level == "today" else "week"
        if len(names) > limit:
            items.append(_item("focus", "needs-you", None,
                               f"{len(names)} departments are focus of the {word}. The limit is {limit}.",
                               ", ".join(names)))
        elif not names:
            items.append(_item("focus", "needs-you", None, f"No focus of the {word} is set",
                               "Choose at the end of the meeting."))

    order = {k: i for i, (k, _) in enumerate(SECTIONS)}
    items.sort(key=lambda it: order[it["kind"]])
    for i, it in enumerate(items, 1):
        it["id"] = i
    return {
        "items": items,
        "counts": {lbl: sum(1 for it in items if it["label"] == lbl) for lbl in ("needs-you", "chief-can", "info")},
    }


def machines_line(snap):
    parts = []
    for n in snap.get("nodes") or []:
        state_text = "this machine" if n["local"] else ("online" if n["online"] else
                                                        f"offline, data from {_ago(n['at'], snap['generatedAt'])} ago")
        parts.append(f"{n['label']} ({n['terminal']}, {state_text})")
    return "Machines: " + ", ".join(parts)


LABEL_TEXT = {"needs-you": "needs you", "chief-can": "the chief can do it", "info": "info"}


def render(snap):
    now = snap["generatedAt"]
    ag = snap["agenda"]
    d_counts = snap["totals"]["departments"]
    lines = [f"# Meeting agenda · {time.strftime('%a %d %b %H:%M', time.localtime(now))}", ""]
    open_teams = len(snap["teams"])
    lines.append("Departments: " + " · ".join(f"{n} {s}" for s, n in d_counts.items())
                 + f" · {open_teams} open teams")
    for level in ("today", "week"):
        names = [d["name"] for d in snap["departments"] if d["focus"] == level]
        lines.append(f"Focus of the {'day' if level == 'today' else 'week'}: {', '.join(names) or 'not set'}")
    c = ag["counts"]
    lines.append(f"Agenda: {c['needs-you']} items need you, the chief can do {c['chief-can']}, "
                 f"{c['info']} for information.")
    lines.append(machines_line(snap))
    lines.append("")
    for kind, title in SECTIONS:
        group = [it for it in ag["items"] if it["kind"] == kind]
        if not group:
            continue
        lines.append(f"## {title}")
        for it in group:
            ref = f" [team {it['teamKey']}]" if it.get("teamKey") else ""
            lines.append(f"{it['id']}. ({LABEL_TEXT[it['label']]}) {it['title']}{ref}")
            if it.get("detail"):
                lines.append("   " + it["detail"].replace("\n", "\n   "))
            if it.get("hint"):
                lines.append(f"   {it['hint']}")
        lines.append("")

    lines.append("## Active and semi-active departments")
    lines.append("name | status | focus | last activity | teams | spend 7d | goal | deadline | next")
    for d in snap["departments"]:
        if d["status"] not in ("active", "semi-active"):
            continue
        src = "" if d["statusSource"] == "you" else " (suggested)"
        lines.append(" | ".join([
            d["name"], d["status"] + src, d["focus"] or "-", _ago(d["lastActivity"], now) + " ago",
            str(len(d["teams"])), f"${d['costUsd']:,.0f}", d.get("goal") or "-", d.get("deadline") or "-",
            d.get("next") or "-",
        ]))
    lines.append("")
    backlog = [d["name"] for d in snap["departments"] if d["status"] == "backlog"]
    if backlog:
        lines.append(f"Backlog ({len(backlog)}): {', '.join(backlog[:40])}{' …' if len(backlog) > 40 else ''}")
    archived = d_counts.get("archived", 0)
    if archived:
        lines.append(f"Archived: {archived} departments. Run `chief dept list --status archived` to see them.")
    notes = state.recent_notes(days=7)
    if notes:
        lines.append("")
        lines.append("## Notes from recent meetings")
        for day, text in notes[:3]:
            lines.append(f"### {day}")
            lines.append(text.strip())
    return "\n".join(lines)
