"""Scheduled tasks: private prompts that the hub starts as new teams at fixed times.

A task is one Markdown file in ~/.chief/tasks/ on the hub. This repository has only the
scheduler, so the prompt of a task stays private. The file has a header and the prompt:

    ---
    schedule: daily 06:00
    departments: docs-site, shop
    ---
    Read the feedback that came after {since}. ...

Header fields:
  schedule     "daily HH:MM", "weekdays HH:MM", or days and a time, for example
               "mon,thu 09:30". The time is the local time of the hub.
  departments  The hub starts one run in each department, as a new team (`chief spawn`).
  on           The machine of the runs. Default: the hub, when the folder is there.
               When that machine is offline, the run starts when it connects (in 12 hours).
  agent        claude (default) or codex.
  permission   The Claude Code permission mode of a run. Default: auto.
  enabled      "no" stops the schedule.

The prompt can use {since}, {today}, {department}, and {run}. {since} is the start time
of the last run in the same department that reported, or "never".

The hub server calls tick() every minute. A new task file starts at its next time.
When the hub was off at the time of a run, the run starts when the hub is on again.

A run in Claude Code starts only in a folder that Claude Code trusts. In a folder with no
trust, the agent stops at the trust dialog, where nobody sees it. Thus the machine does not
open the team, and the run fails with the reason.

Each run reports with `chief tasks report <run> --status running|waiting|done "<summary>"`.
Before a new run starts, the hub closes the tab of the last finished run of the same task
and department. ~/.chief/task-runs.json keeps the runs:
  {"seen": {"<task>": ts},
   "runs": {"<run id>": {"task", "department", "startedAt", "status", "summary",
                         "reportedAt", "pane", "output", "queued", "openedAt"}}}
Status: starting (the hub started the team), running, waiting (for the user), done, failed.
"queued" is the label of the offline machine that the run waits for. "openedAt" is the time
when a machine opened the team of the run, after a wait for that machine.
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

from . import paths
from .sources import tmux

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
STATUSES = ["running", "waiting", "done"]
START_GRACE = 15 * 60  # A run that did not report "running" after this time did not start.
REPORT_GRACE = 6 * 3600  # A run that did not report a result after this time is stuck.
QUEUE_TTL = 12 * 3600  # The start of a run waits this long for its machine to connect.
PANE_OPTION = "@chief_task"

PREAMBLE = """From the chief of staff: this is the scheduled task "{name}" in {department}. Run: {run}.
The last run here that reported started at {since}. Today is {today}.

First, run this command: chief tasks report {run} --status running "Started"

{prompt}

At the end, report the result in one line:
- When nothing waits for the user: chief tasks report {run} --status done "<what you did>"
- When the user must decide: chief tasks report {run} --status waiting "<what waits>". Then end your reply with one clear question. The user answers from the dashboard. After the answer, finish the work and report again with --status done.
"""


def parse_schedule(text):
    """Return (weekdays, hour, minute). Weekdays are 0 (Monday) to 6 (Sunday)."""
    m = re.fullmatch(r"\s*(\S+)\s+(\d{1,2}):(\d{2})\s*", text or "")
    if not m:
        raise ValueError(f'"{text}" is not a schedule. Use "daily 06:00", "weekdays 06:00", or "mon,thu 06:00".')
    days, hour, minute = m.group(1).lower(), int(m.group(2)), int(m.group(3))
    if hour > 23 or minute > 59:
        raise ValueError(f'"{text}" has no valid time.')
    if days == "daily":
        weekdays = set(range(7))
    elif days == "weekdays":
        weekdays = set(range(5))
    else:
        names = days.split(",")
        if not all(n in DAYS for n in names):
            raise ValueError(f'"{days}" has an unknown day. Use daily, weekdays, or mon,tue,wed,thu,fri,sat,sun.')
        weekdays = {DAYS.index(n) for n in names}
    return weekdays, hour, minute


def _slots(task, now, step):
    weekdays, hour, minute = task["when"]
    day = datetime.fromtimestamp(now).replace(hour=hour, minute=minute, second=0, microsecond=0)
    for _ in range(8):
        if day.weekday() in weekdays and (day.timestamp() <= now if step < 0 else day.timestamp() > now):
            return day.timestamp()
        day += timedelta(days=step)
    return None


def last_slot(task, now):
    """The last scheduled time at or before `now`."""
    return _slots(task, now, -1)


def next_slot(task, now):
    """The next scheduled time after `now`."""
    return _slots(task, now, 1)


def parse(text, name):
    """Return the task in a task file. A task with a problem has the key "error"."""
    task = {"name": name, "departments": [], "on": None, "agent": "claude", "permission": "auto",
            "enabled": True, "prompt": text.strip(), "error": None}
    m = re.match(r"---\s*\n(.*?)\n---\s*\n?(.*)", text, re.S)
    if not m:
        task["error"] = "The file has no header. Start it with a --- line, the fields, and a --- line."
        return task
    head, task["prompt"] = m.group(1), m.group(2).strip()
    fields = {}
    for line in head.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.partition(":")
            fields[key.strip().lower()] = value.strip()
    task["schedule"] = fields.get("schedule", "")
    task["departments"] = [d.strip() for d in fields.get("departments", "").split(",") if d.strip()]
    task["on"] = fields.get("on") or None
    task["agent"] = fields.get("agent") or "claude"
    task["permission"] = fields.get("permission") or "auto"
    task["enabled"] = fields.get("enabled", "yes").lower() not in ("no", "false", "off")
    try:
        task["when"] = parse_schedule(task["schedule"])
    except ValueError as exc:
        task["error"] = str(exc)
    if not task["departments"]:
        task["error"] = task["error"] or 'The header has no "departments" field.'
    if task["agent"] not in ("claude", "codex"):
        task["error"] = task["error"] or f'The agent "{task["agent"]}" is unknown. Use claude or codex.'
    if not task["prompt"]:
        task["error"] = task["error"] or "The file has no prompt after the header."
    return task


def load():
    """All tasks in ~/.chief/tasks/, sorted by name."""
    if not paths.TASKS_DIR.is_dir():
        return []
    return [parse(f.read_text(), f.stem) for f in sorted(paths.TASKS_DIR.glob("*.md"))]


def find(name):
    return next((t for t in load() if t["name"] == name), None)


@contextmanager
def _runs(write=False):
    """The run state. With write=True, hold a lock and save the changes at the end."""
    paths.ensure_home()
    with open(paths.TASK_RUNS.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        try:
            data = json.loads(paths.TASK_RUNS.read_text())
        except (OSError, ValueError):
            data = {}
        data.setdefault("seen", {})
        data.setdefault("runs", {})
        yield data
        if write:
            tmp = paths.TASK_RUNS.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(paths.TASK_RUNS)


def load_runs():
    with _runs() as data:
        return data


def _runs_of(data, task, department=None):
    return sorted((dict(r, id=rid) for rid, r in data["runs"].items()
                   if r["task"] == task and (department is None or r["department"] == department)),
                  key=lambda r: r["startedAt"])


def iso(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="minutes")


def since(data, task, department):
    """The start time of the last run in the department that reported, or "never"."""
    done = [r for r in _runs_of(data, task, department) if r["status"] in ("running", "waiting", "done")]
    return iso(done[-1]["startedAt"]) if done else "never"


def _close_finished(data, task, department):
    """Close the tab of each finished run of the task in the department, on this machine.

    The pane option @chief_task proves that the pane still runs that task, because tmux
    can give the same pane id to a new pane after a restart.
    """
    closed = []
    for r in _runs_of(data, task, department):
        pane = r.get("pane")
        if r["status"] != "done" or not pane or r.get("closed"):
            continue
        ok, out = tmux.run(["show-options", "-p", "-v", "-t", pane, PANE_OPTION])
        if ok and out.strip() == r["id"]:
            tmux.run(["kill-pane", "-t", pane])
            closed.append(pane)
        data["runs"][r["id"]]["closed"] = True
    return closed


def _spawn(task, department, text, run_id):
    """Start the run with `chief spawn`. Return (ok, output)."""
    cmd = [sys.executable, "-m", "chief", "spawn", department, text, "--agent", task["agent"], "--run", run_id]
    if task["on"]:
        cmd += ["--on", task["on"]]
    if task["agent"] == "claude" and task["permission"]:
        cmd += ["--permission", task["permission"]]
    env = dict(os.environ, PYTHONPATH=str(paths.REPO_ROOT), CHIEF_LOCAL="1")
    try:
        res = subprocess.run(cmd, cwd=str(paths.REPO_ROOT), env=env, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return res.returncode == 0, (res.stdout + res.stderr).strip()


def make_id(name, department, now):
    """The id of the run of a task in a department that starts at `now`."""
    return f"{name}.{department}.{time.strftime('%Y%m%d-%H%M', time.localtime(now))}"


def start(task, departments=None, now=None, spawn=_spawn):
    """Start one run of the task in each department. Return one line for each run.

    `task["preamble"]` can replace PREAMBLE, for example for the review of an idea (ideas.py).
    """
    now = now or time.time()
    lines = []
    for dept in departments or task["departments"]:
        run_id = make_id(task["name"], dept, now)
        with _runs(write=True) as data:
            if run_id in data["runs"]:
                lines.append(f"{dept}: the run {run_id} exists already.")
                continue
            _close_finished(data, task["name"], dept)
            values = {"name": task["name"], "department": dept, "run": run_id,
                      "since": since(data, task["name"], dept), "today": time.strftime("%Y-%m-%d", time.localtime(now))}
            prompt = task["prompt"]
            for key, value in values.items():
                prompt = prompt.replace("{" + key + "}", value)
            data["runs"][run_id] = {"task": task["name"], "department": dept, "startedAt": now,
                                    "status": "starting", "summary": None}
        ok, output = spawn(task, dept, (task.get("preamble") or PREAMBLE).format(prompt=prompt, **values), run_id)
        with _runs(write=True) as data:
            run = data["runs"][run_id]
            run["output"] = output
            if not ok:
                run["status"], run["summary"] = "failed", output
        lines.append(f"{dept}: {output}")
    return lines


def tick(now=None, spawn=_spawn):
    """Start each task whose time came. Return one line for each run that started."""
    now = now or time.time()
    lines = []
    for task in load():
        if task["error"] or not task["enabled"]:
            continue
        with _runs(write=True) as data:
            seen = data["seen"].setdefault(task["name"], now)
            slot = last_slot(task, now)
            due = slot is not None and slot >= seen and not any(
                r["startedAt"] >= slot for r in _runs_of(data, task["name"]))
        if due:
            lines += [f"{task['name']} / {line}" for line in start(task, now=now, spawn=spawn)]
    return lines


def report(run_id, status, summary, pane=None):
    """Save the report of a run. Return an error text, or None."""
    if status not in STATUSES:
        return f"The status must be one of: {', '.join(STATUSES)}."
    with _runs(write=True) as data:
        run = data["runs"].get(run_id)
        if not run:
            return f"No run has the id {run_id}. See: chief tasks list"
        run.update(status=status, summary=summary.strip(), reportedAt=time.time())
        run.pop("queued", None)
        if pane:
            run["pane"] = pane
    if pane:
        tmux.run(["set-option", "-p", "-t", pane, PANE_OPTION, run_id])
    return None


def queued(run_id, label):
    """The machine of the run is offline. The hub keeps the start until the machine connects."""
    with _runs(write=True) as data:
        run = data["runs"].get(run_id)
        if run and run["status"] == "starting":
            run["queued"] = label


def spawned(run_id, ok, output):
    """Save the result of the start of a run on another machine (see nodes._finish)."""
    with _runs(write=True) as data:
        run = data["runs"].get(run_id)
        if not run or run["status"] != "starting":
            return
        run.pop("queued", None)
        run["output"] = output
        if ok:
            run["openedAt"] = time.time()
        else:
            run["status"], run["summary"] = "failed", output


def when(ts, now):
    """The time of day for a time today, else the day and the time."""
    t = time.localtime(ts)
    fmt = "%H:%M" if time.strftime("%Y%m%d", t) == time.strftime("%Y%m%d", time.localtime(now)) else "%a %d %b %H:%M"
    return time.strftime(fmt, t)


def run_line(run, now):
    """One line about a run: its state and its summary."""
    status, at = run["status"], when(run["startedAt"], now)
    opened = run.get("openedAt") or run["startedAt"]
    if status == "starting":
        if run.get("queued"):
            if now - run["startedAt"] > QUEUE_TTL:
                return f"did not start: {run['queued']} was offline from {at}."
            return f"waits for {run['queued']} to connect (since {at})"
        if now - opened > START_GRACE:
            return f"did not report a start (the team opened at {when(opened, now)}). Open the team."
        return f"starts now ({when(opened, now)})"
    if status == "running" and now - opened > REPORT_GRACE:
        return f"started at {at}, and has no result yet"
    return {"running": f"runs since {at}", "waiting": f"waits for you: {run['summary']}",
            "done": run["summary"], "failed": f"did not start at {at}: {run['summary']}"}[status]


def needs_you(run, now):
    opened = run.get("openedAt") or run["startedAt"]
    if run["status"] == "starting" and run.get("queued"):  # It starts when its machine connects.
        return now - run["startedAt"] > QUEUE_TTL
    return (run["status"] in ("waiting", "failed")
            or (run["status"] == "starting" and now - opened > START_GRACE)
            or (run["status"] == "running" and now - opened > REPORT_GRACE))


def latest(data, task):
    """{department: the last run of the task there}."""
    out = {}
    for r in _runs_of(data, task["name"]):
        out[r["department"]] = r
    return {d: out[d] for d in task["departments"] if d in out}


def finished_panes(data):
    """The panes of runs that are done. Their idle agents are not teams with no work."""
    return {r["pane"] for r in data["runs"].values() if r["status"] == "done" and r.get("pane")}
