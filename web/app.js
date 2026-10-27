"use strict";

// ---------------------------------------------------------------------------
// Small DOM helpers. All data text goes in through text nodes, never innerHTML.

const SVGNS = "http://www.w3.org/2000/svg";
const $ = (id) => document.getElementById(id);

function setAttrs(el, attrs) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "style" && typeof v === "object") {
      for (const [sk, sv] of Object.entries(v)) {
        if (sk.startsWith("--")) el.style.setProperty(sk, sv);
        else el.style[sk] = sv;
      }
    } else if (k.startsWith("on") && typeof v === "function") {
      el.addEventListener(k.slice(2), v);
    } else {
      el.setAttribute(k, v === true ? "" : v);
    }
  }
}
function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}
function replace(el, ...kids) {
  el.replaceChildren();
  append(el, kids);
}
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  setAttrs(el, attrs);
  append(el, kids);
  return el;
}
function s(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  setAttrs(el, attrs);
  append(el, kids);
  return el;
}

// ---------------------------------------------------------------------------
// Formatting

const money = (v) => {
  if (v == null) return "–";
  if (v >= 10000) return `$${(v / 1000).toFixed(0)}K`;
  if (v >= 1000) return `$${(v / 1000).toFixed(1)}K`;
  if (v >= 100) return `$${v.toFixed(0)}`;
  return `$${v.toFixed(2)}`;
};
const moneyAxis = (v) => (v >= 1000 ? `$${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}K` : `$${Math.round(v)}`);
const compact = (n) => {
  if (n == null) return "–";
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)}K`;
  return String(n);
};
const pct = (x) => (x == null || !isFinite(x) ? "–" : `${Math.round(x * 100)}%`);
const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;
function ago(ts) {
  if (!ts) return "never";
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}
function dueText(deadline) {
  if (!deadline) return null;
  const days = Math.round((new Date(`${deadline}T12:00:00`) - new Date(new Date().toDateString() + " 12:00:00")) / 86400000);
  const when = days < 0 ? `${-days}d late` : days === 0 ? "today" : `in ${days}d`;
  return `Due ${new Date(`${deadline}T12:00:00`).toLocaleDateString([], { month: "short", day: "numeric" })} (${when})`;
}
function modelLabel(m) {
  const match = /^claude-([a-z]+)-(\d+)(?:-(\d+))?/.exec(m || "");
  if (!match) return m;
  const name = match[1][0].toUpperCase() + match[1].slice(1);
  return `${name} ${match[2]}${match[3] ? "." + match[3] : ""}`;
}
const topModel = (a) => Object.keys(a.models || {})[0];
const STATUS_LABEL = { needs_input: "Needs you", working: "Working", idle: "Idle", ended: "Ended", none: "No team" };
const DEPT_LABEL = { active: "Active", "semi-active": "Semi-active", backlog: "Backlog", archived: "Archived" };
const FOCUS_LABEL = { today: "Today", week: "This week" };
const ITEM_LABEL = { "needs-you": "Needs you", "chief-can": "The chief can do it", info: "Info" };
const slotColor = (slot) => (slot == null ? "var(--other)" : `var(--s${slot + 1})`);

function statusEl(status) {
  return h("span", { class: `status ${status}` }, h("i", { class: `dot ${status}` }), STATUS_LABEL[status] || status);
}

// ---------------------------------------------------------------------------
// State, persistence, data loading

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(`chief.${key}`);
    localStorage.setItem(`chief.${key}`, value);
  } catch (_) { /* Storage can be blocked. The page still works. */ }
  return null;
}

const state = {
  range: store("range") || "7d",
  snap: null,
  sig: null,
  etag: null,
  openKey: null,
  show: { backlog: false, archived: false, table: false },
  drafts: {},
  composerSig: null,
};

// A background refresh is quiet: it does not dim the page, and it re-renders
// only when the data changed. A user action (range, refresh button) dims the page.
// The server answers 304 when nothing changed, so a quiet refresh every few
// seconds costs little, also on a phone.
async function load(fresh, quiet) {
  if (!quiet) $("app").classList.add("loading");
  try {
    const headers = quiet && !fresh && state.etag ? { "If-None-Match": state.etag } : {};
    const res = await fetch(`/api/snapshot?range=${state.range}${fresh ? "&fresh=1" : ""}`, { headers, cache: "no-store" });
    if (res.status === 304) {
      state.checkedAt = Date.now();
      renderSync();
      return;
    }
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    const sig = JSON.stringify({ ...data, generatedAt: 0, nodes: (data.nodes || []).map((n) => ({ ...n, at: 0 })) });
    const changed = sig !== state.sig;
    state.snap = data;
    state.sig = sig;
    state.etag = res.headers.get("ETag");
    state.checkedAt = Date.now();
    if (changed || !quiet) render();
    renderSync();
  } catch (err) {
    $("sync").textContent = `Could not refresh: ${err.message}`;
  } finally {
    $("app").classList.remove("loading");
  }
}

function nodeState(n) {
  if (n.local || n.online) return "live";
  return `offline, data from ${ago(n.at)}`;
}

function renderSync() {
  const snap = state.snap;
  if (!snap) return;
  const t = new Date(state.checkedAt || Date.now()).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const machines = (snap.nodes || []).map((n) => `${n.label} ${nodeState(n)}`).join(" · ");
  $("sync").textContent = `Checked ${t} · ${machines}`;
}

const multiNode = () => (state.snap?.nodes || []).length > 1;
const canJump = (t) => Boolean(t && (t.terminal === "cmux" || t.mirror));
const nodeBadge = (t) => multiNode() && t?.nodeLabel ? h("span", { class: `node-badge ${t.node}` }, t.nodeLabel) : null;

async function post(path, body) {
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Chief": "1" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok && data.ok !== false, output: data.output || data.error || (res.ok ? "" : `HTTP ${res.status}`) };
  } catch (err) {
    return { ok: false, output: err.message };
  }
}
const jump = (workspaceId, surfaceId) => post("/api/jump", { workspaceId, surfaceId });

async function setDept(key, fields) {
  if ((await post("/api/department", { key, ...fields })).ok) await load(true);
}

const deptByKey = (key) => (state.snap?.departments || []).find((d) => d.key === key) || null;
const teamByKey = (key) => {
  const snap = state.snap;
  if (!snap) return null;
  if (snap.chief && snap.chief.key === key) return snap.chief;
  return snap.teams.find((t) => t.key === key) || null;
};

// ---------------------------------------------------------------------------
// Tooltip

const tip = $("tooltip");
function showTip(evt, title, rows) {
  tip.replaceChildren(
    h("div", { class: "tt-title" }, title),
    ...rows.map((r) => h("div", { class: "tt-row" },
      h("i", { class: "tt-key", style: { background: r.color || "transparent" } }),
      h("b", null, r.value),
      h("span", null, r.label))),
  );
  tip.hidden = false;
  moveTip(evt);
}
function moveTip(evt) {
  const pad = 14;
  const r = tip.getBoundingClientRect();
  let x = evt.clientX + pad;
  let y = evt.clientY + pad;
  if (x + r.width > innerWidth - 8) x = evt.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = evt.clientY - r.height - pad;
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top = `${Math.max(8, y)}px`;
}
let tipOwner = null;
function hideTip() { tip.hidden = true; tipOwner = null; }
document.addEventListener("pointerdown", (e) => {
  if (e.pointerType === "touch" && tipOwner && !tipOwner.contains(e.target)) hideTip();
}, true);
addEventListener("scroll", hideTip, true);
function hoverable(el, titleFn, rowsFn) {
  el.addEventListener("pointerenter", (e) => { tipOwner = el; showTip(e, titleFn(), rowsFn()); });
  el.addEventListener("pointermove", moveTip);
  el.addEventListener("pointerleave", hideTip);
  el.addEventListener("focus", () => {
    const b = el.getBoundingClientRect();
    showTip({ clientX: b.right, clientY: b.top }, titleFn(), rowsFn());
  });
  el.addEventListener("blur", hideTip);
}

// ---------------------------------------------------------------------------
// Render

function render() {
  const snap = state.snap;
  renderLive(snap);
  renderAgenda(snap);
  renderFocus(snap);
  renderTiles(snap);
  renderOrg(snap);
  renderBars(snap);
  renderColumns(snap);
  renderTable(snap);
  $("note").textContent = `${snap.note} Departments are the folders in ${snap.roots.join(", ")}.`;
  renderChiefCard(snap);
  if (state.openKey) renderDrawer();
}

const AGENDA_SECTIONS = [
  ["blocked", "Blocked by you"], ["deadline", "Deadlines"], ["idle", "Teams with no work"],
  ["spend", "Spend"], ["drift", "Drift"], ["housekeeping", "Housekeeping"],
  ["question", "Questions for you"], ["focus", "Focus"],
];

// Agents write Markdown. Remove the emphasis marks for one-line previews.
const plain = (text) => String(text).replace(/\*\*|__|`/g, "").replace(/^#+\s*/gm, "");

// ---------------------------------------------------------------------------
// Teams now: every team on every machine, by status. This is the live view.

const LIVE_GROUPS = [["needs_input", "Needs you"], ["working", "Working"], ["idle", "Idle"], ["ended", "Ended"]];

function liveText(t) {
  const a = t.agents.find((x) => x.status === "needs_input")
    || t.agents.find((x) => x.status === "working")
    || t.agents.find((x) => x.role === "coordinator") || t.agents[0];
  if (!a) return null;
  if (a.status === "needs_input") {
    return a.waitReason === "prompt" ? "Waits at a permission prompt." : plain((a.lastAssistantTail || [])[1] || "Asked you a question.");
  }
  if (a.status === "working") return a.lastUser ? `Working on: ${plain(a.lastUser[1])}` : "Working.";
  return a.lastAssistant ? plain(a.lastAssistant[1]) : null;
}

function liveTeam(t) {
  const open = () => openDrawer(t.key);
  const text = liveText(t);
  const subsRunning = t.agents.reduce((n, a) => n + a.subagents.filter((x) => x.running).length, 0);
  return h("li", {
    class: `live-team ${t.status}`, tabindex: 0, onclick: open,
    onkeydown: (e) => { if (e.key === "Enter") open(); },
  },
  h("i", { class: `dot ${t.status}` }),
  h("div", { class: "live-body" },
    h("div", { class: "live-title" },
      h("span", { class: "live-name" }, t.name), nodeBadge(t),
      h("span", { class: "live-task" }, t.task)),
    text && h("div", { class: "live-text" }, text),
    h("div", { class: "live-meta" }, [
      plural(t.agents.length, "agent"),
      subsRunning ? `${subsRunning} sub-agents running` : null,
      t.agents.some((a) => a.remoteControl) ? "Remote Control on" : null,
    ].filter(Boolean).join(" · "))),
  h("span", { class: "live-when" }, ago(t.lastActivity)));
}

function renderLive(snap) {
  const el = $("live");
  const a = snap.totals.agents;
  const teams = snap.teams.filter((t) => t.agents.length);
  el.replaceChildren(h("div", { class: "card-head" },
    h("h2", null, "Teams now"),
    h("p", { class: "hint" }, `${a.working} working · ${a.needs_input} need you · ${a.idle} idle`),
    h("div", { class: "machines" }, (snap.nodes || []).map((n) => h("span", {
      class: `machine ${n.local || n.online ? "on" : "off"}`, title: `${n.terminal} · ${n.teams} teams · ${n.agents} agents`,
    }, h("i", null), `${n.label} · ${nodeState(n)}`)))));
  if (!teams.length) el.append(h("p", { class: "empty" }, "No agent runs now."));
  for (const [status, title] of LIVE_GROUPS) {
    const group = teams.filter((t) => t.status === status);
    if (!group.length) continue;
    el.append(h("div", { class: "live-group" },
      h("div", { class: "eyebrow" }, `${title} · ${group.length}`),
      h("ul", { class: "live-list" }, group.map(liveTeam))));
  }
  const c = snap.chief;
  el.append(h("p", { class: "ask" }, !c ? "The chief is not running. "
    : c.agents.length ? `Chief of Staff: ${(STATUS_LABEL[c.status] || c.status).toLowerCase()} on ${c.nodeLabel}. `
      : `The chief session on ${c.nodeLabel} has no running agent. `,
  "Send it a message below, open the \"Chief of Staff\" session in the Claude app, or type ", h("code", null, "/chief"), " in any session."));
}

function agendaItem(it) {
  const d = it.departmentKey && deptByKey(it.departmentKey);
  const team = it.teamKey && teamByKey(it.teamKey);
  return h("li", { class: `agenda-item ${it.kind}` },
    h("span", { class: `pill ${it.label}` }, h("i", { "aria-hidden": "true" }), ITEM_LABEL[it.label]),
    h("div", { class: "agenda-body" },
      h("div", { class: "agenda-title" }, it.title),
      it.detail && h("div", { class: "agenda-detail" }, plain(it.detail)),
      it.hint && h("div", { class: "agenda-hint" }, it.hint)),
    h("div", { class: "agenda-actions" },
      canJump(team) && h("button", { class: "btn jump", onclick: () => jump(it.workspaceId, it.surfaceId) }, "Jump ↗"),
      team && h("button", { class: "btn", onclick: () => openDrawer(team.key) }, "Team"),
      d && h("button", { class: "btn", onclick: () => openDrawer(d.key) }, "Details")));
}

function renderAgenda(snap) {
  const el = $("agenda");
  const ag = snap.agenda;
  const c = ag.counts;
  el.replaceChildren(h("div", { class: "card-head" },
    h("h2", null, "Agenda"),
    h("p", { class: "hint" }, `${c["needs-you"]} need you · the chief can do ${c["chief-can"]}`)));
  if (!ag.items.length) {
    el.append(h("p", { class: "empty" }, "Nothing on the agenda. Every team has work, and nothing waits for you."));
  }
  for (const [kind, title] of AGENDA_SECTIONS) {
    const group = ag.items.filter((it) => it.kind === kind);
    if (!group.length) continue;
    el.append(h("div", { class: "agenda-section" },
      h("div", { class: "eyebrow" }, title),
      h("ol", { class: "agenda-list" }, group.map(agendaItem))));
  }
  el.append(h("p", { class: "ask" }, "Talk it through with the chief: send it a message above, open the \"Chief of Staff\" session in the Claude app, or type ",
    h("code", null, "/chief"), " in Claude Code or ", h("code", null, "$chief"), " in Codex."));
}

function renderFocus(snap) {
  const el = $("focus");
  el.replaceChildren(h("div", { class: "card-head" }, h("h2", null, "Focus")));
  for (const level of ["today", "week"]) {
    const list = snap.departments.filter((d) => d.focus === level);
    const limit = level === "today" ? 2 : 5;
    el.append(h("div", { class: "focus-group" },
      h("div", { class: "eyebrow" }, `${FOCUS_LABEL[level]} · ${list.length} of ${limit}`),
      list.length
        ? h("ol", { class: "focus-list" }, list.map((d) => h("li", {
          class: "focus-item", tabindex: 0, onclick: () => openDrawer(d.key),
          onkeydown: (e) => { if (e.key === "Enter") openDrawer(d.key); },
        },
        h("i", { class: "swatch", style: { background: slotColor(d.colorSlot) } }),
        h("div", null,
          h("div", { class: "focus-title" }, d.name),
          d.next && h("div", { class: "focus-next" }, h("b", null, "Next: "), d.next),
          (d.goal || d.deadline) && h("div", { class: "focus-why" },
            [d.goal, dueText(d.deadline)].filter(Boolean).join(" · "))))))
        : h("p", { class: "empty" }, "Not set. The chief asks at the end of the meeting.")));
  }
}

function sparkline(values, w, hgt) {
  const max = Math.max(...values, 0.0001);
  const step = values.length > 1 ? w / (values.length - 1) : w;
  const pts = values.map((v, i) => [i * step, hgt - 3 - (v / max) * (hgt - 6)]);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  const last = pts[pts.length - 1] || [0, hgt];
  return s("svg", { viewBox: `0 0 ${w} ${hgt}`, width: "100%", height: hgt, "aria-hidden": "true", preserveAspectRatio: "none" },
    s("path", { d, fill: "none", stroke: "var(--muted)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", "vector-effect": "non-scaling-stroke" }),
    s("circle", { cx: last[0], cy: last[1], r: 3, fill: "var(--accent)" }));
}

function renderTiles(snap) {
  const t = snap.totals;
  const a = t.agents;
  const dc = t.departments;
  const series = snap.range.buckets.map((b) => t.series[b] || 0);
  const tile = (label, value, sub, extra) => h("article", { class: "card tile" },
    h("div", { class: "tile-label" }, label), h("div", { class: "tile-value" }, value),
    h("div", { class: "tile-sub" }, sub), extra);
  $("tiles").replaceChildren(
    tile("Needs you", String(snap.agenda.counts["needs-you"]), `The chief can do ${snap.agenda.counts["chief-can"]} more`),
    tile("Agents working", String(a.working), `${a.needs_input} wait for you · ${a.idle} idle · ${plural(snap.teams.length, "team")}`),
    tile("Active departments", String(dc.active), `${dc["semi-active"]} semi-active · ${dc.backlog} backlog · ${dc.archived} archived`),
    tile("Spend (API-equivalent)", money(t.costUsd), `${compact(t.tokens.total)} tokens`, sparkline(series, 200, 24)),
  );
}

// ---------------------------------------------------------------------------
// Activity strip: one cell for each of the last 30 days. One hue, light to dark.

function activityStrip(d, big) {
  const cellW = big ? 12 : 6;
  const gap = 2;
  const hgt = big ? 22 : 10;
  const W = d.daily.length * (cellW + gap) - gap;
  const level = (x) => {
    const v = x.agentHours + x.commits;
    return v === 0 ? 0 : v < 2 ? 1 : v < 5 ? 2 : v < 10 ? 3 : 4;
  };
  const svg = s("svg", { class: "strip", viewBox: `0 0 ${W} ${hgt}`, width: W, height: hgt, role: "img",
    "aria-label": `Activity in the last 30 days for ${d.name}` });
  d.daily.forEach((x, i) => {
    const r = s("rect", { x: i * (cellW + gap), y: 0, width: cellW, height: hgt, rx: 2, class: `lv${level(x)}` });
    if (big) hoverable(r, () => new Date(`${x.d}T12:00:00`).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }), () => [
      { value: String(x.agentHours), label: "agent hours" },
      { value: String(x.commits), label: "your commits" },
      { value: money(x.costUsd), label: "spend" },
    ]);
    svg.append(r);
  });
  if (!big) {
    const days = d.daily.filter((x) => level(x) > 0).length;
    const hours = d.daily.reduce((acc, x) => acc + x.agentHours, 0);
    const commits = d.daily.reduce((acc, x) => acc + x.commits, 0);
    hoverable(svg, () => `${d.name} · last 30 days`, () => [
      { value: String(days), label: "days with activity" },
      { value: String(hours), label: "agent hours" },
      { value: String(commits), label: "your commits" },
    ]);
  }
  return svg;
}

// ---------------------------------------------------------------------------
// Org chart

function agentRow(t, a) {
  const model = topModel(a);
  const why = a.waitReason === "question" ? "Asked you a question" : STATUS_LABEL[a.status];
  const go = () => (canJump(t) ? jump(t.workspaceId, a.surfaceId) : openDrawer(t.key));
  return h("li", {
    class: "agent", tabindex: 0, title: `${why}. ${canJump(t) ? "Jump to this tab." : "Show details."}`,
    onclick: (e) => { e.stopPropagation(); go(); },
    onkeydown: (e) => { if (e.key === "Enter") { e.stopPropagation(); go(); } },
  },
  h("i", { class: `dot ${a.status}` }),
  h("span", { class: "agent-title" }, a.title, h("small", null, `${a.role === "coordinator" ? "coordinator" : "worker"} · ${model ? modelLabel(model) : a.kind}`)),
  h("span", { class: "agent-cost" }, money(a.costUsd)));
}

function teamBlock(t) {
  const subs = t.agents.flatMap((a) => a.subagents);
  const running = subs.filter((x) => x.running).length;
  return h("div", { class: "team" },
    h("div", { class: "team-head" }, h("span", { class: "team-name", title: t.task }, `Team · ${t.task}`), nodeBadge(t), statusEl(t.status)),
    t.agents.length
      ? h("ul", { class: "roster" }, t.agents.map((a) => agentRow(t, a)))
      : h("div", { class: "empty" }, "No agent runs in this tab."),
    (subs.length > 0 || t.earlier.sessions > 0) && h("div", { class: "proj-foot" },
      subs.length > 0 && h("span", { class: "chip" },
        h("span", { class: "subdots" }, subs.slice(0, 8).map((x) => h("i", { class: x.running ? "on" : "" }))),
        `${plural(subs.length, "sub-agent")}${running ? ` (${running} running)` : ""}`),
      t.earlier.sessions > 0 && h("span", { class: "chip" }, plural(t.earlier.sessions, "earlier session"))));
}

function deptCard(d, snap) {
  const teams = d.teams.map(teamByKey).filter(Boolean);
  const open = () => openDrawer(d.key);
  const sub = d.goal ? `Goal: ${d.goal}` : d.description || d.path;
  return h("article", {
    class: `proj ${d.teamStatus}`, tabindex: 0, style: { "--swatch": slotColor(d.colorSlot) },
    onclick: open, onkeydown: (e) => { if (e.key === "Enter" && e.target === e.currentTarget) open(); },
  },
  h("div", { class: "proj-head" },
    h("div", { style: { minWidth: "0" } },
      h("div", { class: "proj-name" }, d.name, d.focus && h("span", { class: `badge ${d.focus}` }, FOCUS_LABEL[d.focus])),
      h("div", { class: "proj-task", title: sub }, sub)),
    statusEl(d.teamStatus)),
  h("div", { class: "proj-metrics" },
    h("span", { class: "proj-cost" }, money(d.costUsd)),
    h("span", null, [dueText(d.deadline), `active ${ago(d.lastActivity)}`].filter(Boolean).join(" · "))),
  activityStrip(d),
  ...teams.map(teamBlock),
  !teams.length && h("div", { class: "empty" }, "No open team."));
}

function smallCard(d) {
  const open = () => openDrawer(d.key);
  return h("article", {
    class: "proj small", tabindex: 0, style: { "--swatch": slotColor(d.colorSlot) },
    onclick: open, onkeydown: (e) => { if (e.key === "Enter") open(); },
  },
  h("div", { class: "proj-head" },
    h("div", { style: { minWidth: "0" } },
      h("div", { class: "proj-name" }, d.name),
      h("div", { class: "proj-task", title: d.goal || d.description || "" }, d.goal || d.description || d.folder)),
    h("span", { class: "hint" }, ago(d.lastActivity))),
  activityStrip(d));
}

function chipList(list, limit) {
  return h("div", { class: "dept-chips" }, list.slice(0, limit).map((d) => h("button", {
    class: "dept-chip", onclick: () => openDrawer(d.key), title: d.description || d.path,
  }, d.name, h("small", null, ago(d.lastActivity)))));
}

function section(title, list, body, toggle) {
  const suggested = list.filter((d) => d.statusSource === "suggested").length;
  return h("section", { class: "org-section" },
    h("div", { class: "org-section-head" },
      h("h3", null, title, h("span", { class: "count" }, String(list.length))),
      suggested > 0 && h("span", { class: "hint" }, `${suggested} suggested from activity. Confirm them in the meeting.`),
      toggle),
    body);
}

function renderOrg(snap) {
  const root = $("org");
  const c = snap.chief;
  const byStatus = (st) => snap.departments.filter((d) => d.status === st);
  const you = h("div", { class: "person", id: "node-you" },
    h("div", { class: "avatar" }, "You"),
    h("div", null, h("div", { class: "person-name" }, "You"), h("div", { class: "person-sub" }, "CEO · decides what matters")));
  const chief = h("div", {
    class: "person chief", id: "node-chief", tabindex: 0,
    onclick: () => (c ? openDrawer(c.key) : null),
  },
  h("div", { class: "avatar" }, "CoS"),
  h("div", null,
    h("div", { class: "person-name" }, "Chief of Staff"),
    h("div", { class: "person-sub" }, c ? [statusEl(c.status), ` · ${money(c.costUsd)}`] : "Type /chief, or run: chief start")));

  const active = byStatus("active");
  const grid = h("div", { class: "org-grid", id: "org-grid" }, active.map((d) => deptCard(d, snap)));
  const semi = byStatus("semi-active");
  const backlog = byStatus("backlog");
  const archived = byStatus("archived");
  const toggle = (key, label) => h("button", {
    class: "btn", onclick: () => { state.show[key] = !state.show[key]; renderOrg(state.snap); },
  }, state.show[key] ? "Show less" : label);

  replace(root,
    h("div", { class: "org-top" }, you, chief),
    section("Active departments", active, grid),
    semi.length > 0 && section("Semi-active", semi, h("div", { class: "org-grid small" }, semi.map(smallCard))),
    backlog.length > 0 && section("Backlog", backlog, chipList(backlog, state.show.backlog ? 999 : 12),
      backlog.length > 12 && toggle("backlog", `Show all ${backlog.length}`)),
    archived.length > 0 && section("Archived", archived,
      state.show.archived ? chipList(archived, 999) : h("p", { class: "hint" }, "Hidden. Open one to reactivate it."),
      toggle("archived", "Show")),
    snap.outside.projects.length > 0 && h("p", { class: "hint" },
      `Outside the portfolio folders: ${money(snap.outside.costUsd)} in ${plural(snap.outside.projects.length, "folder")} (${snap.outside.projects.slice(0, 4).map((o) => o.name).join(", ")}).`),
  );
  requestAnimationFrame(drawConnectors);
}

function drawConnectors() {
  const root = $("org");
  const grid = $("org-grid");
  const chief = $("node-chief");
  const youEl = $("node-you");
  if (!root || !grid || !chief) return;
  root.querySelector(".org-svg")?.remove();
  const rb = root.getBoundingClientRect();
  const rel = (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.left - rb.left, y: r.top - rb.top, w: r.width, h: r.height };
  };
  const ch = rel(chief);
  const you = rel(youEl);
  const parts = [];
  if (Math.abs(you.y - ch.y) < 6 && you.x + you.w < ch.x) {
    parts.push(`M${you.x + you.w},${you.y + you.h / 2} H${ch.x}`);
  }
  const cards = [...grid.children].map(rel);
  if (cards.length) {
    const rows = [];
    for (const c of cards) {
      const row = rows.find((r) => Math.abs(r.y - c.y) < 4);
      if (row) row.cards.push(c); else rows.push({ y: c.y, cards: [c] });
    }
    rows.sort((a, b) => a.y - b.y);
    const trunkX = rel(grid).x + 6;
    const busY = (r) => r.y - 14;
    const cx = ch.x + ch.w / 2;
    parts.push(`M${cx},${ch.y + ch.h} V${busY(rows[0])}`);
    parts.push(`M${trunkX},${busY(rows[0])} V${busY(rows[rows.length - 1])}`);
    rows.forEach((r, i) => {
      const centers = r.cards.map((c) => c.x + c.w / 2);
      const right = Math.max(...centers, i === 0 ? cx : 0);
      parts.push(`M${trunkX},${busY(r)} H${right}`);
      for (const x of centers) parts.push(`M${x},${busY(r)} V${r.y}`);
    });
  }
  root.prepend(s("svg", { class: "org-svg", width: rb.width, height: rb.height, "aria-hidden": "true" },
    s("path", { d: parts.join(" "), "stroke-linecap": "round" })));
}

// ---------------------------------------------------------------------------
// Charts

function barPath(x0, x1, y, b) {
  const w = x1 - x0;
  const r = Math.min(4, w / 2, b / 2);
  if (w <= 0.5) return "";
  return `M${x0},${y} H${x1 - r} A${r},${r} 0 0 1 ${x1},${y + r} V${y + b - r} A${r},${r} 0 0 1 ${x1 - r},${y + b} H${x0} Z`;
}

function renderBars(snap) {
  const el = $("bars");
  const TOP = 10;
  const spent = snap.departments.filter((d) => d.costUsd > 0).sort((a, b) => b.costUsd - a.costUsd);
  const rows = spent.slice(0, TOP).map((d) => ({ key: d.key, name: d.name, v: d.costUsd, d }));
  const rest = spent.slice(TOP).reduce((acc, d) => acc + d.costUsd, 0);
  if (rest > 0) rows.push({ name: `${spent.length - TOP} other departments`, v: rest, other: true });
  if (snap.outside.costUsd > 0) rows.push({ name: "Outside the portfolio", v: snap.outside.costUsd, other: true });
  if (snap.chief && snap.chief.costUsd > 0) rows.push({ name: "Chief of Staff", v: snap.chief.costUsd, other: true });
  const W = Math.max(280, el.clientWidth || 560);
  const rowH = 30;
  const bar = 14;
  const labelW = Math.min(170, W * 0.34);
  const valueW = 58;
  const H = rows.length * rowH + 6;
  const max = Math.max(1, ...rows.map((r) => r.v));
  const x = (v) => labelW + ((W - labelW - valueW) * v) / max;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "Spend by department, bar chart" });
  svg.append(s("line", { class: "baseline", x1: labelW, x2: labelW, y1: 0, y2: H }));
  rows.forEach((r, i) => {
    const y = i * rowH + (rowH - bar) / 2 + 3;
    const name = r.name.length > 24 ? `${r.name.slice(0, 23)}…` : r.name;
    const g = s("g", { tabindex: 0 },
      s("text", { class: "lbl", x: labelW - 10, y: y + bar / 2 + 4, "text-anchor": "end" }, name),
      s("path", { class: "mark", d: barPath(labelW, x(r.v), y, bar), fill: r.other ? "var(--other)" : "var(--accent)" }),
      s("text", { class: "lbl-strong", x: x(r.v) + 6, y: y + bar / 2 + 4 }, money(r.v)),
      s("rect", { class: "hit", x: 0, y: i * rowH + 3, width: W, height: rowH }));
    if (r.key) g.addEventListener("click", () => openDrawer(r.key));
    hoverable(g, () => r.name, () => (r.d ? [
      { value: money(r.v), label: "spend in range" },
      { value: compact(r.d.tokens.total), label: "tokens" },
      { value: plural(r.d.teams.length, "open team"), label: "" },
    ] : [{ value: money(r.v), label: "spend in range" }]));
    svg.append(g);
  });
  el.replaceChildren(svg);
  $("spend-hint").textContent = `Total ${money(snap.totals.costUsd)} in this range`;
}

function niceMax(v) {
  if (v <= 0) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(v)));
  const f = v / exp;
  const nice = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
  return nice * exp;
}

function bucketLabel(key, bucket) {
  if (bucket === "hour") return key.slice(11, 16);
  const d = new Date(`${key}T12:00:00`);
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function renderColumns(snap) {
  const el = $("columns");
  const buckets = snap.range.buckets;
  const series = snap.departments
    .filter((d) => d.colorSlot != null)
    .sort((a, b) => a.colorSlot - b.colorSlot)
    .map((d) => ({ name: d.name, color: slotColor(d.colorSlot), data: d.series, key: d.key }));
  const other = {};
  const addOther = (src) => { for (const [k, v] of Object.entries(src || {})) other[k] = (other[k] || 0) + v; };
  addOther(snap.outside.series);
  if (snap.chief) addOther(snap.chief.series);
  snap.departments.filter((d) => d.colorSlot == null).forEach((d) => addOther(d.series));
  series.push({ name: "Other", color: "var(--other)", data: other });
  const shown = series.filter((sr) => buckets.some((b) => (sr.data[b] || 0) > 0));

  $("legend").replaceChildren(...shown.map((sr) => h("span", null, h("i", { style: { background: sr.color } }), sr.name)));
  $("time-hint").textContent = snap.range.bucket === "hour" ? "Each column is one hour" : "Each column is one day";

  const W = Math.max(280, el.clientWidth || 900);
  const H = 240;
  const m = { l: 48, r: 8, t: 10, b: 26 };
  const totals = buckets.map((b) => shown.reduce((acc, sr) => acc + (sr.data[b] || 0), 0));
  const yMax = niceMax(Math.max(1, ...totals) * 1.05);
  const band = (W - m.l - m.r) / buckets.length;
  const colW = Math.max(3, Math.min(24, band * 0.62));
  const y = (v) => H - m.b - (v / yMax) * (H - m.t - m.b);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "Spend over time by department, stacked columns" });
  for (let i = 0; i <= 4; i++) {
    const v = (yMax / 4) * i;
    svg.append(s("line", { class: i === 0 ? "baseline" : "gridline", x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }));
    svg.append(s("text", { x: m.l - 8, y: y(v) + 4, "text-anchor": "end" }, moneyAxis(v)));
  }
  const labelEvery = Math.ceil(buckets.length / Math.max(2, Math.floor((W - m.l) / 56)));
  buckets.forEach((b, i) => {
    const cx = m.l + band * i + band / 2;
    const x0 = cx - colW / 2;
    const g = s("g", { tabindex: 0 });
    let acc = 0;
    const segs = shown.map((sr) => ({ sr, v: sr.data[b] || 0 })).filter((d) => d.v > 0);
    segs.forEach((d, j) => {
      const yTop = y(acc + d.v);
      const yBot = y(acc) - (j > 0 ? 2 : 0); // 2px surface gap between stacked segments
      acc += d.v;
      const hgt = yBot - yTop;
      if (hgt <= 0.5) return;
      const top = j === segs.length - 1;
      const r = top ? Math.min(4, colW / 2, hgt) : 0;
      const d2 = r
        ? `M${x0},${yBot} V${yTop + r} A${r},${r} 0 0 1 ${x0 + r},${yTop} H${x0 + colW - r} A${r},${r} 0 0 1 ${x0 + colW},${yTop + r} V${yBot} Z`
        : `M${x0},${yBot} V${yTop} H${x0 + colW} V${yBot} Z`;
      g.append(s("path", { class: "mark", d: d2, fill: d.sr.color }));
    });
    g.append(s("rect", { class: "hit", x: m.l + band * i, y: m.t, width: band, height: H - m.t - m.b }));
    hoverable(g, () => bucketLabel(b, snap.range.bucket) + (snap.range.bucket === "hour" ? ` · ${b.slice(0, 10)}` : ""), () => {
      const rows = segs.slice().reverse().map((d) => ({ color: d.sr.color, value: money(d.v), label: d.sr.name }));
      return [{ value: money(totals[i]), label: "total" }, ...rows];
    });
    svg.append(g);
    if (i % labelEvery === 0) {
      svg.append(s("text", { x: cx, y: H - m.b + 17, "text-anchor": "middle" }, bucketLabel(b, snap.range.bucket)));
    }
  });
  el.replaceChildren(svg);
}

function renderTable(snap) {
  const all = state.show.table;
  const rows = snap.departments.filter((d) => all || d.status === "active" || d.status === "semi-active");
  const head = ["Department", "Status", "Focus", "Teams", "Spend", "Last activity", "Goal", "Deadline", "Next step"];
  const num = new Set([3, 4]);
  $("table-toggle").textContent = all ? "Show active and semi-active" : `Show all ${snap.departments.length}`;
  $("table").replaceChildren(h("table", null,
    h("thead", null, h("tr", null, head.map((t, i) => h("th", { class: num.has(i) ? "num" : null }, t)))),
    h("tbody", null, rows.map((d) => h("tr", { class: "clickable", onclick: () => openDrawer(d.key) },
      h("td", null, h("i", { class: "swatch", style: { background: slotColor(d.colorSlot) } }), d.name),
      h("td", null, DEPT_LABEL[d.status], d.statusSource === "suggested" && h("small", { class: "muted" }, " suggested")),
      h("td", null, d.focus ? FOCUS_LABEL[d.focus] : "–"),
      h("td", { class: "num" }, d.teams.length || "–"),
      h("td", { class: "num" }, d.costUsd ? money(d.costUsd) : "–"),
      h("td", null, ago(d.lastActivity)),
      h("td", { class: "wrap" }, d.goal || "–"),
      h("td", null, d.deadline || "–"),
      h("td", { class: "wrap" }, d.next || "–"))))));
}

// ---------------------------------------------------------------------------
// Detail drawer: a department, or the chief's own team.

// The open drawer is one entry in the browser history. The Back button of the
// drawer, the back gesture of the phone, and Escape all go back to that entry.
function openDrawer(key) {
  const current = history.state && history.state.drawer;
  if (current !== key) {
    if (current) history.replaceState({ drawer: key }, "");
    else history.pushState({ drawer: key }, "");
  }
  showDrawer(key);
}
function showDrawer(key) {
  const changed = state.openKey !== key;
  state.openKey = key;
  renderDrawer();
  $("drawer").classList.add("open");
  $("drawer").setAttribute("aria-hidden", "false");
  $("backdrop").hidden = false;
  document.body.classList.add("drawer-open");
  if (changed) $("drawer-body").scrollTop = 0;
}
function closeDrawer() {
  if (history.state && history.state.drawer) { history.back(); return; }
  hideDrawer();
}
function hideDrawer() {
  state.openKey = null;
  state.composerSig = null;
  $("drawer").classList.remove("open");
  $("drawer").setAttribute("aria-hidden", "true");
  $("backdrop").hidden = true;
  document.body.classList.remove("drawer-open");
}
addEventListener("popstate", () => {
  const key = history.state && history.state.drawer;
  if (key && state.snap) showDrawer(key); else hideDrawer();
});
$("drawer-back").addEventListener("click", closeDrawer);
$("backdrop").addEventListener("click", closeDrawer);

// ---------------------------------------------------------------------------
// Composer: a message to one agent, from the phone or the laptop. The server
// pastes it into the agent's tmux pane, or sends it through cmux.

async function sendMessage(form, teamKey, surfaceId, text, draftKey) {
  const status = form.querySelector(".composer-status");
  const button = form.querySelector(".send");
  button.disabled = true;
  status.textContent = "Sending…";
  const res = await post("/api/message", { teamKey, surfaceId, text });
  button.disabled = false;
  if (res.ok) {
    form.querySelector("textarea").value = "";
    delete state.drafts[draftKey];
    status.textContent = res.output || "Sent.";
    setTimeout(() => load(true, true), 1500);
  } else {
    status.textContent = `Not sent: ${res.output}`;
  }
}

// Ctrl+Enter or Cmd+Enter sends. Enter alone makes a new line, as on a phone.
function composerKeys(form) {
  return (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); form.requestSubmit(); }
  };
}

function composerTargets(key) {
  const d = deptByKey(key);
  const teams = d ? d.teams.map(teamByKey).filter(Boolean) : [teamByKey(key)].filter(Boolean);
  return teams.flatMap((t) => t.agents.map((a) => ({
    team: t, agent: a, id: `${t.key}|${a.surfaceId}`,
    label: `${a.title} · ${a.role} · ${t.name}${multiNode() ? ` (${t.nodeLabel})` : ""}`,
  })));
}

function renderComposer() {
  const form = $("drawer-foot");
  const targets = composerTargets(state.openKey);
  const sig = `${state.openKey}:${targets.map((x) => x.id).join(",")}`;
  if (sig === state.composerSig) return;
  state.composerSig = sig;
  form.hidden = !targets.length;
  if (!targets.length) { form.replaceChildren(); return; }
  const draftKey = state.openKey;
  const pick = targets.length > 1
    ? h("select", { class: "composer-to", "aria-label": "Recipient" }, targets.map((x) => h("option", { value: x.id }, x.label)))
    : h("span", { class: "composer-to-label" }, `To ${targets[0].label}`);
  const text = h("textarea", {
    rows: 2, placeholder: "Message to this agent…", "aria-label": "Message",
    oninput: (e) => { state.drafts[draftKey] = e.target.value; },
  });
  text.value = state.drafts[draftKey] || "";
  const send = h("button", { class: "btn send", type: "submit" }, "Send");
  const status = h("p", { class: "hint composer-status" }, matchMedia("(hover: hover)").matches ? "Ctrl+Enter sends." : "");
  form.replaceChildren(pick, h("div", { class: "composer-row" }, text, send), status);
  text.addEventListener("keydown", composerKeys(form));
  form.onsubmit = (e) => {
    e.preventDefault();
    const value = text.value.trim();
    if (!value) return;
    const id = targets.length > 1 ? pick.value : targets[0].id;
    const target = targets.find((x) => x.id === id) || targets[0];
    sendMessage(form, target.team.key, target.agent.surfaceId, value, draftKey);
  };
}

function renderChiefCard(snap) {
  const c = snap.chief;
  const live = Boolean(c && c.agents.length);
  $("chief-state").textContent = !c ? "The chief is not running. Start it with: chief up"
    : live ? `${STATUS_LABEL[c.status] || c.status} on ${c.nodeLabel}`
      : `The chief session on ${c.nodeLabel} has no running agent.`;
  $("chief-send").disabled = !live;
  $("chief-text").disabled = !live;
}
$("chief-text").addEventListener("keydown", composerKeys($("chief-card")));
$("chief-card").addEventListener("submit", (e) => {
  e.preventDefault();
  const c = state.snap && state.snap.chief;
  const value = $("chief-text").value.trim();
  if (!c || !value) return;
  sendMessage($("chief-card"), c.key, null, value, "chief");
});

function quote(label, entry) {
  if (!entry) return null;
  return h("div", null, h("div", { class: "quote-label" }, `${label} · ${ago(entry[0])}`), h("div", { class: "quote" }, entry[1]));
}

function segmented(label, options, current, onPick) {
  return h("div", { class: "field" },
    h("div", { class: "eyebrow" }, label),
    h("div", { class: "segmented", role: "radiogroup", "aria-label": label }, options.map(([value, text]) => h("button", {
      role: "radio", "aria-checked": String(value === current),
      onclick: () => { if (value !== current) onPick(value); },
    }, text))));
}

function agentDetails(t) {
  return t.agents.map((a) => h("div", { class: "agent-detail" },
    h("div", { class: "drawer-head" },
      h("div", null,
        h("div", { class: "eyebrow" }, `${a.role} · team ${t.task}`),
        h("strong", null, a.title),
        h("div", { class: "proj-task" }, `${t.nodeLabel || ""} · ${a.kind} · ${a.surfaceRef} · ${money(a.costUsd)} · last activity ${ago(a.lastActivity)}`)),
      canJump(t) && h("button", { class: "btn jump", onclick: () => jump(t.workspaceId, a.surfaceId) }, "Jump ↗")),
    statusEl(a.status),
    a.waitReason === "question" && h("div", { class: "hint" }, "The last reply ends with a question for you."),
    quote("Last prompt", a.lastUser),
    quote(a.waitReason === "question" ? "End of the last reply" : "Last reply",
      a.waitReason === "question" ? a.lastAssistantTail : a.lastAssistant),
    a.subagents.length > 0 && h("div", null,
      h("div", { class: "quote-label" }, `${plural(a.subagents.length, "sub-agent")} · ${money(a.subagentCostUsd)}`),
      h("ul", { class: "sub-list" }, a.subagents.slice(0, 12).map((x) => h("li", null,
        h("i", { class: `dot ${x.running ? "working" : "ended"}` }),
        h("span", { class: "agent-title" }, x.description),
        h("span", null, money(x.costUsd))))))));
}

function renderDrawer() {
  const body = $("drawer-body");
  const d = deptByKey(state.openKey);
  if (!d) {
    const t = teamByKey(state.openKey);
    if (!t) { closeDrawer(); return; }
    $("drawer-title").textContent = t.name;
    replace(body,
      h("div", { class: "drawer-head" },
        h("div", { style: { minWidth: "0" } }, h("div", { class: "eyebrow" }, `Team · ${t.nodeLabel || ""}`), h("h2", null, t.name),
          h("div", { class: "proj-task" }, `${t.task} · ${t.cwd || ""}`)),
        canJump(t) && h("button", { class: "btn jump", onclick: () => jump(t.workspaceId) }, "Jump ↗")),
      ...agentDetails(t));
    renderComposer();
    return;
  }
  $("drawer-title").textContent = d.name;
  const teams = d.teams.map(teamByKey).filter(Boolean);
  const kv = (value, label) => h("div", null, h("b", null, value), h("span", null, label));
  const hours = d.daily.reduce((acc, x) => acc + x.agentHours, 0);
  const commits = d.daily.reduce((acc, x) => acc + x.commits, 0);
  replace(body,
    h("div", { class: "drawer-head" },
      h("div", { style: { minWidth: "0" } },
        h("div", { class: "eyebrow" }, "Department"),
        h("h2", null, d.name),
        h("div", { class: "proj-task", title: d.path },
          Object.entries(d.paths || { "": d.path }).map(([n, p]) => (n ? `${n}: ${p}` : p)).join(" · ")))),
    d.description && h("p", { class: "focus-why", style: { margin: 0 } }, d.description),
    h("div", { class: "fields" },
      segmented("Status", Object.entries(DEPT_LABEL), d.status, (v) => setDept(d.key, { status: v })),
      segmented("Focus", [["today", "Today"], ["week", "This week"], ["", "None"]], d.focus || "", (v) => setDept(d.key, { focus: v }))),
    d.statusSource === "suggested" && h("p", { class: "hint" },
      `The status "${DEPT_LABEL[d.status]}" is a suggestion from the last activity. Click a status to confirm or change it.`),
    h("div", { class: "kv" },
      kv(money(d.costUsd), "spend in range"),
      kv(ago(d.lastActivity), "last activity"),
      kv(String(teams.length), "open teams"),
      kv(String(hours), "agent hours, 30 days"),
      kv(String(commits), "your commits, 30 days"),
      kv(d.deadline ? dueText(d.deadline).replace("Due ", "") : "–", "deadline")),
    h("div", null, h("div", { class: "quote-label" }, "Activity, last 30 days"), activityStrip(d, true)),
    (d.goal || d.next || d.notes) && h("div", { class: "agent-detail" },
      h("div", { class: "eyebrow" }, "From your meetings"),
      d.goal && h("div", null, h("b", null, "Goal: "), d.goal),
      d.next && h("div", null, h("b", null, "Next step: "), d.next),
      d.notes && h("div", { class: "focus-why" }, d.notes)),
    !d.goal && h("p", { class: "hint" }, "No goal yet. The chief asks you in the next meeting."),
    d.lastCommit && h("div", null, h("div", { class: "quote-label" }, `Last commit · ${ago(d.lastCommit.at)}`),
      h("div", { class: "quote" }, d.lastCommit.subject)),
    ...teams.flatMap(agentDetails),
    !teams.length && h("p", { class: "hint" }, "No open team. Ask the chief to start one, or open a tab in this folder."),
  );
  renderComposer();
}

// ---------------------------------------------------------------------------
// Controls

function setRangeButtons() {
  for (const b of document.querySelectorAll("#range button")) {
    b.setAttribute("aria-checked", String(b.dataset.range === state.range));
  }
}

$("range").addEventListener("click", (e) => {
  const r = e.target.closest("button")?.dataset.range;
  if (!r || r === state.range) return;
  state.range = r;
  store("range", r);
  setRangeButtons();
  load();
});
$("refresh").addEventListener("click", () => load(true));
$("table-toggle").addEventListener("click", () => { state.show.table = !state.show.table; renderTable(state.snap); });
$("theme").addEventListener("click", () => {
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const cur = document.documentElement.dataset.theme || (dark ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  store("theme", next);
  if (state.snap) render();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

let resizeTimer = null;
addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (state.snap) render(); }, 150);
});

const savedTheme = store("theme");
if (savedTheme) document.documentElement.dataset.theme = savedTheme;
setRangeButtons();
load().then(() => {
  const key = history.state && history.state.drawer;
  if (key && state.snap) showDrawer(key);
});
setInterval(() => { if (!document.hidden) load(false, true); }, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(false, true); });
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
