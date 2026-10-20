"""Text briefing for the chief agent. The chief reads this to plan the day."""

import time

from .snapshot import ago

PRIORITY_ORDER = {"high": 0, "normal": 1, None: 1, "low": 2, "paused": 3}


def money(v):
    if v is None:
        return "n/a"
    if v >= 1000:
        return f"${v / 1000:.1f}K"
    return f"${v:,.2f}"


def tokens(n):
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.0f}K"
    return str(n)


def signals(snap):
    """Simple rules that point the chief at likely problems."""
    out = []
    now = snap["generatedAt"]
    total_cost = snap["totals"]["costUsd"] or 0
    for p in snap["projects"]:
        share = p["costUsd"] / total_cost if total_cost else 0
        if share >= 0.5:
            out.append(f"{p['name']}: {share:.0%} of all spend in this range.")
        hit = p.get("cacheHitRate")
        if hit is not None and p["costUsd"] > 20 and hit < 0.6:
            out.append(f"{p['name']}: cache hit rate is {hit:.0%}. Long pauses or restarts can cause this.")
        if p["meta"].get("priority") == "high" and p["lastActivity"] and now - p["lastActivity"] > 2 * 86400:
            out.append(f"{p['name']}: high priority, but no agent activity for {ago(p['lastActivity'], now)[:-4]}.")
        unc = (p["output"] or {}).get("uncommitted") or 0
        if unc >= 25:
            out.append(f"{p['name']}: {unc} uncommitted files.")
        if p["earlier"]["costUsd"] > 3 * max(1.0, p["costUsd"] - p["earlier"]["costUsd"]) and p["earlier"]["costUsd"] > 50:
            out.append(f"{p['name']}: most spend ({money(p['earlier']['costUsd'])}) is in earlier sessions, "
                       "so the current agent has only a short history.")
    for n in snap["needsYou"]:
        if n["since"] and now - n["since"] > 1800:
            out.append(f"{n['project']}: an agent waits for you since {ago(n['since'], now)}.")
    return out


def render(snap):
    now = snap["generatedAt"]
    lines = []
    rng = snap["range"]["key"]
    t = snap["totals"]
    lines.append(f"# Briefing for the last {rng} · {time.strftime('%a %d %b %H:%M', time.localtime(now))}")
    lines.append("")
    a = t["agents"]
    lines.append(f"Spend: {money(t['costUsd'])} (API-equivalent) · {tokens(t['tokens']['total'])} tokens · "
                 f"{t['commits']} commits · agents: {a['working']} working, {a['needs_input']} need you, "
                 f"{a['idle']} idle")
    if not snap["cmux"]:
        lines.append("cmux is not available, so tab and status data is missing.")
    lines.append("")

    lines.append(f"## Needs you ({len(snap['needsYou'])})")
    for n in snap["needsYou"]:
        last = (n.get("lastAssistant") or [None, ""])[1]
        lines.append(f"- {n['project']} / {n['agent']}: waiting since {ago(n['since'], now)}. Last reply: \"{last}\"")
    if not snap["needsYou"]:
        lines.append("- Nothing waits for you now.")
    lines.append("")

    lines.append("## Projects")
    projects = sorted(snap["projects"], key=lambda p: (PRIORITY_ORDER.get(p["meta"].get("priority"), 1),
                                                       -(p["lastActivity"] or 0)))
    for p in projects:
        share = p["costUsd"] / t["costUsd"] if t["costUsd"] else 0
        lines.append(f"### {p['name']} [{p['status'].replace('_', ' ')}] · tab: \"{p['task']}\" ({p['workspaceRef']})")
        lines.append(f"Folder: {p['cwd']}" + (f" · branch {p['branch']}" if p["branch"] else ""))
        meta = p["meta"]
        if any(meta.values()):
            lines.append("Priority: {} · Goal: {} · Notes: {}".format(
                meta.get("priority") or "normal", meta.get("goal") or "-", meta.get("notes") or "-"))
        hit = p.get("cacheHitRate")
        lines.append(f"Spend: {money(p['costUsd'])} ({share:.0%}) · {tokens(p['tokens']['total'])} tokens"
                     + (f" · cache hit {hit:.0%}" if hit is not None else ""))
        out = p["output"]
        if out:
            lc = out.get("lastCommit")
            lines.append(f"Output: {out['commits']} commits, +{out['linesAdded']}/-{out['linesRemoved']} lines, "
                         f"{out['uncommitted']} uncommitted files"
                         + (f" · last commit {ago(lc['at'], now)}: \"{lc['subject']}\"" if lc else ""))
        for ag in p["agents"]:
            lines.append(f"- {ag['role'].capitalize()} \"{ag['title']}\" ({ag['kind']}, {ag['surfaceRef']}): "
                         f"{ag['status'].replace('_', ' ')}, last activity {ago(ag['lastActivity'], now)}, "
                         f"{money(ag['costUsd'])}")
            if ag.get("lastUser"):
                lines.append(f"  - Last prompt: \"{ag['lastUser'][1]}\"")
            if ag.get("lastAssistant"):
                lines.append(f"  - Last reply: \"{ag['lastAssistant'][1]}\"")
            if ag["subagents"]:
                running = sum(1 for s in ag["subagents"] if s["running"])
                lines.append(f"  - Sub-agents: {len(ag['subagents'])} ({running} running), "
                             f"{money(ag['subagentCostUsd'])}")
        e = p["earlier"]
        if e["sessions"]:
            cost_text = money(e["costUsd"]) if e["costUsd"] else "no price set (Codex)"
            lines.append(f"- Earlier sessions: {e['sessions']}, {cost_text}")
        lines.append("")

    other = snap["elsewhere"]
    if other["projects"]:
        names = ", ".join(f"{o['name']} ({money(o['costUsd'])})" for o in other["projects"][:6])
        lines.append(f"## Not in a tab: {money(other['costUsd'])} · {names}")
        lines.append("")

    sig = signals(snap)
    lines.append("## Signals")
    lines.extend(f"- {s}" for s in sig) if sig else lines.append("- No signals.")
    lines.append("")

    focus = snap.get("focus")
    if focus:
        lines.append(f"## Current focus plan (set {ago(focus.get('updatedAt'), now)})")
        for i, item in enumerate(focus.get("items", []), 1):
            lines.append(f"{i}. {item.get('project')}: {item.get('why', '')} Next: {item.get('next', '')}")
        if focus.get("note"):
            lines.append(f"Note: {focus['note']}")
    return "\n".join(lines)
