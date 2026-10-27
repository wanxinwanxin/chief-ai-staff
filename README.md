# Chief of AI Staff

A chief of staff for people who run many AI agents in many projects at the same time.

- The **chief** is an agent that runs a meeting with you. It opens with an agenda: agents that wait for you, deadlines, teams with no work, spend that goes up, and projects that drift. It handles what it can with messages to your coordinators, and it asks you only what only you can decide.
- The **dashboard** shows the same agenda, your focus, and an org chart of all your projects (departments) and agent teams. It has a message box for the chief and for each agent, so that you can give instructions from the phone.

The chief works with any agent harness that reads `AGENTS.md` and Agent Skills (`SKILL.md`), for example Claude Code and Codex. cmux gives the best experience: tab status, jump to a tab, and agent-to-agent messages. The core reads agent logs and git, so it also works in other terminals with less data.

## Words

- A **department** is one project folder. By default, each folder in `~/Github` is a department. Its status is active, semi-active, backlog, or archived.
- A **machine** (node) is a computer that runs agents. The **hub** is the machine where the chief and the dashboard run all the time. Other machines send their data to the hub.
- A **team** is one cmux workspace or one tmux session. A department can have zero, one, or more teams, on one or more machines. The same folder name on two machines is one department.
- The **coordinator** is the agent in a team that you talk to. **Workers** are other agents in the team.
- **Focus of the day** (2 maximum) and **focus of the week** (5 maximum) are labels on active departments.

## One machine or many

With one machine, that machine is the hub. Use `chief start` (below).

With an always-on server (for example a cloud VM) and a laptop:

1. On the server (the hub), clone this repository and run:
   ```sh
   ./bin/chief install
   chief up        # dashboard as a systemd user service, chief in the tmux session "chief"
   ```
   The chief starts with Claude Code Remote Control. Open the "Chief of Staff" session in the Claude app to talk to it from the phone.
2. On the laptop (a node), write `~/.chief/config.json`:
   ```json
   {"roots": ["~/Github"], "node": "mac", "label": "Mac", "hub": "agent-cloud"}
   ```
   `hub` is an SSH host. Then run:
   ```sh
   chief node install   # macOS: runs `chief node link` in a cmux Dock terminal, or at login (launchd)
   ```
   The link is one SSH connection. It sends the tabs, the agent status, and the changed log entries every 10 seconds. It gets actions from the hub at once (messages to agents, new teams, jumps). It also forwards the dashboard to http://127.0.0.1:7377/ on the laptop.
3. On a node, `chief meeting`, `chief dept`, `chief notes`, `chief message`, and `chief spawn` run on the hub through SSH, so that every machine sees the same org.

On a tmux hub, each project has one tmux session, and each agent of the project has a window in it. When a new session starts in the folder of a project that has a session already, the hub moves its windows into the older session. `chief spawn` adds a window to that session. cmux on the laptop shows a tmux session as one sidebar row, and each window as a tab. The link opens each new hub session in the cmux sidebar (`cmux ssh-tmux <hub> attach <session>`), so that all hub sessions show without a manual step. Set `"groupSessions": false` on the hub or `"mirrorSessions": false` on the laptop to stop one of these.

The rows of hub sessions show the same data as local rows:

- **Status.** Every 2 seconds, the hub writes the status of each agent (Running, Needs input, or Idle) to the tmux pane option `@cmux_agent`. cmux reads that option in its mirrors.
- **Notifications.** Run `chief hooks install` once on the hub. It adds Notification and Stop hooks to `~/.claude/settings.json` and makes a backup first. The link then shows each Claude Code notification on the tab of its pane, with the same text as for a local agent. Claude Code sessions load the hooks when they start.
- **Git branch.** The link reports the branch of each hub session to its row.

To open the dashboard on a phone, put the hub on your tailnet and run `tailscale serve --bg 7377` on it. Open the `https://<hub>.<tailnet>.ts.net` address, then use "Add to Home Screen". The page is an installable web app. It checks for changes every 5 seconds.

The dashboard opens only for its owner, because a message from the dashboard goes into the terminal of an agent. `tailscale serve` adds the Tailscale login of the device to each request, and the hub compares it with its own Tailscale login. Use the same Tailscale login on the hub and on the phone. When the hub is a tagged device, or when the phone uses a different login, list the logins in `~/.chief/config.json` on the hub:

```json
{"owners": ["you@example.com"]}
```

For a proxy other than `tailscale serve`, add its host name to `"allowedHosts"`, run `chief token` on the hub, and open the dashboard once on each device with `?token=<token>` at the end of the address. The browser keeps the token in a cookie. `chief token --new` replaces the token, and `chief token --off` removes it.

On the phone (720px wide or less), the dashboard shows one screen at a time, with three tabs at the bottom:

- **Today** is the chief's briefing. Each agenda item is a card with its action on it. You can answer a question from an agent on its card. For a team with no work, you can give a task or let the chief assign one. The message box at the bottom goes to the chief.
- **Org** is the org chart as a tree: you, the chief, the projects by status, their teams, and their agents. A filter shows only the teams that need you, work, or are idle.
- **Spend** shows the spend in the time range, by department and over time.

A team or a department opens as a detail screen with a Back button. The back gesture of the phone closes it too. Team detail has a message box at the bottom for the agent that you select. A message goes the same way as `chief message`: the hub pastes it into the agent's tmux pane, or the Mac link gives it to cmux. An agent that waits at a permission prompt refuses the message, so that the text cannot select an option. Above 720px, the dashboard shows all sections on one page, and a detail screen opens in a side panel.

## Start on one machine

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
chief message "<team>" "..."            # a message to a coordinator, on any machine
chief spawn "<department>" "<task>"     # a new team, on the hub or with --on mac
chief nodes            # the machines and when the hub last heard from each
chief stop             # stops the dashboard server
chief token            # the dashboard token for a proxy other than tailscale serve
```

The dashboard runs at http://127.0.0.1:7377/ and listens on your machine only.

## Data sources

| Source | What it gives |
|---|---|
| Folders under the roots (`~/Github`) | Departments, README descriptions, file changes |
| `git log --author=<your email>` | Your commits in each department |
| `cmux tree`, `cmux sidebar-state` | Teams (workspaces), tabs, folders, branches |
| `~/.cmuxterm/*-hook-sessions.json` | Which agent session runs in which tab, and its status |
| `tmux list-panes`, `tmux capture-pane` | Teams (tmux sessions) and agent panes, and permission prompts on the screen |
| `~/.claude/sessions/<pid>.json` | Each running Claude Code session: status, folder, tmux pane, Remote Control |
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
| `~/.chief/nodes/` | On the hub: the last data from each node. |
| `~/.chief/outbox/` | On the hub: actions that wait for a node, and their results. |
| `~/.chief/link.log` | On a node: the link to the hub. |
| `~/.chief/token` | On the hub: the dashboard token from `chief token`. Only you can read it. |
