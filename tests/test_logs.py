"""The full last reply of an agent (chief/sources/logs.py), read from the end of its log.

The tests write small logs to a temporary folder.
"""

import json
import pathlib
import tempfile
import unittest

from chief.sources import logs


def claude(kind, ts, *texts, tool=False):
    content = [{"type": "text", "text": t} for t in texts]
    if tool:
        content.append({"type": "tool_use", "id": "x", "name": "Bash", "input": {}})
    return {"type": kind, "timestamp": ts, "message": {"role": kind, "content": content}}


def line(record):
    return json.dumps(record, separators=(",", ":")).encode() + b"\n"  # Compact, as the agents write it.


def codex(ts, message):
    return {"type": "event_msg", "timestamp": ts, "payload": {"type": "agent_message", "message": message}}


class LastReplyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.n = 0
        logs._replies.clear()

    def tearDown(self):
        self.dir.cleanup()

    def write(self, *records):
        self.n += 1
        path = pathlib.Path(self.dir.name) / f"{self.n}.jsonl"
        path.write_bytes(b"".join(map(line, records)))
        return str(path)

    def test_claude_keeps_the_line_breaks(self):
        path = self.write(
            claude("user", "2026-10-28T09:37:00Z", "Brief me"),
            claude("assistant", "2026-10-28T09:37:10Z", "I read the agenda.", tool=True),
            claude("assistant", "2026-10-28T09:39:00Z", "Six items need you.\n\n1. First\n2. Second\n"),
            {"type": "system", "timestamp": "2026-10-28T09:39:01Z"},
        )
        ts, text = logs.last_reply(path)
        self.assertEqual(text, "Six items need you.\n\n1. First\n2. Second")
        self.assertEqual(ts, logs._epoch("2026-10-28T09:39:00Z"))

    def test_the_last_text_block_of_a_message(self):
        path = self.write(claude("assistant", "2026-10-28T09:39:00Z", "First part.", "Last part."))
        self.assertEqual(logs.last_reply(path)[1], "Last part.")

    def test_codex(self):
        path = self.write(codex("2026-10-28T09:00:00Z", "Old."), codex("2026-10-28T09:05:00Z", "New answer."))
        self.assertEqual(logs.last_reply(path)[1], "New answer.")

    def test_no_reply(self):
        self.assertIsNone(logs.last_reply(self.write(claude("user", "2026-10-28T09:37:00Z", "Hello"))))
        self.assertIsNone(logs.last_reply(str(pathlib.Path(self.dir.name) / "missing.jsonl")))

    def test_long_reply_is_cut(self):
        path = self.write(claude("assistant", "2026-10-28T09:39:00Z", "x" * 50))
        self.assertEqual(logs.last_reply(path, limit=10)[1], "x" * 9 + "…")

    def test_reply_before_a_large_tail(self):
        # The reply is outside the first scan, so the second, larger scan finds it.
        big = claude("user", "2026-10-28T09:40:00Z", "y" * (logs.REPLY_SCANS[0] + 100))
        path = self.write(claude("assistant", "2026-10-28T09:39:00Z", "Found."), big)
        self.assertEqual(logs.last_reply(path)[1], "Found.")

    def test_a_cut_first_line_is_skipped(self):
        # A scan that starts inside a line must not parse that part of the line.
        filler = claude("assistant", "2026-10-28T09:00:00Z", "z" * (logs.REPLY_SCANS[-1] + 10))
        path = self.write(filler, claude("user", "2026-10-28T09:40:00Z", "Hi"))
        self.assertIsNone(logs.last_reply(path))

    def test_cache_follows_the_size(self):
        path = self.write(claude("assistant", "2026-10-28T09:39:00Z", "One."))
        self.assertEqual(logs.last_reply(path)[1], "One.")
        with open(path, "ab") as fh:
            fh.write(line(claude("assistant", "2026-10-28T09:41:00Z", "Two.")))
        self.assertEqual(logs.last_reply(path)[1], "Two.")


if __name__ == "__main__":
    unittest.main()
