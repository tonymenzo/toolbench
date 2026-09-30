/* toolbench campaign dashboard — client.
 *
 * Polls /api/state and re-renders. Selection (run, tab, trial) lives in the
 * URL hash so a refresh or a shared link reopens the same view. Everything
 * read from disk is inserted as text, never as HTML. */

"use strict";

const STATE_LABELS = {
  running: "Live", finished: "Finished", aborted: "Aborted", failed: "Failed",
  stale: "Stale", unknown: "No heartbeat",
};
const TRIAL_LABELS = {
  queued: "Queued", running: "Running", interrupted: "Interrupted",
  passed: "Passed", failed: "Did not pass", error: "Infrastructure error",
  leak: "Integrity leak (quarantined)",
};
const ATTENTION = new Set(["stale", "failed", "aborted"]);
const PROGRESS_ORDER = ["passed", "failed", "error", "leak", "running", "interrupted"];

const ui = {
  state: null,
  filter: "all",
  run: null,          // selected run id
  tab: "trials",      // "trials" | "summary"
  trial: null,        // selected trial id (drawer)
  summaryCache: {},   // run id -> summary text
  lastOk: null,
  timer: null,
};

/* ── utilities ─────────────────────────────────────────────────── */

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const fmt = {
  usd(v) { return v === null || v === undefined ? "—" : `$${Number(v).toFixed(2)}`; },
  reach(v) { return v === null || v === undefined ? "—" : Number(v).toFixed(2); },
  pct(n, d) { return d ? `${Math.round((100 * n) / d)}%` : "—"; },
  duration(s) {
    if (s === null || s === undefined) return "—";
    s = Math.max(0, Math.round(s));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
    const hr = Math.floor(m / 60);
    return hr < 48 ? `${hr}h ${String(m % 60).padStart(2, "0")}m` : `${Math.floor(hr / 24)}d`;
  },
  ago(epoch) {
    if (!epoch) return "—";
    return `${fmt.duration(Date.now() / 1000 - epoch)} ago`;
  },
  time(epoch) {
    return epoch ? new Date(epoch * 1000).toLocaleString() : "—";
  },
};

function done(c) { return c.passed + c.failed + c.error + c.leak; }

function reachBin(score) {
  const s = Number(score) || 0;
  if (s >= 1) return 4;
  return Math.min(3, Math.floor(s * 4));
}

async function getJSON(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

/* ── URL hash <-> selection ────────────────────────────────────── */

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  ui.run = p.get("run");
  ui.tab = p.get("tab") === "summary" ? "summary" : "trials";
  ui.trial = p.get("trial");
}

function writeHash() {
  const p = new URLSearchParams();
  if (ui.run) p.set("run", ui.run);
  if (ui.tab !== "trials") p.set("tab", ui.tab);
  if (ui.trial) p.set("trial", ui.trial);
  history.replaceState(null, "", `#${p}`);
}

/* ── shared pieces ─────────────────────────────────────────────── */

function progressBar(counts) {
  const total = counts.planned || 1;
  const bar = h("div", { class: "progress", role: "img",
    "aria-label": `${done(counts)} of ${counts.planned} trials finished` });
  for (const k of PROGRESS_ORDER) {
    if (!counts[k]) continue;
    bar.append(h("span", { class: `p-${k}`, style: `width:${(100 * counts[k]) / total}%`,
      title: `${TRIAL_LABELS[k]}: ${counts[k]}` }));
  }
  return bar;
}

function pill(state, extra) {
  const label = STATE_LABELS[state] || state;
  return h("span", { class: `pill ${state}` }, extra ? `${label} · ${extra}` : label);
}

function trialSquare(t, { interactive = true } = {}) {
  let cls = `sq ${t.state}`;
  let glyph = "";
  if (t.state === "passed" || t.state === "failed") {
    cls = `sq r${reachBin(t.score)}`;
    if (t.state === "passed") glyph = "✓";
  } else if (t.state === "error") glyph = "!";
  else if (t.state === "leak") glyph = "⚑";
  if (t.trial_id && t.trial_id === ui.trial) cls += " selected";
  if (!interactive) return h("span", { class: cls, "aria-hidden": "true" }, glyph);
  return h("button", {
    class: cls, type: "button", "aria-label": `${t.trial_id}: ${TRIAL_LABELS[t.state]}`,
    dataset: { trial: t.trial_id },
    onclick: () => openTrial(t.trial_id),
    onmouseenter: (e) => showTooltip(e.currentTarget, t),
    onmouseleave: hideTooltip, onfocus: (e) => showTooltip(e.currentTarget, t), onblur: hideTooltip,
  }, glyph);
}

/* ── tooltip ───────────────────────────────────────────────────── */

function showTooltip(anchor, t) {
  const tip = document.getElementById("tooltip");
  const lines = [TRIAL_LABELS[t.state]];
  if (t.score !== undefined && t.score !== null) lines.push(`reach ${fmt.reach(t.score)}`);
  if (t.failure_mode && t.failure_mode !== "NONE") lines.push(t.failure_mode);
  if (t.wall_clock_s) lines.push(`wall ${fmt.duration(t.wall_clock_s)}`);
  if (t.cost_usd) lines.push(`cost ${fmt.usd(t.cost_usd)}`);
  if (t.state === "running") {
    lines.push(`running ${fmt.duration(Date.now() / 1000 - (t.started_at || 0))}`);
    if (t.last_activity) lines.push(`last output ${fmt.ago(t.last_activity)}`);
  }
  tip.replaceChildren(h("strong", {}, t.trial_id), lines.join(" · "));
  tip.hidden = false;
  const r = anchor.getBoundingClientRect();
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let x = r.left + r.width / 2 - tw / 2;
  x = Math.max(8, Math.min(x, window.innerWidth - tw - 8));
  const y = r.top - th - 8 < 8 ? r.bottom + 8 : r.top - th - 8;
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}

function hideTooltip() { document.getElementById("tooltip").hidden = true; }

/* ── overview ──────────────────────────────────────────────────── */

function renderOverview(s) {
  const t = s.totals;
  const scored = t.passed + t.failed;
  const budgets = s.runs.map((r) => r.budget_usd).filter((b) => typeof b === "number");
  const budget = budgets.length === s.runs.length && budgets.length
    ? budgets.reduce((a, b) => a + b, 0) : null;
  const tile = (label, value, sub, extra) =>
    h("div", { class: "tile" }, h("div", { class: "tile-label" }, label),
      h("div", { class: "tile-value num" }, value),
      sub ? h("div", { class: "tile-sub" }, sub) : null, extra);
  document.getElementById("overview").replaceChildren(
    tile("Runs", s.runs.length, `${s.live_runs} live`),
    tile("Trials finished", `${done(t)} / ${t.planned}`,
      `${t.running} running · ${t.queued} queued` + (t.interrupted ? ` · ${t.interrupted} interrupted` : ""),
      progressBar(t)),
    tile("Pass rate", fmt.pct(t.passed, scored), `${t.passed} of ${scored} scored trials`
      + (t.error ? ` · ${t.error} errors excluded` : "")),
    tile("Spend", fmt.usd(s.spent_usd), budget !== null ? `of ${fmt.usd(budget)} budgeted` : "metered API cost"),
  );
}

/* ── run list ──────────────────────────────────────────────────── */

function visibleRuns(s) {
  if (ui.filter === "live") return s.runs.filter((r) => r.state === "running");
  if (ui.filter === "attention") {
    return s.runs.filter((r) => ATTENTION.has(r.state) || r.counts.error || r.counts.leak
      || r.counts.interrupted);
  }
  return s.runs;
}

function renderRunList(s) {
  const runs = visibleRuns(s);
  const list = document.getElementById("run-list");
  if (!runs.length) {
    list.replaceChildren(h("li", {}, h("p", { class: "empty" }, "No runs match this filter.")));
    return;
  }
  list.replaceChildren(...runs.map((r) => h("li", {},
    h("button", {
      class: "run-item", type: "button", "aria-current": r.id === ui.run ? "true" : null,
      onclick: () => selectRun(r.id),
    },
    h("div", { class: "run-item-top" },
      h("span", { class: "run-item-name", title: r.id }, r.name), pill(r.state)),
    h("div", { class: "run-item-meta" },
      [r.benchmark, r.models.join(", "), r.dry_run ? "dry run" : null].filter(Boolean).join(" · ")),
    h("div", { class: "run-item-foot" }, progressBar(r.counts),
      h("span", { class: "num" }, `${done(r.counts)}/${r.counts.planned}`)),
    ))));
}

/* ── run detail ────────────────────────────────────────────────── */

function runNotice(r) {
  if (r.state === "stale") {
    return h("div", { class: "notice stale" },
      `The run reports itself as running, but its heartbeat stopped ${fmt.ago(r.heartbeat_at)}`
      + (r.host ? ` (pid ${r.pid} on ${r.host})` : "")
      + ". The process has most likely exited; `toolbench resume` finishes the remaining trials.");
  }
  if (r.state === "failed") {
    return h("div", { class: "notice failed" },
      `The run stopped before finalizing${r.error ? `: ${r.error}` : "."}`);
  }
  if (r.state === "aborted") {
    const why = r.abort_reason === "session_limit"
      ? "the subscription session limit was reached; resume after the quota resets"
      : "the budget cap was reached";
    return h("div", { class: "notice aborted" }, `Stopped early: ${why}.`);
  }
  if (r.state === "unknown") {
    return h("div", { class: "notice unknown" },
      "This run predates live status reporting, so whether it is still running cannot be "
      + `determined. Last recorded activity: ${fmt.ago(r.last_activity)}.`);
  }
  return null;
}

function renderRunDetail(s) {
  const pane = document.getElementById("run-detail");
  const r = s.runs.find((x) => x.id === ui.run);
  if (!r) {
    pane.replaceChildren(h("p", { class: "empty" },
      s.runs.length ? "Select a run." : "No toolbench runs found under this directory."));
    return;
  }
  const fact = (label, value) => h("div", {}, h("dt", {}, label), h("dd", { class: "num" }, value));
  const facts = [
    fact("Trials", `${done(r.counts)} / ${r.counts.planned}`),
    fact("Running", `${r.counts.running} of ${r.parallel} slots`),
    fact("Spend", r.budget_usd !== null && r.budget_usd !== undefined
      ? `${fmt.usd(r.spent_usd)} of ${fmt.usd(r.budget_usd)}` : fmt.usd(r.spent_usd)),
    fact("Started", r.created_at ? r.created_at.replace("T", " ") : "—"),
  ];
  if (r.state === "running") {
    facts.push(fact("ETA", r.eta_s !== null ? `~${fmt.duration(r.eta_s)}` : "—"));
    facts.push(fact("Heartbeat", fmt.ago(r.heartbeat_at)));
  } else if (r.finished_at) {
    facts.push(fact("Ended", fmt.time(r.finished_at)));
  }
  if (r.host) facts.push(fact("Host", `${r.host} · pid ${r.pid}`));

  const head = h("div", { class: "detail-head" },
    h("div", { class: "detail-title" }, h("h3", {}, r.name),
      pill(r.state, r.state === "aborted" ? (r.abort_reason || "").replace("_", " ") : null)),
    r.id !== r.name ? h("div", { class: "detail-path" }, r.id) : null,
    h("dl", { class: "facts" }, facts),
    runNotice(r));

  if (ui.tab === "summary" && !r.has_summary) ui.tab = "trials";
  const tab = (id, label, disabled) => h("button", {
    class: "tab", type: "button", role: "tab", "aria-selected": String(ui.tab === id),
    disabled: disabled || null, title: disabled ? "summary.txt is written when the run finalizes" : null,
    onclick: () => { ui.tab = id; writeHash(); render(); },
  }, label);
  const tabs = h("div", { class: "tabs", role: "tablist" },
    tab("trials", "Trials"), tab("summary", "Summary", !r.has_summary));

  const panel = h("div", { class: "tab-panel", role: "tabpanel" });
  if (ui.tab === "summary") renderSummary(panel, r);
  else renderTrialGrid(panel, r);
  pane.replaceChildren(head, tabs, panel);
}

function legend() {
  const item = (el, label) => h("span", { class: "legend-item" }, el, label);
  const sq = (state, extra = {}) => trialSquare({ state, ...extra }, { interactive: false });
  return h("div", { class: "legend" },
    item(h("span", { class: "ramp", "aria-hidden": "true" },
      [0, 1, 2, 3, 4].map((i) => h("span", { style: `background:var(--reach-${i})` }))),
      "reach 0 → 1"),
    item(sq("passed", { score: 1 }), "passed"),
    item(sq("running"), "running"),
    item(sq("queued"), "queued"),
    item(sq("interrupted"), "interrupted"),
    item(sq("error"), "infrastructure error"),
    item(sq("leak"), "integrity leak"));
}

function renderTrialGrid(panel, r) {
  if (!r.cells.length) {
    panel.append(h("p", { class: "empty" }, "This run has no planned trials yet."));
    return;
  }
  const maxCols = Math.max(...r.cells.map((c) => c.trials.length));
  const rows = r.cells.map((c) => h("tr", {},
    h("td", { class: "cell-label" },
      h("div", { class: "cell-condition" }, c.condition ?? "—"),
      h("div", { class: "cell-model" }, c.model ?? "")),
    h("td", { class: "col-trials" }, h("div", { class: "squares" }, c.trials.map((t) => trialSquare(t)))),
    h("td", { class: "cell-stat num" }, fmt.reach(c.mean_reach),
      h("div", { class: "muted" }, c.n_scored ? `${c.n_passed}/${c.n_scored} pass` : "—")),
  ));
  panel.append(legend(), h("div", { class: "grid-scroll" }, h("table", { class: "grid" },
    h("thead", {}, h("tr", {},
      h("th", {}, "Cell"),
      h("th", { class: "seeds col-trials" }, `Trials by seed index (n = ${maxCols})`),
      h("th", { class: "cell-stat" }, "Mean reach"))),
    h("tbody", {}, rows))));
}

async function renderSummary(panel, r) {
  const pre = h("pre", { class: "textblock" }, ui.summaryCache[r.id] ?? "Loading summary…");
  panel.append(pre);
  try {
    const res = await fetch(`/api/summary?run=${encodeURIComponent(r.id)}`, { cache: "no-store" });
    if (!res.ok) throw new Error(`${res.status}`);
    ui.summaryCache[r.id] = await res.text();
    pre.textContent = ui.summaryCache[r.id];
  } catch (e) {
    pre.textContent = `Could not load summary.txt (${e.message}).`;
  }
}

/* ── trial drawer ──────────────────────────────────────────────── */

function openTrial(trialId) {
  ui.trial = trialId;
  writeHash();
  render();
  loadTrial();
}

function closeTrial() {
  ui.trial = null;
  writeHash();
  document.getElementById("trial-drawer").hidden = true;
  render();
}

async function loadTrial() {
  const drawer = document.getElementById("trial-drawer");
  const body = document.getElementById("drawer-body");
  if (!ui.trial || !ui.run) { drawer.hidden = true; return; }
  document.getElementById("drawer-title").textContent = ui.trial;
  drawer.hidden = false;
  let d;
  try {
    d = await getJSON(`/api/trial?run=${encodeURIComponent(ui.run)}&trial=${encodeURIComponent(ui.trial)}`);
  } catch (e) {
    body.replaceChildren(h("p", { class: "empty" },
      e.message.startsWith("404") ? "This trial has not started yet." : `Could not load trial (${e.message}).`));
    return;
  }
  // Keep the log scrolled to the bottom when the reader was already there.
  const oldLog = body.querySelector(".textblock");
  const pinned = !oldLog || oldLog.scrollHeight - oldLog.scrollTop - oldLog.clientHeight < 24;

  const sections = [];
  const row = d.row;
  if (row) {
    const kv = (pairs) => h("dl", { class: "kv" },
      pairs.filter(([, v]) => v !== null && v !== undefined && v !== "")
        .flatMap(([k, v]) => [h("dt", {}, k), h("dd", { class: "num" }, v)]));
    sections.push(h("section", {}, h("h4", {}, "Result"), kv([
      ["outcome", TRIAL_LABELS[row.failure_mode === "INTEGRITY_LEAK" ? "leak" : (row.ok ? "passed" : "failed")]],
      ["reach", fmt.reach(row.score)],
      ["failure mode", row.failure_mode],
      ["model", row.resolved_model || row.model],
      ["wall clock", fmt.duration(row.wall_clock_s)],
      ["cost", fmt.usd(row.cost_usd ?? row.estimated_api_equivalent_cost_usd)],
      ["tokens in / out", `${(row.input_tokens || 0).toLocaleString()} / ${(row.output_tokens || 0).toLocaleString()}`],
      ["tool calls", `${row.tool_calls ?? 0}` + (row.tool_errors ? ` (${row.tool_errors} errored)` : "")],
      ["attempts", row.attempts > 1 ? row.attempts : null],
    ])));
    const stages = Object.entries(row.stages || {});
    if (stages.length) {
      sections.push(h("section", {}, h("h4", {}, "Stages"), h("ul", { class: "stages" },
        stages.map(([id, ok]) => h("li", {},
          h("span", { class: `stage-mark ${ok ? "ok" : "no"}`, "aria-label": ok ? "passed" : "not passed" },
            ok ? "✓" : "✕"),
          h("span", {}, id),
          row.stage_credits && !ok && row.stage_credits[id] > 0
            ? h("span", { class: "muted" }, `credit ${fmt.reach(row.stage_credits[id])}`) : null)))));
    }
    const tools = Object.entries(row.tool_calls_by_name || {}).sort((a, b) => b[1] - a[1]);
    if (tools.length) {
      sections.push(h("section", {}, h("h4", {}, "Tool calls"),
        h("div", { class: "chips" }, tools.map(([n, c]) => h("span", { class: "chip" }, `${n} × ${c}`)))));
    }
  }
  const logTitle = row ? "Trial log" : "Live log";
  sections.push(h("section", {}, h("h4", {}, logTitle + (d.log_truncated ? " (tail)" : "")),
    h("pre", { class: "textblock" }, d.log ?? "No console.log yet.")));
  body.replaceChildren(...sections);
  const log = body.querySelector(".textblock");
  if (pinned) log.scrollTop = log.scrollHeight;
}

/* ── top-level render + polling ────────────────────────────────── */

function selectRun(id) {
  ui.run = id;
  ui.trial = null;
  document.getElementById("trial-drawer").hidden = true;
  writeHash();
  render();
}

function renderRefreshState(error) {
  const el = document.getElementById("refresh-state");
  el.classList.toggle("error", Boolean(error));
  el.textContent = error ? `Connection lost — retrying (${error})` : `Updated ${new Date(ui.lastOk).toLocaleTimeString()}`;
}

function render() {
  const s = ui.state;
  if (!s) return;
  document.getElementById("campaign-name").textContent = s.name;
  document.getElementById("campaign-name").title = s.root;
  document.title = `${s.name} · toolbench`;
  if (!ui.run || !s.runs.some((r) => r.id === ui.run)) {
    ui.run = (s.runs.find((r) => r.state === "running") || s.runs[0] || {}).id || null;
    writeHash();
  }
  renderOverview(s);
  renderRunList(s);
  // Re-rendering the summary tab on every poll would refetch it; leave it be.
  const detail = document.getElementById("run-detail");
  if (!(ui.tab === "summary" && detail.dataset.run === ui.run && detail.dataset.tab === "summary")) {
    renderRunDetail(s);
  }
  detail.dataset.run = ui.run || "";
  detail.dataset.tab = ui.tab;
}

async function poll() {
  clearTimeout(ui.timer);
  let delay = 3000;
  try {
    ui.state = await getJSON("/api/state");
    ui.lastOk = Date.now();
    delay = (ui.state.poll_s || 3) * 1000;
    render();
    renderRefreshState();
    if (ui.trial) loadTrial();
  } catch (e) {
    renderRefreshState(e.message);
  }
  ui.timer = setTimeout(poll, delay);
}

/* ── wiring ────────────────────────────────────────────────────── */

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("toolbench-dashboard-theme"); } catch { /* storage blocked */ }
  if (saved) document.documentElement.dataset.theme = saved;
  document.getElementById("theme-toggle").addEventListener("click", () => {
    const current = document.documentElement.dataset.theme
      || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("toolbench-dashboard-theme", next); } catch { /* storage blocked */ }
  });
}

function initFilter() {
  const group = document.getElementById("run-filter");
  group.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-filter]");
    if (!btn) return;
    ui.filter = btn.dataset.filter;
    for (const b of group.querySelectorAll("button")) b.setAttribute("aria-pressed", String(b === btn));
    render();
  });
}

document.getElementById("drawer-close").addEventListener("click", closeTrial);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && ui.trial) closeTrial(); });
window.addEventListener("scroll", hideTooltip, { passive: true });
readHash();
initTheme();
initFilter();
poll();
