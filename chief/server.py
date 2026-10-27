"""Dashboard server. It listens on 127.0.0.1 only.

Other machines reach it through the SSH link of a node (port forward), or through
`tailscale serve` on the hub, which proxies HTTPS on the tailnet to this port.
A request through a proxy must come from the owner (see auth.py).
"""

import gzip
import hashlib
import html
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse

from . import auth, nodes, paths, snapshot, state

DEFAULT_PORT = 7377
CACHE_SECONDS = 4
TMUX_SECONDS = 2

_lock = threading.Lock()
_cache = {}

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}

REFUSED_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Chief of AI Staff</title>
<style>body{{margin:0;padding:48px 16px;font:16px/1.5 system-ui,-apple-system,sans-serif;color:#1f1f1f;background:#f9f9f7}}
main{{max-width:520px;margin:0 auto}}h1{{font-size:20px}}
@media (prefers-color-scheme:dark){{body{{color:#ededed;background:#141414}}}}</style></head>
<body><main><h1>No access</h1><p>{text}</p></main></body></html>
"""


def get_snapshot(range_key, fresh=False):
    """Return (snapshot, gzip body, etag). The etag ignores the build time."""
    with _lock:
        hit = _cache.get(range_key)
        if hit and not fresh and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        snap = snapshot.build(range_key)
        body = json.dumps(snap).encode()
        volatile = dict(snap, generatedAt=0, nodes=[dict(n, at=0) for n in snap.get("nodes") or []])
        stable = json.dumps(volatile).encode()
        entry = (snap, gzip.compress(body, 5), '"%s"' % hashlib.sha1(stable).hexdigest()[:16], body)
        _cache[range_key] = (time.time(), entry)
        return entry


def _known_department(key):
    if not isinstance(key, str) or not key:
        return False
    snap = get_snapshot("7d")[0]
    return any(d["key"] == key for d in snap["departments"]) or key in state.load_org()["projects"]


def _jump(body):
    snap = get_snapshot("7d")[0]
    team = next((t for t in snap["teams"] + ([snap["chief"]] if snap.get("chief") else [])
                 if t["workspaceId"] == body.get("workspaceId")), None)
    if not team:
        return False, "unknown team"
    target = team.get("mirror") if team["terminal"] == "tmux" else team
    if not target or (team["terminal"] != "cmux" and not team.get("mirror")):
        return False, "This team has no cmux tab to jump to."
    surface = body.get("surfaceId") if target is team else None
    action = {"action": "jump", "workspaceId": target["workspaceId"], "surfaceId": surface}
    node = target["node"]
    if node == nodes.config()["node"]:
        result = nodes.execute(action)
        return result["ok"], result["output"]
    nodes.enqueue(node, action)
    return True, f"Sent to {node}."


MESSAGE_WAIT = 8.0
MESSAGE_MAX = 20000


def _message(body):
    """Send a message from the dashboard to one agent, on any machine."""
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return False, "The message is empty."
    if len(text) > MESSAGE_MAX:
        return False, f"The message is longer than {MESSAGE_MAX} characters."
    snap = get_snapshot("7d")[0]
    teams = snap["teams"] + ([snap["chief"]] if snap.get("chief") else [])
    team = next((t for t in teams if t["key"] == body.get("teamKey")), None)
    if not team:
        return False, "unknown team"
    wanted = str(body.get("surfaceId") or "")
    agent = next((a for a in team["agents"] if str(a["surfaceId"]) == wanted), None) if wanted else None
    agent = agent or next((a for a in team["agents"] if a["role"] == "coordinator"), None)
    if not agent:
        return False, "This team has no agent to receive a message."
    action = {"action": "message", "text": text, "sender": body.get("sender") or "You",
              "surfaceRef": agent["surfaceRef"], "workspaceRef": team["workspaceRef"], "paneId": agent.get("paneId")}
    node = team["node"]
    if node == nodes.config()["node"]:
        result = nodes.execute(action)
        return result["ok"], result["output"]
    aid = nodes.enqueue(node, action)
    result = nodes.wait(node, aid, timeout=MESSAGE_WAIT)
    if result is None:
        return True, f"Queued for {team['nodeLabel']}. The agent gets it when the machine connects."
    return result["ok"], result["output"]


class Handler(BaseHTTPRequestHandler):
    server_version = "chief/0.1"

    def log_message(self, fmt, *args):
        pass

    def _allowed(self, url):
        """Return True when the request may go on. Otherwise answer it and return False."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        if host in auth.LOCAL_HOSTS:
            return True
        # Refuse other host names. This blocks DNS rebinding from web pages.
        # Tailscale names (*.ts.net) come through `tailscale serve` on the hub.
        tailnet = host.endswith(".ts.net")
        if not tailnet and host not in [h.lower() for h in nodes.config()["allowedHosts"]]:
            self._send(403, {"error": "host not allowed"})
            return False
        given = parse_qs(url.query).get("token", [None])[0] if self.command == "GET" else None
        if given is not None:
            if not auth.token_ok(given):
                self._refuse(url, 'The token is not correct. Run "chief token" on the hub to see it.')
                return False
            # Keep the token in a cookie, and remove it from the address.
            rest = urlencode([(k, v) for k, v in parse_qsl(url.query, keep_blank_values=True) if k != "token"])
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            cookie = (f"{auth.COOKIE}={auth.token()}; Path=/; Max-Age={auth.COOKIE_SECONDS}; "
                      f"HttpOnly; SameSite=Lax{secure}")
            self._send(303, b"", "text/plain", {"Location": url.path + (f"?{rest}" if rest else ""),
                                                "Set-Cookie": cookie})
            return False
        reason = auth.refusal(self.headers, tailnet)
        if reason:
            self._refuse(url, reason)
            return False
        return True

    def _refuse(self, url, reason):
        print(f"Refused {self.command} {url.path}: {reason}", flush=True)
        if self.command == "GET" and not url.path.startswith("/api/"):
            page = REFUSED_PAGE.format(text=html.escape(reason))
            return self._send(403, page.encode(), "text/html; charset=utf-8")
        return self._send(403, {"error": reason})

    def _send(self, code, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if not self._allowed(url):
            return None
        if url.path in STATIC:
            name, ctype = STATIC[url.path]
            try:
                return self._send(200, (paths.WEB_DIR / name).read_bytes(), ctype)
            except OSError:
                return self._send(404, {"error": "missing file"})
        if url.path == "/api/health":
            return self._send(200, {"ok": True})
        if url.path == "/api/snapshot":
            query = parse_qs(url.query)
            rng = query.get("range", ["7d"])[0]
            if rng not in snapshot.RANGES:
                rng = "7d"
            try:
                _, zipped, etag, body = get_snapshot(rng, fresh="fresh" in query)
            except Exception as exc:  # The dashboard shows the error and keeps the last data.
                return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return None
            if "gzip" in (self.headers.get("Accept-Encoding") or ""):
                return self._send(200, zipped, headers={"ETag": etag, "Content-Encoding": "gzip"})
            return self._send(200, body, headers={"ETag": etag})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if not self._allowed(url):
            return None
        # A custom header forces a CORS preflight, which this server never accepts.
        # Other web pages therefore cannot send these requests.
        if self.headers.get("X-Chief") != "1":
            return self._send(403, {"error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        if url.path == "/api/jump":
            ok, out = _jump(body)
            return self._send(200 if ok else 502, {"ok": ok, "output": out})
        if url.path == "/api/message":
            ok, out = _message(body)
            return self._send(200 if ok else 502, {"ok": ok, "output": out})
        if url.path == "/api/department":
            key = body.get("key")
            if not _known_department(key):
                return self._send(400, {"error": "unknown department"})
            fields = {}
            if body.get("status") in state.STATUSES:
                fields["status"] = body["status"]
            if body.get("focus") in state.FOCUS + [""]:
                fields["focus"] = body["focus"]
            if not fields:
                return self._send(400, {"error": "nothing to change"})
            meta = state.update_dept(key, **fields)
            with _lock:
                _cache.clear()
            return self._send(200, {"ok": True, "department": meta})
        return self._send(404, {"error": "not found"})


def _tmux_loop():
    """On the hub: keep one tmux session for each project (see tmux.group_sessions),
    and show the status of each agent on its cmux mirror row (see tmux.publish_status)."""
    while True:
        try:
            for move in nodes.group_local():
                print(f"Moved the tmux window {move}", flush=True)
        except Exception as exc:  # The dashboard keeps running.
            print(f"Grouping tmux sessions failed: {exc}", flush=True)
        try:
            nodes.publish_local()
        except Exception as exc:
            print(f"Publishing the agent status failed: {exc}", flush=True)
        time.sleep(TMUX_SECONDS)


def serve(port=DEFAULT_PORT):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Chief dashboard: http://127.0.0.1:{port}/", flush=True)
    # Build the first snapshot now, so that the first page load is fast.
    threading.Thread(target=get_snapshot, args=("7d",), daemon=True).start()
    if nodes.is_hub():
        threading.Thread(target=_tmux_loop, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
