"""State that the chief writes: departments, tab roles, config, and meeting notes.

org.json:
  {"projects": {"<project folder>": {"name": "...",
                                      "status": "active|semi-active|backlog|archived",
                                      "focus": "today|week",
                                      "goal": "...", "deadline": "YYYY-MM-DD",
                                      "next": "...", "notes": "..."}},
   "roles": {"<surface id>": "coordinator|worker|ignore"}}

config.json:
  {"roots": ["~/Github"]}

meetings/<YYYY-MM-DD>.md: notes that the chief adds during each meeting.
"""

import json
import os
import time

from . import paths

STATUSES = ["active", "semi-active", "backlog", "archived"]
FOCUS = ["today", "week"]
FOCUS_LIMITS = {"today": 2, "week": 5}
DEPT_FIELDS = ["name", "status", "focus", "goal", "deadline", "next", "notes"]


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


def update_dept(path, **fields):
    """Set fields of one department. An empty string clears a field."""
    org = load_org()
    meta = org["projects"].setdefault(path, {})
    for key, value in fields.items():
        if value is None:
            continue
        if value == "":
            meta.pop(key, None)
        else:
            meta[key] = value
        if key == "status":
            meta["statusSetAt"] = time.time()
    save_org(org)
    return meta


def load_config():
    cfg = _read(paths.CONFIG_FILE, {})
    cfg.setdefault("roots", [str(paths.HOME / "Github")])
    cfg["roots"] = [os.path.expanduser(r) for r in cfg["roots"]]
    return cfg


def notes_file(day=None):
    return paths.MEETINGS_DIR / f"{day or time.strftime('%Y-%m-%d')}.md"


def add_note(text):
    paths.MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    path = notes_file()
    with path.open("a") as fh:
        fh.write(f"- {time.strftime('%H:%M')} {text.strip()}\n")
    return path


def recent_notes(days=7):
    """Return [(day, text)] for the meeting notes of the last `days` days, newest first."""
    if not paths.MEETINGS_DIR.exists():
        return []
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    out = []
    for f in sorted(paths.MEETINGS_DIR.glob("*.md"), reverse=True):
        if f.stem >= cutoff:
            out.append((f.stem, f.read_text()))
    return out
