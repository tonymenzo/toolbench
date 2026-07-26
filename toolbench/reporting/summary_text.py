"""
Run-level summary rendering.

Reads the same dict the cli builds for `summary.json` (plus the
manifest for run-level fields) and emits a styled plain-text report
matching the per-trial transcript aesthetic. Used by `toolbench.cli`
to replace the raw JSON dump at run end and to write `summary.txt`
alongside `summary.json`.
"""

from pathlib import Path

from .transcript import LINE_WIDTH, RULE_DOUBLE, RULE_SINGLE, fmt_wall


# Three-vector display order: productivity → exploration →
# trustworthiness. The equal-weighted reach is rendered alongside the
# rubric-weighted one so the score is interpretable without having to
# know the rubric weights. See the accompanying manuscript.
_TRIPLET = [
    ("reach",        "reach_bar_k",          "productivity     (rubric-weighted)"),
    ("reach (eq-w)", "reach_bar_k_uniform",  "depth            (equal-weight, no rubric)"),
    ("pass@k",       "pass_at_k",            "exploration      (best of k)"),
    ("pass^k",       "pass_caret_k",         "trustworthiness  (worst of k)"),
]


def _section_title(name: str) -> list[str]:
    """Markdown-style underlined title (e.g. 'RESULT\\n------')."""
    return [f"  {name}", "  " + "-" * len(name)]


def _banner(title: str) -> list[str]:
    return [RULE_DOUBLE, title.center(LINE_WIDTH), RULE_DOUBLE]


def render_run_summary(summary: dict, manifest: dict | None = None,
                       run_dir: Path | None = None) -> str:
    """Top-to-bottom run summary string (Option β style)."""
    manifest = manifest or {}
    out: list[str] = []

    out.extend(_banner(f"RUN  {manifest.get('task', summary.get('run_id', ''))}"))
    out.extend(_render_run_header(summary, manifest))
    out.append(RULE_DOUBLE)

    integ_block = _render_integrity(summary)
    if integ_block:
        out.append("")
        out.extend(integ_block)

    sl_block = _render_session_limit(summary)
    if sl_block:
        out.append("")
        out.extend(sl_block)

    for cell in summary.get("cells", []):
        out.append("")
        out.append("")
        out.extend(_render_cell(cell))

    deltas = summary.get("paired_deltas") or []
    if deltas:
        out.append("")
        out.append("")
        out.extend(_render_paired_deltas(deltas))

    if run_dir is not None:
        out.append("")
        out.append("")
        out.extend(_render_artifacts(run_dir))

    return "\n".join(out)


def _render_run_header(summary: dict, manifest: dict) -> list[str]:
    run_id   = summary.get("run_id", manifest.get("run_id", ""))
    task     = manifest.get("task", "")
    k        = summary.get("k", manifest.get("n_per_cell", "?"))
    n_total  = summary.get("n_total_trials", 0)
    n_cells  = len(summary.get("cells", []))
    budget   = manifest.get("max_cost_usd")
    spent    = summary.get("total_spent_usd", 0.0)
    estimated = summary.get("estimated_api_equivalent_cost_usd")
    start    = manifest.get("created_at", "")
    wall     = sum(c.get("mean_wall_clock_s", 0.0) * c.get("n", 0)
                   for c in summary.get("cells", []))

    budget_line = (
        f"${budget:.2f} cap   ${spent:.2f} spent"
        if isinstance(budget, (int, float))
        else f"${spent:.2f} spent"
    )
    if isinstance(estimated, (int, float)):
        budget_line += (
            f"   ${estimated:.2f} API-equivalent (estimated, subscription)"
        )

    lines = [
        f"  run_id     {run_id}",
        f"  task       {task:<24}k          {k}   (n per cell)",
        f"  trials     {n_total:<24}cells      {n_cells}",
        f"  budget     {budget_line}",
        f"  start      {start}",
        f"  wall       {fmt_wall(wall)}",
    ]
    # Provenance: pinned versions + git SHA + harness, so a summary is
    # self-describing for reproducibility.
    prov = summary.get("provenance") or {}
    # `versions` is already the runtime-appropriate set (toolbench + the runtime
    # that drove the run), built in cli.py — render it as-is.
    ver_parts = [f"{k} {v}" for k, v in (prov.get("versions") or {}).items()]
    if ver_parts:
        lines.append(f"  versions   {'  '.join(ver_parts)}")
    tail = []
    if prov.get("git_sha"):
        tail.append(f"git {str(prov['git_sha'])[:10]}")
    if prov.get("harnesses"):
        tail.append(f"harness {', '.join(prov['harnesses'])}")
    if tail:
        lines.append(f"  provenance {'   '.join(tail)}")
    integ = summary.get("integrity") or {}
    if "scanned" in integ:
        nf = len(integ.get("flagged") or {})
        status = (f"CLEAN ({integ['scanned']} trials scanned)" if nf == 0
                  else f"** {nf} TRIAL(S) QUARANTINED — reached the answer key **")
        lines.append(f"  integrity  {status}")
    return lines


def _render_integrity(summary: dict) -> list[str]:
    """Loud section listing any trial quarantined for reaching the ground-truth
    answer key, with a snippet of evidence. Emitted only when something was
    flagged; a clean run just gets the one-line header assurance."""
    integ = summary.get("integrity") or {}
    flagged = integ.get("flagged") or {}
    if not flagged:
        return []
    out = ["!" * LINE_WIDTH,
           "  INTEGRITY ALERT — trials quarantined (scored 0)",
           "  " + "-" * 44, "",
           "  These trials referenced the graded answer key outside their",
           "  sandbox; their scores are voided and excluded from the headline.",
           ""]
    for tid, info in flagged.items():
        out.append(f"      {tid}   ({info.get('n_hits', 0)} hit(s))")
        for h in info.get("sample") or []:
            snip = str(h.get("snippet", "")).replace("\n", " ")[:90]
            out.append(f"        {h.get('tool')}: …{snip}…")
    out.append("!" * LINE_WIDTH)
    return out


def _render_session_limit(summary: dict) -> list[str]:
    """Note when a run touched the subscription session/usage quota.

    Rendered only when something was excluded or the queue aborted on the
    quota. Quota terminations are NOT capability failures: the excluded
    trials are recorded but kept out of the scored metrics, and any
    un-attempted trials are finished by `resume` once the quota resets.
    """
    sl = summary.get("session_limit") or {}
    excl = int(sl.get("excluded_trials", 0) or 0)
    na = int(sl.get("not_attempted", 0) or 0)
    if not (sl.get("aborted") or excl or na):
        return []
    out = [*_section_title("SESSION / USAGE LIMIT"), ""]
    if sl.get("aborted"):
        out += [
            "      The run stopped early — the subscription account's",
            "      session/usage quota was reached. The trials below are NOT",
            "      capability failures; they are excluded from the scored",
            "      metrics (reach / pass@k / stage funnel).",
        ]
    else:
        out += [
            "      Some trials hit the subscription session/usage quota and",
            "      are excluded from the scored metrics (not a capability",
            "      failure).",
        ]
    out += [
        "",
        f"      excluded (recorded, unscored):  {excl}",
    ]
    if na:
        out.append(f"      not attempted (queue aborted):  {na}")
    out += [
        "",
        "      Re-run  `toolbench resume --run-id <id>`  after your quota",
        "      resets to finish the remaining trials.",
    ]
    return out


def _render_cell(cell: dict) -> list[str]:
    title = f"CELL  {cell.get('model', '?')}  ×  {cell.get('condition', '?')}"
    lines: list[str] = []
    lines.extend(_banner(title))
    n_excl = int(cell.get("n_excluded", 0) or 0)
    if n_excl:
        # Make the scored-vs-excluded split explicit so the metrics below are
        # read as being over the scored trials only.
        lines.append("")
        lines.append(f"  n = {cell.get('n', 0)} scored   "
                     f"({n_excl} excluded — session/usage limit, "
                     f"not a capability failure)")
    lines.append("")
    lines.extend(_render_three_vector(cell))
    lines.append("")
    lines.extend(_render_stages(cell))
    trials_block = _render_trials(cell)
    if trials_block:
        lines.append("")
        lines.extend(trials_block)
    tools_block = _render_tools(cell)
    if tools_block:
        lines.append("")
        lines.extend(tools_block)
    failures = cell.get("failure_modes") or {}
    if failures:
        lines.append("")
        lines.extend(_render_failures(failures))
    lines.append("")
    lines.extend(_render_cost(cell))
    return lines


def _render_three_vector(cell: dict) -> list[str]:
    k = cell.get("k", "?")
    out = [
        f"  THREE-VECTOR              (k={k})",
        "  " + "-" * len("THREE-VECTOR"),
        "",
        "      Each row is an expectation over k iid trials of a per-trial",
        "      score M_j ∈ [0,1]. bar_k = E[mean], at_k = E[max], ^k = E[min].",
        "      See the accompanying manuscript for definitions and estimators.",
        "",
    ]
    for label, key, annotation in _TRIPLET:
        v = float(cell.get(key, 0.0) or 0.0)
        out.append(f"      {label:<13} {v:.2f}         {annotation}")
    pt = cell.get("pass_threshold")
    crit = f"reach >= {pt:g}" if isinstance(pt, (int, float)) else "all stages pass"
    out.append("")
    out.append(f"      pass criterion: a trial passes iff {crit}")
    return out


def _render_trials(cell: dict) -> list[str]:
    """Per-trial spread the cell mean hides: individual reaches, blind UX
    ratings, and the reliability (retry/nudge) rollup."""
    scores = cell.get("trial_scores") or []
    ux = cell.get("ux_ratings") or []
    ret = cell.get("retries") or {}
    if not scores and not ux:
        return []
    out = [*_section_title("TRIALS"), ""]
    if scores:
        sc = "  ".join(f"{s:.2f}" for s in scores)
        spread = (f"   (min {min(scores):.2f}  max {max(scores):.2f}  "
                  f"spread {max(scores) - min(scores):.2f})" if len(scores) > 1
                  else "")
        out.append(f"      scores     {sc}{spread}")
    if ux:
        out.append(f"      UX rating  {'  '.join(str(r) for r in ux)}"
                   f"        (blind, 1-10)")
    if any(ret.get(kk) for kk in ("rate_limit", "transient", "nudges")):
        out.append(f"      retries    rate-limit {ret.get('rate_limit', 0)}   "
                   f"transient {ret.get('transient', 0)}   "
                   f"nudges {ret.get('nudges', 0)}")
    return out


def _render_tools(cell: dict) -> list[str]:
    """Tool adoption + per-tool call/error counts across the cell (from the
    trial transcripts). Answers 'did the agents drive the pipeline?' at a
    glance."""
    tu = cell.get("tool_usage") or {}
    per = tu.get("per_tool") or {}
    if not per:
        return []
    out = [*_section_title("TOOLS"), ""]
    n = tu.get("n_trials", cell.get("n", 0))
    mcp = tu.get("adopted_mcp", tu.get("adopted_trials", 0))
    scr = tu.get("adopted_script", 0)
    split = f"  ({mcp} via MCP" + (f", {scr} via script" if scr else "") + ")"
    out.append(f"      adoption   {tu.get('adopted_trials', 0)}/{n} "
               f"trials used domain tools{split if (mcp or scr) else ''}")
    out.append("      MCP calls:")
    errs = tu.get("per_tool_errors") or {}
    longest = max((len(k) for k in per), default=0)
    for name, cnt in per.items():
        e = errs.get(name, 0)
        etxt = f"   ({e} err)" if e else ""
        out.append(f"        {name:<{longest}}  {cnt}{etxt}")
    return out


def _render_stages(cell: dict) -> list[str]:
    stages = cell.get("stages") or {}
    disp = cell.get("stage_display") or {}
    n = max(int(cell.get("n", 0)), 1)
    out = [*_section_title("STAGES"), ""]
    if not stages:
        out.append("      (no stage data)")
        return out
    longest = max(len(sid) for sid in stages)
    for sid, rate in stages.items():
        passed = int(round(rate * n))
        d = disp.get(sid) or {}
        # Binary stages: pass count + rate, as before. Continuous stages: the
        # same binary pass count PLUS the mean [0,1] credit that entered the
        # score and the mean raw distance-to-reference it came from. The switch
        # is purely `continuous`, so binary-only rubrics render unchanged.
        if d.get("continuous"):
            cred = d.get("credit_mean")
            dist = d.get("distance_mean")
            lbl = d.get("distance_label") or ""
            cred_s = f"credit {cred:.2f}" if isinstance(cred, (int, float)) else "credit   - "
            dist_s = (f"dist {dist:.3f} {lbl}".rstrip()
                      if isinstance(dist, (int, float)) else "dist   -")
            out.append(f"      {sid:<{longest}}     {passed}/{n}  {cred_s}   "
                       f"{dist_s}")
        else:
            out.append(
                f"      {sid:<{longest}}     {passed}/{n}       {rate:.2f}")
    return out


def _render_failures(failures: dict) -> list[str]:
    out = [*_section_title("FAILURES"), ""]
    longest = max(len(m) for m in failures)
    for mode, count in sorted(failures.items(), key=lambda kv: (-kv[1], kv[0])):
        out.append(f"      {mode:<{longest}}    {count}")
    return out


def _render_cost(cell: dict) -> list[str]:
    n = max(int(cell.get("n", 0)), 1)
    mean_cost = cell.get("mean_cost_usd")
    mean_estimated = cell.get("mean_estimated_api_equivalent_cost_usd")
    mean_wall = cell.get("mean_wall_clock_s", 0.0)
    total_cost = (mean_cost * n) if isinstance(mean_cost, (int, float)) else None
    total_wall = mean_wall * n
    cost_line = (
        f"      cost      ${total_cost:.2f} total       "
        f"${mean_cost:.2f} / trial"
        if total_cost is not None and mean_cost is not None
        else "      cost      n/a"
    )
    if mean_cost is None and isinstance(mean_estimated, (int, float)):
        total_estimated = mean_estimated * n
        cost_line = (
            f"      cost      ${total_estimated:.2f} total       "
            f"${mean_estimated:.2f} / trial  (estimated, subscription)"
        )
    lines = [
        *_section_title("COST"),
        "",
        cost_line,
        f"      wall      {fmt_wall(total_wall)} total      "
        f"{fmt_wall(mean_wall)} / trial",
    ]
    tok = cell.get("mean_tokens") or {}
    if any(tok.get(k) for k in ("initial_input", "input", "output")):
        # Per-trial means. "initial" is the starting context (system prompt +
        # tools + task); input/output are cumulative over the agentic run.
        lines += [
            f"      tokens    {tok.get('initial_input', 0):,} initial input / trial",
            f"                {tok.get('input', 0):,} input  /  "
            f"{tok.get('output', 0):,} output  (cumulative / trial)",
            f"                {tok.get('cache_read', 0):,} cache read  /  "
            f"{tok.get('cache_creation', 0):,} cache write  / trial",
        ]
    return lines


def _render_paired_deltas(deltas: list[dict]) -> list[str]:
    """One line per (model × condition-pair): the headline ablation numbers.

    Paired over shared seeds, so the CI is the right uncertainty for the
    delta itself (per-seed noise cancels). Direction is b − a in CLI
    condition order.
    """
    out = [
        *_banner("CONDITION DELTAS  (paired by seed)"),
        "",
        "      Δ = condition_b − condition_a, averaged over shared seeds;",
        "      CI95 is a paired bootstrap over the seed dimension.",
        "",
    ]
    for d in deltas:
        pair = f"{d['condition_b']} − {d['condition_a']}"
        ci = d.get("reach_delta_ci95")
        ci_txt = (f"  CI95 [{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci
                  else "  (n<2: no CI)")
        out.append(f"  {d['model']}:  {pair}")
        out.append(f"      Δreach    {d['reach_delta']:+.2f}{ci_txt}"
                   f"      (n_pairs={d['n_pairs']})")
        pci = d.get("pass_delta_ci95")
        pci_txt = (f"  CI95 [{pci[0]:+.2f}, {pci[1]:+.2f}]" if pci else "")
        out.append(f"      Δpass     {d['pass_delta']:+.2f}{pci_txt}")
        out.append("")
    if out[-1] == "":
        out.pop()
    return out


def _render_artifacts(run_dir: Path) -> list[str]:
    rd = str(run_dir)
    paths = [
        ("manifest",            f"{rd}/manifest.json"),
        ("summary (json)",      f"{rd}/summary.json"),
        ("summary (txt)",       f"{rd}/summary.txt"),
        ("trials (jsonl)",      f"{rd}/trials.jsonl"),
        ("per-trial dirs",      f"{rd}/trials/"),
        ("per-trial audit",     f"{rd}/trials/<id>/audit.txt  (+ audit.html)"),
        ("three-vector plot",   f"{rd}/parallel_coords.png"),
        ("k-dependence plot",   f"{rd}/k_sweep.png"),
        ("per-stage k plot",    f"{rd}/per_stage_k.png"),
        ("overview plot",       f"{rd}/overview.png"),
    ]
    out = [*_banner("ARTIFACTS"), ""]
    longest = max(len(label) for label, _ in paths)
    for label, p in paths:
        out.append(f"  {label:<{longest}}  {p}")
    out.append("")
    out.append(RULE_DOUBLE)
    return out
