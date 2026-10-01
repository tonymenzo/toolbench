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
  itab: "overview", // inspector tab: "overview" | "prompts" | "files"
  inspected: null,  // trial the sandbox view was last reset for
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

// One picker entry per run: state glyph, benchmark, progress, state word.
// Runs that share a benchmark are told apart by their start time.
const STATE_GLYPH = { running: "●", finished: "■", aborted: "▲", stale: "▲", failed: "✕", unknown: "○" };

function renderPicker(s) {
  const shared = new Set(s.runs.map((r) => r.benchmark).filter((b, i, all) => all.indexOf(b) !== i));
  const label = (r) => [
    STATE_GLYPH[r.state] || "○",
    (r.benchmark || r.name) + (shared.has(r.benchmark) && r.created_at ? ` (${r.created_at.replace("T", " ")})` : ""),
    `${done(r.counts)}/${r.counts.planned}`,
    RUN_LABEL[r.state] || r.state,
  ].join("  ");
  const options = s.runs.map((r) => [r.id, label(r)]);
  const sel = $("picker");
  // Rebuild only on change, so a poll never closes the list while it is open.
  const sig = JSON.stringify([options, ui.run]);
  if (sel.dataset.sig === sig) return;
  sel.dataset.sig = sig;
  sel.replaceChildren(...options.map(([id, text]) =>
    h("option", { value: id, selected: id === ui.run, title: id }, text)));
}

function renderBar(s, run) {
  // A campaign is named by its directory; a single run by its benchmark.
  const title = s.mode === "campaign" ? s.name : run?.benchmark || s.name;
  $("bench").textContent = title;
  document.title = `${s.live_runs ? "● " : ""}${title} · toolbench`;

  const wrap = $("picker-wrap");
  wrap.hidden = s.mode !== "campaign";
  if (s.mode === "campaign") renderPicker(s);

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
  const scores = run.cells.flatMap((x) => x.trials).filter((t) => SCORED.has(t.state));
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
  if (ui.trial !== ui.inspected) {  // a new trial: start with its tree collapsed
    ui.inspected = ui.trial;
    ui.expanded = new Set();
    ui.ffile = null;
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

async function renderOverview(run, body) {
  const d = await trialApi("trial", run);
  if (!d) {
    body.replaceChildren(h("p", { class: "empty" }, "Queued — not started."));
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

$("picker").addEventListener("change", (e) => {
  ui.run = e.target.value;
  ui.follow = true;
  ui.summaryFor = null;
  render();
});
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
