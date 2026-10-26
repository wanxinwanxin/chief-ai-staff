"""chief: command line for the Chief of AI Staff.

  chief meeting            Print the meeting agenda: what needs you, and what the chief can do.
  chief brief              Print the details of every open team.
  chief dept list|set|accept
                           Show or change departments: status, focus, goal, deadline, next step.
  chief notes add|show     Meeting notes.
  chief message <team> <text>
                           Send a message to the coordinator of a team, on any machine.
  chief spawn <dept> <task> [--on NODE] [--agent claude|codex]
                           Start a new team for a department, on any machine.
  chief nodes              The machines (nodes) and when the hub last heard from each.
  chief up                 On the hub: keep the dashboard and the chief session running.
  chief node link          On a node: keep the connection to the hub (data, actions, dashboard).
  chief node install|uninstall
                           On a Mac node: run `chief node link` at login (launchd).
  chief start [--agent claude|codex]
                           Start the dashboard and open the Chief of Staff tab in cmux.
  chief install            Put `chief` on the PATH and install the /chief skill for Claude Code and Codex.
  chief open | serve | stop
  chief org show|role      Tab roles (coordinator, worker, ignore).

On a node (config.json has "hub"), the commands that read or change the org run on
the hub through SSH, so that every machine sees the same departments and notes.
Set CHIEF_LOCAL=1 to run them on this machine only.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

from . import agenda, brief, nodes, paths, portfolio, snapshot, state
from .server import DEFAULT_PORT, serve
from .sources import cmux, tmux

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


def _start_on_node(args):
    """On a node, the chief runs on the hub. Attach to it, and check the dashboard link."""
    cfg = nodes.config()
    if _server_up(cfg["port"]):
        print(f"Dashboard of the hub: {_url(cfg['port'])}")
    else:
        print("The link to the hub is not running. Start it with: chief node install")
    if cmux.available():
        ok, out = cmux.run(["ssh-tmux", cfg["hub"], "attach", snapshot.CHIEF_SESSION], timeout=20)
        print(out or f"Attached to the chief session on {cfg['hub']}.")
        return 0 if ok else 1
    print(f"Attach to the chief with: ssh -t {cfg['hub']} tmux attach -t {snapshot.CHIEF_SESSION}")
    return 0


def cmd_start(args):
    if nodes.config()["hub"]:
        return _start_on_node(args)
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
    if nodes.config()["hub"]:
        args.port = nodes.config()["port"]
        if not _server_up(args.port):
            print("The link to the hub is not running. Start it with: chief node install", file=sys.stderr)
            return 1
    elif not _ensure_server(args.port):
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
    """Departments without the cost scan: portfolio folders of every node, and org entries."""
    org = state.load_org()
    rows = {}
    folder_lists = [list(portfolio.scan())]
    if nodes.is_hub():
        folder_lists += [list(n.get("portfolio") or {}) for n in nodes.load_remote(exclude=nodes.config()["node"])]
    for folders in folder_lists:
        for f in folders:
            rows.setdefault(portfolio.key(f), {"key": portfolio.key(f), "path": f})
    for k in org["projects"]:
        rows.setdefault(k, {"key": k, "path": k})
    for k, r in rows.items():
        meta = org["projects"].get(k, {})
        r["name"] = meta.get("name") or snapshot._pretty_folder(k)
        r["folder"] = k
        r["meta"] = meta
    return list(rows.values())


def _resolve_dept(query, rows=None):
    rows = rows if rows is not None else _departments()
    q = query.strip().lower().rstrip("/")
    exact = [r for r in rows if q in (r["key"].lower(), r["path"].lower(), r["name"].lower())]
    if len(exact) == 1:
        return exact[0]
    hits = [r for r in rows if q in r["name"].lower() or q in r["key"].lower()]
    return hits[0] if len(hits) == 1 else None


def _check_focus(key, level):
    if level not in state.FOCUS_LIMITS:
        return None
    org = state.load_org()
    current = [f for f, m in org["projects"].items() if m.get("focus") == level and f != key]
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
                              d.get("goal") or "-", d.get("deadline") or "-",
                              ", ".join(f"{n}: {p}" for n, p in d["paths"].items())]))
        return 0
    if args.action == "accept":
        snap = snapshot.build("7d")
        targets = [d for d in snap["departments"] if d["statusSource"] == "suggested" and d["inRoots"]]
        if args.names:
            wanted = {(_resolve_dept(n) or {}).get("key") for n in args.names}
            targets = [d for d in targets if d["key"] in wanted]
        org = state.load_org()
        for d in targets:
            org["projects"].setdefault(d["key"], {}).update(status=d["status"], statusSetAt=time.time())
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
    problem = _check_focus(dept["key"], focus)
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
    meta = state.update_dept(dept["key"], **fields)
    print(f"Updated {dept['name']} ({dept['key']}): {json.dumps(meta)}")
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
        snap = snapshot.build("24h")
        for t in snap["teams"]:
            for a in t["agents"]:
                if args.target in (a["surfaceRef"], a["surfaceId"]):
                    org["roles"][a["surfaceId"]] = args.role
                    state.save_org(org)
                    print(f"Set {t['nodeLabel']} {a['surfaceRef']} to {args.role}.")
                    return 0
        print(f"No agent matches {args.target}.", file=sys.stderr)
        return 1
    return 0


def _find_team(query, snap):
    """Find one team by department, team key, workspace ref, tmux session, or agent ref."""
    q = query.strip().lower()
    teams = snap["teams"] + ([snap["chief"]] if snap.get("chief") else [])
    for t in teams:
        refs = {t["key"].lower(), str(t["workspaceRef"]).lower(), str(t["workspaceId"]).lower()}
        for a in t["agents"]:
            if q in (str(a["surfaceRef"]).lower(), str(a["surfaceId"]).lower()):
                return t, a, None
        if q in refs:
            return t, None, None
    dept = _resolve_dept(query, [{"key": d["key"], "path": d["path"], "name": d["name"]}
                                 for d in snap["departments"]])
    hits = [t for t in snap["teams"] if dept and t["department"] == dept["key"]]
    if len(hits) == 1:
        return hits[0], None, None
    if not hits:
        return None, None, f"No open team matches '{query}'."
    refs = ", ".join(f"{t['key']} ({t['nodeLabel']}: {t['task']})" for t in hits)
    return None, None, f"'{query}' has {len(hits)} teams: {refs}. Give one team key."


def _run_action(node, action, wait=15.0):
    """Run an action on this machine, or send it to the node through the hub outbox."""
    if node == nodes.config()["node"]:
        return nodes.execute(action)
    aid = nodes.enqueue(node, action)
    result = nodes.wait(node, aid, timeout=wait)
    if result is None:
        return {"ok": True, "queued": True,
                "output": f"Queued for {node}. The node is offline or slow. It gets the action when it connects."}
    return result


def cmd_message(args):
    snap = snapshot.build("24h")
    team, agent, problem = _find_team(args.target, snap)
    if not team:
        print(problem, file=sys.stderr)
        return 1
    agent = agent or next((a for a in team["agents"] if a["role"] == "coordinator"), None)
    if not agent:
        print(f"The team {team['key']} has no agent to receive a message.", file=sys.stderr)
        return 1
    text = " ".join(args.text)
    if text == "-":
        text = sys.stdin.read()
    action = {"action": "message", "text": text, "sender": args.sender, "surfaceRef": agent["surfaceRef"],
              "workspaceRef": team["workspaceRef"], "paneId": agent.get("paneId")}
    result = _run_action(team["node"], action)
    print(f"{team['nodeLabel']} / {team['name']} / {agent['title']}: {result['output']}")
    return 0 if result["ok"] else 1


SPAWN_COMMANDS = {
    "claude": lambda name, task, term: ["claude", *(["--remote-control", name] if term == "tmux" else []), task],
    "codex": lambda name, task, term: ["codex", task],
}


def cmd_spawn(args):
    snap = snapshot.build("24h")
    rows = [{"key": d["key"], "path": d["path"], "name": d["name"]} for d in snap["departments"]]
    found = _resolve_dept(args.department, rows)
    dept = next((d for d in snap["departments"] if found and d["key"] == found["key"]), None)
    if not dept:
        print(f"No single department matches '{args.department}'. Run: chief dept list", file=sys.stderr)
        return 1
    hub = nodes.config()["node"]
    node = args.on or (hub if hub in dept["paths"] else (dept["nodes"][0] if len(dept["nodes"]) == 1 else None))
    if not node:
        print(f"{dept['name']} is on {', '.join(dept['nodes'])}. Choose one with --on.", file=sys.stderr)
        return 1
    cwd = dept["paths"].get(node)
    if not cwd:
        print(f"{dept['name']} has no folder on {node}. Clone it there first.", file=sys.stderr)
        return 1
    row = next((r for r in snap["nodes"] if r["node"] == node), {})
    term = row.get("terminal")
    task = " ".join(args.task)
    if term == "tmux":
        taken = set(tmux.session_names()) if node == hub else {t["workspaceId"] for t in snap["teams"]}
        name, i = f"project-{dept['key']}", 2
        while name in taken:
            name, i = f"project-{dept['key']}-{i}", i + 1
    else:
        name = dept["name"]
    command = shlex.join(SPAWN_COMMANDS[args.agent](name, task, term))
    result = _run_action(node, {"action": "spawn", "name": name, "cwd": cwd, "command": command,
                                "window": args.agent}, wait=20)
    print(f"{row.get('label', node)} / {dept['name']}: {result['output']}")
    return 0 if result["ok"] else 1


def cmd_nodes(args):
    snap = snapshot.build("24h")
    for r in snap["nodes"]:
        state_text = "this machine" if r["local"] else ("online" if r["online"] else "offline")
        print(f"{r['label']} ({r['node']}): {state_text}, {r['terminal']}, {r['teams']} teams, "
              f"{r['agents']} agents ({r['working']} working), data from {snapshot.ago(r['at'])}")
    return 0


HUB_UNIT = """[Unit]
Description=Chief of AI Staff hub (dashboard and node data)

[Service]
ExecStart={chief} serve --port {port}
Restart=always
RestartSec=5
Environment=PATH={home}/.local/bin:{home}/.npm-global/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
"""

CHIEF_RC_NAME = "Chief of Staff"
SESSIONS_TSV = paths.HOME / ".config" / "agent-workspace" / "sessions.tsv"


def cmd_up(args):
    """On the hub: run the dashboard as a user service, and the chief in the tmux session "chief"."""
    if not nodes.is_hub():
        print("Run `chief up` on the hub. This machine is a node of " + nodes.config()["hub"], file=sys.stderr)
        return 2
    chief_bin = paths.BIN_LINK if paths.BIN_LINK.exists() else paths.REPO_ROOT / "bin" / "chief"
    if sys.platform.startswith("linux"):
        unit = paths.HOME / ".config" / "systemd" / "user" / "chief-hub.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(HUB_UNIT.format(chief=chief_bin, port=args.port, home=paths.HOME))
        for cmd in (["daemon-reload"], ["enable", "--now", "chief-hub.service"], ["restart", "chief-hub.service"]):
            subprocess.run(["systemctl", "--user", *cmd], check=False)
        print(f"The dashboard runs as the user service chief-hub on 127.0.0.1:{args.port}.")
    else:
        _ensure_server(args.port)
    if not tmux.available() and not shutil.which("tmux"):
        print("tmux is not installed, so the chief session cannot run in the background.", file=sys.stderr)
        return 1
    agent_cmd = os.environ.get("CHIEF_AGENT_CMD") or shlex.join(
        ["claude", "--remote-control", CHIEF_RC_NAME, "--permission-mode", "auto"] if args.agent == "claude"
        else AGENT_COMMANDS[args.agent]()[:-1])
    if tmux.run(["has-session", "-t", f"={snapshot.CHIEF_SESSION}"])[0]:
        print(f"The tmux session '{snapshot.CHIEF_SESSION}' runs already.")
    else:
        ok, out = tmux.spawn(snapshot.CHIEF_SESSION, str(paths.AGENT_DIR), agent_cmd)
        print(out)
    if SESSIONS_TSV.exists() and not any(line.split("\t")[0] == snapshot.CHIEF_SESSION
                                         for line in SESSIONS_TSV.read_text().splitlines()):
        resume = agent_cmd.replace("claude ", "claude --continue ", 1) if args.agent == "claude" else agent_cmd
        with SESSIONS_TSV.open("a") as fh:
            fh.write(f"{snapshot.CHIEF_SESSION}\t{paths.AGENT_DIR}\t{resume} || {agent_cmd}\n")
        print(f"Added the chief session to {SESSIONS_TSV}, so that it comes back after a reboot.")
    print("Talk to the chief: Claude app > Remote Control > \"Chief of Staff\", or `agent-remote chief` on the Mac.")
    return 0


LAUNCH_LABEL = "ai.chief.link"
LAUNCH_PLIST = paths.HOME / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"
PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array><string>{chief}</string><string>node</string><string>link</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def cmd_node(args):
    if args.action == "link":
        return nodes.run_link()
    if args.action == "serve-link":
        return nodes.serve_link(args.name)
    if args.action == "status":
        return cmd_nodes(args)
    if sys.platform != "darwin":
        print("`chief node install` uses launchd (macOS). On Linux, use a systemd user service.", file=sys.stderr)
        return 2
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LAUNCH_LABEL}"], capture_output=True, check=False)
    if args.action == "uninstall":
        LAUNCH_PLIST.unlink(missing_ok=True)
        pid = nodes.link_pid()
        if pid:
            os.kill(pid, signal.SIGTERM)
        print("Stopped the link. Data from this machine stops going to the hub.")
        return 0
    if not nodes.config()["hub"]:
        print('Set "hub" in ~/.chief/config.json first, for example {"hub": "agent-cloud"}.', file=sys.stderr)
        return 2
    if cmux.available() and cmux.socket_mode() == "cmuxOnly":
        # A launchd job cannot use cmux in this mode. Run the link in a cmux Dock terminal.
        if nodes.link_pid():
            print("A link runs already.")
            return 0
        link_cmd = [str(paths.REPO_ROOT / "bin" / "chief"), "node", "link"]
        ok, out = cmux.run(["new-pane", "--type", "terminal", "--placement", "dock", "--command", shlex.join(link_cmd)])
        if not ok:
            print(f"cmux did not open the Dock terminal: {out}", file=sys.stderr)
            return 1
        # Store the restart command on the surface, so that cmux can run it again when it restores the Dock.
        surface = re.search(r"surface:\d+", out or "")
        if surface:
            cmux.run(["surface", "resume", "set", "--surface", surface.group(0), "--cwd", str(paths.REPO_ROOT),
                      "--name", "Chief link", "--", *link_cmd])
        print("The link runs in a terminal in the cmux Dock (right sidebar), because the cmux socket "
              "accepts only processes that cmux started. When cmux restores the Dock after a restart, "
              "it asks to run the link again. Choose to always allow it.")
        print(f"Dashboard of the hub: http://127.0.0.1:{nodes.config()['port']}/")
        return 0
    LAUNCH_PLIST.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_PLIST.write_text(PLIST.format(label=LAUNCH_LABEL, chief=paths.REPO_ROOT / "bin" / "chief",
                                         log=paths.LINK_LOG))
    res = subprocess.run(["launchctl", "bootstrap", domain, str(LAUNCH_PLIST)], capture_output=True, text=True)
    if res.returncode != 0:
        print(f"launchctl failed: {res.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"The link runs at login ({LAUNCH_PLIST}). Log: {paths.LINK_LOG}")
    print(f"Dashboard of the hub: http://127.0.0.1:{nodes.config()['port']}/")
    return 0


PROXIED = {"meeting", "brief", "dept", "notes", "message", "spawn", "org", "nodes"}


def _proxy(argv):
    """Run this command on the hub through SSH. Stdin and the exit code pass through."""
    cfg = nodes.config()
    remote = f"{cfg['hubChief']} {shlex.join(argv)}"
    try:
        return subprocess.call(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", cfg["hub"], remote])
    except OSError as exc:
        print(f"Cannot reach the hub {cfg['hub']}: {exc}", file=sys.stderr)
        return 1


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

    p = sub.add_parser("message", help="Send a message to the coordinator of a team, on any machine")
    p.add_argument("target", help="department name, team key, workspace ref, tmux session, or agent ref")
    p.add_argument("text", nargs="+")
    p.add_argument("--sender", default=SENDER)

    p = sub.add_parser("spawn", help="Start a new team for a department, on any machine")
    p.add_argument("department")
    p.add_argument("task", nargs="+")
    p.add_argument("--on", help="node name, for example mac or cloud")
    p.add_argument("--agent", choices=list(SPAWN_COMMANDS), default="claude")

    sub.add_parser("nodes", help="The machines and when the hub last heard from each")

    p = sub.add_parser("up", help="On the hub: run the dashboard service and the chief session")
    p.add_argument("--port", type=int, default=int(os.environ.get("CHIEF_PORT", DEFAULT_PORT)))
    p.add_argument("--agent", choices=list(AGENT_COMMANDS), default=os.environ.get("CHIEF_AGENT", "claude"))

    p = sub.add_parser("node", help="Connection between a node and the hub")
    nsub = p.add_subparsers(dest="action", required=True)
    nsub.add_parser("link")
    nsub.add_parser("status")
    nsub.add_parser("install")
    nsub.add_parser("uninstall")
    pn = nsub.add_parser("serve-link")
    pn.add_argument("name")

    argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(argv)
    if args.cmd in PROXIED and nodes.config()["hub"] and not os.environ.get("CHIEF_LOCAL"):
        return _proxy(argv)
    handlers = {
        "start": cmd_start, "open": cmd_open, "stop": cmd_stop, "install": cmd_install,
        "meeting": cmd_meeting, "brief": cmd_brief, "dept": cmd_dept, "notes": cmd_notes,
        "org": cmd_org, "message": cmd_message, "spawn": cmd_spawn, "nodes": cmd_nodes,
        "up": cmd_up, "node": cmd_node,
        "serve": lambda a: serve(a.port) or 0,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
