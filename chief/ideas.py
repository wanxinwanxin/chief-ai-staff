"""The idea inbox: ideas that the user dictates on the go, and the runs that review them.

The user saves a note from the Ideas tab of the dashboard, or with `chief ideas add`.
A note can be a word salad, and it can hold more than one idea. The hub sorts each new
note with one headless Claude Code call (the triage). The triage splits the note into
ideas, and gives each idea a title, a summary, and a kind:

  project  The idea is about an existing department. Its review run reads the code of
           that department and writes an assessment. The run changes no files there.
  explore  The idea is a new project. Its review run gets a new local git repository in
           the ideas root (default ~/ideas), explores the idea there, and writes FINDINGS.md.
  memo     A to-do or a reminder. It has no run. The briefing shows it until you close it.

When config.json has Claude Code accounts (state.py), the triage also gives each explore
idea an account, for example "work" when the note says that it is a work project. The new
department keeps that account, so its runs start with the command of the account.

At the review time, the hub starts one run for each queued idea, with a maximum of
"max" runs each time. A run is a team, the same as a run of a scheduled task (tasks.py):
it reports its state with `chief tasks report`, and it saves its findings with
`chief ideas report`. The briefing then shows the findings, so that you can talk about
them with the chief. The settings are in ~/.chief/config.json on the hub:

  {"ideas": {"schedule": "daily 05:00", "max": 3, "root": "~/ideas", "agent": "claude"}}

"schedule" has the form of a task schedule (tasks.parse_schedule), or "off".
"model" is the model of the triage. Default: the default model of Claude Code.

~/.chief/ideas.json (only the user can read it):
  {"seen": ts, "lastReview": ts,
   "ideas": {"<id>": {"text", "at", "status", "title", "summary", "kind", "department",
                      "folder", "account", "path", "parent", "tries", "triedAt", "error", "run",
                      "startedAt", "result", "resultAt", "note", "closedAt"}}}

Status: new (waits for the triage), sorting (the triage runs), queued (waits for the
review time), running (a run works on it), ready (the findings or the memo wait for
you), closed (you decided).
"""

import fcntl
import json
import os
import re
import shlex
import subprocess
import time
import uuid
from contextlib import contextmanager

from . import paths, portfolio, state, tasks

KINDS = ["project", "explore", "memo"]
STATUSES = ["new", "sorting", "queued", "running", "ready", "closed"]
DEFAULTS = {"schedule": "daily 05:00", "max": 3, "agent": "claude", "model": None}
TEXT_MAX = 20000
RESULT_MAX = 40000
SPLIT_MAX = 8  # The maximum number of ideas from one note.
TRIAGE_TRIES = 3
TRIAGE_RETRY = 5 * 60  # The wait before the next try of a triage that failed.
TRIAGE_TIMEOUT = 240
SORTING_STALE = 10 * 60  # A triage that runs longer than this died.
CLOSED_DAYS = 7  # The dashboard shows a closed idea for this long.

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {"ideas": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "kind": {"type": "string", "enum": KINDS},
            "department": {"type": ["string", "null"]},
            "folder": {"type": ["string", "null"]},
        },
        "required": ["title", "summary", "kind", "department", "folder"],
    }}},
    "required": ["ideas"],
}

TRIAGE_PROMPT = """The user dictated this note on the go, for the idea inbox of their chief of staff. \
The note can be a word salad from speech to text, and it can hold more than one idea.

Split the note into ideas. Two parts are one idea when they are about the same thing. For each idea, give:
- title: a short name, at most 8 words.
- summary: what the user wants, in clear sentences. Keep every detail, question, intuition, and \
constraint that the user gave, for example that it is a work project or which data exists. Do not add facts.
- kind: "project" when the idea is about one of the existing projects below. "explore" when it is a \
new project, or a new question to research or to model. "memo" when it is only a to-do or a reminder \
that needs no research.
- department: for "project", the key of the project, exactly as in the list. Else null.
- folder: for "explore", a short name for the folder of a new repository: two to four lowercase \
words with hyphens. Else null.{accounts}

Existing projects (key: name. status. description):
{departments}

The note:
<note>
{text}
</note>
"""

ACCOUNT_LINE = """
- account: for "explore", the Claude Code account of the new project, one of: {names}. Give the \
account that the note asks for, for example "work" for a work project. When the note does not say, give \
null: then the project uses the default account ({default})."""

REVIEW_PREAMBLE = """From the chief of staff: this run reviews an idea from the idea inbox of the user. Run: {run}. Today is {today}.

First, run this command: chief tasks report {run} --status running "Started"

{prompt}

At the end, report the result in one line:
- When nothing waits for the user: chief tasks report {run} --status done "<the main finding>"
- When the user must decide before you can go on: chief tasks report {run} --status waiting "<what waits>". Then end your reply with one clear question. The user answers from the dashboard. After the answer, finish the work and report again with --status done.
"""

IDEA_TEXT = """The idea "{title}" (id {id}):
{summary}

The note as the user dictated it on the go (it can hold other ideas too, so look only at the part about this idea):
<note>
{text}
</note>"""

PROJECT_PROMPT = """{idea}

The idea is about the project in this folder. Think about it for this project, so that the user can decide in the next briefing:
1. Read the rules of the repository (AGENTS.md, CLAUDE.md, README.md) and the code that the idea touches.
2. Do not change files, do not commit, and do not start other work in this folder. This run gives an assessment only.
3. Write the assessment in Markdown, in about 600 words or less:
   - What the idea means for this project, in two or three sentences.
   - How to build it: the approach, and the files that change.
   - The effort in hours, and the risks.
   - Questions for the user.
   - Your recommendation: build it now, build it later, or not, and why.
4. Save the assessment on the hub. Write it to a temporary file outside this folder, then run:
   chief ideas report {id} - < <that file>
"""

EXPLORE_PROMPT = """{idea}

This folder is a new local git repository for the idea. The chief of staff made it from the idea inbox. \
Explore the idea, and give a first insight that the user can discuss with the chief in the next briefing:
1. Find what other people did on this question: papers, products, open-source code, and data. Give the sources with links.
2. Make the idea precise: the question, the terms, and what a good answer looks like. When the idea needs a model, sketch the model. When it needs data, find the data that the user can get, and the first test.
3. Do the first small step when it takes less than about one hour, for example a small model, a calculation, or a query.
4. Write FINDINGS.md in this folder. Start with a summary of five lines or less. Then give what exists, the approach, the first results, the open questions, and the next steps. End with your recommendation: pursue, park, or drop, and why.
5. Commit your files in this repository. Do not add a remote, and do not push.
6. Save the findings on the hub: chief ideas report {id} - < FINDINGS.md
"""

README = """# {title}

{summary}

The chief of staff started this repository from the idea inbox on {date} (idea {id}). \
The review run writes its first insight to FINDINGS.md.

## The note

The user dictated this note on the go. It can hold other ideas too.

> {quote}
"""


def settings():
    cfg = state.load_config()
    raw = cfg.get("ideas") if isinstance(cfg.get("ideas"), dict) else {}
    out = dict(DEFAULTS, **{k: v for k, v in raw.items() if v is not None})
    out["root"] = cfg["ideasRoot"]
    try:
        out["max"] = max(0, int(out["max"]))
    except (TypeError, ValueError):
        out["max"] = DEFAULTS["max"]
    out["when"] = None
    if str(out["schedule"]).strip().lower() not in ("off", "no", "none", ""):
        try:
            out["when"] = tasks.parse_schedule(out["schedule"])
        except ValueError:
            out["when"] = None
    return out


@contextmanager
def _store(write=False):
    """The idea inbox. With write=True, hold a lock and save the changes at the end."""
    paths.ensure_home()
    with open(paths.IDEAS_FILE.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        try:
            data = json.loads(paths.IDEAS_FILE.read_text())
        except (OSError, ValueError):
            data = {}
        data.setdefault("ideas", {})
        yield data
        if write:
            tmp = paths.IDEAS_FILE.with_suffix(".tmp")
            # The notes can hold private or work text. Only the user can read the file.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(data, indent=2))
            tmp.replace(paths.IDEAS_FILE)


def load():
    with _store() as data:
        return data


def get(iid):
    return load()["ideas"].get(iid)


def _new_id(data):
    while True:
        iid = uuid.uuid4().hex[:6]
        if iid not in data["ideas"]:
            return iid


def add(text, now=None):
    """Save a new note. Return its id. The triage sorts it later (see triage)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("The idea is empty.")
    if len(text) > TEXT_MAX:
        raise ValueError(f"The idea is longer than {TEXT_MAX} characters.")
    with _store(write=True) as data:
        iid = _new_id(data)
        data["ideas"][iid] = {"text": text, "at": now or time.time(), "status": "new", "tries": 0}
    return iid


def slug(text):
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    out = ""
    for w in words:
        if len(out) + len(w) + 1 > 40:
            break
        out = f"{out}-{w}" if out else w
    return out


# The triage ------------------------------------------------------------------


def _claude(prompt, schema, model=None):
    """One headless Claude Code call with no tools. Return the structured output."""
    starter = shlex.split(state.claude_account()["command"])
    cmd = starter + ["-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
                     "--output-format", "json", "--json-schema", json.dumps(schema)]
    if model:
        cmd += ["--model", model]
    paths.ensure_home()
    try:
        res = subprocess.run(cmd, input=prompt, cwd=str(paths.CHIEF_HOME), capture_output=True,
                             text=True, timeout=TRIAGE_TIMEOUT)
    except FileNotFoundError:
        raise RuntimeError(f"Claude Code ({starter[0]}) is not on the PATH of the hub.") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"The triage took longer than {TRIAGE_TIMEOUT} seconds.") from None
    try:
        out = json.loads(res.stdout)
    except ValueError:
        raise RuntimeError((res.stderr or res.stdout or f"claude exited with {res.returncode}").strip()[:300]) from None
    if out.get("is_error"):
        raise RuntimeError(str(out.get("result") or out.get("subtype") or "The triage failed.")[:300])
    found = out.get("structured_output")
    if found is None:
        try:
            found = json.loads(out.get("result") or "")
        except ValueError:
            raise RuntimeError("The triage gave no structured answer.") from None
    return found


def _dept_lines(departments):
    rows = []
    for d in departments:
        if d.get("status") == "archived":
            continue
        desc = " ".join((d.get("description") or "").split())[:160]
        goal = f" Goal: {d['goal']}." if d.get("goal") else ""
        rows.append(f"- {d['key']}: {d.get('name') or d['key']}. {d.get('status') or ''}.{goal} {desc}".rstrip())
    return "\n".join(rows) or "(none)"


def _claim(iid, now):
    """Mark the idea as sorting. Return False when it does not wait for a triage."""
    with _store(write=True) as data:
        idea = data["ideas"].get(iid)
        if not idea:
            return False
        stale = idea["status"] == "sorting" and now - (idea.get("triedAt") or 0) > SORTING_STALE
        if idea["status"] != "new" and not stale:
            return False
        idea.update(status="sorting", triedAt=now, tries=(idea.get("tries") or 0) + 1)
        return True


def triage(iid, departments, runner=_claude, now=None):
    """Sort one note into ideas. Return the ids of the ideas, or None when another triage has it."""
    now = now or time.time()
    if not _claim(iid, now):
        return None
    idea = get(iid)
    cfg = settings()
    accounts = list(state.accounts())
    schema, line = TRIAGE_SCHEMA, ""
    if accounts:
        schema = json.loads(json.dumps(TRIAGE_SCHEMA))
        item = schema["properties"]["ideas"]["items"]
        item["properties"]["account"] = {"type": ["string", "null"]}
        item["required"].append("account")
        line = ACCOUNT_LINE.format(names=", ".join(accounts), default=state.default_account())
    prompt = TRIAGE_PROMPT.format(departments=_dept_lines(departments), text=idea["text"], accounts=line)
    try:
        found = runner(prompt, schema, cfg.get("model"))
        items = [x for x in (found or {}).get("ideas") or [] if isinstance(x, dict)]
        if not items:
            raise RuntimeError("The triage found no idea in the note.")
    except Exception as exc:  # The idea waits for the next try, or for the chief.
        with _store(write=True) as data:
            cur = data["ideas"].get(iid)
            if cur:
                cur.update(status="new", error=f"The triage failed: {exc}")
        return []
    return _apply(iid, items[:SPLIT_MAX], {d["key"] for d in departments}, accounts)


def _fields(item, known, accounts=()):
    kind = item.get("kind") if item.get("kind") in KINDS else "explore"
    dept = item.get("department") if kind == "project" else None
    if kind == "project" and dept not in known:
        kind, dept = "explore", None
    title = " ".join(str(item.get("title") or "").split())[:120] or "Untitled idea"
    return {"title": title, "summary": str(item.get("summary") or "").strip()[:4000], "kind": kind,
            "department": dept, "folder": (slug(item.get("folder") or title) or None) if kind == "explore" else None,
            "account": item.get("account") if kind == "explore" and item.get("account") in accounts else None,
            "status": "ready" if kind == "memo" else "queued", "error": None}


def _apply(iid, items, known, accounts=()):
    ids = []
    with _store(write=True) as data:
        first = data["ideas"].get(iid)
        if not first:
            return ids
        for i, item in enumerate(items):
            fields = _fields(item, known, accounts)
            if i == 0:
                first.update(fields)
                ids.append(iid)
                continue
            new = _new_id(data)
            data["ideas"][new] = dict(fields, text=first["text"], at=first["at"], parent=iid, tries=first["tries"])
            ids.append(new)
    return ids


def change(iid, kind=None, department=None, folder=None, title=None, summary=None, account=None):
    """Correct the triage of an idea. Return an error text, or None."""
    with _store(write=True) as data:
        idea = data["ideas"].get(iid)
        if not idea:
            return f"No idea has the id {iid}. See: chief ideas list"
        if idea["status"] in ("running", "closed", "sorting"):
            return f"The idea {iid} is {idea['status']}. Change it when no run or triage works on it."
        if idea["status"] == "ready" and idea.get("run") and kind:
            return f"The idea {iid} has a review already. Close it, and add a new idea."
        if title:
            idea["title"] = title.strip()
        if summary:
            idea["summary"] = summary.strip()
        if account is not None:
            if account and account not in state.accounts():
                names = ", ".join(state.accounts()) or "none"
                return f"No account '{account}' in {paths.CONFIG_FILE}. Accounts: {names}."
            idea["account"] = account or None
        if kind:
            if kind not in KINDS:
                return f"The kind must be one of: {', '.join(KINDS)}."
            if kind == "project" and not department:
                return "Give the department of the project."
            idea.update(kind=kind, department=department if kind == "project" else None,
                        status="ready" if kind == "memo" else "queued", error=None)
            idea["folder"] = (slug(folder or idea.get("title")) or None) if kind == "explore" else None
            if not idea.get("title"):
                idea["title"] = " ".join(idea["text"].split()[:8])
        elif folder and idea.get("kind") == "explore":
            idea["folder"] = slug(folder) or idea.get("folder")
    return None


# The review runs -------------------------------------------------------------


def _taken_names():
    return {os.path.basename(f) for f in portfolio.folders()} | set(state.load_org()["projects"])


def _make_repo(iid, idea, root, now):
    """Make the local git repository of an explore idea. Return its path."""
    if idea.get("path") and os.path.isdir(idea["path"]):
        return idea["path"]
    os.makedirs(root, exist_ok=True)
    base = idea.get("folder") or slug(idea.get("title")) or f"idea-{iid}"
    taken = _taken_names()
    name, i = base, 2
    while name in taken or os.path.exists(os.path.join(root, name)):
        name, i = f"{base}-{i}", i + 1
    path = os.path.join(root, name)
    os.makedirs(path)
    quote = "\n> ".join(idea["text"].splitlines())
    with open(os.path.join(path, "README.md"), "w") as fh:
        fh.write(README.format(title=idea["title"], summary=idea.get("summary") or "", id=iid, quote=quote,
                               date=time.strftime("%Y-%m-%d", time.localtime(now))))
    for cmd in (["git", "init", "-q"], ["git", "add", "README.md"],
                ["git", "commit", "-q", "-m", "Start the repository from the idea inbox"]):
        try:
            subprocess.run(cmd, cwd=path, capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            break
    state.update_dept(name, name=idea["title"], notes=f"From the idea inbox (idea {iid}).",
                      account=idea.get("account"))
    return path


def review(iid, now=None, spawn=None):
    """Start the review run of one queued idea. Return (ok, text)."""
    now = now or time.time()
    idea = get(iid)
    if not idea:
        return False, f"No idea has the id {iid}. See: chief ideas list"
    if idea["status"] != "queued":
        return False, f"The idea {iid} is {idea['status']}. Only a queued idea gets a review run."
    cfg = settings()
    text = IDEA_TEXT.format(title=idea["title"], id=iid, summary=idea.get("summary") or "", text=idea["text"])
    if idea["kind"] == "project":
        dept, prompt = idea["department"], PROJECT_PROMPT.format(idea=text, id=iid)
    else:
        try:
            path = _make_repo(iid, idea, cfg["root"], now)
        except OSError as exc:
            _failed(iid, f"Could not make the folder of the idea: {exc}")
            return False, str(exc)
        with _store(write=True) as data:
            data["ideas"][iid]["path"] = path
        dept, prompt = os.path.basename(path), EXPLORE_PROMPT.format(idea=text, id=iid)
    task = {"name": f"idea-{iid}", "agent": cfg["agent"], "permission": "auto", "on": None,
            "prompt": prompt, "preamble": REVIEW_PREAMBLE}
    run_id = tasks.make_id(task["name"], dept, now)
    with _store(write=True) as data:
        data["ideas"][iid].update(status="running", run=run_id, startedAt=now, error=None)
    line = tasks.start(task, [dept], now=now, spawn=spawn or tasks._spawn)[0]
    run = tasks.load_runs()["runs"].get(run_id) or {}
    if run.get("status") == "failed":
        _failed(iid, run.get("summary") or line)
        return False, line
    return True, line


def _failed(iid, reason):
    """The run did not start. The idea waits for the next review time."""
    with _store(write=True) as data:
        idea = data["ideas"].get(iid)
        if idea:
            idea.update(status="queued", error=reason, run=None)


def report(iid, text, now=None):
    """Save the findings of a review run. Return an error text, or None."""
    text = (text or "").strip()
    if not text:
        return "The findings are empty."
    with _store(write=True) as data:
        idea = data["ideas"].get(iid)
        if not idea:
            return f"No idea has the id {iid}. See: chief ideas list"
        if idea["status"] == "closed":
            return f"The idea {iid} is closed."
        idea.update(result=text[:RESULT_MAX], resultAt=now or time.time(), status="ready", error=None)
    return None


def close(iid, note=None, now=None):
    with _store(write=True) as data:
        idea = data["ideas"].get(iid)
        if not idea:
            return f"No idea has the id {iid}. See: chief ideas list"
        idea.update(status="closed", closedAt=now or time.time())
        if note:
            idea["note"] = note.strip()
    return None


def retry(iid):
    """Let the triage sort a note again, after it failed."""
    with _store(write=True) as data:
        idea = data["ideas"].get(iid)
        if not idea:
            return f"No idea has the id {iid}. See: chief ideas list"
        if idea["status"] != "new":
            return f"The idea {iid} is {idea['status']}. Only a note that is not sorted gets a new triage."
        idea.update(tries=0, triedAt=0, error=None)
    return None


def reopen(iid):
    with _store(write=True) as data:
        idea = data["ideas"].get(iid)
        if not idea:
            return f"No idea has the id {iid}. See: chief ideas list"
        if idea["status"] != "closed":
            return f"The idea {iid} is not closed."
        idea.pop("closedAt", None)
        if not idea.get("kind"):
            idea.update(status="new", tries=0)
        elif idea.get("result") or idea["kind"] == "memo":
            idea["status"] = "ready"
        else:
            idea["status"] = "queued"
    return None


def sync(now=None):
    """Follow the review runs: a finished run makes its idea ready."""
    now = now or time.time()
    runs = tasks.load_runs()["runs"]
    with _store(write=True) as data:
        for idea in data["ideas"].values():
            if idea["status"] != "running":
                continue
            run = runs.get(idea.get("run"))
            if not run:
                idea.update(status="queued", run=None)
            elif run["status"] == "failed":
                idea.update(status="queued", error=f"The review run did not start: {run.get('summary')}", run=None)
            elif run["status"] == "done":
                if not idea.get("result"):
                    idea["result"] = run.get("summary") or "The run finished, and it saved no findings."
                    idea["resultAt"] = run.get("reportedAt") or now
                idea["status"] = "ready"


def next_review(now=None):
    cfg = settings()
    return tasks.next_slot(cfg, now or time.time()) if cfg["when"] else None


def due(now=None):
    """True once for each review time that came. A new inbox waits for its next time."""
    now = now or time.time()
    cfg = settings()
    if not cfg["when"]:
        return False
    with _store(write=True) as data:
        seen = data.setdefault("seen", now)
        slot = tasks.last_slot(cfg, now)
        if slot is None or slot < seen or slot <= (data.get("lastReview") or 0):
            return False
        data["lastReview"] = slot
        return True


def tick(departments, now=None, runner=_claude, spawn=None):
    """The work of the hub each minute. `departments` is a function that gives the departments."""
    now = now or time.time()
    lines = []
    sync(now)
    ideas = load()["ideas"]
    waiting = [iid for iid, x in sorted(ideas.items(), key=lambda kv: kv[1]["at"])
               if (x["status"] == "new" and (x.get("tries") or 0) < TRIAGE_TRIES
                   and now - (x.get("triedAt") or 0) > TRIAGE_RETRY)
               or (x["status"] == "sorting" and now - (x.get("triedAt") or 0) > SORTING_STALE)]
    if waiting:
        depts = departments()
        for iid in waiting:
            found = triage(iid, depts, runner=runner, now=now)
            if found:
                lines.append(f"sorted the note {iid} into {len(found)} idea(s)")
    if due(now):
        queued = sorted(((iid, x) for iid, x in load()["ideas"].items() if x["status"] == "queued"),
                        key=lambda kv: kv[1]["at"])
        for iid, _ in queued[:settings()["max"]]:
            ok, line = review(iid, now=now, spawn=spawn)
            lines.append(f"review of {iid}: {line}")
    return lines


# Views -----------------------------------------------------------------------


def for_snapshot(teams, now=None):
    """The ideas for the dashboard: all open ideas, and the ideas closed in the last days."""
    now = now or time.time()
    data = load()
    runs = tasks.load_runs()["runs"]
    by_pane = {a["paneId"]: t for t in teams for a in t["agents"] if a.get("paneId")}
    out = []
    for iid, x in data["ideas"].items():
        if x["status"] == "closed" and now - (x.get("closedAt") or 0) > CLOSED_DAYS * 86400:
            continue
        run = runs.get(x.get("run")) if x.get("run") else None
        row = {k: x.get(k) for k in ("text", "at", "status", "title", "summary", "kind", "department", "folder",
                                     "parent", "error", "result", "resultAt", "note", "closedAt", "startedAt")}
        row["id"] = iid
        row["dept"] = os.path.basename(x["path"]) if x.get("path") else x.get("department")
        if run:
            row["run"] = {"id": x["run"], "status": run["status"], "text": tasks.run_line(run, now),
                          "needsYou": tasks.needs_you(run, now),
                          "teamKey": (by_pane.get(run.get("pane")) or {}).get("key")}
        row["stuck"] = x["status"] == "new" and (x.get("tries") or 0) >= TRIAGE_TRIES
        out.append(row)
    out.sort(key=lambda r: -(r["at"] or 0))
    nxt = next_review(now)
    return {"items": out, "nextReview": nxt, "nextReviewText": tasks.when(nxt, now) if nxt else None,
            "max": settings()["max"]}


def describe(iid, x, depts=None):
    """One line about an idea for the command line."""
    where = {"project": f"project {(depts or {}).get(x.get('department'), x.get('department'))}",
             "explore": f"new project {x.get('folder') or ''}".rstrip(), "memo": "memo"}.get(x.get("kind"), "not sorted")
    title = x.get("title") or " ".join(x["text"].split()[:10]) + " …"
    return f"{iid} · {x['status']} · {where} · {title}"
