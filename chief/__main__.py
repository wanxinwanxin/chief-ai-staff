"""chief: command line for the Chief of AI Staff.

  chief meeting            Print the meeting agenda: what needs you, and what the chief can do.
  chief brief              Print the details of every open team.
  chief dept list|set|accept
                           Show or change departments: status, focus, goal, deadline, next step.
  chief notes add|show     Meeting notes.
  chief message <dept> <text>
                           Send a message to the coordinator of a team through cmux.
  chief start [--agent claude|codex]
                           Start the dashboard and open the Chief of Staff tab in cmux.
  chief install            Put `chief` on the PATH and install the /chief skill for Claude Code and Codex.
  chief open | serve | stop
  chief org show|role      Tab roles (coordinator, worker, ignore).
"""

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.request

from . import agenda, brief, paths, portfolio, snapshot, state
from .server import DEFAULT_PORT, serve
from .sources import cmux

SENDER = "Chief of Staff"
OPENING = "Start our meeting."

# How each harness starts the chief in the agent/ folder. Each harness reads
# AGENTS.md there (Claude Code through the one-line CLAUDE.md).
AGENT_COMMANDS = {
    "claude": lambda: ["claude", "--permission-mode", "auto", OPENING],
    "codex": lambda: ["codex", "--sandbox", "workspace-write", "--ask-for-approval", "on-request",
                      "-c", "sandbox_workspace_write.network_access=true",
                      "-c", f'sandbox_workspace_write.writable_roots=["{paths.CHIEF_HOME}"]', OPENING],
}


def _url(port):
    return f"http://127.0.0.1:{port}/"


def _server_up(port):
    try:
        with urllib.request.urlopen(_url(port) + "api/health", timeout=1) as res:
            return res.status == 200
    except OSError:
        return False


def _ensure_server(port):
    if _server_up(port):
        return True
    paths.ensure_home()
    log = open(paths.SERVER_LOG, "ab")
    env = dict(os.environ, PYTHONPATH=str(paths.REPO_ROOT))
    proc = subprocess.Popen(
        [sys.executable, "-m", "chief", "serve", "--port", str(port)],
        cwd=str(paths.REPO_ROOT), stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        env=env, start_new_session=True,
    )
    paths.SERVER_PID.write_text(str(proc.pid))
    for _ in range(40):
        if _server_up(port):
            return True
        time.sleep(0.25)
    print(f"The server did not start. See {paths.SERVER_LOG}", file=sys.stderr)
    return False


def _find_ref(text):
    m = re.search(r"workspace:\d+", text or "")
    return m.group(0) if m else None


def cmd_start(args):
    if not _ensure_server(args.port):
        return 1
    url = _url(args.port)
    if not cmux.available():
        print(f"Dashboard: {url}")
        print("cmux is not available. Open the URL in a browser, and run your agent in the agent/ folder.")
        return 0
    existing = next((w for w in cmux.workspaces() if w["title"] == snapshot.CHIEF_TITLE), None)
    if existing:
        cmux.run(["select-workspace", "--workspace", existing["ref"]])
        print(f"The Chief of Staff tab is already open ({existing['ref']}). Dashboard: {url}")
        return 0
    agent_cmd = os.environ.get("CHIEF_AGENT_CMD") or shlex.join(AGENT_COMMANDS[args.agent]())
    ok, out = cmux.run([
        "new-workspace", "--name", snapshot.CHIEF_TITLE, "--cwd", str(paths.AGENT_DIR),
        "--description", "Chief of AI Staff", "--command", agent_cmd, "--focus", "true",
    ])
    ref = _find_ref(out) if ok else None
    if not ref:
        ref = next((w["ref"] for w in cmux.workspaces() if w["title"] == snapshot.CHIEF_TITLE), None)
    if not ref:
        print(f"cmux did not create the workspace: {out}", file=sys.stderr)
        return 1
    cmux.run(["new-pane", "--type", "browser", "--url", url, "--workspace", ref, "--direction", "right"])
    print(f"Opened the Chief of Staff tab ({ref}). Dashboard: {url}")
    return 0


def cmd_open(args):
    if not _ensure_server(args.port):
        return 1
    url = _url(args.port)
    if cmux.available() and os.environ.get("CMUX_WORKSPACE_ID"):
        ok, out = cmux.run(["browser", "open", url])
        if ok:
            return 0
    subprocess.run(["open", url], check=False)
    return 0


def cmd_stop(args):
    try:
        pid = int(paths.SERVER_PID.read_text())
        os.kill(pid, signal.SIGTERM)
        paths.SERVER_PID.unlink()
        print("Stopped the dashboard server.")
    except (OSError, ValueError):
        print("No dashboard server is running from this machine's pid file.")
    return 0


def cmd_brief(args):
    snap = snapshot.build(args.range)
    if args.json:
        snap["signals"] = brief.signals(snap)
        print(json.dumps(snap, indent=1, default=str))
    else:
        print(brief.render(snap))
    return 0


def cmd_meeting(args):
    snap = snapshot.build("7d")
    if args.json:
        print(json.dumps({"agenda": snap["agenda"], "departments": snap["departments"],
                          "totals": snap["totals"]}, indent=1, default=str))
    else:
        print(agenda.render(snap))
    return 0


def _departments():
    """Departments without the cost scan: portfolio folders, org entries, and open tabs."""
    org = state.load_org()
    rows = {f: {"path": f} for f in portfolio.folders()}
    for f in org["projects"]:
        rows.setdefault(f, {"path": f})
    tabs = []
    if cmux.available():
        for w in cmux.workspaces():
            cwd = cmux.sidebar_state(w["ref"]).get("cwd")
            dept = portfolio.match(cwd, list(rows)) or cwd
            if dept:
                rows.setdefault(dept, {"path": dept})
                tabs.append((w, dept))
    for f, r in rows.items():
        meta = org["projects"].get(f, {})
        r["name"] = meta.get("name") or snapshot._pretty_folder(f)
        r["folder"] = os.path.basename(f.rstrip("/"))
        r["meta"] = meta
    return list(rows.values()), tabs


def _resolve_dept(query):
    rows, tabs = _departments()
    q = query.strip().lower().rstrip("/")
    for w, dept in tabs:
        if q in (w["ref"].lower(), (w["id"] or "").lower(), w["title"].lower()):
            return next(r for r in rows if r["path"] == dept)
    exact = [r for r in rows if q in (r["path"].lower(), r["name"].lower(), r["folder"].lower())]
    if len(exact) == 1:
        return exact[0]
    hits = [r for r in rows if q in r["name"].lower() or q in r["folder"].lower()]
    return hits[0] if len(hits) == 1 else None


def _check_focus(path, level):
    if level not in state.FOCUS_LIMITS:
        return None
    org = state.load_org()
    current = [f for f, m in org["projects"].items() if m.get("focus") == level and f != path]
    limit = state.FOCUS_LIMITS[level]
    if len(current) >= limit:
        names = ", ".join(org["projects"][f].get("name") or snapshot._pretty_folder(f) for f in current)
        word = "day" if level == "today" else "week"
        return (f"The focus of the {word} already has {len(current)} departments ({names}). "
                f"The limit is {limit}. Remove one first with: chief dept set <name> --focus none")
    return None


def cmd_dept(args):
    if args.action == "list":
        snap = snapshot.build("7d")
        now = snap["generatedAt"]
        for d in snap["departments"]:
            if args.status and d["status"] != args.status:
                continue
            src = "" if d["statusSource"] == "you" else " (suggested)"
            print(" | ".join([d["name"], d["status"] + src, d["focus"] or "-",
                              snapshot.ago(d["lastActivity"], now), f"{len(d['teams'])} teams",
                              d.get("goal") or "-", d.get("deadline") or "-", d["path"]]))
        return 0
    if args.action == "accept":
        snap = snapshot.build("7d")
        targets = [d for d in snap["departments"] if d["statusSource"] == "suggested" and d["inRoots"]]
        if args.names:
            wanted = {(_resolve_dept(n) or {}).get("path") for n in args.names}
            targets = [d for d in targets if d["path"] in wanted]
        org = state.load_org()
        for d in targets:
            org["projects"].setdefault(d["path"], {}).update(status=d["status"], statusSetAt=time.time())
        state.save_org(org)
        print(f"Confirmed the suggested status of {len(targets)} departments.")
        return 0
    dept = _resolve_dept(args.name)
    if not dept:
        print(f"No single department matches '{args.name}'. Run: chief dept list", file=sys.stderr)
        return 1
    focus = args.focus
    if focus == "none":
        focus = ""
    problem = _check_focus(dept["path"], focus)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    if args.deadline not in (None, "", "none"):
        try:
            time.strptime(args.deadline, "%Y-%m-%d")
        except ValueError:
            print("Give the deadline as YYYY-MM-DD, or 'none' to clear it.", file=sys.stderr)
            return 2
    fields = {
        "name": args.rename, "status": args.status, "focus": focus, "goal": args.goal,
        "deadline": "" if args.deadline == "none" else args.deadline, "next": args.next, "notes": args.notes,
    }
    if focus and not args.status and dept["meta"].get("status") != "active":
        fields["status"] = "active"  # A focus department is always active.
    meta = state.update_dept(dept["path"], **fields)
    print(f"Updated {dept['name']} ({dept['path']}): {json.dumps(meta)}")
    return 0


def cmd_notes(args):
    if args.action == "add":
        text = " ".join(args.text)
        if text == "-":
            text = sys.stdin.read()
        if not text.strip():
            print("Give the note text.", file=sys.stderr)
            return 2
        print(f"Saved to {state.add_note(text)}")
        return 0
    for day, text in state.recent_notes(days=args.days):
        print(f"## {day}\n{text}")
    return 0


SKILL_TARGETS = ("claude", "codex")


def cmd_install(args):
    """Link the chief command into ~/.local/bin, and install the chief skill.

    The skill goes to ~/.agents/skills/chief (the Agent Skills standard location).
    Harnesses that read another folder get a symlink to it.
    """
    bin_src = paths.REPO_ROOT / "bin" / "chief"
    paths.BIN_LINK.parent.mkdir(parents=True, exist_ok=True)
    if paths.BIN_LINK.is_symlink() or not paths.BIN_LINK.exists():
        paths.BIN_LINK.unlink(missing_ok=True)
        paths.BIN_LINK.symlink_to(bin_src)
        print(f"Linked {paths.BIN_LINK} -> {bin_src}")
    else:
        print(f"{paths.BIN_LINK} exists and is not a link. Skipped it.")
    on_path = str(paths.BIN_LINK.parent) in os.environ.get("PATH", "").split(":")
    if not on_path:
        print(f"Add {paths.BIN_LINK.parent} to your PATH, so that agents can run `chief`.")

    template = (paths.SKILL_DIR / "SKILL.md").read_text()
    text = (template.replace("{{CHIEF}}", str(paths.BIN_LINK))
                    .replace("{{AGENTS_MD}}", str(paths.AGENT_DIR / "AGENTS.md")))
    target = paths.AGENTS_SKILLS / "chief"
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(text)
    print(f"Wrote {target / 'SKILL.md'}")
    for harness in SKILL_TARGETS:
        root = paths.HARNESS_SKILLS[harness]
        if not root.parent.exists():
            continue  # This harness is not installed.
        root.mkdir(exist_ok=True)
        link = root / "chief"
        if link.is_symlink():
            link.unlink()
        if link.exists():
            print(f"{link} exists and is not a link. Skipped it.")
            continue
        link.symlink_to(target)
        print(f"Linked {link} -> {target}")
    print("In Claude Code, type /chief. In Codex, type $chief.")
    return 0


def cmd_org(args):
    org = state.load_org()
    if args.action == "show":
        print(json.dumps(org, indent=2))
        return 0
    if args.action == "role":
        target = args.target
        for w in cmux.workspaces():
            for s in w["surfaces"]:
                if target in (s["ref"], s["id"]):
                    org["roles"][s["id"]] = args.role
                    state.save_org(org)
                    print(f"Set {s['ref']} to {args.role}.")
                    return 0
        print(f"No tab matches {target}.", file=sys.stderr)
        return 1
    return 0


def cmd_message(args):
    target = args.target
    if not re.fullmatch(r"(surface|workspace):\d+", target):
        dept = _resolve_dept(target)
        _, tabs = _departments()
        refs = [w["ref"] for w, d in tabs if dept and d == dept["path"]]
        if not refs:
            print(f"No open team matches '{target}'. Give a workspace or surface ref.", file=sys.stderr)
            return 1
        if len(refs) > 1:
            print(f"'{target}' has {len(refs)} teams ({', '.join(refs)}). Give one ref.", file=sys.stderr)
            return 1
        target = refs[0]
    text = " ".join(args.text)
    if text == "-":
        text = sys.stdin.read()
    ok, out = cmux.message(target, text, args.sender)
    print(out)
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="chief", description="Chief of AI Staff")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("start", "open", "serve"):
        p = sub.add_parser(name)
        p.add_argument("--port", type=int, default=int(os.environ.get("CHIEF_PORT", DEFAULT_PORT)))
        if name == "start":
            p.add_argument("--agent", choices=list(AGENT_COMMANDS), default=os.environ.get("CHIEF_AGENT", "claude"))
    sub.add_parser("stop")
    sub.add_parser("install", help="Put chief on the PATH and install the /chief skill")

    p = sub.add_parser("meeting", help="Print the meeting agenda")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("brief", help="Print the details of every open team")
    p.add_argument("--range", choices=list(snapshot.RANGES), default="7d")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("dept", help="Show or change departments")
    dsub = p.add_subparsers(dest="action", required=True)
    pl = dsub.add_parser("list")
    pl.add_argument("--status", choices=state.STATUSES)
    pa = dsub.add_parser("accept", help="Confirm suggested statuses (all, or the names you give)")
    pa.add_argument("names", nargs="*")
    ps = dsub.add_parser("set")
    ps.add_argument("name", help="department name, folder, path, or workspace ref")
    ps.add_argument("--status", choices=state.STATUSES)
    ps.add_argument("--focus", choices=state.FOCUS + ["none"])
    ps.add_argument("--goal")
    ps.add_argument("--deadline", help="YYYY-MM-DD, or none")
    ps.add_argument("--next", help="the next step")
    ps.add_argument("--notes")
    ps.add_argument("--rename")

    p = sub.add_parser("notes", help="Meeting notes")
    nsub = p.add_subparsers(dest="action", required=True)
    na = nsub.add_parser("add")
    na.add_argument("text", nargs="+")
    nl = nsub.add_parser("show")
    nl.add_argument("--days", type=int, default=7)

    p = sub.add_parser("org", help="Tab roles")
    osub = p.add_subparsers(dest="action", required=True)
    osub.add_parser("show")
    pr = osub.add_parser("role")
    pr.add_argument("target", help="surface ref or id")
    pr.add_argument("role", choices=["coordinator", "worker", "ignore"])

    p = sub.add_parser("message", help="Send a message to the coordinator of a team through cmux")
    p.add_argument("target", help="department name or folder, workspace ref, or surface ref")
    p.add_argument("text", nargs="+")
    p.add_argument("--sender", default=SENDER)

    args = parser.parse_args(argv)
    handlers = {
        "start": cmd_start, "open": cmd_open, "stop": cmd_stop, "install": cmd_install,
        "meeting": cmd_meeting, "brief": cmd_brief, "dept": cmd_dept, "notes": cmd_notes,
        "org": cmd_org, "message": cmd_message,
        "serve": lambda a: serve(a.port) or 0,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
