# Chief of AI Staff

A chief of staff for people who run many AI agents in many projects at the same time.

- The **dashboard** shows an org chart of your projects and agents, spend for each project and agent, output (git commits), and who waits for you.
- The **chief agent** reads the same data, gives you a short briefing, recommends what to focus on, and sends messages to the coordinator of each project.

cmux gives the best experience: tab status, jump to a tab, and agent-to-agent messages. The core reads agent logs and git, so the dashboard also works in other terminals with less data.

## Start (prototype)

```sh
./bin/chief start      # starts the dashboard and opens a "Chief of Staff" tab in cmux
./bin/chief brief      # prints the briefing that the chief agent reads
./bin/chief stop       # stops the dashboard server
```

The dashboard runs at http://127.0.0.1:7377/ and listens on your machine only.

## Data sources

| Source | What it gives |
|---|---|
| `cmux tree`, `cmux sidebar-state` | Projects (workspaces), tabs, folders, branches |
| `~/.cmuxterm/*-hook-sessions.json` | Which agent session runs in which tab, and its status |
| `~/.claude/projects/**/*.jsonl` | Claude Code tokens, models, last prompt and reply, sub-agents |
| `~/.codex/sessions/**/*.jsonl` | Codex tokens and last messages |
| `git log --author=<your email>` | Your commits and changed lines in each project |

Dollar values are API-equivalent estimates from list prices. Subscription plans bill differently. To set a price for a model that has no price (for example, Codex models), add it to `~/.chief/pricing.json`:

```json
{"gpt-6.1-sol": [1.25, 10.0, 0, 0, 0.125]}
```

The order is: input, output, cache write 5m, cache write 1h, cache read (USD per million tokens).

## State that the chief writes

- `~/.chief/org.json`: project priorities, goals, notes, and tab roles.
- `~/.chief/focus.json`: the focus plan that the dashboard shows at the top.
- `~/.chief/cache/`: the log index, so that a refresh reads only new log lines.
