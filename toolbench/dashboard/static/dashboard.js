/* toolbench run dashboard — client.
 *
 * Polls /api/state and re-renders one run. The inspector follows the most
 * recently started trial until the viewer picks one (which pins it);
 * "follow" resumes. Selection lives in the URL hash so a reload keeps the
 * view. Everything read from disk is inserted as text, never as HTML. */

"use strict";

const TRIAL_LABEL = {
  queued: "queued", running: "running", interrupted: "interrupted",
  passed: "passed", failed: "not passed", error: "infra error", leak: "integrity leak",
};
const RUN_LABEL = {
  running: "live", finished: "finished", aborted: "aborted", failed: "failed",
  stale: "stale", unknown: "no heartbeat",
};
const PROGRESS_ORDER = ["passed", "failed", "error", "leak", "interrupted", "running"];

const ui = {
  state: null,
  run: null,        // selected run id (campaign mode)
  view: "matrix",   // "matrix" | "summary"
  trial: null,      // trial shown in the inspector
  follow: true,     // inspector tracks the newest active trial
  summaryFor: null, // run id whose summary is loaded
  timer: null,
};

const $ = (id) => document.getElementById(id);

/* ── utilities ────────────────────────────────────────────── */

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.setAttribute("style", v);
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
  usd: (v) => (v === null || v === undefined ? "—" : `$${Number(v).toFixed(2)}`),
  r: (v) => (v === null || v === undefined ? "—" : Number(v).toFixed(2)),
  dur(s) {
    if (s === null || s === undefined) return "—";
    s = Math.max(0, Math.round(s));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m${String(s % 60).padStart(2, "0")}s`;
    const hr = Math.floor(m / 60);
    return hr < 48 ? `${hr}h${String(m % 60).padStart(2, "0")}m` : `${Math.floor(hr / 24)}d`;
  },
  ago: (t) => (t ? fmt.dur(Date.now() / 1000 - t) : "—"),
};

const done = (c) => c.passed + c.failed + c.error + c.leak;
const bin = (s) => ((Number(s) || 0) >= 1 ? 4 : Math.min(3, Math.floor((Number(s) || 0) * 4)));
const short = (tid) => tid.replace(/__seed\d+$/, "");

/* ── hash <-> selection ───────────────────────────────────── */

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  ui.run = p.get("run");
  ui.view = p.get("view") === "summary" ? "summary" : "matrix";
  ui.trial = p.get("trial");
  ui.follow = !ui.trial;
}

function writeHash() {
  const p = new URLSearchParams();
  if (ui.run && ui.state?.mode === "campaign") p.set("run", ui.run);
  if (ui.view !== "matrix") p.set("view", ui.view);
  if (!ui.follow && ui.trial) p.set("trial", ui.trial);
  history.replaceState(null, "", p.toString() ? `#${p}` : location.pathname);
}

/* ── pieces ───────────────────────────────────────────────── */

function square(t, { live = true } = {}) {
  let cls = `sq ${t.state}`, glyph = "";
  if (t.state === "passed" || t.state === "failed") {
    cls = `sq r${bin(t.score)}`;
    if (t.state === "passed") glyph = "✓";
  } else if (t.state === "error") glyph = "!";
  else if (t.state === "leak") glyph = "⚑";
  if (!live) return h("span", { class: cls, "aria-hidden": "true" }, glyph);
  if (t.trial_id === ui.trial) cls += " sel";
  return h("button", {
    class: cls, type: "button", "aria-label": `${t.trial_id}: ${TRIAL_LABEL[t.state]}`,
    onclick: () => pin(t.trial_id),
    onmouseenter: (e) => tip(e.currentTarget, t), onmouseleave: untip,
    onfocus: (e) => tip(e.currentTarget, t), onblur: untip,
  }, glyph);
}

function tip(anchor, t) {
  const parts = [TRIAL_LABEL[t.state]];
  if (t.score !== undefined && t.score !== null) parts.push(`reach ${fmt.r(t.score)}`);
  if (t.failure_mode && t.failure_mode !== "NONE") parts.push(t.failure_mode);
  if (t.wall_clock_s) parts.push(fmt.dur(t.wall_clock_s));
  if (t.state === "running") parts.push(`up ${fmt.ago(t.started_at)}`, `last out ${fmt.ago(t.last_activity)}`);
  const el = $("tip");
  el.replaceChildren(h("b", {}, t.trial_id), parts.join("  ·  "));
  el.hidden = false;
  const r = anchor.getBoundingClientRect();
  const x = Math.max(8, Math.min(r.left + r.width / 2 - el.offsetWidth / 2, innerWidth - el.offsetWidth - 8));
  const y = r.top - el.offsetHeight - 8 < 52 ? r.bottom + 8 : r.top - el.offsetHeight - 8;
  el.style.left = `${x}px`;
  el.style.top = `${y}px`;
}
const untip = () => { $("tip").hidden = true; };

/* ── header + figures ─────────────────────────────────────── */

function renderBar(s, run) {
  $("bench").textContent = run?.benchmark || s.name;
  document.title = `${run?.state === "running" ? "● " : ""}${run?.benchmark || s.name} · toolbench`;

  const wrap = $("picker-wrap");
  wrap.hidden = s.mode !== "campaign";
  if (s.mode === "campaign") {
    const sel = $("picker");
    sel.replaceChildren(...s.runs.map((r) => h("option", { value: r.id, selected: r.id === ui.run },
      `${r.state === "running" ? "● " : "  "}${r.id}`)));
  }

  const meta = [];
  if (run?.state === "running") meta.push(`hb ${fmt.ago(run.heartbeat_at)}`);
  if (run?.host) meta.push(`${run.pid}@${run.host}`);
  $("liveness").replaceChildren(
    run ? h("span", { class: `state ${run.state}` }, RUN_LABEL[run.state] || run.state) : "",
    meta.length ? h("span", { class: "meta" }, meta.join("  ")) : "");
}

function renderHead(run) {
  $("run-id").textContent = run.name;
  const c = run.counts;
  const scored = c.passed + c.failed;
  const scores = run.cells.flatMap((x) => x.trials).filter((t) => t.state === "passed" || t.state === "failed");
  const reach = scores.length ? scores.reduce((a, t) => a + (Number(t.score) || 0), 0) / scores.length : null;
  const fig = (label, value, sub) => h("div", {}, h("dt", {}, label),
    h("dd", {}, value, sub ? h("small", {}, sub) : null));
  const figs = [
    fig("trials", `${done(c)}`, `/${c.planned}`),
    fig("active", `${c.running}`, `/${run.parallel}`),
    fig("passed", `${c.passed}`, `/${scored}`),
    fig("reach", fmt.r(reach)),
  ];
  // Subscription runs: no metered spend, so no cap applies; the API-equivalent
  // figure is an estimate, labelled as one (matching summary.txt).
  if (run.subscription) {
    figs.push(fig("spend", fmt.usd(0), " subscription"));
    if (run.api_equivalent_usd !== null) {
      figs.push(fig("api equiv. (est.)", `~${fmt.usd(run.api_equivalent_usd)}`));
    }
  } else {
    figs.push(fig("spend", fmt.usd(run.spent_usd), run.budget_usd !== null && run.budget_usd !== undefined
      ? `/${fmt.usd(run.budget_usd)}` : null));
  }
  if (c.error) figs.push(fig("errors", `${c.error}`));
  if (run.state === "running") figs.push(fig("eta", run.eta_s !== null ? `~${fmt.dur(run.eta_s)}` : "—"));
  $("figures").replaceChildren(...figs);

  const bar = $("progress");
  bar.setAttribute("aria-label", `${done(c)} of ${c.planned} trials finished`);
  bar.replaceChildren(...PROGRESS_ORDER.filter((k) => c[k]).map((k) =>
    h("span", { class: k, style: `width:${(100 * c[k]) / (c.planned || 1)}%`, title: `${TRIAL_LABEL[k]} ${c[k]}` })));

  const notice = $("notice");
  const msg = {
    stale: `Heartbeat stopped ${fmt.ago(run.heartbeat_at)} ago${run.host ? ` (pid ${run.pid} on ${run.host})` : ""}. `
      + "The process has most likely exited; `toolbench resume` finishes the remaining trials.",
    failed: `Stopped before finalizing${run.error ? `: ${run.error}` : "."}`,
    aborted: run.abort_reason === "session_limit"
      ? "Stopped early: subscription session limit. Resume after the quota resets."
      : "Stopped early: budget cap reached.",
    unknown: "Recorded before live status existed; liveness cannot be determined. "
      + `Last activity ${fmt.ago(run.last_activity)} ago.`,
  }[run.state];
  notice.hidden = !msg;
  notice.className = `notice ${run.state}`;
  notice.textContent = msg || "";
}

/* ── matrix + recent ──────────────────────────────────────── */

function renderLegend() {
  const s = (state, extra) => square({ state, ...extra }, { live: false });
  const item = (el, text) => h("span", {}, el, text);
  $("legend").replaceChildren(
    item(h("span", { class: "ramp", "aria-hidden": "true" }, [0, 1, 2, 3, 4].map((i) =>
      h("i", { style: `background:var(--r${i})` }))), "reach 0→1"),
    item(s("passed", { score: 1 }), "✓ passed"),
    item(s("running"), "running"),
    item(s("queued"), "queued"),
    item(s("interrupted"), "interrupted"),
    item(s("error"), "! infra error"),
    item(s("leak"), "⚑ leak"));
}

function renderMatrix(run) {
  const m = $("matrix");
  if (!run.cells.length) {
    m.replaceChildren(h("p", { class: "empty" }, "No planned trials."));
    return;
  }
  const n = Math.max(...run.cells.map((c) => c.trials.length));
  const header = h("div", { class: "row" },
    h("div", { class: "cellname hdr" }, "cell"),
    h("div", { class: "seeds hdr" }, Array.from({ length: n }, (_, i) => h("span", { class: "idx" }, i))),
    h("div", { class: "stats hdr" }, "mean reach", h("span", { class: "pass" }, "pass")));
  const rows = run.cells.map((c) => h("div", { class: "row" },
    h("div", { class: "cellname" }, h("b", {}, c.condition ?? "—"), h("span", {}, c.model ?? "")),
    h("div", { class: "seeds" }, c.trials.map((t) => square(t))),
    h("div", { class: "stats" },
      h("span", { class: "meter", "aria-hidden": "true" },
        h("i", { style: `width:${100 * (c.mean_reach || 0)}%` })),
      h("span", { class: "val" }, fmt.r(c.mean_reach)),
      h("span", { class: "pass" }, c.n_scored ? `${c.n_passed}/${c.n_scored}` : "—"))));
  m.replaceChildren(header, ...rows);
}

function renderRecent(run) {
  const ol = $("recent");
  if (!run.recent.length) {
    ol.replaceChildren(h("li", { class: "empty" }, "Nothing finished yet."));
    return;
  }
  ol.replaceChildren(...run.recent.map((t) => h("li", { onclick: () => pin(t.trial_id), title: t.trial_id },
    h("span", { class: `tag ${t.state}` }, t.state === "failed" ? "miss" : t.state === "passed" ? "pass" : t.state),
    h("span", { class: "tid" }, short(t.trial_id)),
    h("span", { class: "num" }, fmt.r(t.score)),
    h("span", { class: "num wall" }, fmt.dur(t.wall_clock_s)))));
}

async function renderSummary(run) {
  const pre = $("view-summary");
  if (ui.summaryFor === run.id) return;
  pre.textContent = "loading…";
  try {
    const res = await fetch(`/api/summary?run=${encodeURIComponent(run.id)}`, { cache: "no-store" });
    if (!res.ok) throw new Error(res.status);
    pre.textContent = await res.text();
    ui.summaryFor = run.id;
  } catch (e) {
    pre.textContent = `summary.txt unavailable (${e.message})`;
  }
}

/* ── inspector ────────────────────────────────────────────── */

function followTarget(run) {
  const trials = run.cells.flatMap((c) => c.trials);
  const running = trials.filter((t) => t.state === "running")
    .sort((a, b) => (b.started_at || 0) - (a.started_at || 0));
  return running[0]?.trial_id || run.recent[0]?.trial_id || null;
}

function pin(trialId) {
  ui.trial = trialId;
  ui.follow = false;
  writeHash();
  render();
}

async function renderInspector(run) {
  const btn = $("follow");
  btn.className = `ghost follow ${ui.follow ? "on" : ""}`;
  btn.textContent = ui.follow ? "● following" : "follow live";
  $("trial-id").textContent = ui.trial || "—";
  const body = $("inspector");
  if (!ui.trial) {
    body.replaceChildren(h("p", { class: "empty" }, "No trial has started."));
    return;
  }
  let d;
  try {
    const res = await fetch(`/api/trial?run=${encodeURIComponent(run.id)}&trial=${encodeURIComponent(ui.trial)}`,
      { cache: "no-store" });
    if (res.status === 404) {
      body.replaceChildren(h("p", { class: "empty" }, "Queued — not started."));
      return;
    }
    if (!res.ok) throw new Error(res.status);
    d = await res.json();
  } catch (e) {
    body.replaceChildren(h("p", { class: "empty" }, `unavailable (${e.message})`));
    return;
  }
  if (d.trial_id !== ui.trial) return; // selection moved on while loading

  const prevLog = body.querySelector(".log");
  const atBottom = !prevLog || prevLog.scrollHeight - prevLog.scrollTop - prevLog.clientHeight < 24;
  const out = [];
  const r = d.row;
  if (r) {
    const state = r.failure_mode === "INTEGRITY_LEAK" ? "leak" : r.ok ? "passed" : "failed";
    const kv = [
      ["outcome", h("span", { class: `tag ${state}` }, TRIAL_LABEL[state])],
      ["reach", fmt.r(r.score)],
      ["failure", r.failure_mode && r.failure_mode !== "NONE" ? r.failure_mode : null],
      ["model", r.resolved_model || r.model],
      ["wall", fmt.dur(r.wall_clock_s)],
      ...(run.subscription
        ? [["cost", `${fmt.usd(0)} · subscription`],
          ["api equiv.", r.estimated_api_equivalent_cost_usd !== null && r.estimated_api_equivalent_cost_usd !== undefined
            ? `~${fmt.usd(r.estimated_api_equivalent_cost_usd)} (est.)` : null]]
        : [["cost", fmt.usd(r.cost_usd)]]),
      ["tokens", `${(r.input_tokens || 0).toLocaleString()} in · ${(r.output_tokens || 0).toLocaleString()} out`],
      ["tool calls", `${r.tool_calls ?? 0}${r.tool_errors ? ` · ${r.tool_errors} err` : ""}`],
    ].filter(([, v]) => v !== null && v !== undefined);
    out.push(h("dl", { class: "kv" }, kv.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])));

    const stages = Object.entries(r.stages || {});
    if (stages.length) {
      out.push(h("h3", { class: "label" }, "stages"), h("ul", { class: "stages" }, stages.map(([id, ok]) =>
        h("li", {}, h("span", { class: ok ? "ok" : "no" }, ok ? "■" : "□"), id,
          !ok && r.stage_credits?.[id] > 0 ? h("span", { class: "credit" }, fmt.r(r.stage_credits[id])) : null))));
    }
    const tools = Object.entries(r.tool_calls_by_name || {}).sort((a, b) => b[1] - a[1]);
    if (tools.length) {
      const max = tools[0][1];
      out.push(h("h3", { class: "label" }, "tools"), h("ul", { class: "tools" }, tools.map(([n, k]) =>
        h("li", {}, h("span", { class: "name" }, n),
          h("span", {}, h("i", { class: "bar", style: `display:block;width:${(100 * k) / max}%` })),
          h("span", { class: "n" }, k)))));
    }
  }
  out.push(h("h3", { class: "label" }, (r ? "log" : "live log") + (d.log_truncated ? " · tail" : "")),
    h("pre", { class: "log" }, d.log ?? "no console.log yet"));
  body.replaceChildren(...out);
  const log = body.querySelector(".log");
  if (atBottom) log.scrollTop = log.scrollHeight;
}

/* ── render + poll ────────────────────────────────────────── */

function currentRun(s) {
  if (s.mode === "run") return s.runs[0] || null;
  let run = s.runs.find((r) => r.id === ui.run);
  if (!run) {
    run = s.runs[0] || null;
    ui.run = run?.id || null;
  }
  return run;
}

function render() {
  const s = ui.state;
  if (!s) return;
  const run = currentRun(s);
  renderBar(s, run);
  if (!run) {
    $("run-id").textContent = "No toolbench runs found.";
    return;
  }
  if (ui.follow) ui.trial = followTarget(run);
  if (ui.view === "summary" && !run.has_summary) ui.view = "matrix";
  writeHash();

  renderHead(run);
  for (const b of document.querySelectorAll(".views button")) {
    b.setAttribute("aria-selected", String(b.dataset.view === ui.view));
    if (b.dataset.view === "summary") {
      b.disabled = !run.has_summary;
      b.title = run.has_summary ? "" : "written when the run finalizes";
    }
  }
  $("view-matrix").hidden = ui.view !== "matrix";
  $("view-summary").hidden = ui.view !== "summary";
  if (ui.view === "matrix") {
    renderMatrix(run);
    renderRecent(run);
  } else {
    renderSummary(run);
  }
  renderInspector(run);
}

async function poll() {
  clearTimeout(ui.timer);
  let delay = 3000;
  try {
    const res = await fetch("/api/state", { cache: "no-store" });
    if (!res.ok) throw new Error(res.status);
    ui.state = await res.json();
    delay = (ui.state.poll_s || 3) * 1000;
    render();
  } catch (e) {
    $("liveness").replaceChildren(h("span", { class: "state failed" }, "disconnected"));
  }
  ui.timer = setTimeout(poll, delay);
}

/* ── wiring ───────────────────────────────────────────────── */

/* Drag the inspector's left edge to resize it; the width persists per
   browser. Double-click restores the default. */
function initSplitter() {
  const body = document.querySelector(".body");
  const handle = $("splitter");
  const KEY = "toolbench-inspector-w";
  const apply = (px) => body.style.setProperty("--inspector-w", `${px}px`);
  try {
    const saved = Number(localStorage.getItem(KEY));
    if (saved) apply(saved);
  } catch { /* storage blocked */ }

  handle.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    handle.classList.add("dragging");
    document.body.classList.add("resizing");
    const right = body.getBoundingClientRect().right;
    const move = (ev) => {
      const w = Math.round(right - ev.clientX);
      apply(Math.max(360, Math.min(w, body.clientWidth - 420)));
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.classList.remove("dragging");
      document.body.classList.remove("resizing");
      try {
        localStorage.setItem(KEY, String(parseInt(body.style.getPropertyValue("--inspector-w"), 10)));
      } catch { /* storage blocked */ }
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up, { once: true });
  });
  handle.addEventListener("dblclick", () => {
    body.style.removeProperty("--inspector-w");
    try { localStorage.removeItem(KEY); } catch { /* storage blocked */ }
  });
}

$("picker").addEventListener("change", (e) => {
  ui.run = e.target.value;
  ui.follow = true;
  ui.summaryFor = null;
  render();
});
$("follow").addEventListener("click", () => { ui.follow = true; render(); });
for (const b of document.querySelectorAll(".views button")) {
  b.addEventListener("click", () => { if (!b.disabled) { ui.view = b.dataset.view; render(); } });
}
$("theme").addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme
    || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("toolbench-theme", next); } catch { /* storage blocked */ }
});
try {
  const saved = localStorage.getItem("toolbench-theme");
  if (saved) document.documentElement.dataset.theme = saved;
} catch { /* storage blocked */ }
window.addEventListener("scroll", untip, { passive: true });
initSplitter();
renderLegend();
readHash();
poll();
