"""Live Claude Code sessions from ~/.claude/sessions/<pid>.json.

Claude Code keeps one small file for each running session: the session id, the
folder, the status, the tmux pane when it runs in tmux, and the Remote Control
session when that is on. This works in every terminal. A file can stay after a
crash, so the reader keeps only sessions whose process is alive. Each account in
config.json (state.accounts) has its own sessions/ folder, and the reader reads all.
"""

import json
import os
from pathlib import Path

from .. import paths, state

# Claude Code statuses: busy (a turn runs), shell (idle, with a background shell),
# idle, and waiting (it waits for the user, for example at a permission prompt).
STATUS_MAP = {"busy": "working", "shell": "idle", "idle": "idle", "waiting": "needs_input"}


def alive(pid):
    if not pid:
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError):
        return True
    return True


def sessions():
    roots = [paths.CLAUDE_SESSIONS] + [d / "sessions" for d in state.account_dirs()]
    files = [f for root in roots if root.exists() for f in root.glob("*.json")]
    out = []
    for f in files:
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not alive(d.get("pid")):
            continue
        tmux = d.get("tmux") or ""
        updated = d.get("statusUpdatedAt") or d.get("updatedAt") or 0
        out.append({
            "kind": "claude",
            "pid": d.get("pid"),
            "sessionId": d.get("sessionId"),
            "cwd": d.get("cwd"),
            "status": STATUS_MAP.get(d.get("status"), "working"),
            "rawStatus": d.get("status"),
            "name": d.get("name"),
            "pane": tmux.rsplit(".", 1)[-1] if "%" in tmux else None,
            "tmuxSession": tmux.split(":", 1)[0] if tmux else None,
            "remoteControl": bool(d.get("bridgeSessionId")),
            "updatedAt": updated / 1000 if updated > 1e11 else updated,
            "startedAt": (d.get("startedAt") or 0) / 1000 or None,
        })
    return out


def trusted(folder, config_dir=None):
    """True when Claude Code trusts the folder or a parent folder, False when it does not.

    In a folder with no trust, Claude Code shows the trust dialog and waits for a key,
    before it writes a session file. Thus an agent that nobody watches waits there with
    no sign. None when the settings are not readable: then the caller cannot know.
    Claude Code keeps the real path of a folder, so a link to a trusted folder is trusted.
    Each login keeps its own trust: config_dir is the CLAUDE_CONFIG_DIR of the account
    (None for the default login, with its settings in ~/.claude.json).
    """
    settings = paths.CLAUDE_CONFIG
    config_dir = os.path.expanduser(config_dir or "")
    if config_dir and os.path.realpath(config_dir) != os.path.realpath(paths.HOME / ".claude"):
        settings = Path(config_dir) / ".claude.json"
    try:
        projects = json.loads(settings.read_text()).get("projects") or {}
    except (OSError, ValueError, AttributeError):
        return None
    folder = os.path.expanduser(folder)
    for path in {os.path.abspath(folder), os.path.realpath(folder)}:
        while True:
            if (projects.get(path) or {}).get("hasTrustDialogAccepted"):
                return True
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    return False
