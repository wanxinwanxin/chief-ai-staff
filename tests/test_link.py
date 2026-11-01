"""The hub side of the link (chief/nodes.py): the outbox pump and one link for each node.

The tests use a temporary outbox, so that they do not read or change ~/.chief.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from chief import nodes, paths


class LinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(paths, "OUTBOX_DIR", pathlib.Path(self.tmp.name) / "outbox")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.box = nodes._box("mac")
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)

    def age(self, path, seconds):
        when = time.time() - seconds
        os.utime(path, (when, when))

    def result(self, aid):
        return json.loads((self.box / "done" / f"{aid}.json").read_text())

    def sleeper(self, *args):
        """A process with these arguments, as `ps` shows them."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *args])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        time.sleep(0.2)
        return proc

    # The pump ------------------------------------------------------------

    def test_pump_sends_a_waiting_action(self):
        aid = nodes.enqueue("mac", {"action": "message", "text": "hi"})
        self.age(self.box / f"{aid}.json", 300)
        self.assertTrue(nodes._pump(self.box, self.send, live=True))
        self.assertEqual([(m["type"], m["id"], m["text"]) for m in self.sent], [("action", aid, "hi")])
        sent = self.box / f"{aid}.sent"
        self.assertTrue(sent.exists())
        self.assertLess(time.time() - sent.stat().st_mtime, 5)  # The mtime is the time of the send.

    def test_pump_keeps_the_actions_while_the_node_is_silent(self):
        aid = nodes.enqueue("mac", {"action": "message", "text": "hi"})
        self.assertTrue(nodes._pump(self.box, self.send, live=False))
        self.assertEqual(self.sent, [])
        self.assertTrue((self.box / f"{aid}.json").exists())

    def test_pump_skips_an_action_that_another_process_took(self):
        aid = nodes.enqueue("mac", {"action": "message", "text": "hi"})
        with mock.patch.object(nodes.os, "utime", side_effect=FileNotFoundError):
            self.assertTrue(nodes._pump(self.box, self.send, live=True))
        self.assertEqual(self.sent, [])
        self.assertTrue((self.box / f"{aid}.json").exists())

    def test_pump_puts_the_action_back_when_the_send_fails(self):
        aid = nodes.enqueue("mac", {"action": "message", "text": "hi"})

        def broken(msg):
            raise BrokenPipeError

        self.assertFalse(nodes._pump(self.box, broken, live=True))
        self.assertTrue((self.box / f"{aid}.json").exists())
        self.assertFalse((self.box / f"{aid}.sent").exists())

    def test_pump_closes_an_expired_action(self):
        aid = nodes.enqueue("mac", {"action": "notify", "ttl": 120})
        data = json.loads((self.box / f"{aid}.json").read_text())
        (self.box / f"{aid}.json").write_text(json.dumps(dict(data, at=time.time() - 200)))
        nodes._pump(self.box, self.send, live=True)
        self.assertEqual(self.sent, [])
        self.assertIn("Expired", self.result(aid)["output"])

    def test_pump_closes_a_sent_action_with_no_ack_after_its_ttl(self):
        old = nodes.enqueue("mac", {"action": "notify", "ttl": 120})
        new = nodes.enqueue("mac", {"action": "notify", "ttl": 120})
        nodes._pump(self.box, self.send, live=True)
        data = json.loads((self.box / f"{old}.sent").read_text())
        (self.box / f"{old}.sent").write_text(json.dumps(dict(data, at=time.time() - 200)))
        self.age(self.box / f"{old}.sent", nodes.ACK_SECONDS + 10)
        nodes._pump(self.box, self.send, live=True)
        self.assertEqual(self.result(old), dict(self.result(old), ok=False, output=nodes.NO_ACK))
        self.assertFalse((self.box / f"{old}.sent").exists())
        self.assertTrue((self.box / f"{new}.sent").exists())
        self.assertEqual(len(self.sent), 2)  # No second send.

    # One link for each node ------------------------------------------------

    def test_take_over_resends_only_recent_sends(self):
        recent = nodes.enqueue("mac", {"action": "message", "text": "now"})
        old = nodes.enqueue("mac", {"action": "message", "text": "before"})
        nodes._pump(self.box, self.send, live=True)
        self.age(self.box / f"{old}.sent", nodes.ACK_SECONDS + 10)
        nodes._take_over(self.box, "mac")
        self.assertTrue((self.box / f"{recent}.json").exists())
        self.assertFalse((self.box / f"{old}.sent").exists())
        self.assertEqual(self.result(old)["output"], nodes.NO_ACK)
        self.assertEqual((self.box / nodes.LINK_FILE).read_text(), str(os.getpid()))
        self.assertTrue(nodes._owns(self.box))

    def test_take_over_stops_the_old_link_of_the_node(self):
        old = self.sleeper("node", "serve-link", "mac")
        self.assertTrue(nodes._is_link(old.pid, "mac"))
        self.box.mkdir(parents=True)
        (self.box / nodes.LINK_FILE).write_text(str(old.pid))
        nodes._take_over(self.box, "mac")
        self.assertIsNotNone(old.wait(timeout=5))
        self.assertTrue(nodes._owns(self.box))

    def test_take_over_does_not_stop_another_process(self):
        other = self.sleeper("node", "serve-link", "cloud")
        self.assertFalse(nodes._is_link(other.pid, "mac"))
        self.box.mkdir(parents=True)
        (self.box / nodes.LINK_FILE).write_text(str(other.pid))
        nodes._take_over(self.box, "mac")
        self.assertIsNone(other.poll())

    def test_a_replaced_link_does_not_own_the_outbox(self):
        self.box.mkdir(parents=True)
        nodes._take_over(self.box, "mac")
        (self.box / nodes.LINK_FILE).write_text(str(os.getpid() + 1))
        self.assertFalse(nodes._owns(self.box))


if __name__ == "__main__":
    unittest.main()
