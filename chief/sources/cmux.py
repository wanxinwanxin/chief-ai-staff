"""cmux adapter: tabs, agent sessions, jump, and agent messages.

Every function returns None or an empty value when cmux is not available, so the
rest of the code also works in other terminals.
"""

import json
import os
import shutil
import subprocess

from .. import paths

_FALLBACK = "/Applications/cmux.app/Contents/Resources/bin/cmux"

STATUS_MAP = {
    "needsInput": "needs_input",
    "running": "working",
    "idle": "idle",
    "unknown": "idle",
}


def binary():
    found = shutil.which("cmux")
    if found:
        return found
    return _FALLBACK if os.path.exists(_FALLBACK) else None


def run(args, input_text=None, timeout=6):
    """Run a cmux command. Return (ok, output)."""
    exe = binary()
    if not exe:
        return False, "cmux is not installed"
    try:
        res = subprocess.run(
            [exe, *args], capture_output=True, text=True, timeout=timeout, input=input_text
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return res.returncode == 0, (res.stdout if res.returncode == 0 else (res.stderr or res.stdout)).strip()


def available():
    return run(["ping"], timeout=3)[0]


def workspaces():
    ok, out = run(["tree", "--all", "--json", "--id-format", "both"])
    if not ok:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    result = []
    for win in data.get("windows", []):
        for ws in win.get("workspaces", []):
            surfaces = []
            for p_index, pane in enumerate(ws.get("panes", [])):
                for s in pane.get("surfaces", []):
                    surfaces.append(
                        {
                            "id": s.get("id"),
                            "ref": s.get("ref"),
                            "title": s.get("title") or "",
                            "type": s.get("type"),
                            "order": [pane.get("index", p_index), s.get("index_in_pane", 0)],
                        }
                    )
            result.append(
                {
                    "id": ws.get("id"),
                    "ref": ws.get("ref"),
                    "title": ws.get("title") or "",
                    "description": ws.get("description"),
                    "index": ws.get("index"),
                    "selected": bool(ws.get("selected")),
                    "surfaces": surfaces,
                }
            )
    return result


def sidebar_state(ref):
    ok, out = run(["sidebar-state", "--workspace", ref], timeout=4)
    if not ok:
        return {}
    state = {}
    for line in out.splitlines():
        if line.startswith(" ") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        state[key.strip()] = value.strip()
    return state


def _load(name):
    try:
        return json.loads((paths.CMUX_STATE / name).read_text())
    except (OSError, ValueError):
        return None


def _alive(pid):
    if not pid:
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError):
        return True
    return True


def _normalize(raw, kind):
    lifecycle = raw.get("agentLifecycle") or raw.get("runtimeStatus") or "unknown"
    status = STATUS_MAP.get(lifecycle, "idle")
    if not _alive(raw.get("pid")):
        status = "ended"
    return {
        "kind": kind,
        "sessionId": raw.get("sessionId"),
        "cwd": raw.get("cwd"),
        "status": status,
        "transcriptPath": raw.get("transcriptPath"),
        "updatedAt": raw.get("updatedAt") or 0,
        "startedAt": raw.get("startedAt"),
        "lastBody": raw.get("lastBody"),
        "workspaceId": raw.get("workspaceId"),
    }


def agent_sessions():
    """Return {surface id: agent session} from the cmux hook state files."""
    by_surface = {}
    claude = _load("claude-hook-sessions.json") or {}
    sessions = claude.get("sessions") or {}
    for surface_id, ref in (claude.get("activeSessionsBySurface") or {}).items():
        raw = sessions.get((ref or {}).get("sessionId"))
        if raw:
            by_surface[surface_id] = _normalize(raw, "claude")
    codex = _load("codex-hook-sessions.json") or {}
    for key, raw in (codex.get("sessions") or {}).items():
        surface_id = raw.get("surfaceId")
        if not surface_id:
            continue
        raw = dict(raw, sessionId=raw.get("sessionId") or key)
        cur = by_surface.get(surface_id)
        if cur is None or (raw.get("updatedAt") or 0) > cur["updatedAt"]:
            by_surface[surface_id] = _normalize(raw, "codex")
    return by_surface


def teams(node):
    """Raw teams for the snapshot: one for each workspace, with the agents in its tabs."""
    sessions = agent_sessions()
    out = []
    for ws in workspaces():
        side = sidebar_state(ws["ref"])
        branch_raw = (side.get("git_branch") or "none").split()
        agents = []
        for surf in sorted(ws["surfaces"], key=lambda s: s["order"]):
            sess = sessions.get(surf["id"])
            if sess:
                agents.append({"surfaceId": surf["id"], "surfaceRef": surf["ref"], "title": surf["title"],
                               "session": sess})
        out.append({
            "key": f"{node}:{ws['id']}",
            "workspaceId": ws["id"],
            "workspaceRef": ws["ref"],
            "title": ws["title"],
            "cwd": side.get("cwd") or (agents[0]["session"].get("cwd") if agents else None),
            "branch": None if branch_raw[0] == "none" else branch_raw[0],
            "dirty": len(branch_raw) > 1 and branch_raw[1] == "dirty",
            "selected": ws["selected"],
            "agents": agents,
        })
    return out


def spawn(name, cwd, command):
    ok, out = run(["new-workspace", "--name", name, "--cwd", cwd, "--command", command, "--focus", "false"])
    return ok, out or f"Opened workspace {name}."


def jump(workspace_id, surface_id=None):
    ok, out = run(["rpc", "workspace.select", json.dumps({"workspace_id": workspace_id})])
    if ok and surface_id:
        ok, out = run(["rpc", "surface.focus", json.dumps({"surface_id": surface_id})])
    return ok, out


def message(target, text, sender):
    """Send a message through the agent's own hooks. cmux never types into the terminal."""
    return run(["agent", "message", target, "--from", sender, "-"], input_text=text)


def inbox(limit=30):
    ok, out = run(["agent", "inbox", "--limit", str(limit), "--json"])
    if not ok:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    return data.get("messages", data) if isinstance(data, dict) else data
