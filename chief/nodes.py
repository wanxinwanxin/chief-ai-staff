"""Nodes: the machines that run agents, and the hub that joins them.

A node is one machine, for example "mac" or "cloud". The hub is the node where the
chief and the dashboard run all the time. Each other node keeps one SSH connection
to the hub (`chief node link`). Through that connection:

  - the node sends its data every few seconds (only the changed log entries),
  - the hub sends actions for the node at once (messages to agents, jumps, new teams),
  - the dashboard port of the hub is forwarded to the node.

Node data (on the hub, one file for each node: ~/.chief/nodes/<name>.json):
  {"node": "mac", "label": "Mac", "terminal": "cmux|tmux|none", "at": <time>,
   "agentDir": "<the chief's folder on that node>",
   "teams": [raw teams from the terminal adapter],
   "logs": {<log path>: <log index entry>},
   "portfolio": {<folder>: {git, mtime, description}},
   "git": {<team folder>: {"24h": activity, "7d": ..., "30d": ...}}}

config.json keys for nodes: "node" (name), "label", "hub" (SSH host of the hub; not
set on the hub itself), "hubChief" (the chief command on the hub), "port".
"""

import json
import shlex
import socket
import subprocess
import sys
import threading
import time
import uuid

from . import paths, portfolio, state
from .sources import claude_sessions, cmux, git, tmux
from .sources.logs import LogIndex

RANGES = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
DEFAULT_PORT = 7377
INTERVAL = 10
ONLINE_SECONDS = 45
ACTION_TTL = 2 * 3600

_index = None


def config():
    cfg = state.load_config()
    default = "mac" if sys.platform == "darwin" else socket.gethostname().split(".")[0]
    name = cfg.get("node") or default
    return {
        "node": name,
        "label": cfg.get("label") or name.capitalize(),
        "hub": cfg.get("hub"),
        "hubChief": cfg.get("hubChief") or "$HOME/.local/bin/chief",
        "port": int(cfg.get("port") or DEFAULT_PORT),
        "hubPort": int(cfg.get("hubPort") or DEFAULT_PORT),
        "interval": float(cfg.get("interval") or INTERVAL),
        "allowedHosts": cfg.get("allowedHosts") or [],
    }


def is_hub():
    return not config()["hub"]


def terminal():
    if cmux.available():
        return "cmux"
    if tmux.available():
        return "tmux"
    return "none"


def _plain_teams(node):
    """Teams for other terminals: one for each running Claude Code session."""
    out = []
    for s in claude_sessions.sessions():
        out.append({"key": f"{node}:{s['sessionId']}", "workspaceId": s["sessionId"],
                    "workspaceRef": s.get("name") or s["sessionId"][:8], "title": s.get("name") or "",
                    "cwd": s.get("cwd"), "branch": None, "dirty": None, "selected": False,
                    "agents": [{"surfaceId": f"{node}:{s['pid']}", "surfaceRef": str(s["pid"]),
                                "title": s.get("name") or "Claude", "session": s}]})
    return out


def collect_local(with_git=False):
    """Collect the data of this machine."""
    global _index
    cfg = config()
    term = terminal()
    port = portfolio.scan()
    folders = list(port)
    if term == "cmux":
        teams = cmux.teams(cfg["node"])
    elif term == "tmux":
        teams = tmux.teams(cfg["node"], folders)
    else:
        teams = _plain_teams(cfg["node"])
    if _index is None:
        _index = LogIndex()
    data = {
        "node": cfg["node"],
        "label": cfg["label"],
        "terminal": term,
        "at": time.time(),
        "agentDir": str(paths.AGENT_DIR),
        "teams": teams,
        "logs": _index.refresh(),
        "portfolio": port,
        "portfolioAt": portfolio.scanned_at(),
        "sessions": tmux.session_names() if term == "tmux" else [],
    }
    if with_git:
        now = time.time()
        data["git"] = {t["cwd"]: {k: git.activity(t["cwd"], now - span) for k, span in RANGES.items()}
                       for t in teams if t.get("cwd")}
    return data


def gather():
    """The data of every node: this machine (live), and on the hub also the other nodes."""
    local = collect_local()
    local.update(gitFn=git.activity, online=True, local=True, receivedAt=local["at"])
    out = [local]
    if is_hub():
        out += load_remote(exclude=local["node"])
    return out


_remote = {}


def load_remote(exclude=None):
    out = []
    if not paths.NODES_DIR.exists():
        return out
    for f in sorted(paths.NODES_DIR.glob("*.json")):
        if f.stem == exclude:
            continue
        try:
            mtime = f.stat().st_mtime
            hit = _remote.get(f.stem)
            if hit and hit[0] == mtime:
                data = hit[1]
            else:
                data = json.loads(f.read_text())
                _remote[f.stem] = (mtime, data)
        except (OSError, ValueError):
            continue
        data["online"] = time.time() - (data.get("receivedAt") or 0) < ONLINE_SECONDS
        data["local"] = False
        out.append(data)
    return out


# Local actions -------------------------------------------------------------


def deliver(agent, text, sender):
    """Deliver a message to an agent on this machine."""
    term = terminal()
    if term == "cmux":
        return cmux.message(agent.get("surfaceRef") or agent.get("workspaceRef"), text, sender)
    if term == "tmux" and agent.get("paneId"):
        return tmux.deliver(agent["paneId"], text)
    return False, "This machine has no terminal adapter that can deliver messages."


def spawn(name, cwd, command):
    term = terminal()
    if term == "cmux":
        return cmux.spawn(name, cwd, command)
    if term == "tmux":
        return tmux.spawn(name, cwd, command)
    return False, "This machine has no cmux or tmux to open a new team in."


def execute(action):
    kind = action.get("action")
    try:
        if kind == "message":
            ok, out = deliver(action, action["text"], action.get("sender") or "Chief of Staff")
        elif kind == "jump":
            ok, out = cmux.jump(action["workspaceId"], action.get("surfaceId"))
        elif kind == "spawn":
            ok, out = spawn(action["name"], action["cwd"], action["command"])
        elif kind == "ping":
            ok, out = True, f"pong from {config()['node']}"
        else:
            ok, out = False, f"Unknown action: {kind}"
    except Exception as exc:  # Report the failure to the hub. The link stays up.
        ok, out = False, f"{type(exc).__name__}: {exc}"
    return {"ok": ok, "output": out}


# Outbox (on the hub) -------------------------------------------------------


def _box(node):
    return paths.OUTBOX_DIR / node


def enqueue(node, action):
    box = _box(node)
    box.mkdir(parents=True, exist_ok=True)
    aid = uuid.uuid4().hex[:12]
    tmp = box / f"{aid}.tmp"
    tmp.write_text(json.dumps(dict(action, id=aid, at=time.time())))
    tmp.rename(box / f"{aid}.json")
    return aid


def wait(node, aid, timeout=15.0):
    done = _box(node) / "done" / f"{aid}.json"
    end = time.time() + timeout
    while time.time() < end:
        try:
            return json.loads(done.read_text())
        except (OSError, ValueError):
            time.sleep(0.3)
    return None


def _finish(box, path, result):
    (box / "done").mkdir(exist_ok=True)
    aid = path.name.split(".")[0]
    (box / "done" / f"{aid}.json").write_text(json.dumps(dict(result, at=time.time())))
    path.unlink(missing_ok=True)


# The link: hub side ----------------------------------------------------------


def _merge(current, data):
    out = {k: v for k, v in current.items() if k != "logs"}
    for k, v in data.items():
        if k not in ("logs", "logKeys", "full"):
            out[k] = v
    logs = {} if data.get("full") else dict(current.get("logs") or {})
    logs.update(data.get("logs") or {})
    keys = data.get("logKeys")
    missing = False
    if keys is not None:
        missing = any(k not in logs for k in keys)
        logs = {k: logs[k] for k in keys if k in logs}
    out["logs"] = logs
    return out, missing


def serve_link(name):
    """Run on the hub, with stdin and stdout connected to the node through SSH."""
    if not name.replace("-", "").replace("_", "").isalnum():
        print(json.dumps({"type": "error", "error": "bad node name"}), flush=True)
        return 2
    paths.NODES_DIR.mkdir(parents=True, exist_ok=True)
    box = _box(name)
    box.mkdir(parents=True, exist_ok=True)
    for f in box.glob("*.sent"):  # Sent on a link that broke before the node answered.
        f.rename(f.with_suffix(".json"))
    lock = threading.Lock()
    stop = threading.Event()

    def send(msg):
        with lock:
            sys.stdout.write(json.dumps(msg) + "\n")
            sys.stdout.flush()

    def pump():
        while not stop.is_set():
            for f in sorted(box.glob("*.json")):
                try:
                    action = json.loads(f.read_text())
                except (OSError, ValueError):
                    continue
                if time.time() - action.get("at", 0) > ACTION_TTL:
                    _finish(box, f, {"ok": False, "output": "Expired before the node connected."})
                    continue
                sent = f.with_suffix(".sent")
                f.rename(sent)
                try:
                    send({"type": "action", **action})
                except OSError:
                    sent.rename(f)
                    stop.set()
            stop.wait(0.5)

    threading.Thread(target=pump, daemon=True).start()
    path = paths.NODES_DIR / f"{name}.json"
    try:
        current = json.loads(path.read_text())
    except (OSError, ValueError):
        current = {}
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("type") == "push":
            merged, missing = _merge(current, msg.get("data") or {})
            if missing:
                send({"type": "resync"})
                continue
            current = merged
            current["receivedAt"] = time.time()
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(current))
            tmp.replace(path)
            send({"type": "ok", "at": current["receivedAt"]})
        elif msg.get("type") == "ack" and msg.get("id"):
            _finish(box, box / f"{msg['id']}.sent", msg)
    stop.set()
    return 0


# The link: node side ---------------------------------------------------------


def _log(text):
    paths.ensure_home()
    with open(paths.LINK_LOG, "a") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")


def _payload(data, sent):
    """Keep only the log entries and the portfolio that changed since the last push."""
    logs = data.pop("logs")
    data.pop("gitFn", None)
    full = sent.get("full", True)
    sizes = {k: (e.get("size"), e.get("mtime")) for k, e in logs.items()}
    old = {} if full else sent.get("logs", {})
    data["logs"] = {k: e for k, e in logs.items() if old.get(k) != sizes[k]}
    data["logKeys"] = list(logs)
    data["full"] = full
    if not full and data.get("portfolioAt") == sent.get("portfolioAt"):
        data.pop("portfolio", None)
    sent.update(full=False, logs=sizes, portfolioAt=data.get("portfolioAt"))
    return data


def _link_once(cfg):
    remote = f"{cfg['hubChief']} node serve-link {shlex.quote(cfg['node'])}"
    cmd = ["ssh", "-C", "-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15",
           "-o", "ServerAliveCountMax=3", "-o", "ExitOnForwardFailure=no",
           "-L", f"127.0.0.1:{cfg['port']}:127.0.0.1:{cfg['hubPort']}", cfg["hub"], remote]
    err = open(paths.LINK_LOG, "a")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                            text=True, bufsize=1)
    lock = threading.Lock()
    sent = {"full": True}

    def send(msg):
        with lock:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()

    def act(msg):
        result = execute(msg)
        _log(f"action {msg.get('action')} {msg.get('id')}: {'ok' if result['ok'] else 'failed'}")
        try:
            send({"type": "ack", "id": msg["id"], **result})
        except OSError:
            pass

    def reader():
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "action":
                threading.Thread(target=act, args=(msg,), daemon=True).start()
            elif msg.get("type") == "resync":
                sent["full"] = True

    threading.Thread(target=reader, daemon=True).start()
    _log(f"link to {cfg['hub']} started")
    try:
        while proc.poll() is None:
            send({"type": "push", "data": _payload(collect_local(with_git=True), sent)})
            end = time.time() + cfg["interval"]
            while time.time() < end and proc.poll() is None:
                time.sleep(0.25)
    except (OSError, ValueError) as exc:
        _log(f"link error: {exc}")
    finally:
        if proc.poll() is None:
            proc.terminate()
        err.close()
    _log(f"link to {cfg['hub']} ended (code {proc.returncode})")


def run_link():
    cfg = config()
    if not cfg["hub"]:
        print("This machine is the hub. Set \"hub\" in ~/.chief/config.json on a node.", file=sys.stderr)
        return 2
    print(f"Linking {cfg['node']} to the hub {cfg['hub']}. Dashboard: http://127.0.0.1:{cfg['port']}/",
          flush=True)
    delay = 2
    while True:
        started = time.time()
        _link_once(cfg)
        delay = 2 if time.time() - started > 60 else min(delay * 2, 60)
        time.sleep(delay)
