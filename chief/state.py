"""State that the chief writes: departments, tab roles, config, and meeting notes.

org.json:
  {"projects": {"<department key (folder name)>": {"name": "...",
                                      "status": "active|semi-active|backlog|archived",
                                      "focus": "today|week",
                                      "goal": "...", "deadline": "YYYY-MM-DD",
                                      "next": "...", "notes": "...",
                                      "account": "<a key of accounts in config.json>"}},
   "roles": {"<surface id>": "coordinator|worker|ignore"}}

config.json:
  {"roots": ["~/Github"], "ideas": {"root": "~/ideas", "schedule": "daily 05:00", "max": 3}}
  The root of the ideas (ideas.py) is a root too, when the folder exists.

  Optional Claude Code accounts, for a machine with more than one login:
  {"accounts": {"personal": {"command": "claude-home", "configDir": "~/.claude-home"},
                "work": {"command": "claude"}},
   "defaultAccount": "personal"}
  "command" starts Claude Code with the login of the account. "configDir" is the
  CLAUDE_CONFIG_DIR of that login (default ~/.claude). A department uses the account
  in its "account" field, else the default account. With no accounts, agents run `claude`.

meetings/<YYYY-MM-DD>.md: notes that the chief adds during each meeting.
"""

import json
import os
import time
from pathlib import Path

from . import paths

STATUSES = ["active", "semi-active", "backlog", "archived"]
FOCUS = ["today", "week"]
FOCUS_LIMITS = {"today": 2, "week": 5}
DEPT_FIELDS = ["name", "status", "focus", "goal", "deadline", "next", "notes", "account"]


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
    if any("/" in k for k in org["projects"]):
        org["projects"] = merge_projects({}, org["projects"])
        save_org(org)
    return org


def merge_projects(base, extra):
    """Add departments from `extra` to `base`. Keys become folder names (old files used paths).

    When both have a field, the newer status decision wins for status, and `base` wins for the rest.
    """
    out = {}
    for src in (base, extra):
        for k, meta in src.items():
            key = os.path.basename(k.rstrip("/")) or k
            cur = out.setdefault(key, {})
            for field, value in meta.items():
                if field not in cur:
                    cur[field] = value
            if (meta.get("statusSetAt") or 0) > (cur.get("statusSetAt") or 0):
                cur["status"], cur["statusSetAt"] = meta.get("status"), meta["statusSetAt"]
    return out


def save_org(org):
    _write(paths.ORG_FILE, org)


def update_dept(key, **fields):
    """Set fields of one department. An empty string clears a field."""
    org = load_org()
    meta = org["projects"].setdefault(key, {})
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


IDEAS_ROOT = "~/ideas"


def load_config():
    cfg = _read(paths.CONFIG_FILE, {})
    cfg.setdefault("roots", [str(paths.HOME / "Github")])
    cfg["roots"] = [os.path.expanduser(r) for r in cfg["roots"]]
    ideas = cfg.get("ideas") if isinstance(cfg.get("ideas"), dict) else {}
    cfg["ideasRoot"] = os.path.abspath(os.path.expanduser(ideas.get("root") or IDEAS_ROOT))
    if os.path.isdir(cfg["ideasRoot"]) and cfg["ideasRoot"] not in cfg["roots"]:
        cfg["roots"].append(cfg["ideasRoot"])  # Each new project from an idea is a department.
    return cfg


def accounts():
    """{name: {"command", "configDir"}} from config.json. configDir is None for the default login."""
    raw = _read(paths.CONFIG_FILE, {}).get("accounts")
    out = {}
    for name, acc in (raw.items() if isinstance(raw, dict) else ()):
        if isinstance(acc, dict) and acc.get("command"):
            out[name] = {"command": acc["command"], "configDir": acc.get("configDir") or None}
    return out


def default_account():
    """The name of the default account, or None when config.json has no accounts."""
    accs = accounts()
    name = _read(paths.CONFIG_FILE, {}).get("defaultAccount")
    return name if name in accs else next(iter(accs), None)


def claude_account(name=None):
    """The account with this name, else the default account. Plain `claude` when there are no accounts."""
    accs = accounts()
    acc = accs.get(name) or accs.get(default_account())
    return acc or {"command": "claude", "configDir": None}


def account_dirs():
    """The config folders of the accounts, other than the default ~/.claude."""
    default = os.path.realpath(paths.HOME / ".claude")
    out = []
    for acc in accounts().values():
        if acc["configDir"]:
            d = os.path.expanduser(acc["configDir"])
            if os.path.realpath(d) != default and d not in out:
                out.append(d)
    return [Path(d) for d in out]


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
