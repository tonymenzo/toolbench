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

    for cell in summary.get("cells", []):
        out.append("")
        out.append("")
        out.extend(_render_cell(cell))

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
    start    = manifest.get("created_at", "")
    wall     = sum(c.get("mean_wall_clock_s", 0.0) * c.get("n", 0)
                   for c in summary.get("cells", []))

    budget_line = (
        f"${budget:.2f} cap   ${spent:.2f} spent"
        if isinstance(budget, (int, float))
        else f"${spent:.2f} spent"
    )
    if spent == 0.0:
        budget_line += "  (local via litellm)"

    return [
        f"  run_id     {run_id}",
        f"  task       {task:<24}k          {k}   (n per cell)",
        f"  trials     {n_total:<24}cells      {n_cells}",
        f"  budget     {budget_line}",
        f"  start      {start}",
        f"  wall       {fmt_wall(wall)}",
    ]


def _render_cell(cell: dict) -> list[str]:
    title = f"CELL  {cell.get('model', '?')}  ×  {cell.get('condition', '?')}"
    lines: list[str] = []
    lines.extend(_banner(title))
    lines.append("")
    lines.extend(_render_three_vector(cell))
    lines.append("")
    lines.extend(_render_stages(cell))
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
    return out


def _render_stages(cell: dict) -> list[str]:
    stages = cell.get("stages") or {}
    n = max(int(cell.get("n", 0)), 1)
    out = [*_section_title("STAGES"), ""]
    if not stages:
        out.append("      (no stage data)")
        return out
    longest = max(len(sid) for sid in stages)
    for sid, rate in stages.items():
        passed = int(round(rate * n))
        out.append(f"      {sid:<{longest}}     {passed}/{n}       {rate:.2f}")
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
    mean_wall = cell.get("mean_wall_clock_s", 0.0)
    total_cost = (mean_cost * n) if isinstance(mean_cost, (int, float)) else None
    total_wall = mean_wall * n
    cost_line = (
        f"      cost      ${total_cost:.2f} total       "
        f"${mean_cost:.2f} / trial"
        if total_cost is not None and mean_cost is not None
        else "      cost      n/a"
    )
    if total_cost == 0.0:
        cost_line = f"{cost_line}      (local)"
    return [
        *_section_title("COST"),
        "",
        cost_line,
        f"      wall      {fmt_wall(total_wall)} total      "
        f"{fmt_wall(mean_wall)} / trial",
    ]


def _render_artifacts(run_dir: Path) -> list[str]:
    rd = str(run_dir)
    paths = [
        ("manifest",            f"{rd}/manifest.json"),
        ("summary (json)",      f"{rd}/summary.json"),
        ("summary (txt)",       f"{rd}/summary.txt"),
        ("trials (jsonl)",      f"{rd}/trials.jsonl"),
        ("per-trial dirs",      f"{rd}/trials/"),
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
