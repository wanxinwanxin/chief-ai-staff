"""tmux adapter: teams, agents, status, message delivery, and new teams.

A team is one tmux session. An agent is a pane that runs Claude Code or Codex.
Claude Code panes connect to their session through ~/.claude/sessions. Codex panes
connect to their log through the log files that the Codex processes hold open.

Status comes from Claude Code when it is available. The screen adds two facts for
both agents: a permission menu means that the agent waits for the user, and
"esc to interrupt" means that a turn runs.
"""

import json
import os
import re
import shutil
import subprocess
import time

from .. import paths
from . import claude_sessions

FIELDS = ["session_name", "session_attached", "window_index", "window_name", "pane_id",
          "pane_index", "pane_active", "pane_current_path", "pane_current_command", "pane_pid"]
FORMAT = "\t".join("#{%s}" % f for f in FIELDS)
SCREEN_LINES = 18

_WORKING = re.compile(r"esc to interrupt", re.I)
# A permission menu: a question line and a numbered "Yes" option near the bottom.
_ASK_LINE = re.compile(r"(do you want to|would you like to|allow .*\?|approve)", re.I)
_YES_OPTION = re.compile(r"^\s*[❯›>▌]?\s*1\.\s*yes\b", re.I | re.M)


def run(args, input_text=None, timeout=5):
    try:
        res = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=timeout,
                             input=input_text)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return res.returncode == 0, (res.stdout if res.returncode == 0 else res.stderr).strip()


def available():
    return bool(shutil.which("tmux")) and run(["list-sessions"], timeout=3)[0]


def panes():
    ok, out = run(["list-panes", "-a", "-F", FORMAT])
    if not ok:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == len(FIELDS):
            rows.append(dict(zip(FIELDS, parts)))
    return rows


def screen(pane, lines=SCREEN_LINES):
    ok, out = run(["capture-pane", "-p", "-t", pane, "-S", f"-{lines}"])
    if not ok:
        return ""
    return "\n".join(out.rstrip("\n").splitlines()[-lines:])


def waits_at_prompt(text):
    return bool(_YES_OPTION.search(text)) and bool(_ASK_LINE.search(text))


def classify(text):
    """Return the status that the screen shows: needs_input, working, or None."""
    if waits_at_prompt(text):
        return "needs_input"
    if _WORKING.search(text):
        return "working"
    return None


def _codex_open_logs():
    """Codex logs that a running Codex process holds open (Linux /proc only)."""
    root = str(paths.CODEX_SESSIONS)
    found = {}
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return found
    for pid in pids:
        try:
            with open(f"/proc/{pid}/comm") as fh:
                if fh.read().strip() != "codex":
                    continue
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if target.startswith(root) and target.endswith(".jsonl"):
                found[target] = _codex_meta(target)
    return found


def _codex_meta(path):
    try:
        with open(path, "rb") as fh:
            rec = json.loads(fh.readline())
        payload = rec.get("payload") or {}
        return {"cwd": payload.get("cwd"), "sessionId": payload.get("id"), "mtime": os.path.getmtime(path)}
    except (OSError, ValueError):
        return {}


def _branch(cwd):
    try:
        res = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout.strip() or None if res.returncode == 0 else None


def teams(node, folders=()):
    """Raw teams for the snapshot. Keep tmux sessions with an agent, or with a project folder."""
    claude_by_pane = {s["pane"]: s for s in claude_sessions.sessions() if s["pane"]}
    codex_logs = None
    groups = {}
    for p in panes():
        groups.setdefault(p["session_name"], []).append(p)
    out = []
    for name, rows in groups.items():
        agents = []
        for p in sorted(rows, key=lambda r: (int(r["window_index"]), int(r["pane_index"]))):
            sess = claude_by_pane.get(p["pane_id"])
            if sess:
                raw = dict(sess)
            elif p["pane_current_command"] == "codex":
                if codex_logs is None:
                    codex_logs = _codex_open_logs()
                mine = sorted(((path, m) for path, m in codex_logs.items() if m.get("cwd") == p["pane_current_path"]),
                              key=lambda x: -(x[1].get("mtime") or 0))
                path, meta = mine[0] if mine else (None, {})
                raw = {"kind": "codex", "sessionId": meta.get("sessionId"), "cwd": p["pane_current_path"],
                       "status": "idle", "transcriptPath": path, "updatedAt": meta.get("mtime") or 0}
            else:
                continue
            text = screen(p["pane_id"])
            seen = classify(text)
            raw["waitReason"] = None
            if seen == "needs_input" or raw["status"] == "needs_input":
                raw["status"], raw["waitReason"] = "needs_input", "prompt"
            elif seen == "working":
                raw["status"] = "working"
            raw.setdefault("transcriptPath", None)
            agents.append({
                "surfaceId": f"{node}:{p['pane_id']}",
                "surfaceRef": f"{name}:{p['window_index']}.{p['pane_index']}",
                "paneId": p["pane_id"],
                "title": raw.get("name") or p["window_name"],
                "session": raw,
            })
        cwd = agents[0]["session"].get("cwd") if agents else next(
            (r["pane_current_path"] for r in rows if r["pane_active"] == "1"), rows[0]["pane_current_path"])
        in_project = any(cwd == f or cwd.startswith(f + "/") for f in folders)
        if not agents and not in_project:
            continue
        out.append({
            "key": f"{node}:{name}",
            "workspaceId": name,
            "workspaceRef": name,
            "title": name,
            "cwd": cwd,
            "branch": _branch(cwd) if cwd else None,
            "dirty": None,
            "selected": rows[0]["session_attached"] != "0",
            "agents": agents,
        })
    return out


def deliver(pane, text):
    """Paste a message into an agent pane and press Enter.

    Refuse when the agent waits at a permission menu, because a pasted line could
    choose an option there.
    """
    if waits_at_prompt(screen(pane)):
        return False, "The agent waits at a permission prompt. Answer it in the tab first."
    ok, out = run(["load-buffer", "-b", "chief-message", "-"], input_text=text)
    if not ok:
        return False, out
    ok, out = run(["paste-buffer", "-p", "-d", "-b", "chief-message", "-t", pane])
    if not ok:
        return False, out
    time.sleep(0.5)
    ok, out = run(["send-keys", "-t", pane, "Enter"])
    return ok, "Delivered." if ok else out


def spawn(name, cwd, command):
    if run(["has-session", "-t", f"={name}"])[0]:
        return False, f"The tmux session {name} exists already."
    shell = f'export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"; {command}; exec bash'
    ok, out = run(["new-session", "-d", "-s", name, "-c", cwd, shell])
    return ok, f"Started tmux session {name}." if ok else out


def session_names():
    ok, out = run(["list-sessions", "-F", "#{session_name}"])
    return out.splitlines() if ok else []
