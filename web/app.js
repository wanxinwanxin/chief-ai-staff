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
function modelLabel(m) {
  const match = /^claude-([a-z]+)-(\d+)(?:-(\d+))?/.exec(m || "");
  if (!match) return m;
  const name = match[1][0].toUpperCase() + match[1].slice(1);
  return `${name} ${match[2]}${match[3] ? "." + match[3] : ""}`;
}
const topModel = (a) => Object.keys(a.models || {})[0];
const STATUS_LABEL = { needs_input: "Needs you", working: "Working", idle: "Idle", ended: "Ended", none: "No agent" };
const slotColor = (slot) => (slot == null ? "var(--other)" : `var(--s${slot + 1})`);
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

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
  openKey: null,
};

async function load(fresh) {
  $("app").classList.add("loading");
  try {
    const res = await fetch(`/api/snapshot?range=${state.range}${fresh ? "&fresh=1" : ""}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    state.snap = data;
    render();
    const t = new Date(data.generatedAt * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    $("sync").textContent = `Updated ${t} · ${plural(data.projects.length, "project")} · ${data.cmux ? "cmux connected" : "cmux not found"}`;
  } catch (err) {
    $("sync").textContent = `Could not refresh: ${err.message}`;
  } finally {
    $("app").classList.remove("loading");
  }
}

async function jump(workspaceId, surfaceId) {
  try {
    await fetch("/api/jump", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Chief": "1" },
      body: JSON.stringify({ workspaceId, surfaceId }),
    });
  } catch (_) { /* The jump is a convenience. Ignore network errors. */ }
}

function findProject(key) {
  const snap = state.snap;
  if (!snap) return null;
  if (snap.chief && snap.chief.key === key) return snap.chief;
  return snap.projects.find((p) => p.key === key) || null;
}

function findByName(name) {
  const q = (name || "").toLowerCase().trim();
  return state.snap.projects.find((p) =>
    [p.name, p.folder, p.task].some((v) => (v || "").toLowerCase() === q)) ||
    state.snap.projects.find((p) => q && [p.name, p.folder, p.task].some((v) => (v || "").toLowerCase().includes(q)));
}

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
function hideTip() { tip.hidden = true; }
function hoverable(el, titleFn, rowsFn) {
  el.addEventListener("pointerenter", (e) => showTip(e, titleFn(), rowsFn()));
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
  renderFocus(snap);
  renderNeeds(snap);
  renderTiles(snap);
  renderOrg(snap);
  renderBars(snap);
  renderScatter(snap);
  renderColumns(snap);
  renderSignals(snap);
  renderTable(snap);
  $("note").textContent = `${snap.note} Data: cmux tabs and hooks, Claude Code and Codex session logs, git.`;
  if (state.openKey) renderDrawer();
}

function renderFocus(snap) {
  const el = $("focus");
  const f = snap.focus;
  el.replaceChildren(h("div", { class: "card-head" },
    h("h2", null, "Today's focus"),
    h("p", { class: "hint" }, f && f.updatedAt ? `Set by the chief ${ago(f.updatedAt)}` : "")));
  if (!f || !(f.items || []).length) {
    el.append(h("p", { class: "ask" }, "There is no focus plan yet. In the Chief of Staff tab, ask: ",
      h("code", null, "What should I focus on today?")));
    return;
  }
  el.append(h("ol", { class: "focus-list" }, f.items.slice(0, 4).map((it, i) => {
    const p = findByName(it.project);
    return h("li", {
      class: "focus-item", style: p ? { cursor: "pointer" } : null,
      onclick: p ? () => openDrawer(p.key) : null,
    },
    h("span", { class: "focus-num" }, i + 1),
    h("div", null,
      h("div", { class: "focus-title" }, it.project),
      it.why && h("div", { class: "focus-why" }, it.why),
      it.next && h("div", { class: "focus-next" }, h("b", null, "Next: "), it.next)));
  })));
  if (f.note) el.append(h("p", { class: "focus-note" }, f.note));
  if ((f.later || []).length) el.append(h("div", { class: "later" }, `In the background: ${f.later.join(", ")}`));
}

function renderNeeds(snap) {
  const el = $("needs");
  const list = snap.needsYou || [];
  el.replaceChildren(h("div", { class: "card-head" },
    h("h2", null, "Needs you"), h("p", { class: "hint" }, list.length ? plural(list.length, "agent") : "")));
  if (!list.length) {
    el.append(h("p", { class: "empty" }, "No agent waits for you now."));
    return;
  }
  el.append(h("ul", { class: "needs-list" }, list.map((n) => h("li", { class: "needs-item" },
    h("div", { class: "needs-top" },
      h("strong", null, n.project),
      h("button", { class: "btn", onclick: () => jump(n.workspaceId, n.surfaceId) }, "Jump ↗")),
    h("div", { class: "proj-task" }, `${n.agent} · waiting ${ago(n.since).replace(" ago", "")}`),
    n.lastAssistant && h("div", { class: "needs-msg" }, n.lastAssistant[1])))));
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
  const lines = snap.projects.reduce((acc, p) => {
    if (p.output) { acc[0] += p.output.linesAdded; acc[1] += p.output.linesRemoved; }
    return acc;
  }, [0, 0]);
  const series = snap.range.buckets.map((b) => t.series[b] || 0);
  const tile = (label, value, sub, extra) => h("article", { class: "card tile" },
    h("div", { class: "tile-label" }, label), h("div", { class: "tile-value" }, value),
    h("div", { class: "tile-sub" }, sub), extra);
  $("tiles").replaceChildren(
    tile("Spend (API-equivalent)", money(t.costUsd),
      `${compact(t.tokens.total)} tokens · cache hit ${pct(t.cacheHitRate)}`, sparkline(series, 200, 28)),
    tile("Agents working", String(a.working), `${a.idle} idle · ${plural(snap.projects.length, "project")}`),
    tile("Needs you", String(a.needs_input), a.needs_input ? "Agents wait for your input" : "Nothing waits for you"),
    tile("Output", plural(t.commits, "commit"), `+${compact(lines[0])} / −${compact(lines[1])} lines`),
  );
}

// ---------------------------------------------------------------------------
// Org chart

function agentRow(p, a) {
  const model = topModel(a);
  const row = h("li", {
    class: "agent", tabindex: 0, title: "Jump to this tab",
    onclick: (e) => { e.stopPropagation(); jump(p.workspaceId, a.surfaceId); },
    onkeydown: (e) => { if (e.key === "Enter") { e.stopPropagation(); jump(p.workspaceId, a.surfaceId); } },
  },
  h("i", { class: `dot ${a.status}`, title: STATUS_LABEL[a.status] }),
  h("span", { class: "agent-title" }, a.title, h("small", null, model ? modelLabel(model) : a.kind)),
  h("span", { class: "agent-cost" }, money(a.costUsd)));
  return row;
}

function projectCard(p, maxCost, totalCost) {
  const coordinators = p.agents.filter((a) => a.role === "coordinator");
  const workers = p.agents.filter((a) => a.role !== "coordinator");
  const subs = p.agents.flatMap((a) => a.subagents);
  const running = subs.filter((x) => x.running).length;
  const out = p.output;
  const open = () => openDrawer(p.key);
  return h("article", {
    class: `proj ${p.status}`, tabindex: 0, style: { "--swatch": slotColor(p.colorSlot) },
    onclick: open, onkeydown: (e) => { if (e.key === "Enter") open(); },
  },
  h("div", { class: "proj-head" },
    h("div", { style: { minWidth: "0" } },
      h("div", { class: "proj-name" }, p.name),
      h("div", { class: "proj-task", title: p.task }, p.task)),
    statusEl(p.status)),
  h("div", { class: "proj-metrics" },
    h("span", { class: "proj-cost" }, money(p.costUsd)),
    h("span", null, out ? `${plural(out.commits, "commit")} · ${pct(totalCost ? p.costUsd / totalCost : 0)} of spend` : `no git data · ${pct(totalCost ? p.costUsd / totalCost : 0)} of spend`)),
  h("div", { class: "meter", "aria-hidden": "true" },
    h("span", { style: { width: `${Math.max(1.5, (p.costUsd / maxCost) * 100)}%` } })),
  coordinators.length > 0 && h("div", null,
    h("div", { class: "roster-label" }, "Coordinator"),
    h("ul", { class: "roster" }, coordinators.map((a) => agentRow(p, a)))),
  workers.length > 0 && h("div", null,
    h("div", { class: "roster-label" }, workers.length === 1 ? "Worker" : "Workers"),
    h("ul", { class: "roster" }, workers.map((a) => agentRow(p, a)))),
  !p.agents.length && h("div", { class: "empty" }, "No agent runs in this tab."),
  h("div", { class: "proj-foot" },
    subs.length > 0 && h("span", { class: "chip" },
      h("span", { class: "subdots" }, subs.slice(0, 8).map((x) => h("i", { class: x.running ? "on" : "" }))),
      `${plural(subs.length, "sub-agent")}${running ? ` (${running} running)` : ""}`),
    p.earlier.sessions > 0 && h("span", { class: "chip" }, `${plural(p.earlier.sessions, "earlier session")} · ${
      p.earlier.costUsd || !p.earlier.unpricedTokens ? money(p.earlier.costUsd) : `${compact(p.earlier.unpricedTokens)} Codex tokens`}`),
    p.meta.priority && h("span", { class: "chip" }, `Priority: ${p.meta.priority}`)));
}

function otherCard(other) {
  return h("article", { class: "proj other" },
    h("div", { class: "proj-head" },
      h("div", null,
        h("div", { class: "proj-name" }, "Not in a tab"),
        h("div", { class: "proj-task" }, "Sessions in folders with no open cmux tab")),
      h("span", { class: "proj-cost" }, money(other.costUsd))),
    h("ul", { class: "other-list" }, other.projects.slice(0, 6).map((o) =>
      h("li", null, h("span", null, `${o.name} · ${ago(o.lastActivity)}`), h("span", null, money(o.costUsd))))));
}

function renderOrg(snap) {
  const root = $("org");
  const total = snap.totals.costUsd;
  const c = snap.chief;
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
    h("div", { class: "person-sub" }, c
      ? [statusEl(c.status), ` · ${money(c.costUsd)}`]
      : "Not started. Run: chief start")));
  const grid = h("div", { class: "org-grid", id: "org-grid" });
  const maxCost = Math.max(1, ...snap.projects.map((p) => p.costUsd));
  for (const p of snap.projects) grid.append(projectCard(p, maxCost, total));
  if (snap.elsewhere.projects.length) grid.append(otherCard(snap.elsewhere));
  root.replaceChildren(h("div", { class: "org-top" }, you, chief), grid);
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

function projectTipRows(p) {
  const subCost = p.agents.reduce((acc, a) => acc + a.subagentCostUsd, 0);
  const liveCost = p.costUsd - p.earlier.costUsd;
  return [
    { value: money(p.costUsd), label: "spend in range" },
    { value: money(liveCost - subCost), label: "open agents" },
    { value: money(subCost), label: "sub-agents" },
    { value: money(p.earlier.costUsd), label: "earlier sessions" },
    { value: compact(p.tokens.total), label: "tokens" },
    { value: pct(p.cacheHitRate), label: "cache hit" },
  ];
}

function renderBars(snap) {
  const el = $("bars");
  const rows = snap.projects.map((p) => ({ key: p.key, name: p.name, v: p.costUsd, p }))
    .sort((a, b) => b.v - a.v);
  if (snap.elsewhere.costUsd > 0) rows.push({ key: null, name: "Not in a tab", v: snap.elsewhere.costUsd, other: true });
  if (snap.chief && snap.chief.costUsd > 0) rows.push({ key: snap.chief.key, name: "Chief of Staff", v: snap.chief.costUsd, other: true, p: snap.chief });
  const W = Math.max(280, el.clientWidth || 560);
  const rowH = 30;
  const bar = 14;
  const labelW = Math.min(170, W * 0.34);
  const valueW = 58;
  const H = rows.length * rowH + 6;
  const max = Math.max(1, ...rows.map((r) => r.v));
  const x = (v) => labelW + ((W - labelW - valueW) * v) / max;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "Spend by project, bar chart" });
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
    hoverable(g, () => r.name, () => (r.p ? projectTipRows(r.p) : [
      { value: money(r.v), label: "spend in range" },
      ...snap.elsewhere.projects.slice(0, 5).map((o) => ({ value: money(o.costUsd), label: o.name })),
    ]));
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

function renderScatter(snap) {
  const el = $("scatter");
  const pts = snap.projects.filter((p) => p.output && p.costUsd > 0);
  const missing = snap.projects.filter((p) => !p.output && p.costUsd > 0);
  const W = Math.max(280, el.clientWidth || 560);
  const H = 250;
  const m = { l: 40, r: 18, t: 12, b: 30 };
  if (!pts.length) {
    el.replaceChildren(h("p", { class: "empty" }, "No project with git data has spend in this range."));
    return;
  }
  const minC = Math.min(...pts.map((p) => p.costUsd));
  const maxC = Math.max(...pts.map((p) => p.costUsd));
  const lo = Math.pow(10, Math.floor(Math.log10(Math.max(0.01, minC))));
  const hi = Math.pow(10, Math.ceil(Math.log10(Math.max(lo * 10, maxC))));
  // Both axes are log scales: spend and commits each span several orders of magnitude.
  // Commits use log10(1 + n), so a project with zero commits sits on the baseline.
  const yTop = Math.pow(10, Math.ceil(Math.log10(1 + Math.max(1, ...pts.map((p) => p.output.commits)))));
  const ylog = (v) => Math.log10(1 + v);
  const x = (v) => m.l + ((Math.log10(Math.max(lo, v)) - Math.log10(lo)) / (Math.log10(hi) - Math.log10(lo))) * (W - m.l - m.r);
  const y = (v) => H - m.b - (ylog(v) / ylog(yTop)) * (H - m.t - m.b);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "Commits against spend, scatter plot, log scales" });
  for (let v = 0; v <= yTop; v = v === 0 ? 1 : v * 10) {
    svg.append(s("line", { class: v === 0 ? "baseline" : "gridline", x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }));
    svg.append(s("text", { x: m.l - 8, y: y(v) + 4, "text-anchor": "end" }, compact(v)));
  }
  for (let d = lo; d <= hi * 1.0001; d *= 10) {
    svg.append(s("line", { class: "gridline", x1: x(d), x2: x(d), y1: m.t, y2: H - m.b }));
    svg.append(s("text", { x: x(d), y: H - m.b + 18, "text-anchor": "middle" }, moneyAxis(d)));
  }
  svg.append(s("text", { x: W - m.r, y: H - 4, "text-anchor": "end" }, "spend (log) →"));
  svg.append(s("text", { x: m.l, y: m.t - 2 }, "↑ commits (log)"));

  // Place labels without overlap. The most expensive projects get a label first.
  // A label that fits nowhere is left out. The tooltip and the table still show the value.
  const dots = pts.map((p) => ({ p, cx: x(p.costUsd), cy: y(p.output.commits) }));
  const boxes = dots.map((d) => ({ x0: d.cx - 6, x1: d.cx + 6, y0: d.cy - 6, y1: d.cy + 6 }));
  const hits = (b) => b.x0 < m.l || b.x1 > W - 2 || b.y0 < 0 || b.y1 > H - m.b + 2 ||
    boxes.some((o) => b.x0 < o.x1 && b.x1 > o.x0 && b.y0 < o.y1 && b.y1 > o.y0);
  const labels = new Map();
  for (const d of [...dots].sort((a, b) => b.p.costUsd - a.p.costUsd)) {
    const w = d.p.name.length * 6.6;
    const options = [
      { x: d.cx + 9, y: d.cy + 4, anchor: "start", b: { x0: d.cx + 8, x1: d.cx + 10 + w, y0: d.cy - 7, y1: d.cy + 7 } },
      { x: d.cx - 9, y: d.cy + 4, anchor: "end", b: { x0: d.cx - 10 - w, x1: d.cx - 8, y0: d.cy - 7, y1: d.cy + 7 } },
      { x: d.cx, y: d.cy - 11, anchor: "middle", b: { x0: d.cx - w / 2, x1: d.cx + w / 2, y0: d.cy - 22, y1: d.cy - 8 } },
      { x: d.cx, y: d.cy + 19, anchor: "middle", b: { x0: d.cx - w / 2, x1: d.cx + w / 2, y0: d.cy + 8, y1: d.cy + 22 } },
    ];
    const pick = options.find((o) => !hits(o.b));
    if (pick) { boxes.push(pick.b); labels.set(d.p.key, pick); }
  }
  for (const { p, cx, cy } of dots) {
    const lab = labels.get(p.key);
    const g = s("g", { tabindex: 0 },
      s("circle", { class: "mark", cx, cy, r: 5, fill: "var(--accent)", stroke: "var(--surface)", "stroke-width": 2 }),
      lab && s("text", { class: "lbl", x: lab.x, y: lab.y, "text-anchor": lab.anchor }, p.name),
      s("circle", { class: "hit", cx, cy, r: 13 }));
    g.addEventListener("click", () => openDrawer(p.key));
    hoverable(g, () => p.name, () => [
      { value: money(p.costUsd), label: "spend" },
      { value: String(p.output.commits), label: "commits" },
      { value: `+${compact(p.output.linesAdded)} / −${compact(p.output.linesRemoved)}`, label: "lines" },
      { value: p.output.commits ? money(p.costUsd / p.output.commits) : "–", label: "spend per commit" },
    ]);
    svg.append(g);
  }
  el.replaceChildren(svg);
  if (missing.length) {
    el.append(h("p", { class: "hint" }, `Not shown, no git data: ${missing.map((p) => `${p.name} (${money(p.costUsd)})`).join(", ")}`));
  }
}

function bucketLabel(key, bucket) {
  if (bucket === "hour") return key.slice(11, 16);
  const d = new Date(`${key}T12:00:00`);
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function renderColumns(snap) {
  const el = $("columns");
  const buckets = snap.range.buckets;
  const series = snap.projects
    .filter((p) => p.colorSlot != null)
    .sort((a, b) => a.colorSlot - b.colorSlot)
    .map((p) => ({ name: p.name, color: slotColor(p.colorSlot), data: p.series, key: p.key }));
  const other = {};
  const addOther = (src) => { for (const [k, v] of Object.entries(src || {})) other[k] = (other[k] || 0) + v; };
  addOther(snap.elsewhere.series);
  if (snap.chief) addOther(snap.chief.series);
  snap.projects.filter((p) => p.colorSlot == null).forEach((p) => addOther(p.series));
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
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "Spend over time by project, stacked columns" });
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

function renderSignals(snap) {
  const el = $("signals");
  const list = snap.signals || [];
  el.replaceChildren(h("div", { class: "card-head" }, h("h2", null, "Signals"),
    h("p", { class: "hint" }, "Simple rules. The chief reads these too.")));
  if (!list.length) {
    el.append(h("p", { class: "empty" }, "No signals. Spend and output look balanced."));
    return;
  }
  el.append(h("ul", { class: "sig-list" }, list.map((t) => h("li", null, h("span", { class: "sig-icon", "aria-hidden": "true" }, "!"), t))));
}

function renderTable(snap) {
  const total = snap.totals.costUsd || 0;
  const rows = [...snap.projects].sort((a, b) => b.costUsd - a.costUsd);
  const head = ["Project", "Status", "Agents", "Spend", "Share", "Tokens", "Cache hit", "Commits", "Lines", "Per commit", "Last activity"];
  const num = new Set([2, 3, 4, 5, 6, 7, 8, 9]);
  const tr = rows.map((p) => {
    const o = p.output;
    return h("tr", { class: "clickable", onclick: () => openDrawer(p.key) },
      h("td", null, h("i", { class: "swatch", style: { background: slotColor(p.colorSlot) } }), p.name),
      h("td", null, statusEl(p.status)),
      h("td", { class: "num" }, p.agents.length),
      h("td", { class: "num" }, money(p.costUsd)),
      h("td", { class: "num" }, pct(total ? p.costUsd / total : 0)),
      h("td", { class: "num" }, compact(p.tokens.total)),
      h("td", { class: "num" }, pct(p.cacheHitRate)),
      h("td", { class: "num" }, o ? o.commits : "–"),
      h("td", { class: "num" }, o ? `+${compact(o.linesAdded)} −${compact(o.linesRemoved)}` : "–"),
      h("td", { class: "num" }, o && o.commits ? money(p.costUsd / o.commits) : "–"),
      h("td", null, ago(p.lastActivity)));
  });
  if (snap.elsewhere.costUsd > 0) {
    tr.push(h("tr", null,
      h("td", null, h("i", { class: "swatch" }), "Not in a tab"), h("td", null, "–"), h("td", { class: "num" }, "–"),
      h("td", { class: "num" }, money(snap.elsewhere.costUsd)),
      h("td", { class: "num" }, pct(total ? snap.elsewhere.costUsd / total : 0)),
      h("td", { class: "num" }, compact(snap.elsewhere.tokens.total)),
      h("td", { class: "num" }, pct(snap.elsewhere.cacheHitRate)),
      h("td", { class: "num" }, "–"), h("td", { class: "num" }, "–"), h("td", { class: "num" }, "–"), h("td", null, "–")));
  }
  $("table").replaceChildren(h("table", null,
    h("thead", null, h("tr", null, head.map((t, i) => h("th", { class: num.has(i) ? "num" : null }, t)))),
    h("tbody", null, tr)));
}

// ---------------------------------------------------------------------------
// Detail drawer

function openDrawer(key) {
  state.openKey = key;
  renderDrawer();
  $("drawer").classList.add("open");
  $("drawer").setAttribute("aria-hidden", "false");
}
function closeDrawer() {
  state.openKey = null;
  $("drawer").classList.remove("open");
  $("drawer").setAttribute("aria-hidden", "true");
}

function quote(label, entry) {
  if (!entry) return null;
  return h("div", null, h("div", { class: "quote-label" }, `${label} · ${ago(entry[0])}`), h("div", { class: "quote" }, entry[1]));
}

function renderDrawer() {
  const p = findProject(state.openKey);
  const body = $("drawer-body");
  if (!p) { closeDrawer(); return; }
  const o = p.output;
  const kv = (value, label) => h("div", null, h("b", null, value), h("span", null, label));
  const meta = p.meta || {};
  body.replaceChildren(
    h("div", { class: "drawer-head" },
      h("div", null,
        h("div", { class: "eyebrow" }, p.isChief ? "Chief of Staff" : "Project"),
        h("h2", null, p.name),
        h("div", { class: "proj-task" }, `${p.cwd || "unknown folder"}${p.branch ? ` · ${p.branch}${p.dirty ? " (uncommitted changes)" : ""}` : ""}`),
        h("div", { class: "proj-task" }, `Tab: ${p.task}`)),
      h("div", { style: { display: "flex", gap: "6px" } },
        h("button", { class: "btn", onclick: () => jump(p.workspaceId) }, "Jump ↗"),
        h("button", { class: "btn", onclick: closeDrawer, "aria-label": "Close" }, "✕"))),
    statusEl(p.status),
    h("div", { class: "kv" },
      kv(money(p.costUsd), "spend in range"),
      kv(compact(p.tokens.total), "tokens"),
      kv(pct(p.cacheHitRate), "cache hit"),
      kv(o ? String(o.commits) : "–", "commits"),
      kv(o ? `+${compact(o.linesAdded)} −${compact(o.linesRemoved)}` : "–", "lines"),
      kv(o && o.commits ? money(p.costUsd / o.commits) : "–", "spend per commit")),
    (meta.goal || meta.priority || meta.notes) && h("div", { class: "agent-detail" },
      h("div", { class: "eyebrow" }, "Set by the chief"),
      meta.priority && h("div", null, `Priority: ${meta.priority}`),
      meta.goal && h("div", null, `Goal: ${meta.goal}`),
      meta.notes && h("div", { class: "focus-why" }, meta.notes)),
    Object.keys(p.models || {}).length > 0 && h("div", { class: "models" },
      Object.entries(p.models).map(([mdl, c]) => h("span", null, `${modelLabel(mdl)} ${money(c)}`))),
    ...p.agents.map((a) => h("div", { class: "agent-detail" },
      h("div", { class: "drawer-head" },
        h("div", null,
          h("div", { class: "eyebrow" }, a.role),
          h("strong", null, a.title),
          h("div", { class: "proj-task" }, `${a.kind} · ${a.surfaceRef} · ${money(a.costUsd)} · last activity ${ago(a.lastActivity)}`)),
        h("button", { class: "btn", onclick: () => jump(p.workspaceId, a.surfaceId) }, "Jump ↗")),
      statusEl(a.status),
      quote("Last prompt", a.lastUser),
      quote("Last reply", a.lastAssistant),
      a.subagents.length > 0 && h("div", null,
        h("div", { class: "quote-label" }, `${plural(a.subagents.length, "sub-agent")} · ${money(a.subagentCostUsd)}`),
        h("ul", { class: "sub-list" }, a.subagents.slice(0, 12).map((x) => h("li", null,
          h("i", { class: `dot ${x.running ? "working" : "ended"}` }),
          h("span", { class: "agent-title" }, x.description),
          h("span", null, money(x.costUsd)))))))),
    p.earlier.sessions > 0 && h("p", { class: "hint" },
      `${plural(p.earlier.sessions, "earlier session")} in this folder cost ${money(p.earlier.costUsd)} in this range.`),
  );
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
load();
setInterval(() => { if (!document.hidden) load(); }, 15000);
