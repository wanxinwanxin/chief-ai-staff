# Mobile redesign: implementation guide (round 2)

The design canvas has three tab screens, two detail screens, and one information architecture map.

Status: round 1 (five tabs: Inbox, Teams, Chief, Projects, Spend) is in `web/`. Round 2 replaces the first four tabs with two: Today and Org. Spend, Team detail and Project detail stay as built. Section 10 lists the changes from round 1.

## 1. The problem with round 1

Four of the five tabs showed the same team rows in different orders. The org chart was gone. The Chief tab looked like the detail view of one more team, so its job was unclear.

Round 2 gives each tab a different job and a different shape:

- **Today** is the chief's briefing. The chief says what needs you. Each item is an action card shaped for its kind, with the action on the card. The message box at the bottom talks to the chief.
- **Org** is the org chart as a tree: projects, their teams, their agents. It is the one list of sessions in the app.
- **Spend** stays as built.

## 2. The information architecture

| Tab | Job | Content from `app.js` |
|---|---|---|
| Today | What needs you, and the chief to delegate to | `snap.agenda.items` as action cards, `snap.chief` for the summary line and the composer, `snap.departments` with `focus` for the focus line |
| Org | The structure: projects, teams, agents, with a status filter | `snap.departments` grouped by `status`, `d.teams` through `teamByKey`, `t.agents` |
| Spend | Range, totals, by project, over time | `phoneSpend` as built |

Detail screens push over a tab with a Back button and one history entry, as built: `phoneTeam` and `phoneDept`.

## 3. Routing

| Route | Screen |
|---|---|
| `#/today` (default for `/`) | Today |
| `#/org` | Org |
| `#/org/teams/<teamKey>` | Team detail |
| `#/org/projects/<deptKey>` | Project detail |
| `#/spend` | Spend |

Keep the old hashes as aliases for one release: `#/inbox` and `#/chief` open Today, `#/teams` and `#/projects` open Org, `#/teams/<key>` and `#/projects/<key>` open the detail screens. The `route()`, `pushState` and `replaceState` rules from round 1 stay.

## 4. Breakpoints

Unchanged: phone layout at 720px and less, the wide layout keeps the one-page dashboard with the drawer.

## 5. Today

Header: the title "Today", a pill with the chief's status and machine (`snap.chief`), and a subtitle with the date, the count of items that need you, and the count of teams with no work. The "…" menu holds the theme switch and the refresh button.

### The chief's line

One bubble from the chief, with the "CoS" avatar and the label "Chief · <time>". Phase 1 builds the text from the agenda counts and the focus departments:

> Two things wait for you, and three teams have no work. Focus today: Chief ai staff and Docs site. Act on a card below, or tell me what to do.

Phase 3 replaces it with the chief's own last reply when that reply is newer than the snapshot's agenda.

### Action cards

One card per agenda item, in the agenda's order (`AGENDA_SECTIONS`). The card's head is always the same: a kind pill, the name, the machine badge, the time. The body and the actions depend on the kind. This is the part that must differ from the Org rows.

| Kind (`it.kind`, `waitReason`) | Body | Actions |
|---|---|---|
| Question (`blocked`, `question`) | The end of the last reply (`lastAssistantTail`) as a bubble | An inline one-line reply field and Send. It posts to `/api/message` for that agent without leaving Today. "Open the team" as a link. |
| Permission prompt (`blocked`, `prompt`) | The last lines of the agent's screen in monospace (phase 3; phase 1 shows "Waits at a permission prompt.") | "Open the team". No reply field: a message cannot select an option. Show "Remote Control on" when it is, so the user knows the Claude app can answer it. |
| Team with no work (`idle`) | What the team finished (the last reply, two lines) and the project's goal or "No goal is set for this project." | "Give a task" (accent) opens Team detail with the composer focused. "Let the chief assign" posts a fixed message to the chief: `Give <team> its next task from the goal of <project>.` |
| Deadline (`deadline`) | The next step and the last commit time | "Open the project" |
| Spend, drift, housekeeping, question for you | The agenda detail text | "Open the project" or "Open the team", as the item has a key |

A card that needs you takes the warning tint. The other cards sit on `surface2`.

### Composer

Pinned above the tab bar. Two quick buttons fill the box: "Start the meeting" (`/chief`) and "Brief me". The box posts to the chief's team, as the round 1 Chief tab does. The draft lives in `state.drafts.chief`.

## 6. Org

Header: the title "Org", the machine chips, a subtitle with the counts, and a segmented filter: All, Needs you, Working, Idle. The filter prunes the tree to the projects that have a team in that state and expands those projects.

The tree:

1. **You** (CEO), then **Chief of staff** under a short rail. The chief node opens Today.
2. **Active** group. One node per active department: the color swatch, the name, the focus badge, the team status pill, the spend in the range, and a toggle. The name opens Project detail. The toggle expands the teams.
3. Under an expanded department: one node per team, with the status dot, "Team · <task>", the machine badge. The node opens Team detail. Under a team: one node per agent, with the dot, the role, the title, the spend. The node opens Team detail with that agent picked.
4. A collapsed department shows one meta line: "1 team on Cloud · 2 agents · Remote Control on", or "No open team".
5. **Semi-active**, **Backlog**, **Archived** as collapsed group rows with counts. A tap expands the group into department nodes without teams.

Rails: each child list has a 1.5px left border and a 12px tick per child (`.kids`, `.kids > *::before` in the mockup's `phone.css`). Indent is 29px per level, so four levels fit at 390px.

State to keep in `localStorage`: the open departments (`chief.orgOpen`) and the filter (`chief.orgFilter`). Default: projects with a team that needs you or works are open, the rest are closed.

Search: a search field is not in the mockup. Add it only when the active list goes past about 20 projects.

## 7. Team detail and Project detail

Unchanged from round 1, with two changes:

- Back goes to Org.
- "Ask the chief" on Project detail opens Today with the draft `About <project>: `.

## 8. Behaviors to keep

Unchanged from round 1: the quiet refresh, the composer outside the re-rendered body, the refusal reason from `/api/message`, Jump hidden on touch screens, tooltips that hide on a tap elsewhere, real controls with 44px targets.

New: the inline reply field on a question card is also outside the re-rendered body, or the body skips the card that holds the focused field. Keep its draft in `state.drafts[teamKey]`.

## 9. Visual tokens

Unchanged from round 1. The token map from round 1 applies. New styles in the mockup: `.act` (action card), `.screen` (monospace excerpt), `.tree`, `.node`, `.kids`, `.toggle`, `.group`.

## 10. Changes from round 1, in order

1. Routes: add `#/today` and `#/org`, keep the old hashes as aliases.
2. Tab bar: three tabs. Icons: the chat bubble for Today, the tree for Org, the bars for Spend. The count badge moves to Today.
3. Today: build `phoneToday` from `phoneInbox` and `phoneChief`. Add the chief's line, the action cards by kind, the inline reply on question cards, and the "Let the chief assign" action.
4. Org: build `phoneOrg` from `phoneTeams` and `phoneProjects`. Reuse `renderOrg`'s grouping and `teamRow`. Add the toggles, the rails, and the filter.
5. Remove `phoneInbox`, `phoneTeams`, `phoneChief`, `phoneProjects` once Today and Org pass the checklist.
6. Phase 3 (server): `screen` tail for an agent that waits at a prompt (the hub has `tmux.screen`), and the chief's last reply in `snap.chief` for the Today line.

## 11. Acceptance checklist

- At 390px width, Today shows the chief's line, then cards whose bodies differ by kind. A question card sends a reply without leaving Today.
- Org shows the tree with rails. A toggle expands a project to its teams and agents. The filter "Needs you" leaves only the projects with such a team, expanded.
- The tab bar has three tabs, and the badge on Today shows the count of items that need you.
- A detail screen opens with a push, and Back returns to the tab that opened it.
- A reload on `#/inbox`, `#/chief`, `#/teams` or `#/projects` opens the matching new tab.
- A draft in any composer survives a data refresh.
- Above 720px, the page looks as it does today.
- `node --check web/app.js` passes, and the headless phone checks pass with `/api/message` intercepted.
