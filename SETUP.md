# Set up Chief of AI Staff

These steps install chief on a machine. A person can do them, or an agent (Claude Code or Codex) can do them for the user.

**Instructions for an agent:**

1. Ask the user the questions in step 1 before you change anything.
2. Do the steps in order. Run the commands yourself, in the folder of this repository.
3. After each step, run `./bin/chief doctor`. When it shows a problem, do the fix that it prints.
4. Stop when `chief doctor` shows "No problems". Then tell the user how to open the dashboard and how to start a meeting.
5. Do not change the files of this repository. Settings go in `~/.chief/config.json`.
6. When `~/.chief/config.json` exists, add the new keys to it. Keep the keys that are in it.

## 1. Decisions for the user

1. **Machines.** Do you use one machine, or a server that runs all the time (the hub) and other machines (nodes)? A node is, for example, a laptop.
2. **Project folders.** Which folders hold your projects? Each folder in them is one project. The default is `~/Github`.
3. **This machine** (only with more than one machine). Is this machine the hub or a node? For a node: what is the SSH host name of the hub? What short name and label do you want for this machine, for example `mac` and `Mac`?
4. **Phone** (only on the hub). Do you want to use the dashboard on your phone? This needs Tailscale on the hub and on the phone.

## 2. Requirements

| Item | Machine | Check |
|---|---|---|
| Python 3.9 or later | every machine | `python3 --version` |
| git | every machine | `git --version` |
| Claude Code or Codex | every machine that runs agents | `claude --version` or `codex --version` |
| tmux | the hub on Linux | `tmux -V` |
| cmux (recommended) | macOS | `cmux ping` |
| Tailscale | the hub and the phone, for the phone | `tailscale status` |

chief uses only the Python standard library. It needs no package installation.

Use a hub that only you can log in to. Every user of the hub can open the dashboard on 127.0.0.1 and send text to your agents.

## 3. Install chief (every machine)

In the folder of this repository, run:

```sh
./bin/chief install
```

This command puts `chief` in `~/.local/bin` and installs the `/chief` skill for Claude Code and Codex. When it says that `~/.local/bin` is not on the PATH, add this line to the shell profile (for example `~/.zshrc` or `~/.bashrc`):

```sh
export PATH="$HOME/.local/bin:$PATH"
```

When the projects are in other folders than `~/Github`, write the folders to `~/.chief/config.json`:

```json
{"roots": ["~/Github", "~/work"]}
```

## 4. Start the hub (one machine, or the hub)

**Linux:**

```sh
chief up
sudo loginctl enable-linger "$USER"
```

- `chief up` runs the dashboard as the systemd user service `chief-hub`, and the chief in the tmux session `chief`.
- `loginctl enable-linger` keeps the service on when you are not logged in.
- The chief starts with Claude Code Remote Control. In the Claude app, open the session "Chief of Staff" to talk to the chief.

**macOS:**

```sh
chief start
```

This command starts the dashboard and opens a "Chief of Staff" tab in cmux. Without cmux, open http://127.0.0.1:7377/ in a browser, and start your agent in the `agent/` folder of this repository.

## 5. Connect a node (only with more than one machine)

Do these steps on the node, after step 3. Install chief on the hub first (steps 3 and 4).

1. Make sure that `ssh <hub>` logs in with a key, without a password prompt. The link cannot type a password. To test it, run:
   ```sh
   ssh -o BatchMode=yes <hub> true
   ```
2. Add these keys to `~/.chief/config.json`. Use the answers from step 1.
   ```json
   {"node": "mac", "label": "Mac", "hub": "<SSH host of the hub>"}
   ```
3. Start the link.
   - On macOS, run `chief node install`. The link runs at login, or in a cmux Dock terminal.
   - On Linux, keep `chief node link` running, for example in a tmux session. `chief node install` is only for macOS.

The link sends the data of the node to the hub every 10 seconds. It also forwards the dashboard of the hub to http://127.0.0.1:7377/ on the node.

On the hub, run `chief hooks install` when a node uses cmux. Then cmux on the node shows the notifications of the hub agents.

## 6. Open the dashboard on the phone (optional, hub only)

1. Install Tailscale on the hub and on the phone. Log in to the same Tailscale account on both.
2. On the hub, run:
   ```sh
   tailscale serve --bg 7377
   ```
3. On the phone, open `https://<hub>.<tailnet>.ts.net`. `chief doctor` shows the address in the line "Phone".
4. Use "Add to Home Screen". The page is an installable web app.

The dashboard opens only for its owner. `tailscale serve` sends the Tailscale login of the phone, and the hub compares it with its own login. When the hub is a tagged device, or when the phone uses a different login, list the logins on the hub in `~/.chief/config.json`:

```json
{"owners": ["you@example.com"]}
```

For a proxy other than `tailscale serve`, add its host name to `"allowedHosts"` and run `chief token` on the hub. Then open the dashboard once on each device with `?token=<token>` at the end of the address.

## 7. Check

```sh
chief doctor
```

The setup is complete when `chief doctor` shows "No problems". To start a meeting, type `/chief` in Claude Code or `$chief` in Codex, or talk to the chief session of the hub.

## Remove chief

Do the steps that apply to the machine:

1. On a node, run `chief node uninstall`.
2. On the hub, run `chief hooks uninstall`.
3. On a Linux hub, run `systemctl --user disable --now chief-hub.service` and `tmux kill-session -t chief`. Then remove `~/.config/systemd/user/chief-hub.service`.
4. On a macOS hub, run `chief stop`.
5. When you use the phone, run `tailscale serve --https=443 off` on the hub.
6. Remove `~/.local/bin/chief`, `~/.agents/skills/chief`, `~/.claude/skills/chief`, and `~/.codex/skills/chief`.
7. Remove `~/.chief` last. It holds your departments and meeting notes.
