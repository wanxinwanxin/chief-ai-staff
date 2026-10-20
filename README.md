# Chief of AI Staff

A chief of staff for people who run many AI agents in many projects at the same time.

- The **chief** is an agent that runs a meeting with you. It opens with an agenda: agents that wait for you, deadlines, teams with no work, spend that goes up, and projects that drift. It handles what it can with messages to your coordinators, and it asks you only what only you can decide.
- The **dashboard** shows the same agenda, your focus, and an org chart of all your projects (departments) and agent teams.

The chief works with any agent harness that reads `AGENTS.md` and Agent Skills (`SKILL.md`), for example Claude Code and Codex. cmux gives the best experience: tab status, jump to a tab, and agent-to-agent messages. The core reads agent logs and git, so it also works in other terminals with less data.

## Words

- A **department** is one project folder. By default, each folder in `~/Github` is a department. Its status is active, semi-active, backlog, or archived.
- A **team** is one cmux workspace (one tab group). A department can have zero, one, or more teams.
- The **coordinator** is the agent in a team that you talk to. **Workers** are other agents in the team.
- **Focus of the day** (2 maximum) and **focus of the week** (5 maximum) are labels on active departments.

## Start (prototype)

```sh
./bin/chief install    # puts `chief` in ~/.local/bin and installs the /chief skill
chief start            # starts the dashboard and opens a "Chief of Staff" tab in cmux
chief start --agent codex
```

You can also start a meeting from any agent session:

- In Claude Code, type `/chief`.
- In Codex, type `$chief`.

Other commands:

```sh
chief meeting          # the agenda that the chief reads
chief brief            # the details of each open team
chief dept list        # all departments
chief dept set "<name>" --status active --focus today --goal "..." --deadline 2026-11-01 --next "..."
chief dept accept      # confirm the suggested statuses
chief notes add "..."  # meeting notes
chief stop             # stops the dashboard server
```

The dashboard runs at http://127.0.0.1:7377/ and listens on your machine only.

## Data sources

| Source | What it gives |
|---|---|
| Folders under the roots (`~/Github`) | Departments, README descriptions, file changes |
| `git log --author=<your email>` | Your commits in each department |
| `cmux tree`, `cmux sidebar-state` | Teams (workspaces), tabs, folders, branches |
| `~/.cmuxterm/*-hook-sessions.json` | Which agent session runs in which tab, and its status |
| `~/.claude/projects/**/*.jsonl` | Claude Code tokens, models, last prompt and reply, sub-agents |
| `~/.codex/sessions/**/*.jsonl` | Codex tokens and last messages |

An agent whose last reply ends with a question counts as "waits for you", also when its tab shows it as idle.

To use other roots, write `~/.chief/config.json`:

```json
{"roots": ["~/Github", "~/work"]}
```

Dollar values are API-equivalent estimates from list prices. Subscription plans bill differently. To set a price for a model that has no price (for example, Codex models), add it to `~/.chief/pricing.json`:

```json
{"gpt-6.1-sol": [1.25, 10.0, 0, 0, 0.125]}
```

The order is: input, output, cache write 5m, cache write 1h, cache read (USD per million tokens).

## Files

| Path | What it holds |
|---|---|
| `agent/AGENTS.md` | The role and the meeting steps of the chief. Every harness reads it. |
| `skills/chief/SKILL.md` | The skill template. `chief install` writes it to `~/.agents/skills/chief` and links it for Claude Code and Codex. |
| `~/.chief/org.json` | Department status, focus, goal, deadline, next step, notes, and tab roles. |
| `~/.chief/meetings/` | Meeting notes, one file for each day. |
| `~/.chief/cache/` | The log index and the portfolio scan, so that a refresh reads only new data. |
