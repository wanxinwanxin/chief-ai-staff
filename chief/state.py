"""State that the chief agent writes: org overrides and the focus plan.

org.json:
  {"projects": {"<project folder>": {"priority": "high|normal|low|paused",
                                      "goal": "...", "notes": "..."}},
   "roles": {"<surface id>": "coordinator|worker|ignore"}}

focus.json:
  {"updatedAt": <epoch>, "note": "...",
   "items": [{"project": "<title>", "why": "...", "next": "..."}],
   "later": ["<title>", ...]}
"""

import json
import time

from . import paths


def _read(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write(path, data):
    paths.ensure_home()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def load_org():
    org = _read(paths.ORG_FILE, {})
    org.setdefault("projects", {})
    org.setdefault("roles", {})
    return org


def save_org(org):
    _write(paths.ORG_FILE, org)


def load_focus():
    return _read(paths.FOCUS_FILE, None)


def save_focus(focus):
    focus = dict(focus)
    focus["updatedAt"] = time.time()
    _write(paths.FOCUS_FILE, focus)
    return focus
