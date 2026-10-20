"""File locations that the collectors read and the chief writes."""

import os
from pathlib import Path

HOME = Path.home()

# State that the chief owns: org overrides, focus plan, caches, server pid.
CHIEF_HOME = Path(os.environ.get("CHIEF_HOME", HOME / ".chief"))
CACHE_DIR = CHIEF_HOME / "cache"
ORG_FILE = CHIEF_HOME / "org.json"
CONFIG_FILE = CHIEF_HOME / "config.json"
MEETINGS_DIR = CHIEF_HOME / "meetings"
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
SKILL_DIR = REPO_ROOT / "skills" / "chief"

# Where `chief install` puts the command and the skill.
BIN_LINK = HOME / ".local" / "bin" / "chief"
AGENTS_SKILLS = HOME / ".agents" / "skills"  # Agent Skills standard location
HARNESS_SKILLS = {"claude": HOME / ".claude" / "skills", "codex": HOME / ".codex" / "skills"}


def ensure_home():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
