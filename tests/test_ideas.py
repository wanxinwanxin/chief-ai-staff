"""The idea inbox (chief/ideas.py) and its section in the agenda.

The tests use a temporary chief home and a temporary ideas root, a fake triage, and a
fake spawn, so that they do not read or change ~/.chief, call Claude Code, or start agents.
"""

import json
import os
import pathlib
import stat
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from chief import agenda, ideas, paths, tasks

DEPTS = [{"key": "docs-site", "name": "Docs site", "status": "active", "description": "The docs of the shop."},
         {"key": "shop", "name": "Shop", "status": "active", "description": "A small web shop."},
         {"key": "old-blog", "name": "Old blog", "status": "archived", "description": "Not used."}]


def ts(text):
    return datetime.fromisoformat(text).timestamp()


class IdeasTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = pathlib.Path(self.tmp.name) / "chief"
        home.mkdir()
        self.root = pathlib.Path(self.tmp.name) / "ideas"
        self.config = {"roots": [str(pathlib.Path(self.tmp.name) / "projects")],
                       "ideas": {"root": str(self.root), "schedule": "daily 05:00", "max": 2}}
        (home / "config.json").write_text(json.dumps(self.config))
        for name, value in (("CHIEF_HOME", home), ("CACHE_DIR", home / "cache"), ("CONFIG_FILE", home / "config.json"),
                            ("ORG_FILE", home / "org.json"), ("TASKS_DIR", home / "tasks"),
                            ("TASK_RUNS", home / "task-runs.json"), ("IDEAS_FILE", home / "ideas.json")):
            patcher = mock.patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(tasks.tmux, "run", lambda args: (True, ""))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.answer = {"ideas": []}
        self.prompts = []
        self.schemas = []
        self.spawned = []
        self.spawn_ok = True

    def runner(self, prompt, schema, model):
        self.prompts.append(prompt)
        self.schemas.append(schema)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def spawn(self, task, department, text, run_id):
        self.spawned.append((department, text, run_id))
        return self.spawn_ok, "Added a tab." if self.spawn_ok else "No folder."

    def sort(self, text, *items, now=None):
        self.answer = {"ideas": list(items)}
        iid = ideas.add(text, now=now)
        return iid, ideas.triage(iid, DEPTS, runner=self.runner, now=now)

    def test_a_note_splits_into_ideas(self):
        iid, found = self.sort(
            "fix the search of the docs and also model how fast markets copy ideas, and buy milk",
            {"title": "Better docs search", "summary": "Fix the search.", "kind": "project",
             "department": "docs-site", "folder": None},
            {"title": "Model how fast markets copy ideas", "summary": "A model.", "kind": "explore",
             "department": None, "folder": "Idea Copy Speed!"},
            {"title": "Buy milk", "summary": "Buy milk.", "kind": "memo", "department": None, "folder": None})
        self.assertEqual(found[0], iid)
        self.assertEqual(len(found), 3)
        rows = [ideas.get(i) for i in found]
        self.assertEqual([(r["kind"], r["status"]) for r in rows],
                         [("project", "queued"), ("explore", "queued"), ("memo", "ready")])
        self.assertEqual(rows[0]["department"], "docs-site")
        self.assertEqual(rows[1]["folder"], "idea-copy-speed")
        self.assertEqual(rows[1]["parent"], iid)
        self.assertEqual(rows[2]["text"], rows[0]["text"])
        # The prompt lists the departments that are not archived, and has the note.
        self.assertIn("- docs-site: Docs site. active.", self.prompts[0])
        self.assertNotIn("old-blog", self.prompts[0])
        self.assertIn("buy milk\n</note>", self.prompts[0])
        # With no accounts in config.json, the triage does not ask for one.
        self.assertNotIn("account", self.prompts[0])
        self.assertNotIn("account", self.schemas[0]["properties"]["ideas"]["items"]["properties"])

    def test_an_unknown_department_is_a_new_project(self):
        _, found = self.sort("a thing", {"title": "A thing", "summary": "", "kind": "project",
                                         "department": "nope", "folder": None})
        idea = ideas.get(found[0])
        self.assertEqual((idea["kind"], idea["department"], idea["folder"]), ("explore", None, "a-thing"))

    def test_a_failed_triage_waits_and_stops_after_three_tries(self):
        self.answer = RuntimeError("no network")
        iid = ideas.add("an idea", now=ts("2026-11-01T10:00"))
        self.assertEqual(ideas.triage(iid, DEPTS, runner=self.runner, now=ts("2026-11-01T10:00")), [])
        idea = ideas.get(iid)
        self.assertEqual((idea["status"], idea["tries"]), ("new", 1))
        self.assertIn("no network", idea["error"])
        # The hub tries again after a wait, and stops after three tries.
        for minute in ("10:01", "10:10", "10:20", "10:30", "10:40"):
            ideas.tick(lambda: DEPTS, now=ts(f"2026-11-01T{minute}"), runner=self.runner)
        self.assertEqual(ideas.get(iid)["tries"], 3)
        view = ideas.for_snapshot([], now=ts("2026-11-01T11:00"))
        self.assertTrue(view["items"][0]["stuck"])
        self.assertIsNone(ideas.retry(iid))
        self.assertEqual(ideas.get(iid)["tries"], 0)

    def test_one_triage_at_a_time(self):
        iid = ideas.add("an idea")
        self.assertTrue(ideas._claim(iid, ts("2026-11-01T10:00")))
        self.assertIsNone(ideas.triage(iid, DEPTS, runner=self.runner, now=ts("2026-11-01T10:01")))
        # A triage that died gets a new try.
        self.answer = {"ideas": [{"title": "X", "summary": "", "kind": "memo", "department": None, "folder": None}]}
        self.assertEqual(ideas.triage(iid, DEPTS, runner=self.runner, now=ts("2026-11-01T10:30")), [iid])

    def test_the_file_is_private(self):
        ideas.add("a private note")
        self.assertEqual(stat.S_IMODE(os.stat(paths.IDEAS_FILE).st_mode), 0o600)

    def test_the_review_waits_for_its_time(self):
        start = ts("2026-11-01T10:00")
        ideas.tick(lambda: DEPTS, now=start, runner=self.runner)
        self.sort("docs", {"title": "Docs search", "summary": "Fix it.", "kind": "project",
                           "department": "docs-site", "folder": None}, now=start)
        self.assertEqual(ideas.tick(lambda: DEPTS, now=ts("2026-11-01T23:00"), spawn=self.spawn), [])
        lines = ideas.tick(lambda: DEPTS, now=ts("2026-11-02T05:00"), spawn=self.spawn)
        self.assertEqual(len(lines), 1)
        department, text, run_id = self.spawned[0]
        self.assertEqual(department, "docs-site")
        self.assertTrue(run_id.startswith("idea-"))
        self.assertIn(f"chief tasks report {run_id} --status running", text)
        self.assertIn("Do not change files", text)
        iid = run_id.split(".")[0][5:]
        self.assertIn(f"chief ideas report {iid} -", text)
        self.assertEqual(ideas.get(iid)["status"], "running")
        # One review for each time.
        self.assertEqual(ideas.tick(lambda: DEPTS, now=ts("2026-11-02T05:01"), spawn=self.spawn), [])

    def test_the_maximum_of_runs(self):
        ideas.tick(lambda: DEPTS, now=ts("2026-11-01T10:00"))
        for n in range(3):
            self.sort(f"memo {n}", {"title": f"Idea {n}", "summary": "", "kind": "project",
                                    "department": "shop", "folder": None}, now=ts("2026-11-01T10:00") + n)
        ideas.tick(lambda: DEPTS, now=ts("2026-11-02T05:00"), spawn=self.spawn)
        self.assertEqual(len(self.spawned), 2)
        ideas.tick(lambda: DEPTS, now=ts("2026-11-03T05:00"), spawn=self.spawn)
        self.assertEqual(len(self.spawned), 3)

    def test_an_explore_idea_gets_a_new_repository(self):
        os.makedirs(self.root / "copy-speed")  # The name is taken.
        _, found = self.sort("model it", {"title": "Copy speed", "summary": "Model how fast ideas get copied.",
                                          "kind": "explore", "department": None, "folder": "copy-speed"})
        iid = found[0]
        ok, _ = ideas.review(iid, now=ts("2026-11-01T10:00"), spawn=self.spawn)
        self.assertTrue(ok)
        path = self.root / "copy-speed-2"
        self.assertEqual(ideas.get(iid)["path"], str(path))
        readme = (path / "README.md").read_text()
        self.assertIn("# Copy speed", readme)
        self.assertIn("> model it", readme)
        self.assertTrue((path / ".git").is_dir())
        department, text, _ = self.spawned[0]
        self.assertEqual(department, "copy-speed-2")
        self.assertIn("FINDINGS.md", text)
        self.assertIn("Do not add a remote", text)
        org = json.loads(paths.ORG_FILE.read_text())
        self.assertEqual(org["projects"]["copy-speed-2"]["name"], "Copy speed")
        # The ideas root is a root of the departments.
        from chief import state
        self.assertIn(str(self.root), state.load_config()["roots"])

    def test_a_work_idea_gets_the_work_account(self):
        self.config.update(accounts={"personal": {"command": "claude-home", "configDir": "~/.claude-home"},
                                     "work": {"command": "claude"}}, defaultAccount="personal")
        paths.CONFIG_FILE.write_text(json.dumps(self.config))
        _, found = self.sort("a model for work, and one more",
                             {"title": "Venue liquidity", "summary": "This is a work project.", "kind": "explore",
                              "department": None, "folder": "venue-liquidity", "account": "work"},
                             {"title": "Other", "summary": "", "kind": "explore", "department": None,
                              "folder": "other", "account": "nope"})
        self.assertIn("one of: personal, work", self.prompts[0])
        self.assertIn("account", self.schemas[0]["properties"]["ideas"]["items"]["required"])
        self.assertEqual([ideas.get(i)["account"] for i in found], ["work", None])
        ideas.review(found[0], now=ts("2026-11-01T10:00"), spawn=self.spawn)
        org = json.loads(paths.ORG_FILE.read_text())
        self.assertEqual(org["projects"]["venue-liquidity"]["account"], "work")
        # The chief can correct the account before the run.
        self.assertIn("No account 'nope'", ideas.change(found[1], account="nope"))
        self.assertIsNone(ideas.change(found[1], account="work"))
        self.assertEqual(ideas.get(found[1])["account"], "work")

    def test_a_run_that_did_not_start_waits_for_the_next_time(self):
        self.spawn_ok = False
        _, found = self.sort("x", {"title": "X", "summary": "", "kind": "project", "department": "shop",
                                   "folder": None})
        ok, _ = ideas.review(found[0], now=ts("2026-11-01T10:00"), spawn=self.spawn)
        self.assertFalse(ok)
        idea = ideas.get(found[0])
        self.assertEqual((idea["status"], idea["run"]), ("queued", None))
        self.assertIn("No folder.", idea["error"])

    def test_findings_and_close(self):
        _, found = self.sort("x", {"title": "X", "summary": "", "kind": "project", "department": "shop",
                                   "folder": None})
        iid = found[0]
        ideas.review(iid, now=ts("2026-11-01T10:00"), spawn=self.spawn)
        run_id = self.spawned[0][2]
        self.assertIsNone(ideas.report(iid, "## Findings\nBuild it later."))
        self.assertEqual(ideas.get(iid)["status"], "ready")
        tasks.report(run_id, "done", "Build it later.")
        ideas.sync()
        self.assertEqual(ideas.get(iid)["result"], "## Findings\nBuild it later.")
        self.assertIsNone(ideas.close(iid, "Park it."))
        self.assertEqual((ideas.get(iid)["status"], ideas.get(iid)["note"]), ("closed", "Park it."))
        self.assertIsNone(ideas.reopen(iid))
        self.assertEqual(ideas.get(iid)["status"], "ready")

    def test_a_done_run_with_no_findings(self):
        _, found = self.sort("x", {"title": "X", "summary": "", "kind": "project", "department": "shop",
                                   "folder": None})
        ideas.review(found[0], now=ts("2026-11-01T10:00"), spawn=self.spawn)
        tasks.report(self.spawned[0][2], "done", "The idea is easy.")
        ideas.sync()
        self.assertEqual((ideas.get(found[0])["status"], ideas.get(found[0])["result"]), ("ready", "The idea is easy."))

    def test_change_the_triage(self):
        _, found = self.sort("x", {"title": "X", "summary": "", "kind": "memo", "department": None, "folder": None})
        iid = found[0]
        self.assertIn("department", ideas.change(iid, kind="project"))
        self.assertIsNone(ideas.change(iid, kind="explore", folder="My Thing"))
        self.assertEqual((ideas.get(iid)["status"], ideas.get(iid)["folder"]), ("queued", "my-thing"))
        self.assertIsNone(ideas.change(iid, kind="project", department="shop"))
        self.assertEqual((ideas.get(iid)["department"], ideas.get(iid)["folder"]), ("shop", None))

    def test_agenda_section(self):
        now = ts("2026-11-01T10:00")
        ideas.tick(lambda: DEPTS, now=now)
        _, (a,) = self.sort("a", {"title": "Search", "summary": "", "kind": "project", "department": "shop",
                                  "folder": None}, now=now)
        _, (b,) = self.sort("b", {"title": "Buy milk", "summary": "", "kind": "memo", "department": None,
                                  "folder": None}, now=now)
        _, (c,) = self.sort("c", {"title": "Copy speed", "summary": "", "kind": "explore", "department": None,
                                  "folder": None}, now=now)
        ideas.review(a, now=now, spawn=self.spawn)
        ideas.report(a, "Build it later. " * 50)
        snap = {"generatedAt": now, "departments": [], "teams": [], "nodes": [],
                "ideas": ideas.for_snapshot([], now)}
        items = [it for it in agenda.build(snap)["items"] if it["kind"] == "idea"]
        self.assertEqual([(it["label"], it["title"]) for it in items], [
            ("needs-you", "Search: the review is ready (project shop)"),
            ("info", "1 memo: Buy milk"),
            ("info", "1 idea waits for the review at Mon 02 Nov 05:00"),
        ])
        self.assertEqual(items[0]["idea"], a)
        self.assertTrue(items[0]["detail"].endswith("…"))
        self.assertEqual(items[2]["ideas"], [c])
        self.assertEqual(items[1]["ideas"], [b])


if __name__ == "__main__":
    unittest.main()
