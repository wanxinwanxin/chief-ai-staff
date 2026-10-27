# Mobile redesign: implementation guide

The design canvas has eight artboards: five tab screens, two detail screens, and one information architecture map.

This guide tells a developer how to turn the mockups into the dashboard in `web/`. The dashboard stays one static page with one script. The server does not change in phase 1.

## 1. The problem

The current page shows every section in one long column: live teams, the chief card, the agenda, focus, tiles, the org chart, two charts, and a table. On a phone, the user scrolls through all of it to find the one item that needs them. The detail view opens as a side drawer, which is a desktop pattern.

## 2. The information architecture

Five bottom tabs. Each tab is one job.

| Tab | Job | Content from the current page |
|---|---|---|
| Inbox | What needs you now | `renderAgenda` (the agenda items, grouped by kind) |
| Teams | Every team on every machine, by status | `renderLive` (the live list, the machine chips) |
| Chief | Talk to the chief, see the focus | `renderFocus`, the chief card, the last exchange with the chief |
| Projects | Departments by status, search | `renderOrg` (as a list, not a chart), `renderTable` |
| Spend | Range, totals, by project, over time | `renderTiles`, `renderBars`, `renderColumns`, the range control |

Two detail screens push over a tab. They have a Back button and one browser history entry.

| Screen | Opens from | Content from the current page |
|---|---|---|
| Team detail | Inbox, Teams, Project detail | `renderDrawer` for a team: the agent, the last prompt, the last reply, the composer |
| Project detail | Inbox, Projects, Team detail (the "Project" tab) | `renderDrawer` for a department: status, focus, goal, teams, activity |

The theme switch and the refresh button move to a small menu on the Chief screen. The time range control lives only on the Spend screen.

## 3. Routing

Use hash routes, so that the service worker and `start_url` stay as they are.

| Route | Screen |
|---|---|
| `#/inbox` (default for `/`) | Inbox |
| `#/teams` | Teams |
| `#/teams/<teamKey>` | Team detail |
| `#/chief` | Chief |
| `#/projects` | Projects |
| `#/projects/<deptKey>` | Project detail |
| `#/spend` | Spend |

Rules:

- A tap on a tab calls `history.replaceState`, so that the tab bar never fills the history.
- A tap on a row that opens a detail screen calls `history.pushState`. The Back button calls `history.back()`. The `popstate` handler renders the route in `location.hash`. This is the same mechanism that the drawer uses today (`openDrawer`, `closeDrawer`, `hideDrawer` in `app.js`), so move that code into one `route()` function.
- On load, render the route in the hash. If the hash names a team or a department that is not in the snapshot, fall back to the tab root.
- Keep the last selected tab in `localStorage` under `chief.tab`, so that the home screen app opens on it.

## 4. Breakpoints

- Phone layout at `max-width: 720px`: one tab visible at a time, the tab bar at the bottom, detail screens full-screen.
- Wide layout above 720px: the current page stays as it is in phase 1. The tab bar is hidden, and detail screens use the existing drawer. The same render functions serve both, so the data code does not fork.

## 5. The screens

Each phone screen has the same frame:

- A header with 54px top padding (the real status bar sits there), a `heading-3` title, and a one-line subtitle.
- A scroll area with 20px side padding and 20px between sections.
- The bottom tab bar: 5 items, 48px tall plus the safe area (`env(safe-area-inset-bottom)`), a 24px icon over a 12px label. The active icon takes the accent color and the active label is medium weight. The Inbox tab shows a count badge for items that need the user.

### Inbox

- Sections in this order: Needs you, Teams with no work, Deadlines, Spend, Drift, Housekeeping, Questions for you. Hide empty sections. Reuse `AGENDA_SECTIONS` and the `kind` of each item.
- An item that needs the user is a card on the warning tint (`--warn-wash`). It shows the status dot, the team name, the machine badge, the time, the detail text (two lines), and the hint.
- Actions on a card: "Reply" (accent, opens Team detail and focuses the composer) and "Open team" (secondary). For a permission prompt there is no Reply, because a message cannot answer a prompt.
- Idle teams and deadlines are rows with a chevron. A row opens Team detail or Project detail.

### Teams

- Machine chips in the header, from `snap.nodes`.
- A segmented control: All, Needs you, Working, Idle. It filters the list. Reuse `LIVE_GROUPS`.
- Groups by status, each row: dot, name, machine badge, time, one line of text from `liveText`, and a meta line (agents, sub-agents, Remote Control).
- A row opens Team detail.

### Chief

- Header: the title, a status pill from `snap.chief.status`, and a subtitle with the machine, the spend today, and the date of the last meeting notes.
- Focus block: the "today" and "week" departments. "Change" opens the Projects tab.
- The last exchange with the chief as two bubbles (the user's prompt on the right, the reply on the left), from `lastUser` and `lastAssistant` of the chief agent.
- Quick action buttons that fill the composer with a fixed text: "Start the meeting" (`/chief`), "Assign idle teams", "Brief me".
- The composer is pinned above the tab bar. It posts to `/api/message` with the chief's team key, as the current chief card does.

### Projects

- A search input that filters by name, folder, and goal.
- Sections: Active, Semi-active, Backlog and archived (as chips). The Active section shows the first five rows and a "Show N more" link.
- A row: a 4px swatch in the department color, the name, a focus badge, the spend in the range, the goal or description, and a meta line (teams, machine, team status, last activity). A row that needs the user takes the warning tint.
- A row opens Project detail.

### Spend

- The range control (24h, 7 days, 30 days) is a segmented control. It sets `state.range` and calls `load()`, as the top bar does today.
- Two tiles: the spend with the token count, and the agent counts.
- By project: rows with a name, a bar, and a value. Draw the bars as `div` elements, not SVG, so that they fit any width. A row opens Project detail.
- Over time: stacked columns as `div` elements, one column per bucket, with an axis row and a legend. Reuse the series from `renderColumns`.

### Team detail

- Header: a Back link to the tab that opened it, the team name, a status pill, a subtitle (machine, task, agent count, spend).
- A segmented control with one entry per agent (Coordinator, worker titles) and a last entry "Project" that opens Project detail. With one agent, show only that agent and "Project".
- The agent card: the title, the harness, the model, the surface, and the sub-agent count.
- The last exchange as bubbles. When the last reply ends with a question, label the reply "the reply ends with a question" and show up to two quick-reply buttons from the sentence options when the server gives them. Phase 1 shows the bubbles only.
- The composer is pinned at the bottom. The tab bar is hidden on this screen. The composer shows the recipient, the last sent message and its result, and the reason when the server refuses a message.
- Keep the draft in `state.drafts[teamKey]`, as today, so that a refresh does not clear the box.

### Project detail

- Header: a Back link, the name, the focus badge, the paths.
- Status and Focus as segmented controls. A tap posts to `/api/department` as `setDept` does today.
- Three stat tiles: spend in range, last activity, open teams.
- The goal, the next step, and the deadline in one card, with "Ask the chief" that opens the Chief tab.
- The 30-day activity strip as 30 `div` cells with a level class.
- Teams as rows that open Team detail. The last commit as a card.
- A full-width accent button "Message the coordinator" at the bottom opens Team detail with the composer focused. With more than one team, it opens a list to pick the team.

## 6. Behaviors to keep

- The quiet refresh every 5 seconds with `If-None-Match` stays. A re-render must never replace the composer while it has focus. Today the composer lives outside the re-rendered body for this reason. Keep that structure: the screen body re-renders, the composer and the tab bar do not.
- A message goes to one agent. The server refuses it when the agent waits at a permission prompt, and the screen shows the reason next to the composer.
- "Jump" is a desktop action. It stays hidden on touch screens (`@media (hover: none) and (pointer: coarse)`).
- Tooltips hide on a touch tap elsewhere and on scroll.
- Every control is a real `button`, `a`, `input`, or `textarea`. Touch targets are 44px or taller.

## 7. Visual tokens

The dashboard keeps its own tokens in `web/style.css`. Map the mockup tokens to them like this, or adopt the mockup values. The structure is what matters.

| Mockup token | Dashboard token | Note |
|---|---|---|
| `surface1` | `--page`, `--surface` | Page and card background |
| `surface2` | `--surface-2` | Rows, inputs, tiles |
| `surface3` | `--ring`, `--hover` | Hairlines and subtle fills |
| `neutral1` | `--ink` | Primary text |
| `neutral2` | `--ink-2` | Secondary text, 4.5:1 or better |
| `neutral3` | `--muted` | Placeholders and disabled text only |
| `accent1` | `--accent` | One primary action per screen, the active tab icon, the working dot |
| `warning`, `warning-secondary` | `--st-warning`, `--warn-wash` | Needs you |
| `success` | `--st-good` | Idle |
| `radius-16` | `--radius` | Rows, cards, tab containers |
| `heading-3` (24px) | `h1` | Screen titles |
| `body-2` (16px) | body | Row names and the composer text. Use 16px in inputs, so that iOS does not zoom. |
| `body-3` (14px), `body-4` (12px) | 13px, 12px | Secondary text and meta lines |

Both themes stay. Every token has a light and a dark value, as today.

## 8. Phases

1. Routing and the tab bar at phone width. Move the sections into the five tabs. Turn the drawer into the two full-screen detail screens on the phone. No new data.
2. The Chief screen with the last exchange and the quick actions. The Spend charts as `div` elements.
3. Optional server work: a `thread` field per agent in `/api/snapshot` with the last N exchanges, so that Team detail and Chief show a longer conversation. Quick-reply options for a reply that ends with a question.

## 9. Acceptance checklist

- At 390px width, each tab shows only its own content, and the tab bar stays at the bottom while the content scrolls.
- A detail screen opens with a push, and the phone's back gesture closes it. The Back button does the same.
- A reload on a detail route shows that screen.
- A draft in the composer survives a data refresh. Focus stays in the composer.
- A refused message shows the reason from the server.
- Above 720px, the page looks as it does today.
- No text under 4.5:1 contrast in either theme, except placeholders.
- `node --check web/app.js` passes, and the headless phone checks from the last change still pass (open a card, type a draft, wait 6 seconds, go back, use the Back button).
