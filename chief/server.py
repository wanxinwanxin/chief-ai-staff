"""Local dashboard server. It listens on 127.0.0.1 only."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import brief, paths, snapshot
from .sources import cmux

DEFAULT_PORT = 7377
CACHE_SECONDS = 8

_lock = threading.Lock()
_cache = {}

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def get_snapshot(range_key, fresh=False):
    with _lock:
        hit = _cache.get(range_key)
        if hit and not fresh and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        snap = snapshot.build(range_key)
        snap["signals"] = brief.signals(snap)
        _cache[range_key] = (time.time(), snap)
        return snap


class Handler(BaseHTTPRequestHandler):
    server_version = "chief/0.1"

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        # Refuse other host names. This blocks DNS rebinding from web pages.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "host not allowed"})
        url = urlparse(self.path)
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
                return self._send(200, get_snapshot(rng, fresh="fresh" in query))
            except Exception as exc:  # The dashboard shows the error and keeps the last data.
                return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        # A custom header forces a CORS preflight, which this server never accepts.
        # Other web pages therefore cannot send these requests.
        if not self._host_ok() or self.headers.get("X-Chief") != "1":
            return self._send(403, {"error": "forbidden"})
        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        if url.path == "/api/jump":
            ok, out = cmux.jump(body.get("workspaceId"), body.get("surfaceId"))
            return self._send(200 if ok else 502, {"ok": ok, "output": out})
        return self._send(404, {"error": "not found"})


def serve(port=DEFAULT_PORT):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Chief dashboard: http://127.0.0.1:{port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
