"""Incremental index of token usage in Claude Code and Codex session logs.

Both agents append JSON lines to one log file for each session. The index keeps a
byte offset for each file, so a refresh reads only the new lines. Usage goes into
hourly buckets for each model, so every time range uses the same cache.

Usage vectors are always: [input, output, cache_write_5m, cache_write_1h, cache_read].
"""

import json
import time
from datetime import datetime

from .. import paths

CACHE_VERSION = 2
HORIZON_SECONDS = 31 * 86400
TEXT_LIMIT = 400


def _epoch(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def clip(text, limit=TEXT_LIMIT):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _add(entry, ts, model, vec):
    hour = str(int(ts // 3600 * 3600))
    bucket = entry["hours"].setdefault(hour, {})
    cur = bucket.get(model)
    bucket[model] = vec[:] if cur is None else [a + b for a, b in zip(cur, vec)]


def _touch(entry, ts):
    if entry.get("firstTs") is None or ts < entry["firstTs"]:
        entry["firstTs"] = ts
    if entry.get("lastTs") is None or ts > entry["lastTs"]:
        entry["lastTs"] = ts


# Claude Code ---------------------------------------------------------------


def _claude_vec(usage):
    cc = usage.get("cache_creation") or {}
    if cc:
        w5 = cc.get("ephemeral_5m_input_tokens") or 0
        w1h = cc.get("ephemeral_1h_input_tokens") or 0
    else:
        w5, w1h = usage.get("cache_creation_input_tokens") or 0, 0
    return [
        usage.get("input_tokens") or 0,
        usage.get("output_tokens") or 0,
        w5,
        w1h,
        usage.get("cache_read_input_tokens") or 0,
    ]


def _user_text(content):
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = next(
            (b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"),
            "",
        )
    else:
        return None
    text = text.strip()
    if not text or text.startswith("<") or text.startswith("[Request interrupted"):
        return None
    return text


def _parse_claude(entry, lines):
    for raw in lines:
        is_assistant = b'"type":"assistant"' in raw
        is_user = b'"type":"user"' in raw and b'"tool_use_id"' not in raw
        if not (is_assistant or is_user) and entry.get("cwd"):
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not entry.get("cwd") and rec.get("cwd"):
            entry["cwd"] = rec["cwd"]
        ts = _epoch(rec.get("timestamp"))
        if ts is None:
            continue
        msg = rec.get("message") or {}
        if rec.get("type") == "assistant":
            _touch(entry, ts)
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip():
                    entry["lastAssistant"] = [ts, clip(block["text"])]
            usage, model = msg.get("usage"), msg.get("model")
            if not usage or not model or model.startswith("<"):
                continue
            vec = _claude_vec(usage)
            mid = msg.get("id") or rec.get("requestId")
            # One API message spans several lines with the same id. Count it once,
            # and add only the growth when a later line reports larger numbers.
            if mid and mid == entry.get("lastMid"):
                prev = entry["lastUsage"]
                new = [max(a, b) for a, b in zip(prev, vec)]
                delta = [n - p for n, p in zip(new, prev)]
                entry["lastUsage"] = new
            else:
                delta = vec
                entry["lastMid"], entry["lastUsage"] = mid, vec
            if any(delta):
                _add(entry, ts, model, delta)
        elif rec.get("type") == "user" and not rec.get("isMeta"):
            text = _user_text(msg.get("content"))
            if text:
                _touch(entry, ts)
                entry["lastUser"] = [ts, clip(text)]


def _claude_files():
    root = paths.CLAUDE_PROJECTS
    if not root.exists():
        return []
    return list(root.glob("*/*.jsonl")) + list(root.glob("*/*/subagents/*.jsonl"))


def _claude_init(path):
    entry = {"kind": "claude", "hours": {}, "sessionId": path.stem}
    if path.parent.name == "subagents":
        entry["parentSessionId"] = path.parent.parent.name
        try:
            meta = json.loads(path.with_name(path.stem + ".meta.json").read_text())
            entry["description"] = meta.get("description")
            entry["agentType"] = meta.get("agentType")
        except (OSError, ValueError):
            pass
    return entry


# Codex ---------------------------------------------------------------------


def _codex_vec(total):
    cached = total.get("cached_input_tokens") or 0
    return [
        max(0, (total.get("input_tokens") or 0) - cached),
        total.get("output_tokens") or 0,
        total.get("cache_write_input_tokens") or 0,
        0,
        cached,
    ]


_CODEX_MARKERS = (b'"token_count"', b'"session_meta"', b'"turn_context"', b'"user_message"', b'"agent_message"')


def _parse_codex(entry, lines):
    for raw in lines:
        if not any(m in raw for m in _CODEX_MARKERS):
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        payload = rec.get("payload") or {}
        ts = _epoch(rec.get("timestamp"))
        kind = rec.get("type")
        if kind == "session_meta":
            entry["sessionId"] = payload.get("id") or entry.get("sessionId")
            entry["cwd"] = payload.get("cwd") or entry.get("cwd")
        elif kind == "turn_context":
            entry["model"] = payload.get("model") or entry.get("model")
        elif kind == "event_msg" and ts is not None:
            sub = payload.get("type")
            if sub == "token_count":
                total = (payload.get("info") or {}).get("total_token_usage")
                if not total:
                    continue
                vec = _codex_vec(total)
                prev = entry.get("lastUsage") or [0] * 5
                delta = [max(0, a - b) for a, b in zip(vec, prev)]
                entry["lastUsage"] = vec
                _touch(entry, ts)
                if any(delta):
                    _add(entry, ts, entry.get("model") or "codex", delta)
            elif sub == "user_message" and payload.get("message"):
                _touch(entry, ts)
                entry["lastUser"] = [ts, clip(payload["message"])]
            elif sub == "agent_message" and payload.get("message"):
                _touch(entry, ts)
                entry["lastAssistant"] = [ts, clip(payload["message"])]


def _codex_files():
    root = paths.CODEX_SESSIONS
    return list(root.glob("*/*/*/*.jsonl")) if root.exists() else []


def _codex_init(path):
    return {"kind": "codex", "hours": {}, "sessionId": path.stem}


# Index ---------------------------------------------------------------------

_SOURCES = {
    "claude": (_claude_files, _claude_init, _parse_claude),
    "codex": (_codex_files, _codex_init, _parse_codex),
}


class LogIndex:
    def __init__(self):
        paths.ensure_home()
        self.cache_path = paths.CACHE_DIR / "log-index.json"
        self.files = {}
        try:
            data = json.loads(self.cache_path.read_text())
            if data.get("version") == CACHE_VERSION:
                self.files = data.get("files", {})
        except (OSError, ValueError):
            pass

    def refresh(self):
        horizon = time.time() - HORIZON_SECONDS
        seen = set()
        changed = False
        for files_fn, init_fn, parse_fn in _SOURCES.values():
            for path in files_fn():
                key = str(path)
                try:
                    st = path.stat()
                except OSError:
                    continue
                if st.st_mtime < horizon:
                    continue
                seen.add(key)
                entry = self.files.get(key)
                if entry and entry.get("size") == st.st_size:
                    continue
                if entry is None or st.st_size < entry.get("offset", 0):
                    entry = init_fn(path)
                lines = self._read_new(path, entry, st.st_size)
                parse_fn(entry, lines)
                entry["size"], entry["mtime"] = st.st_size, st.st_mtime
                self.files[key] = entry
                changed = True
        for key in list(self.files):
            if key not in seen:
                del self.files[key]
                changed = True
        if changed:
            self._prune(horizon)
            self._save()
        return self.files

    @staticmethod
    def _read_new(path, entry, size):
        start = entry.get("offset", 0)
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(size - start)
        end = data.rfind(b"\n")
        if end < 0:
            return []
        entry["offset"] = start + end + 1
        return [line for line in data[:end].split(b"\n") if line]

    def _prune(self, horizon):
        cutoff = int(horizon // 3600 * 3600)
        for entry in self.files.values():
            hours = entry.get("hours", {})
            for h in [h for h in hours if int(h) < cutoff]:
                del hours[h]

    def _save(self):
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": CACHE_VERSION, "files": self.files}))
        tmp.replace(self.cache_path)


def usage_since(entry, since):
    """Return {model: usage vector} for the hours at or after `since`."""
    out = {}
    floor = since // 3600 * 3600
    for hour, models in entry.get("hours", {}).items():
        if int(hour) < floor:
            continue
        for model, vec in models.items():
            cur = out.get(model)
            out[model] = vec[:] if cur is None else [a + b for a, b in zip(cur, vec)]
    return out


def usage_by_hour(entry, since):
    floor = since // 3600 * 3600
    return {int(h): m for h, m in entry.get("hours", {}).items() if int(h) >= floor}
