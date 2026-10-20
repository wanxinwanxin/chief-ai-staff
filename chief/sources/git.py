"""Output signals from git: your commits, changed lines, and uncommitted files.

Only commits by the git identity of each repository count (`git config user.email`).
Agents commit with that identity, and shared repositories also hold commits of other people.

When a project folder is not a git repository, the collector reads the git
repositories one level below it. Worktrees of one repository share a single log scan.
"""

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

_cache = {}
_TTL = 60


def _git(cwd, *args, timeout=10):
    try:
        res = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout if res.returncode == 0 else None


def _worktrees(cwd):
    if _git(cwd, "rev-parse", "--is-inside-work-tree") is not None:
        return [cwd]
    try:
        names = sorted(os.listdir(cwd))
    except OSError:
        return []
    return [os.path.join(cwd, n) for n in names if os.path.exists(os.path.join(cwd, n, ".git"))]


def activity(cwd, since):
    if not cwd:
        return None
    key = (cwd, int(since // 300))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    result = _collect(cwd, since)
    _cache[key] = (time.time(), result)
    return result


def _common_dir(path):
    out = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir", timeout=5)
    return out.strip() if out else None


def _uncommitted(path):
    out = _git(path, "status", "--porcelain", timeout=5) or ""
    return len([ln for ln in out.splitlines() if ln.strip()])


def _collect(cwd, since):
    trees = _worktrees(cwd)
    if not trees:
        return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        commons = list(pool.map(_common_dir, trees))
        uncommitted = sum(pool.map(_uncommitted, trees))
    repos = {}
    for tree, common in zip(trees, commons):
        repos.setdefault(common or tree, tree)

    seen = set()
    added = removed = 0
    files = set()
    last = None
    for common, tree in repos.items():
        email = (_git(tree, "config", "user.email") or "").strip()
        args = ["log", "--all", "--no-merges", f"--since=@{int(since)}",
                "--numstat", "--format=%x1e%H%x09%at%x09%s"]
        if email:
            args.append(f"--author={email}")
        log = _git(tree, *args, timeout=20) or ""
        for chunk in log.split("\x1e"):
            lines = chunk.strip("\n").split("\n")
            head = lines[0].split("\t", 2) if lines and lines[0] else []
            if len(head) < 3 or head[0] in seen:
                continue
            seen.add(head[0])
            at = int(head[1])
            if last is None or at > last["at"]:
                last = {"at": at, "subject": head[2][:120]}
            for row in lines[1:]:
                parts = row.split("\t")
                if len(parts) == 3:
                    added += int(parts[0]) if parts[0].isdigit() else 0
                    removed += int(parts[1]) if parts[1].isdigit() else 0
                    files.add((common, parts[2]))
    return {
        "commits": len(seen),
        "linesAdded": added,
        "linesRemoved": removed,
        "filesChanged": len(files),
        "lastCommit": last,
        "uncommitted": uncommitted,
        "repos": len(repos),
        "worktrees": len(trees),
    }
