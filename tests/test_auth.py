"""The access check of the dashboard server (chief/auth.py and Handler._allowed).

The tests run the server handler on a free port of 127.0.0.1. They use a temporary
token file and a fixed config, so that they do not read or change ~/.chief.
"""

import http.client
import json
import pathlib
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from chief import auth, nodes, paths, server

OWNER = "owner@example.com"
TAILNET = "hub.example.ts.net"
PROXY = "chief.example.com"


def fake_config(**extra):
    cfg = {"node": "hub", "label": "Hub", "hub": None, "allowedHosts": [PROXY], "owners": []}
    cfg.update(extra)
    return lambda: cfg


class AccessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token_file = pathlib.Path(self.tmp.name) / "token"
        self.addCleanup(self.tmp.cleanup)
        for patch in (mock.patch.object(paths, "TOKEN_FILE", self.token_file),
                      mock.patch.object(paths, "CHIEF_HOME", pathlib.Path(self.tmp.name)),
                      mock.patch.object(paths, "CACHE_DIR", pathlib.Path(self.tmp.name) / "cache"),
                      mock.patch.object(nodes, "config", fake_config(owners=[OWNER])),
                      mock.patch.object(auth, "tailscale_json", lambda *a: None),
                      mock.patch.object(server, "print", lambda *a, **k: None, create=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def request(self, path="/api/health", host=TAILNET, method="GET", **headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        headers = {"Host": host, **headers}
        body = b"{}" if method == "POST" else None
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res, data

    def status(self, *args, **kwargs):
        return self.request(*args, **kwargs)[0].status

    # Host names

    def test_local_hosts_need_no_login(self):
        self.assertEqual(self.status(host="127.0.0.1:7377"), 200)
        self.assertEqual(self.status(host="localhost:7377"), 200)

    def test_other_host_is_refused(self):
        self.assertEqual(self.status(host="evil.example.com", **{auth.LOGIN_HEADER: OWNER}), 403)

    # Tailscale login

    def test_tailnet_owner_login(self):
        self.assertEqual(self.status(**{auth.LOGIN_HEADER: OWNER}), 200)
        self.assertEqual(self.status(**{auth.LOGIN_HEADER: "Owner@Example.com"}), 200)

    def test_tailnet_other_login_is_refused(self):
        res, data = self.request(**{auth.LOGIN_HEADER: "other@example.com"})
        self.assertEqual(res.status, 403)
        self.assertIn("other@example.com", json.loads(data)["error"])

    def test_tailnet_without_login_is_refused(self):
        self.assertEqual(self.status(), 403)

    def test_login_header_counts_only_on_tailnet_names(self):
        # Another proxy can pass a header from the client, so only `tailscale serve` is believed.
        self.assertEqual(self.status(host=PROXY, **{auth.LOGIN_HEADER: OWNER}), 403)

    def test_owner_is_the_login_of_this_machine(self):
        status = {"Self": {"UserID": 7}, "User": {"7": {"LoginName": "Me@Example.com"}}}
        with mock.patch.object(nodes, "config", fake_config()), \
                mock.patch.object(auth, "tailscale_json", lambda *a: status), \
                mock.patch.dict(auth._owner, at=0.0, login=None):
            self.assertEqual(self.status(**{auth.LOGIN_HEADER: "me@example.com"}), 200)
            self.assertEqual(self.status(**{auth.LOGIN_HEADER: OWNER}), 403)

    def test_tagged_machine_has_no_owner(self):
        status = {"Self": {"UserID": 7, "Tags": ["tag:server"]}, "User": {"7": {"LoginName": "tagged-devices"}}}
        with mock.patch.object(nodes, "config", fake_config()), \
                mock.patch.object(auth, "tailscale_json", lambda *a: status), \
                mock.patch.dict(auth._owner, at=0.0, login=None):
            res, data = self.request(**{auth.LOGIN_HEADER: OWNER})
            self.assertEqual(res.status, 403)
            self.assertIn('"owners"', json.loads(data)["error"])

    def test_encoded_login(self):
        with mock.patch.object(nodes, "config", fake_config(owners=["zoë@example.com"])):
            self.assertEqual(self.status(**{auth.LOGIN_HEADER: "=?utf-8?q?zo=C3=AB@example.com?="}), 200)

    # Token

    def test_token_off_by_default(self):
        self.assertEqual(self.status(host=PROXY, Authorization="Bearer anything"), 403)
        self.assertFalse(self.token_file.exists())

    def test_token_in_header_or_cookie(self):
        value = auth.new_token()
        self.assertEqual(self.token_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.status(host=PROXY, Authorization=f"Bearer {value}"), 200)
        self.assertEqual(self.status(host=PROXY, Cookie=f"other=1; {auth.COOKIE}={value}"), 200)
        self.assertEqual(self.status(host=TAILNET, Cookie=f"{auth.COOKIE}={value}"), 200)
        self.assertEqual(self.status(host=PROXY, Cookie=f"{auth.COOKIE}=wrong"), 403)

    def test_token_in_address_sets_the_cookie(self):
        value = auth.new_token()
        res, _ = self.request(f"/?range=24h&token={value}", host=PROXY, **{"X-Forwarded-Proto": "https"})
        self.assertEqual(res.status, 303)
        self.assertEqual(res.getheader("Location"), "/?range=24h")
        cookie = res.getheader("Set-Cookie")
        self.assertIn(f"{auth.COOKIE}={value};", cookie)
        for part in ("HttpOnly", "SameSite=Lax", "Secure"):
            self.assertIn(part, cookie)

    def test_wrong_token_in_address(self):
        auth.new_token()
        res, data = self.request("/?token=wrong", host=PROXY)
        self.assertEqual(res.status, 403)
        self.assertIn(b"No access", data)
        self.assertEqual(res.getheader("Set-Cookie"), None)

    def test_new_token_replaces_the_old_one(self):
        old = auth.new_token()
        auth.new_token()
        self.assertEqual(self.status(host=PROXY, Authorization=f"Bearer {old}"), 403)

    # Refusal formats and POST

    def test_page_refusal_is_html(self):
        res, data = self.request("/", **{auth.LOGIN_HEADER: "<b>x</b>@example.com"})
        self.assertEqual(res.status, 403)
        self.assertTrue(res.getheader("Content-Type").startswith("text/html"))
        self.assertIn(b"&lt;b&gt;", data)
        self.assertNotIn(b"<b>x", data)

    def test_post_needs_the_owner_and_the_custom_header(self):
        owner = {auth.LOGIN_HEADER: OWNER}
        self.assertEqual(self.status("/api/none", method="POST", **{"X-Chief": "1"}), 403)
        self.assertEqual(self.status("/api/none", method="POST", **owner), 403)
        self.assertEqual(self.status("/api/none", method="POST", **owner, **{"X-Chief": "1"}), 404)


if __name__ == "__main__":
    unittest.main()
