"""Who may use the dashboard from another machine.

A request for 127.0.0.1 or localhost comes from this machine, or through the SSH link
of a node. It needs no check. A request for another host name comes through a proxy,
and the server accepts it only from the owner:

  - `tailscale serve` (a *.ts.net name) adds the Tailscale login of the device that
    sends the request (the Tailscale-User-Login header). It removes that header from
    the request that it gets, so a device cannot send a false login. The login must be
    in "owners" in config.json. When "owners" is empty, the owner is the Tailscale
    login of this machine. A tagged machine has no login, so set "owners" there.
  - Any proxy: the token from `chief token`, in the chief_token cookie or in the
    header "Authorization: Bearer <token>". Open the dashboard once with
    ?token=<token> at the end of the address, and the browser keeps the cookie.
"""

import email.errors
import email.header
import hmac
import http.cookies
import json
import os
import secrets
import shutil
import subprocess
import time

from . import nodes, paths

LOCAL_HOSTS = ("127.0.0.1", "localhost")
LOGIN_HEADER = "Tailscale-User-Login"
COOKIE = "chief_token"
COOKIE_SECONDS = 365 * 86400
TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
OWNER_SECONDS = 300  # How long the server keeps the Tailscale login of this machine.
RETRY_SECONDS = 30  # The wait before the next try, when Tailscale gave no login.

_owner = {"at": 0.0, "login": None}


def tailscale():
    """The Tailscale command, or None."""
    found = shutil.which("tailscale")
    if found:
        return found
    return TAILSCALE_APP if os.path.exists(TAILSCALE_APP) else None


def tailscale_json(*args):
    """The JSON output of a Tailscale command, for example ("status", "--json"), or None."""
    cmd = tailscale()
    if not cmd:
        return None
    try:
        res = subprocess.run([cmd, *args], capture_output=True, text=True, timeout=5)
        return json.loads(res.stdout) if res.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def machine_login(status):
    """The Tailscale login of this machine from `tailscale status --json`, or None.

    A tagged machine has no login.
    """
    me = (status or {}).get("Self") or {}
    if not me or me.get("Tags"):
        return None
    user = (status.get("User") or {}).get(str(me.get("UserID"))) or {}
    return (user.get("LoginName") or "").strip().lower() or None


def owners():
    """The Tailscale logins (lowercase) that may use the dashboard from another machine."""
    listed = nodes.config()["owners"]
    if listed:
        return listed
    now = time.time()
    if now - _owner["at"] > (OWNER_SECONDS if _owner["login"] else RETRY_SECONDS):
        _owner.update(at=now, login=machine_login(tailscale_json("status", "--json")))
    return [_owner["login"]] if _owner["login"] else []


def token():
    """The dashboard token, or None when token access is off."""
    try:
        return paths.TOKEN_FILE.read_text().strip() or None
    except OSError:
        return None


def new_token():
    """Write a new token that only this user can read. The old token stops working."""
    paths.ensure_home()
    value = secrets.token_urlsafe(32)
    fd = os.open(paths.TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value + "\n")
    os.chmod(paths.TOKEN_FILE, 0o600)  # Also when the file was there before.
    return value


def token_ok(value):
    expected = token()
    return bool(value and expected) and hmac.compare_digest(value.encode(), expected.encode())


def _sent_token(headers):
    """The token in a request: an "Authorization: Bearer" header, or the cookie."""
    given = headers.get("Authorization") or ""
    if given.lower().startswith("bearer "):
        return given[7:].strip()
    jar = http.cookies.SimpleCookie()
    try:
        jar.load(headers.get("Cookie") or "")
    except http.cookies.CookieError:
        return ""
    return jar[COOKIE].value if COOKIE in jar else ""


def _login(value):
    """The login in the header. Tailscale encodes a login that is not ASCII (RFC 2047)."""
    if value.startswith("=?"):
        try:
            value = str(email.header.make_header(email.header.decode_header(value)))
        except (email.errors.HeaderParseError, LookupError, UnicodeDecodeError):
            return ""
    return value.strip().lower()


def refusal(headers, tailnet):
    """None when a request from another machine may go on. Otherwise the reason, for the person.

    `tailnet` is True when the host name ends with .ts.net, that is, when the request
    came through `tailscale serve`. Only then is the login header true.
    """
    if token_ok(_sent_token(headers)):
        return None
    if tailnet:
        login = _login(headers.get(LOGIN_HEADER) or "")
        known = owners()
        if login and login in known:
            return None
        if not known:
            return ('The hub does not know its owner, because the hub has no Tailscale login '
                    '(it is a tagged device). Set "owners" in ~/.chief/config.json on the hub.')
        if login:
            return (f'This dashboard is open only to its owner. You use Tailscale as {login}. '
                    'The owner can add this login to "owners" in ~/.chief/config.json on the hub.')
        return ('This dashboard is open only to its owner. This request has no Tailscale login. '
                'Tagged devices and Tailscale Funnel do not send a login.')
    if token() is None:
        return ('This dashboard is open only to its owner. Run "chief token" on the hub, '
                'then open this page once with ?token=<token> at the end of the address.')
    return ('This dashboard is open only to its owner. Open this page once with ?token=<token> '
            'at the end of the address. Run "chief token" on the hub to see the token.')
