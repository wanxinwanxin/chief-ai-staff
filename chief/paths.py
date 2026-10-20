"""File locations that the collectors read and the chief writes."""

import os
from pathlib import Path

HOME = Path.home()

# State that the chief owns: org overrides, focus plan, caches, server pid.
CHIEF_HOME = Path(os.environ.get("CHIEF_HOME", HOME / ".chief"))
CACHE_DIR = CHIEF_HOME / "cache"
ORG_FILE = CHIEF_HOME / "org.json"
FOCUS_FILE = CHIEF_HOME / "focus.json"
PRICING_FILE = CHIEF_HOME / "pricing.json"
SERVER_PID = CHIEF_HOME / "server.pid"
SERVER_LOG = CHIEF_HOME / "server.log"

# Agent logs that the collectors read.
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_SESSIONS = HOME / ".codex" / "sessions"

# cmux hook state (agent session to tab mapping).
CMUX_STATE = HOME / ".cmuxterm"

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
AGENT_DIR = REPO_ROOT / "agent"


def ensure_home():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
