"""Scheduled tasks (chief/tasks.py) and their section in the agenda.

The tests use a temporary folder for the task files and the run state, and a fake
spawn and a fake tmux, so that they do not read or change ~/.chief or start agents.
"""

import json
import pathlib
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from chief import agenda, nodes, paths, tasks
from chief.sources import claude_sessions

TASK = """---
schedule: daily 06:00
departments: docs-site, shop
---
Read the feedback after {since} for {department}.
"""


def ts(text):
    return datetime.fromisoformat(text).timestamp()


class ScheduleTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(tasks.parse_schedule("daily 06:00"), (set(range(7)), 6, 0))
        self.assertEqual(tasks.parse_schedule("weekdays 9:30"), (set(range(5)), 9, 30))
        self.assertEqual(tasks.parse_schedule("mon,thu 22:15"), ({0, 3}, 22, 15))

    def test_bad_forms(self):
        for text in ("", "daily", "6am daily", "daily 25:00", "monday 06:00", "mon,xyz 06:00"):
            with self.assertRaises(ValueError, msg=text):
                tasks.parse_schedule(text)

    def test_slots(self):
        task = {"when": tasks.parse_schedule("mon,thu 06:00")}
        wed = ts("2026-10-28T12:00")  # A Wednesday.
        self.assertEqual(tasks.last_slot(task, wed), ts("2026-10-26T06:00"))
        self.assertEqual(tasks.next_slot(task, wed), ts("2026-10-29T06:00"))
        self.assertEqual(tasks.last_slot(task, ts("2026-10-29T06:00")), ts("2026-10-29T06:00"))

    def test_parse(self):
        task = tasks.parse(TASK, "feedback")
        self.assertIsNone(task["error"])
        self.assertEqual(task["departments"], ["docs-site", "shop"])
        self.assertEqual((task["agent"], task["permission"], task["enabled"]), ("claude", "auto", True))
        self.assertTrue(task["prompt"].startswith("Read the feedback"))

    def test_parse_problems(self):
        self.assertIn("no header", tasks.parse("Only a prompt.", "x")["error"])
        self.assertIn("departments", tasks.parse("---\nschedule: daily 06:00\n---\nDo it.", "x")["error"])
        self.assertIn("not a schedule", tasks.parse("---\nschedule: often\ndepartments: a\n---\nDo it.", "x")["error"])
        self.assertIn("no prompt", tasks.parse("---\nschedule: daily 06:00\ndepartments: a\n---\n", "x")["error"])
        self.assertFalse(tasks.parse("---\nschedule: daily 06:00\ndepartments: a\nenabled: no\n---\nGo.", "x")["enabled"])


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = pathlib.Path(self.tmp.name)
        (home / "tasks").mkdir()
        (home / "tasks" / "feedback.md").write_text(TASK)
        for name, value in (("CACHE_DIR", home / "cache"), ("TASKS_DIR", home / "tasks"),
                            ("TASK_RUNS", home / "task-runs.json")):
            patcher = mock.patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmux = []
        patcher = mock.patch.object(tasks.tmux, "run", self.fake_tmux)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.spawned = []
        self.spawn_ok = True

    def tearDown(self):
        self.tmp.cleanup()

    def fake_tmux(self, args):
        self.tmux.append(args)
        if args[0] == "show-options":
            return True, self.pane_value
        return True, ""

    def spawn(self, task, department, text, run_id):
        self.spawned.append((department, text, run_id))
        return self.spawn_ok, "Added a tab." if self.spawn_ok else "No folder."

    def tick(self, when):
        return tasks.tick(now=ts(when), spawn=self.spawn)

    def test_a_new_task_waits_for_its_next_time(self):
        self.assertEqual(self.tick("2026-10-28T12:00"), [])
        self.assertEqual(self.tick("2026-10-28T23:59"), [])
        self.assertEqual(len(self.tick("2026-10-29T06:00")), 2)
        self.assertEqual([d for d, _, _ in self.spawned], ["docs-site", "shop"])
        self.assertEqual(self.tick("2026-10-29T06:01"), [])

    def test_a_missed_time_runs_once(self):
        self.tick("2026-10-27T12:00")
        self.assertEqual(len(self.tick("2026-10-30T09:00")), 2)  # The hub was off for three runs.
        self.assertEqual(self.tick("2026-10-30T09:01"), [])

    def test_the_prompt_has_the_run_and_since(self):
        self.tick("2026-10-28T12:00")
        self.tick("2026-10-29T06:00")
        _, text, run_id = self.spawned[0]
        run = "feedback.docs-site.20261029-0600"
        self.assertEqual(run_id, run)
        self.assertIn(f"chief tasks report {run} --status running", text)
        self.assertIn("Read the feedback after never for docs-site.", text)
        self.assertIsNone(tasks.report(run, "done", "Shipped two fixes.", pane="%7"))
        self.assertIn(["set-option", "-p", "-t", "%7", tasks.PANE_OPTION, run], self.tmux)
        self.pane_value = run
        self.tick("2026-10-30T06:00")
        _, text, _ = self.spawned[2]
        self.assertIn(f"Read the feedback after {tasks.iso(ts('2026-10-29T06:00'))} for docs-site.", text)
        self.assertIn(["kill-pane", "-t", "%7"], self.tmux)

    def test_a_reused_pane_stays_open(self):
        self.tick("2026-10-28T12:00")
        self.tick("2026-10-29T06:00")
        tasks.report("feedback.docs-site.20261029-0600", "done", "Nothing new.", pane="%7")
        self.pane_value = ""  # After a tmux restart, %7 is another pane.
        self.tick("2026-10-30T06:00")
        self.assertNotIn(["kill-pane", "-t", "%7"], self.tmux)

    def test_a_failed_start_is_not_repeated(self):
        self.spawn_ok = False
        self.tick("2026-10-28T12:00")
        self.tick("2026-10-29T06:00")
        self.tick("2026-10-29T06:01")
        self.assertEqual(len(self.spawned), 2)
        run = tasks.load_runs()["runs"]["feedback.shop.20261029-0600"]
        self.assertEqual((run["status"], run["summary"]), ("failed", "No folder."))

    def test_a_run_waits_for_an_offline_machine(self):
        self.tick("2026-10-28T12:00")
        self.tick("2026-10-29T06:00")
        run = "feedback.shop.20261029-0600"
        tasks.queued(run, "Mac")
        r = tasks.load_runs()["runs"][run]
        self.assertEqual(tasks.run_line(r, ts("2026-10-29T08:00")), "waits for Mac to connect (since 06:00)")
        self.assertFalse(tasks.needs_you(r, ts("2026-10-29T08:00")))
        self.assertEqual(tasks.run_line(r, ts("2026-10-29T18:01")), "did not start: Mac was offline from 06:00.")
        self.assertTrue(tasks.needs_you(r, ts("2026-10-29T18:01")))

    def test_the_late_start_of_a_run(self):
        self.tick("2026-10-28T12:00")
        self.tick("2026-10-29T06:00")
        run = "feedback.shop.20261029-0600"
        tasks.queued(run, "Mac")
        with mock.patch.object(tasks.time, "time", return_value=ts("2026-10-29T09:00")):
            tasks.spawned(run, True, "Added a tab.")
        r = tasks.load_runs()["runs"][run]
        self.assertNotIn("queued", r)
        # The grace time starts when the machine opened the team, not at the time of the run.
        self.assertEqual(tasks.run_line(r, ts("2026-10-29T09:05")), "starts now (09:00)")
        self.assertFalse(tasks.needs_you(r, ts("2026-10-29T09:05")))
        self.assertTrue(tasks.needs_you(r, ts("2026-10-29T09:16")))

    def test_the_hub_saves_a_late_result(self):
        self.tick("2026-10-28T12:00")
        self.tick("2026-10-29T06:00")
        box = pathlib.Path(self.tmp.name) / "outbox" / "mac"
        box.mkdir(parents=True)
        sent = box / "a1.sent"
        sent.write_text(json.dumps({"action": "spawn", "id": "a1", "run": "feedback.shop.20261029-0600"}))
        nodes._finish(box, sent, {"ok": False, "output": "Expired before the node connected."})
        r = tasks.load_runs()["runs"]["feedback.shop.20261029-0600"]
        self.assertEqual((r["status"], r["summary"]), ("failed", "Expired before the node connected."))
        self.assertFalse(sent.exists())
        # A result that comes after the run reported does not change the run.
        tasks.report("feedback.docs-site.20261029-0600", "running", "Started")
        tasks.spawned("feedback.docs-site.20261029-0600", False, "Expired before the node connected.")
        self.assertEqual(tasks.load_runs()["runs"]["feedback.docs-site.20261029-0600"]["status"], "running")

    def test_report_problems(self):
        self.assertIn("No run", tasks.report("nothing", "done", "x"))
        self.assertIn("status", tasks.report("nothing", "finished", "x"))

    def test_agenda_section(self):
        self.tick("2026-10-28T12:00")
        snap = {"generatedAt": ts("2026-10-28T12:00"), "departments": [], "teams": [], "nodes": []}
        item = next(it for it in agenda.build(snap)["items"] if it["kind"] == "task")
        self.assertEqual((item["label"], item["title"]), ("info", "feedback: the first run is Thu 29 Oct 06:00"))

        self.tick("2026-10-29T06:00")
        tasks.report("feedback.docs-site.20261029-0600", "done", "Shipped two fixes.", pane="%7")
        tasks.report("feedback.shop.20261029-0600", "waiting", "PR 14 waits for a review.", pane="%8")
        team = {"key": "cloud:project-docs-site", "status": "idle", "lastActivity": ts("2026-10-29T06:20"),
                "agents": [{"paneId": "%7", "role": "coordinator", "status": "idle"}], "department": "docs-site"}
        snap = {"generatedAt": ts("2026-10-29T08:00"), "departments": [], "teams": [team], "nodes": []}
        items = agenda.build(snap)["items"]
        item = next(it for it in items if it["kind"] == "task")
        self.assertEqual((item["label"], item["title"]), ("needs-you", "feedback: 1 run needs you"))
        self.assertEqual(item["detail"], "docs-site: Shipped two fixes.\nshop: waits for you: PR 14 waits for a review.")
        self.assertEqual(item["runs"][0]["teamKey"], "cloud:project-docs-site")
        # The idle agent of the finished run is not a team with no work.
        self.assertFalse([it for it in items if it["kind"] == "idle"])


class TrustTest(unittest.TestCase):
    """Claude Code stops at the trust dialog in a new folder, so a run must not open there."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = pathlib.Path(self.tmp.name) / ".claude.json"
        for target, name, value in ((paths, "CLAUDE_CONFIG", self.config),
                                    (nodes, "terminal", lambda: "none"),
                                    (nodes, "config", lambda: {"label": "Cloud"})):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def trust(self, *folders):
        self.config.write_text(json.dumps({"projects": {f: {"hasTrustDialogAccepted": True} for f in folders}}))

    def test_the_folder_or_a_parent(self):
        self.trust("/work/shop", "/home/me/Github")
        self.assertTrue(claude_sessions.trusted("/work/shop"))
        self.assertTrue(claude_sessions.trusted("/home/me/Github/docs-site"))
        self.assertFalse(claude_sessions.trusted("/work/docs-site"))
        self.assertFalse(claude_sessions.trusted("/work"))

    def test_a_link_to_a_trusted_folder(self):
        real = pathlib.Path(self.tmp.name) / "real"
        real.mkdir()
        (pathlib.Path(self.tmp.name) / "link").symlink_to(real)
        self.trust(str(real.resolve()))
        self.assertTrue(claude_sessions.trusted(str(pathlib.Path(self.tmp.name) / "link")))

    def test_unreadable_settings(self):
        self.assertIsNone(claude_sessions.trusted("/work/shop"))
        self.config.write_text("not json")
        self.assertIsNone(claude_sessions.trusted("/work/shop"))

    def test_a_run_does_not_open_in_a_folder_with_no_trust(self):
        self.trust("/work/shop")
        ok, out = nodes.spawn("project-docs-site", "/work/docs-site", "claude go", needs_trust=True)
        self.assertFalse(ok)
        self.assertIn("does not trust the folder /work/docs-site on Cloud", out)
        # A trusted folder, and a team that a person opens, go on to the terminal.
        for folder, needs_trust in (("/work/shop", True), ("/work/docs-site", False)):
            ok, out = nodes.spawn("project", folder, "claude go", needs_trust=needs_trust)
            self.assertIn("no cmux or tmux", out)


if __name__ == "__main__":
    unittest.main()
