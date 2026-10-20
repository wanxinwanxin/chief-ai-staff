"""chief: command line for the Chief of AI Staff.

  chief start              Start the dashboard and open the Chief of Staff tab in cmux.
  chief open               Open the dashboard (in cmux, a browser split).
  chief serve              Run the dashboard server in the foreground.
  chief stop               Stop the background dashboard server.
  chief brief              Print a briefing of all projects for the chief agent.
  chief focus show|set|clear
  chief org show|set|role
  chief message <project> <text>
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

from . import brief, paths, snapshot, state
from .server import DEFAULT_PORT, serve
from .sources import cmux

SENDER = "Chief of Staff"


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
    agent_cmd = os.environ.get(
        "CHIEF_AGENT_CMD", "claude --permission-mode auto " + shlex.quote("Give me my briefing.")
    )
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


def cmd_focus(args):
    if args.action == "show":
        print(json.dumps(state.load_focus(), indent=2))
    elif args.action == "clear":
        paths.FOCUS_FILE.unlink(missing_ok=True)
        print("Cleared the focus plan.")
    else:
        raw = sys.stdin.read() if args.json == "-" else args.json
        try:
            focus = json.loads(raw)
        except (TypeError, ValueError):
            print('Give the plan as JSON: {"note": "...", "items": [{"project": "...", "why": "...", '
                  '"next": "..."}], "later": ["..."]}', file=sys.stderr)
            return 2
        state.save_focus(focus)
        print("Saved the focus plan. The dashboard shows it at the next refresh.")
    return 0


def _projects_light():
    """Workspaces with their folders, without the cost scan."""
    org = state.load_org()
    rows = []
    for w in cmux.workspaces():
        cwd = cmux.sidebar_state(w["ref"]).get("cwd")
        meta = org["projects"].get(cwd or "", {})
        rows.append({**w, "cwd": cwd, "folder": os.path.basename((cwd or "").rstrip("/")),
                     "name": meta.get("name") or snapshot._pretty_folder(cwd)})
    return rows


def _resolve(query):
    rows = _projects_light()
    q = query.strip().lower()
    for r in rows:
        if q in (r["ref"].lower(), (r["id"] or "").lower()):
            return r
    for r in rows:
        if q in (r["name"].lower(), r["folder"].lower(), r["title"].lower(), (r["cwd"] or "").lower()):
            return r
    hits = [r for r in rows if q in r["name"].lower() or q in r["folder"].lower() or q in r["title"].lower()]
    return hits[0] if len(hits) == 1 else None


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
    proj = _resolve(args.project)
    if not proj or not proj["cwd"]:
        print(f"No single project matches '{args.project}'.", file=sys.stderr)
        return 1
    meta = org["projects"].setdefault(proj["cwd"], {})
    for key in ("name", "priority", "goal", "notes"):
        value = getattr(args, key)
        if value is not None:
            meta[key] = value or None
    state.save_org(org)
    print(f"Updated {proj['name']} ({proj['cwd']}): {json.dumps(meta)}")
    return 0


def cmd_message(args):
    target = args.target
    if not re.fullmatch(r"(surface|workspace):\d+", target):
        proj = _resolve(target)
        if not proj:
            print(f"No single project matches '{target}'.", file=sys.stderr)
            return 1
        target = proj["ref"]
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
    sub.add_parser("stop")

    p = sub.add_parser("brief", help="Print a briefing of all projects")
    p.add_argument("--range", choices=list(snapshot.RANGES), default="7d")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("focus", help="Show or set the focus plan that the dashboard shows")
    p.add_argument("action", choices=["show", "set", "clear"])
    p.add_argument("json", nargs="?", default="-", help="JSON plan, or - to read stdin")

    p = sub.add_parser("org", help="Project priorities, goals, and tab roles")
    osub = p.add_subparsers(dest="action", required=True)
    osub.add_parser("show")
    ps = osub.add_parser("set")
    ps.add_argument("project")
    ps.add_argument("--name")
    ps.add_argument("--priority", choices=["high", "normal", "low", "paused", ""])
    ps.add_argument("--goal")
    ps.add_argument("--notes")
    pr = osub.add_parser("role")
    pr.add_argument("target", help="surface ref or id")
    pr.add_argument("role", choices=["coordinator", "worker", "ignore"])

    p = sub.add_parser("message", help="Send a message to a project's agent through cmux")
    p.add_argument("target", help="project name, folder, workspace ref, or surface ref")
    p.add_argument("text", nargs="+")
    p.add_argument("--sender", default=SENDER)

    args = parser.parse_args(argv)
    handlers = {
        "start": cmd_start, "open": cmd_open, "stop": cmd_stop, "brief": cmd_brief,
        "focus": cmd_focus, "org": cmd_org, "message": cmd_message,
        "serve": lambda a: serve(a.port) or 0,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
