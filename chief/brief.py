"""Team details for the chief agent: every open team, its agents, and their last messages.

The meeting agenda (agenda.py) is the summary. This briefing is the detail behind it.
"""

import time

from .agenda import machines_line
from .snapshot import ago

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


def render(snap):
    now = snap["generatedAt"]
    lines = []
    rng = snap["range"]["key"]
    t = snap["totals"]
    lines.append(f"# Team details for the last {rng} · {time.strftime('%a %d %b %H:%M', time.localtime(now))}")
    lines.append("")
    a = t["agents"]
    lines.append(f"Spend: {money(t['costUsd'])} (API-equivalent) · {tokens(t['tokens']['total'])} tokens · "
                 f"agents: {a['working']} working, {a['needs_input']} need you, {a['idle']} idle")
    lines.append(machines_line(snap))
    lines.append("")

    lines.append(f"## Needs you ({len(snap['needsYou'])})")
    for n in snap["needsYou"]:
        last = (n.get("lastAssistant") or [None, ""])[1]
        tail = (n.get("lastAssistantTail") or [None, last])[1]
        lines.append(f"- {n['project']} ({n['node']}) / {n['agent']} (team {n['teamKey']}, agent {n['surfaceRef']}): "
                     f"waiting since {ago(n['since'], now)}. "
                     f"End of the last reply: \"{tail}\"")
    if not snap["needsYou"]:
        lines.append("- Nothing waits for you now.")
    lines.append("")

    lines.append("## Open teams")
    for p in snap["teams"]:
        share = p["costUsd"] / t["costUsd"] if t["costUsd"] else 0
        lines.append(f"### {p['name']} [{p['status'].replace('_', ' ')}] · {p['nodeLabel']} · tab: \"{p['task']}\" "
                     f"(team {p['key']})")
        lines.append(f"Folder: {p['cwd']}" + (f" · branch {p['branch']}" if p["branch"] else ""))
        meta = p["meta"]
        if any(meta.values()):
            lines.append(" · ".join(f"{k.capitalize()}: {v}" for k, v in meta.items() if v))
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
            lines.append(f"- {ag['role'].capitalize()} \"{ag['title']}\" ({ag['kind']}, agent {ag['surfaceRef']}): "
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

    other = snap["outside"]
    if other["projects"]:
        names = ", ".join(f"{o['name']} ({money(o['costUsd'])})" for o in other["projects"][:6])
        lines.append(f"## Outside the portfolio folders: {money(other['costUsd'])} · {names}")
        lines.append("")
    return "\n".join(lines)
