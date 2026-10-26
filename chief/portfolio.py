"""The portfolio: every project folder under the roots (default ~/Github).

Each folder is a department. Its status (active, semi-active, backlog, archived)
comes from org.json when the user set it. When the user did not set it, the
status is suggested from the last activity.

This module reads only git and the file system. The snapshot adds agent activity
from the session logs and the open tabs.
"""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

from . import paths, state
from .sources import git

CACHE_FILE = "portfolio.json"
CACHE_VERSION = 1
TTL = 600  # Git data for 120 folders takes a few seconds. Reuse it for 10 minutes.
DAYS = 30
# (days since the last activity, suggested status). Older than the last step: archived.
SUGGEST = [(7, "active"), (30, "semi-active"), (180, "backlog")]

_memo = {"at": 0, "data": None}


def suggest(last_activity, has_team, now=None):
    if has_team:
        return "active"
    if not last_activity:
        return "archived"
    age = ((now or time.time()) - last_activity) / 86400
    for days, status in SUGGEST:
        if age <= days:
            return status
    return "archived"


def folders():
    out = []
    for root in state.load_config()["roots"]:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            path = os.path.join(root, name)
            if name.startswith(".") or not os.path.isdir(path):
                continue
            out.append(path)
    return out


def _fs_mtime(folder):
    try:
        newest = os.stat(folder).st_mtime  # Changes when a file is added or removed.
    except OSError:
        newest = 0
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.name in (".git", ".DS_Store"):
                    continue
                try:
                    newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    pass
    except OSError:
        pass
    return newest or None


_BADGE = re.compile(r"^(\[!\[|!\[|<|\||```|---|===)")


def _description(folder):
    for name in ("README.md", "readme.md", "Readme.md", "README"):
        try:
            with open(os.path.join(folder, name), encoding="utf-8", errors="ignore") as fh:
                text = fh.read(6000)
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or _BADGE.match(line):
                continue
            line = re.sub(r"[*_`]", "", line)
            return line if len(line) <= 160 else line[:159] + "…"
    return None


def _git_scan(folder, since):
    trees = git._worktrees(folder)
    if not trees:
        return None
    if len(trees) > 1:
        repos = {}
        for tree in trees:
            repos.setdefault(git._common_dir(tree) or tree, tree)
        trees = list(repos.values())
    last, subject = None, None
    days = {}
    for tree in trees:
        head = (git._git(tree, "log", "-1", "--all", "--format=%ct%x09%s", timeout=10) or "").strip()
        if head and "\t" in head:
            at, subj = head.split("\t", 1)
            if at.isdigit() and (last is None or int(at) > last):
                last, subject = int(at), subj[:120]
        email = (git._git(tree, "config", "user.email") or "").strip()
        args = ["log", "--all", "--no-merges", f"--since=@{int(since)}", "--format=%ct"]
        if email:
            args.append(f"--author={email}")
        for row in (git._git(tree, *args, timeout=15) or "").split():
            if row.isdigit():
                d = time.strftime("%Y-%m-%d", time.localtime(int(row)))
                days[d] = days.get(d, 0) + 1
    return {"lastCommit": {"at": last, "subject": subject} if last else None,
            "commitDays": days, "repos": len(trees)}


def _scan_one(folder, since):
    return folder, {
        "git": _git_scan(folder, since),
        "mtime": _fs_mtime(folder),
        "description": _description(folder),
    }


def scan(force=False):
    """Return {folder: {git, mtime, description}} for every folder under the roots."""
    now = time.time()
    if not force and _memo["data"] is not None and now - _memo["at"] < TTL:
        return _memo["data"]
    cache_path = paths.CACHE_DIR / CACHE_FILE
    wanted = folders()
    if not force:
        try:
            cached = json.loads(cache_path.read_text())
            if (cached.get("version") == CACHE_VERSION and now - cached.get("at", 0) < TTL
                    and set(cached["folders"]) == set(wanted)):
                _memo.update(at=cached["at"], data=cached["folders"])
                return cached["folders"]
        except (OSError, ValueError, KeyError):
            pass
    since = now - DAYS * 86400
    with ThreadPoolExecutor(max_workers=8) as pool:
        data = dict(pool.map(lambda f: _scan_one(f, since), wanted))
    paths.ensure_home()
    tmp = cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": CACHE_VERSION, "at": now, "folders": data}))
    tmp.replace(cache_path)
    _memo.update(at=now, data=data)
    return data


def scanned_at():
    return _memo["at"]


def key(path):
    """The department key: the folder name. One project has the same key on every machine."""
    return os.path.basename((path or "").rstrip("/")) or path


def pretty(path):
    name = os.path.basename((path or "").rstrip("/")) or path or "Unknown"
    return name.replace("-", " ").replace("_", " ").strip().capitalize()


def match(path, folders_list):
    """Return the folder that contains `path` (the longest match), or None."""
    if not path:
        return None
    path = path.rstrip("/")
    best = None
    for f in folders_list:
        if path == f or path.startswith(f + "/"):
            if best is None or len(f) > len(best):
                best = f
    return best
