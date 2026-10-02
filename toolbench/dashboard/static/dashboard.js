/* toolbench run dashboard — client.
 *
 * Polls /api/state and re-renders one run. The inspector follows the most
 * recently started trial until the viewer picks one (which pins it);
 * "follow" resumes. Selection lives in the URL hash so a reload keeps the
 * view. Everything read from disk is inserted as text, never as HTML. */

"use strict";

const TRIAL_LABEL = {
  queued: "queued", running: "running", interrupted: "interrupted",
  passed: "passed", failed: "not passed", crashed: "crashed (scored 0)",
  excluded: "excluded (not scored)", leak: "integrity leak (scored 0)",
};
const RUN_LABEL = {
  running: "live", finished: "finished", aborted: "aborted", failed: "failed",
  stale: "stale", unknown: "no heartbeat",
};
const PROGRESS_ORDER = ["passed", "failed", "crashed", "leak", "excluded", "interrupted", "running"];
// Finished trials that enter the metrics (summary.txt leaves out only "excluded").
const SCORED = new Set(["passed", "failed", "crashed", "leak"]);

const ui = {
  state: null,
  run: null,        // selected run id (campaign mode)
  view: "matrix",   // "matrix" | "summary"
  trial: null,      // trial shown in the inspector
  follow: true,     // inspector tracks the newest active trial
  itab: "overview", // inspector tab: "overview" | "log" | "prompts" | "files"
  inspected: null,  // trial the inspector's per-trial state was last reset for
  feed: null,       // the inspected trial's activity (see freshFeed)
  toolsOpen: new Set(), // tool-call cards expanded in the log
  toolFilter: null, // tool name the log is narrowed to
  tlOpen: true,     // log timeline expanded
  raw: false,       // log shows console.log as written
  dotsW: 0,         // width the run strip was last laid out for
  expanded: new Set(), // sandbox directories open in the tree
  ffile: null,      // sandbox file being viewed, or FEEDBACK
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
  bytes(n) {
    if (n === null || n === undefined) return "";
    if (n < 1024) return `${n} B`;
    const u = ["KB", "MB", "GB"];
    let v = n / 1024, i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${u[i]}`;
  },
};

const done = (c) => c.passed + c.failed + c.crashed + c.excluded + c.leak;
const bin = (s) => ((Number(s) || 0) >= 1 ? 4 : Math.min(3, Math.floor((Number(s) || 0) * 4)));
const short = (tid) => tid.replace(/__seed\d+$/, "");

/* ── hash <-> selection ───────────────────────────────────── */

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  ui.run = p.get("run");
  ui.view = p.get("view") === "summary" ? "summary" : "matrix";
  ui.trial = p.get("trial");
  ui.follow = !ui.trial;
  const pane = p.get("pane");
  if (pane === "log" || pane === "tools") ui.itab = "log";
  if (pane === "prompts") ui.itab = "prompts";
  if (pane === "sandbox") ui.itab = "files";
}

function writeHash() {
  const p = new URLSearchParams();
  if (ui.run && ui.state?.mode === "campaign") p.set("run", ui.run);
  if (ui.view !== "matrix") p.set("view", ui.view);
  if (!ui.follow && ui.trial) p.set("trial", ui.trial);
  if (ui.itab !== "overview") p.set("pane", ui.itab === "files" ? "sandbox" : ui.itab);
  history.replaceState(null, "", p.toString() ? `#${p}` : location.pathname);
}

/* ── pieces ───────────────────────────────────────────────── */

function square(t, { live = true } = {}) {
  let cls = `sq ${t.state}`, glyph = "";
  if (t.state === "passed" || t.state === "failed") {
    cls = `sq r${bin(t.score)}`;
    if (t.state === "passed") glyph = "✓";
  } else if (t.state === "crashed") glyph = "!";
  else if (t.state === "excluded") glyph = "–";
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
  showTip(anchor, t.trial_id, parts.join("  ·  "));
}

function showTip(anchor, title, text) {
  const el = $("tip");
  el.replaceChildren(h("b", {}, title), text);
  el.hidden = false;
  const r = anchor.getBoundingClientRect();
  const x = Math.max(8, Math.min(r.left + r.width / 2 - el.offsetWidth / 2, innerWidth - el.offsetWidth - 8));
  const y = r.top - el.offsetHeight - 8 < 52 ? r.bottom + 8 : r.top - el.offsetHeight - 8;
  el.style.left = `${x}px`;
  el.style.top = `${y}px`;
}
const untip = () => { $("tip").hidden = true; };

/* ── header + figures ─────────────────────────────────────── */

/* The run strip: one circle per run, as many as the bar has room for, plus
   a button that opens the full library. A circle's ring is the run's
   progress in its state colour (a live run's ring has an orbiting sweep);
   its fill is the run's mean reach on the matrix's ramp. */
const DOT = 18, DOT_GAP = 7, MORE_W = 52;

function runReach(run) {
  const scored = run.cells.flatMap((c) => c.trials).filter((t) => SCORED.has(t.state));
  return scored.length ? scored.reduce((a, t) => a + (Number(t.score) || 0), 0) / scored.length : null;
}

// Runs that share a benchmark are told apart by their start time.
function runTitle(r, shared) {
  return (r.benchmark || r.name)
    + (shared.has(r.benchmark) && r.created_at ? ` (${r.created_at.replace("T", " ")})` : "");
}

function runLine(r) {
  const reach = runReach(r);
  return [`${done(r.counts)}/${r.counts.planned} trials`, reach === null ? null : `reach ${fmt.r(reach)}`,
    RUN_LABEL[r.state] || r.state].filter(Boolean).join("  ·  ");
}

function dot(r, { live = true, shared = new Set() } = {}) {
  const reach = runReach(r);
  const pct = r.counts.planned ? (100 * done(r.counts)) / r.counts.planned : 0;
  const style = `--p:${pct.toFixed(1)}%;${reach === null ? "" : `--fill:var(--r${bin(reach)})`}`;
  const cls = `dot ${r.state}${reach === null ? "" : " scored"}`;
  if (!live) return h("span", { class: cls, style, "aria-hidden": "true" });
  return h("button", {
    class: cls + (r.id === ui.run ? " sel" : ""), type: "button", style,
    "aria-label": `${runTitle(r, shared)}: ${runLine(r)}`,
    "aria-current": r.id === ui.run ? "true" : null,
    onclick: () => selectRun(r.id),
    onmouseenter: (e) => showTip(e.currentTarget, runTitle(r, shared), `${r.name}\n${runLine(r)}`),
    onmouseleave: untip,
    onfocus: (e) => showTip(e.currentTarget, runTitle(r, shared), `${r.name}\n${runLine(r)}`),
    onblur: untip,
  });
}

function sharedBenchmarks(s) {
  return new Set(s.runs.map((r) => r.benchmark).filter((b, i, all) => all.indexOf(b) !== i));
}

function renderRuns(s) {
  const nav = $("runs");
  const shared = sharedBenchmarks(s);
  ui.dotsW = nav.clientWidth;
  // How many circles fit beside the library button; the selected run is
  // always among them, taking the last slot if it would otherwise be hidden.
  const fit = Math.max(1, Math.floor((ui.dotsW - MORE_W + DOT_GAP) / (DOT + DOT_GAP)));
  let shown = s.runs.slice(0, fit);
  const sel = s.runs.find((r) => r.id === ui.run);
  if (sel && !shown.includes(sel)) shown = [...shown.slice(0, fit - 1), sel];
  $("dots").replaceChildren(...shown.map((r) => dot(r, { shared })));

  const hidden = s.runs.length - shown.length;
  const more = $("more");
  more.textContent = hidden ? `+${hidden}` : "all";
  more.title = `All ${s.runs.length} runs`;
  more.setAttribute("aria-label", `All ${s.runs.length} runs`);
  if (!$("library").hidden) renderLibrary(s);
}

function renderLibrary(s) {
  const q = $("lib-q").value.trim().toLowerCase();
  const shared = sharedBenchmarks(s);
  const runs = s.runs.filter((r) => !q || [r.benchmark, r.name, r.id, RUN_LABEL[r.state]]
    .some((x) => String(x || "").toLowerCase().includes(q)));
  const list = $("lib-list");
  if (!runs.length) {
    list.replaceChildren(h("li", { class: "empty" }, "No runs match."));
    return;
  }
  list.replaceChildren(...runs.map((r) => h("li", {},
    h("button", { type: "button", class: r.id === ui.run ? "sel" : null, title: r.id,
      onclick: () => { closeLibrary(); selectRun(r.id); } },
      dot(r, { live: false }),
      h("span", { class: "lib-name" }, h("b", {}, runTitle(r, shared)), h("span", {}, r.name)),
      h("span", { class: "lib-prog" }, `${done(r.counts)}/${r.counts.planned}`),
      h("span", { class: "lib-reach" }, fmt.r(runReach(r))),
      h("span", { class: `lib-state ${r.state}` }, RUN_LABEL[r.state] || r.state)))));
}

function openLibrary() {
  $("library").hidden = false;
  $("more").setAttribute("aria-expanded", "true");
  untip();
  renderLibrary(ui.state);
  $("lib-q").focus();
}

function closeLibrary() {
  $("library").hidden = true;
  $("more").setAttribute("aria-expanded", "false");
}

function selectRun(id) {
  ui.run = id;
  ui.follow = true;
  ui.summaryFor = null;
  render();
}

function renderBar(s, run) {
  // A campaign is named by its directory; a single run by its benchmark.
  const title = s.mode === "campaign" ? s.name : run?.benchmark || s.name;
  $("bench").textContent = title;
  document.title = `${s.live_runs ? "● " : ""}${title} · toolbench`;

  $("runs").hidden = s.mode !== "campaign";
  if (s.mode === "campaign") renderRuns(s);

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
  const scored = c.passed + c.failed + c.crashed + c.leak;
  const reach = runReach(run);
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
  if (c.crashed) figs.push(fig("crashed", `${c.crashed}`));
  if (c.excluded) figs.push(fig("excluded", `${c.excluded}`));
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
    item(s("crashed"), "! crashed"),
    item(s("excluded"), "– excluded"),
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

const MARK_GLYPH = { same: "", modified: "~", added: "+", deleted: "−" };

async function trialApi(route, run, extra = {}) {
  const q = new URLSearchParams({ run: run.id, trial: ui.trial, ...extra });
  const res = await fetch(`/api/${route}?${q}`, { cache: "no-store" });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(res.status);
  return res.json();
}

async function renderInspector(run) {
  const btn = $("follow");
  btn.className = `ghost follow ${ui.follow ? "on" : ""}`;
  btn.textContent = ui.follow ? "● following" : "follow live";
  for (const b of document.querySelectorAll(".itabs button")) {
    b.setAttribute("aria-selected", String(b.dataset.itab === ui.itab));
  }
  $("trial-id").textContent = ui.trial || "—";
  if (ui.trial !== ui.inspected) {  // a new trial: start with everything collapsed
    ui.inspected = ui.trial;
    ui.expanded = new Set();
    ui.ffile = null;
    ui.toolsOpen = new Set();
    ui.toolFilter = null;
  }
  const body = $("inspector");
  if (!ui.trial) {
    body.replaceChildren(h("p", { class: "empty" }, "No trial has started."));
    return;
  }
  const key = `${ui.trial}|${ui.itab}|${ui.ffile}`;
  try {
    if (ui.itab === "prompts") {
      if (body.dataset.key !== key) await renderPrompts(run, body);  // prompts never change
    } else if (ui.itab === "files") {
      await renderFiles(run, body);
    } else if (ui.itab === "log") {
      await renderLog(run, body, key);
    } else {
      await renderOverview(run, body);
    }
    body.dataset.key = key;
  } catch (e) {
    body.replaceChildren(h("p", { class: "empty" }, `unavailable (${e.message})`));
  }
}

async function renderPrompts(run, body) {
  const p = await trialApi("prompts", run);
  if (!p) {
    body.replaceChildren(h("p", { class: "empty" },
      "No prompts recorded (the trial has not started, or predates prompt recording)."));
    return;
  }
  body.replaceChildren(
    h("h3", { class: "label" }, "system"), h("pre", { class: "log prompt" }, p.system || "(empty)"),
    h("h3", { class: "label" }, "user"), h("pre", { class: "log prompt" }, p.user || "(empty)"));
}

// Pseudo-path for the agent's post-task feedback, pinned atop the tree.
const FEEDBACK = "\u0000feedback";

/* The sandbox tab: an expandable tree, or one file (or the feedback) open
   for reading. Expanded folders are re-listed on every poll, so the marks
   stay live; scroll position survives the refresh. */
async function renderFiles(run, body) {
  const scroller = body.querySelector(".scroll");
  const keep = body.dataset.key === `${ui.trial}|${ui.itab}|${ui.ffile}`;
  const top = keep && scroller ? scroller.scrollTop : 0;
  const back = h("button", { type: "button", class: "ghost back",
    onclick: () => { ui.ffile = null; render(); } }, "‹ sandbox");

  if (ui.ffile === FEEDBACK) {
    body.replaceChildren(back, ...feedbackView(await trialApi("feedback", run)));
  } else if (ui.ffile) {
    const f = await trialApi("file", run, { path: ui.ffile });
    if (!f) { ui.ffile = null; return renderFiles(run, body); }  // removed since
    body.replaceChildren(
      h("div", { class: "filehead" }, back, h("span", { class: "fpath" }, ui.ffile)),
      h("div", { class: "fmeta" }, `${fmt.bytes(f.size)}${f.truncated ? " · first 256 KB" : ""}`),
      h("pre", { class: "log scroll" }, f.binary ? "(binary file)" : f.text));
  } else {
    const root = await trialApi("files", run, { path: "" });
    if (!root) {
      body.replaceChildren(h("p", { class: "empty" }, "No workspace yet."));
      return;
    }
    // Fetch every open folder in parallel; forget ones that have vanished.
    const open = [...ui.expanded];
    const listed = await Promise.all(open.map((d) => trialApi("files", run, { path: d }).catch(() => null)));
    const children = new Map();
    open.forEach((d, i) => (listed[i] ? children.set(d, listed[i].entries) : ui.expanded.delete(d)));

    const rows = [];
    if (root.has_feedback) {
      rows.push(h("li", { class: "f feedback", onclick: () => { ui.ffile = FEEDBACK; render(); } },
        h("span", { class: "mk" }, "◆"), h("span", { class: "nm" }, "agent feedback"), h("span", { class: "sz" }, "")));
    }
    const walk = (entries, depth) => {
      for (const e of entries) {
        const isDir = e.type === "dir";
        const isOpen = isDir && ui.expanded.has(e.path);
        rows.push(h("li", {
          class: `f ${e.mark || ""} ${e.type}`,
          style: `--depth:${depth}`,
          "aria-expanded": isDir ? String(isOpen) : null,
          onclick: e.mark === "deleted" ? null : () => {
            if (isDir) {
              if (isOpen) ui.expanded.delete(e.path); else ui.expanded.add(e.path);
            } else {
              ui.ffile = e.path;
            }
            render();
          },
        },
          h("span", { class: "mk" }, MARK_GLYPH[e.mark] ?? ""),
          h("span", { class: "nm" },
            h("span", { class: "caret" }, isDir && e.mark !== "deleted" ? (isOpen ? "▾" : "▸") : ""),
            e.name + (isDir ? "/" : "")),
          h("span", { class: "sz" }, e.size === null ? "" : fmt.bytes(e.size))));
        if (isOpen) walk(children.get(e.path) || [], depth + 1);
      }
    };
    walk(root.entries, 0);

    const source = {
      sandbox: run.state === "running" ? "live sandbox" : "sandbox (kept)",
      artifacts: "preserved artifacts · sandbox removed",
      none: "no sandbox or artifacts on disk",
    }[root.source];
    body.replaceChildren(
      h("div", { class: "fmeta" }, source,
        root.has_init ? h("span", { class: "fkey" },
          h("span", { class: "added" }, "+ added"), h("span", { class: "modified" }, "~ modified"),
          root.source === "sandbox" ? h("span", { class: "deleted" }, "− deleted") : null,
          h("span", { class: "same" }, "at start"))
          : " · no initial snapshot"),
      h("ul", { class: "files scroll" }, rows.length ? rows : h("li", { class: "empty" }, "empty")),
      root.truncated ? h("p", { class: "empty" }, "listing truncated") : null);
  }
  const sc = body.querySelector(".scroll");
  if (sc) sc.scrollTop = top;
}

/* The agent's post-task feedback, labelled by the turn that produced it. */
function feedbackView(fb) {
  if (!fb) return [h("p", { class: "empty" }, "No feedback recorded for this trial.")];
  const out = [];
  const block = (title, note, text) => out.push(
    h("h3", { class: "label" }, title, note ? h("span", { class: "note" }, note) : null),
    h("pre", { class: "log prose" }, text));
  if (fb.blind_rating && fb.response) {
    block("before grading", "blind rating", fb.blind_rating);
    block("after grading", "audit + critique, grade revealed", fb.response);
  } else if (fb.blind_rating) {
    block("experience rating", "no domain tools served", fb.blind_rating);
  } else if (fb.response) {
    block("critique", "grade not revealed", fb.response);
  }
  if (fb.error) block("feedback turn failed", null, fb.error);
  return out;
}

function showTab(itab, filter = null) {
  ui.itab = itab;
  if (itab === "log") ui.toolFilter = filter;
  $("inspector").dataset.key = "";
  render();
}

async function renderOverview(run, body) {
  const d = await trialApi("trial", run);
  const t = run.cells.flatMap((c) => c.trials).find((x) => x.trial_id === ui.trial);
  if (!d) {
    body.replaceChildren(h("p", { class: "empty" }, "Queued — not started."));
    return;
  }
  if (d.trial_id !== ui.trial) return; // selection moved on while loading

  const r = d.row;
  const toLog = h("button", { type: "button", class: "ghost link", onclick: () => showTab("log") }, "log ›");
  if (!r) {
    const live = t?.state === "running";
    body.replaceChildren(
      h("dl", { class: "kv" },
        h("dt", {}, "status"), h("dd", {}, h("span", { class: `tag ${t?.state || ""}` }, TRIAL_LABEL[t?.state] || "—")),
        live ? [h("dt", {}, "up"), h("dd", {}, fmt.ago(t.started_at)),
          h("dt", {}, "last output"), h("dd", {}, `${fmt.ago(t.last_activity)} ago`)] : null),
      h("p", { class: "empty" }, live ? "In flight — follow it live in the " : "Did not finish — see the ", toLog,
        live ? ". The grade appears here when it finishes." : "."));
    return;
  }
  const out = [];
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
  // Per-tool counts; a name opens the tools tab narrowed to that tool.
  const tools = Object.entries(r.tool_calls_by_name || {}).sort((a, b) => b[1] - a[1]);
  if (tools.length) {
    const max = tools[0][1];
    out.push(h("h3", { class: "label" }, "tool use",
      h("button", { type: "button", class: "ghost link note", onclick: () => showTab("log") }, "all calls ›")),
    h("ul", { class: "tools" }, tools.map(([n, k]) =>
      h("li", { onclick: () => showTab("log", n), title: `show the ${n} calls` },
        h("span", { class: "name" }, n),
        h("span", {}, h("i", { class: "bar", style: `display:block;width:${(100 * k) / max}%` })),
        h("span", { class: "n" }, k)))));
  }
  body.replaceChildren(...out);
}

/* ── log tab ──────────────────────────────────────────────── */

/* The log tab is the trial's activity as it happens: the agent's messages
   and its tool calls (expandable to their full input and output), with a
   timeline of the calls above. Live trials stream events.jsonl; each poll
   fetches only the bytes appended since the last one and appends their
   cards, so a long trial costs no more per poll than a short one. Trials
   recorded before events.jsonl fall back to the transcript once finished,
   or to the raw console.log while running. "raw" shows console.log. */

const RETRY_LABEL = { format_retry: "format retry", rate_limit_retry: "rate-limit resume",
  transient_retry: "transient-error resume" };
const TRUNC_MARK = "...[truncated]";

const clock = (t) => {
  t = Math.max(0, Number(t) || 0);
  const m = Math.floor(t / 60);
  return `${String(m).padStart(2, "0")}:${(t - 60 * m).toFixed(1).padStart(4, "0")}`;
};

// A tool call's duration: tenths below ten seconds, where most calls fall.
const callDur = (s) => (Number(s) < 10 ? `${(Number(s) || 0).toFixed(1)}s` : fmt.dur(s));

// One-line preview of a call's arguments: key=value, values abbreviated.
function argPreview(args) {
  if (!args || typeof args !== "object") return "";
  return Object.entries(args).map(([k, v]) => {
    let s = typeof v === "string" ? v : JSON.stringify(v);
    s = String(s).replace(/\s+/g, " ");
    return `${k}=${s.length > 40 ? `${s.slice(0, 39)}…` : s}`;
  }).join("  ");
}

// A value as readable text: strings verbatim, anything else as indented JSON.
const pretty = (v) => (typeof v === "string" ? v : JSON.stringify(v, null, 2));

// Results are recorded as str(result): pretty-print it when it is JSON.
function prettyResult(text) {
  const t = text.trim();
  if (/^[[{]/.test(t)) {
    try { return JSON.stringify(JSON.parse(t), null, 2); } catch { /* not JSON */ }
  }
  return text;
}

// The inspected trial's activity: events fetched so far and where to resume.
function freshFeed() {
  return { trial: ui.trial, events: [], next: 0, source: null, calls: 0 };
}

async function pullFeed(run) {
  const f = ui.feed;
  const added = [];
  for (let i = 0; i < 16 && f.next !== null; i++) {  // a long backlog: keep reading
    const d = await trialApi("events", run, { since: f.next });
    if (ui.feed !== f) return null;  // the trial changed while loading
    if (!d) { f.source = "queued"; break; }
    f.source = d.source;
    f.next = d.next;
    added.push(...d.events);
    if (!d.more) break;
  }
  f.events.push(...added);
  return added;
}

const feedCalls = (events) => events.filter((e) => e.type === "tool_call" || e.type === "tool_start");

async function renderLog(run, body, key) {
  const t = run.cells.flatMap((c) => c.trials).find((x) => x.trial_id === ui.trial);
  if (ui.raw) return renderRaw(run, body, key);
  if (ui.feed?.trial !== ui.trial) ui.feed = freshFeed();
  const added = await pullFeed(run);
  if (added === null) return;
  const f = ui.feed;
  if (f.source === "queued") {
    body.replaceChildren(h("p", { class: "empty" }, "Queued — not started."));
    return;
  }
  if (f.source === "none") {
    // No event stream (an older runner): its console.log is all there is.
    if (t?.state === "running") return renderRaw(run, body, key, "this trial does not stream events — showing console.log");
    body.replaceChildren(logBar(), h("p", { class: "empty" }, "No activity was recorded for this trial."));
    return;
  }
  const feed = body.querySelector(".feed");
  if (body.dataset.key === key && feed) {
    if (!added.length) return;
    const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 32;
    for (const e of added) placeEvent(feed, e);
    redrawTimeline(body);
    body.querySelector(".tsum").replaceWith(feedSummary());
    if (atBottom) feed.scrollTop = feed.scrollHeight;
    return;
  }
  // A full build: a new trial, tab or filter.
  const list = h("div", { class: "feed scroll" });
  for (const e of f.events) placeEvent(list, e);
  if (!list.childElementCount) list.append(h("p", { class: "empty waiting" }, "Waiting for the agent's first move…"));
  body.replaceChildren(logBar(), timelineBox(), list);
  redrawTimeline(body);
  if (t?.state === "running") list.scrollTop = list.scrollHeight;
}

// Summary + filter chip (when narrowed) on the left; expand/collapse and
// the raw toggle on the right.
function logBar() {
  return h("div", { class: "tctl" },
    h("span", { class: "tleft" }, ui.raw ? null : feedSummary(),
      ui.toolFilter ? h("span", { class: "chip" }, ui.toolFilter,
        h("button", { type: "button", class: "ghost", "aria-label": "clear filter",
          onclick: () => showTab("log") }, "×")) : null),
    h("span", {},
      ui.raw ? null : h("button", { type: "button", class: "ghost", onclick: () => toggleAll(true) }, "expand all"),
      ui.raw ? null : h("button", { type: "button", class: "ghost", onclick: () => toggleAll(false) }, "collapse all"),
      h("button", { type: "button", class: `ghost${ui.raw ? " on" : ""}`, title: "console.log as written",
        onclick: () => { ui.raw = !ui.raw; showTab("log", ui.toolFilter); } }, ui.raw ? "‹ activity" : "raw")));
}

function feedSummary() {
  const calls = feedCalls(ui.feed.events);
  const done = calls.filter((c) => c.type === "tool_call");
  const failed = done.filter((c) => !c.ok).length;
  const finished = new Set(done.map((c) => c.id));
  const open = calls.filter((c) => c.type === "tool_start" && !finished.has(c.id)).length;
  const names = new Set(done.map((c) => c.name));
  return h("div", { class: "fmeta tsum" },
    h("span", {}, `${done.length} call${done.length === 1 ? "" : "s"}`),
    failed ? h("span", { class: "bad" }, `${failed} failed`) : null,
    open > 0 ? h("span", { class: "live" }, `${open} running`) : null,
    h("span", {}, `${names.size} tool${names.size === 1 ? "" : "s"}`),
    ui.feed.source === "transcript" ? h("span", {}, "from transcript") : null);
}

// Append one event to the feed: a tool_call replaces its tool_start's card.
function placeEvent(list, e) {
  list.querySelector(".waiting")?.remove();
  const hiddenByFilter = ui.toolFilter && (e.type === "agent" || ((e.type === "tool_call" || e.type === "tool_start") && e.name !== ui.toolFilter));
  let el;
  if (e.type === "tool_start" || e.type === "tool_call") {
    if (e.type === "tool_call" && e.id === undefined) e.id = null;
    el = callRow(e);
    const prior = e.id !== null && list.querySelector(`details[data-n="${e.id}"]`);
    if (prior) {
      el.open = prior.open;
      prior.replaceWith(el);
      return;
    }
  } else if (e.type === "agent") {
    const long = e.text.length > 600 || e.text.split("\n").length > 6;
    el = h("div", { class: `msg${long ? " long" : ""}`, title: long ? "click to expand" : null,
      onclick: (ev) => ev.currentTarget.classList.add("full") },
      h("span", { class: "ct" }, clock(e.t)), h("div", { class: "mtext" }, e.text));
  } else if (e.type === "intervention") {
    el = retryRow(e);
  } else {
    return;
  }
  if (hiddenByFilter) el.hidden = true;
  list.append(el);
}

function callRow(c) {
  const running = c.type === "tool_start";
  const n = c.id ?? "·";
  const args = c.args && typeof c.args === "object" ? Object.entries(c.args) : [];
  const res = String(c.result_summary ?? "");
  const truncated = res.endsWith(TRUNC_MARK);
  const start = running ? Number(c.t) || 0 : (Number(c.t) || 0) - (Number(c.duration_s) || 0);
  return h("details", { class: `call${running ? " pending" : c.ok ? "" : " bad"}`, "data-n": c.id ?? null,
    open: c.id !== null && ui.toolsOpen.has(c.id),
    ontoggle: (ev) => { if (c.id === null) return; ev.target.open ? ui.toolsOpen.add(c.id) : ui.toolsOpen.delete(c.id); } },
    h("summary", {},
      h("span", { class: "cn" }, String(n).padStart(2, "0")),
      h("span", { class: "ct" }, clock(start)),
      h("span", { class: "ck" }, running ? h("i", { class: "spin", "aria-label": "running" }) : c.ok ? "✓" : "✕"),
      h("span", { class: "cname" }, c.name),
      h("span", { class: "cargs" }, argPreview(c.args)),
      h("span", { class: "cdur" }, running ? "…" : callDur(c.duration_s))),
    h("div", { class: "cbody" },
      h("h4", { class: "label" }, "input"),
      args.length
        ? h("dl", { class: "args" }, args.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, h("pre", {}, pretty(v)))]))
        : h("p", { class: "empty" }, "(no arguments)"),
      h("h4", { class: "label" }, running ? "output" : c.ok ? "output" : "output · error",
        truncated ? h("span", { class: "note" }, "first 1000 chars, as recorded") : null),
      running ? h("p", { class: "empty" }, "running…")
        : h("pre", { class: "out" }, res ? prettyResult(truncated ? res.slice(0, -TRUNC_MARK.length) : res) : "(empty)")));
}

function retryRow(e) {
  return h("details", { class: "call retry" },
    h("summary", {}, h("span", { class: "cn" }, "↻"),
      h("span", { class: "rlabel" }, RETRY_LABEL[e.kind] || e.kind || "recovery turn"),
      h("span", { class: "cargs" }, e.reason || "")),
    h("div", { class: "cbody" }, h("h4", { class: "label" }, "injected message"),
      h("pre", { class: "out" }, e.injected_message || "(none)")));
}

/* Timeline: a lane per tool (in order of first use), each call a mark from
   its start to its end — a running call reaches to the latest event.
   Recovery turns are dashed rules across every lane. Collapsible. */
function timelineBox() {
  return h("details", { class: "tl", open: ui.tlOpen,
    ontoggle: (ev) => { ui.tlOpen = ev.target.open; } },
    h("summary", { class: "label" }, "timeline"), h("div", { class: "timeline" }));
}

function redrawTimeline(body) {
  const box = body.querySelector(".timeline");
  if (!box) return;
  const evs = ui.feed.events;
  const done = new Map(), started = new Map();
  let calls = 0;
  for (const e of evs) {
    if (e.type === "tool_start") started.set(e.id, e);
    else if (e.type === "tool_call") done.set(e.id ?? `x${calls}`, e), calls++;
  }
  const marks = [];
  for (const [id, c] of done) {
    const end = Number(c.t) || 0;
    marks.push({ id, name: c.name, start: end - (Number(c.duration_s) || 0), end, ok: c.ok, dur: c.duration_s });
  }
  const span = Math.max(1e-6, ...evs.map((e) => Number(e.t) || 0));
  for (const [id, c] of started) {
    if (!done.has(id)) marks.push({ id, name: c.name, start: Number(c.t) || 0, end: span, running: true });
  }
  if (!marks.length) {
    box.replaceChildren(h("p", { class: "empty" }, "No tool calls yet."));
    return;
  }
  marks.sort((a, b) => a.start - b.start);
  const names = [...new Set(marks.map((m) => m.name))];
  const at = (t) => `${(100 * Math.max(0, t)) / span}%`;
  // Transcript-sourced recovery turns carry no time: place them after their call.
  const callEnds = marks.filter((m) => !m.running).map((m) => m.end);
  const retries = evs.filter((e) => e.type === "intervention")
    .map((e) => (e.t !== undefined ? Number(e.t) : e.after_tool_call ? callEnds[e.after_tool_call - 1] ?? 0 : 0));
  box.replaceChildren(
    ...names.map((name) => h("div", { class: `lane${ui.toolFilter === name ? " on" : ""}${ui.toolFilter && ui.toolFilter !== name ? " dim" : ""}` },
      h("button", { type: "button", class: "lane-name", title: `show only ${name}`,
        onclick: () => showTab("log", ui.toolFilter === name ? null : name) }, name),
      h("div", { class: "track" },
        retries.map((t) => h("i", { class: "retry", style: `left:${at(t)}` })),
        marks.filter((m) => m.name === name).map((m) => {
          const info = m.running ? `running since ${clock(m.start)}` : `${m.ok ? "ok" : "failed"}  ·  ${callDur(m.dur)} at ${clock(m.start)}`;
          return h("button", {
            type: "button", class: `tick${m.running ? " running" : m.ok ? "" : " bad"}`,
            style: `left:${at(m.start)};width:${at(m.end - m.start)}`,
            "aria-label": `call ${m.id}, ${name}: ${info}`,
            onmouseenter: (ev) => showTip(ev.currentTarget, `#${m.id}  ${name}`, info),
            onmouseleave: untip,
            onclick: () => openCall(m.id, m.name),
          });
        })))),
    h("div", { class: "axis" }, h("span", {}, "0s"), h("span", {}, fmt.dur(span / 2)), h("span", {}, fmt.dur(span))));
}

function openCall(id, name) {
  if (ui.toolFilter && ui.toolFilter !== name) showTab("log", null);
  const el = $("inspector").querySelector(`details[data-n="${id}"]`);
  if (!el) return;
  el.open = true;
  el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 900);
}

function toggleAll(open) {
  for (const d of $("inspector").querySelectorAll("details.call[data-n]")) d.open = open;
}

/* console.log's tail, as written. Kept pinned to the bottom while the
   viewer is there; updated in place so a selection or scroll survives. */
async function renderRaw(run, body, key, note = null) {
  const d = await trialApi("trial", run);
  if (!d) {
    body.replaceChildren(h("p", { class: "empty" }, "Queued — not started."));
    return;
  }
  if (d.trial_id !== ui.trial) return;
  const title = `console.log${d.log_truncated ? " · tail" : ""}${note ? ` · ${note}` : ""}`;
  const text = d.log ?? "no console.log yet";
  let log = body.dataset.key === key ? body.querySelector(".log.raw") : null;
  if (!log) {
    log = h("pre", { class: "log raw" });
    body.replaceChildren(note ? h("span", {}) : logBar(), h("h3", { class: "label" }, title), log);
    log.textContent = text;
    log.scrollTop = log.scrollHeight;
    return;
  }
  body.querySelector("h3.label").textContent = title;
  if (log.textContent === text) return;
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
  log.textContent = text;
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
  for (const b of document.querySelectorAll("#views button")) {
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

$("more").addEventListener("click", () => ($("library").hidden ? openLibrary() : closeLibrary()));
$("lib-q").addEventListener("input", () => renderLibrary(ui.state));
$("lib-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("lib-list").querySelector("button")?.click();
});
document.addEventListener("pointerdown", (e) => {
  if (!$("library").hidden && !e.target.closest("#library, #more")) closeLibrary();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("library").hidden) { closeLibrary(); $("more").focus(); }
});
// Re-fit the run strip when the bar's room for it changes.
new ResizeObserver(() => {
  if (ui.state?.mode === "campaign" && $("runs").clientWidth !== ui.dotsW) renderRuns(ui.state);
}).observe($("runs"));
$("follow").addEventListener("click", () => { ui.follow = true; render(); });
for (const b of document.querySelectorAll(".itabs button")) {
  b.addEventListener("click", () => { ui.itab = b.dataset.itab; render(); });
}
for (const b of document.querySelectorAll("#views button")) {
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
// A pasted link or back/forward changes only the hash: apply it in place.
// (writeHash uses replaceState, which does not fire this event.)
window.addEventListener("hashchange", () => { readHash(); render(); });
initSplitter();
renderLegend();
readHash();
poll();
