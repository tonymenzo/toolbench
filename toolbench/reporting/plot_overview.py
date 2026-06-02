"""
Multi-panel overview plot for a single eval run.

Reads `runs/<run_id>/summary.json` and `runs/<run_id>/trials.jsonl`,
produces a multi-panel PNG covering: stagewise success vector,
failure-mode breakdown, per-trial score vs cost, wall-clock
distribution, the (pass@k, pass^k, reach) metric radar, and a
bootstrap pairwise-scatter showing the joint covariance of the
three-vector across resamples.

Usage:
    python -m toolbench.reporting.plot_overview --run-dir eval/runs/<run_id>
"""

import argparse
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

# REPO_ROOT is placed on sys.path by `eval/__init__.py`.
from toolbench.core.failure_modes import (
    AGENT_CRASH, GRADE_ERROR, MODEL_FORMAT_CRASH, MODEL_STOPPED_EARLY,
    NONE, UNKNOWN, incomplete_at,
)
from toolbench.core.metrics import (
    reach_bar_k, pass_at_k, pass_caret_k,
)


# Categorical palette — kept colour-blind friendly.
_PALETTE = [
    "#4C78A8",  # blue
    "#F58518",  # orange
    "#54A24B",  # green
    "#E45756",  # red
    "#72B7B2",  # teal
    "#EECA3B",  # yellow
    "#B279A2",  # purple
    "#FF9DA6",  # pink
]

# Color map for the failure-mode breakdown bar. The `INCOMPLETE_AT_*`
# entries are benchmark-specific (the stage ids are from
# `hep_bsm_demo`); adding a new benchmark means extending this dict
# (or, longer term, deriving the per-stage colors at plot time).
_FAILURE_COLORS = {
    NONE:                                  "#54A24B",  # green
    incomplete_at("ufo_built"):            "#F58518",
    incomplete_at("mg_ran"):               "#E45756",
    incomplete_at("shower_ok"):            "#B279A2",
    incomplete_at("jets_clustered"):       "#72B7B2",
    incomplete_at("mass_reconstructed"):   "#EECA3B",
    incomplete_at("mass_within_tol"):      "#FF9DA6",
    MODEL_FORMAT_CRASH:                    "#D62728",  # bright red — flag prominently
    AGENT_CRASH:                           "#9A6FB0",
    MODEL_STOPPED_EARLY:                   "#5778A4",
    GRADE_ERROR:                           "#999999",
    UNKNOWN:                               "#cccccc",
}


def _condition_color(cond: str, all_conds: list[str]) -> str:
    return _PALETTE[all_conds.index(cond) % len(_PALETTE)]


def _failure_color(fm: str) -> str:
    return _FAILURE_COLORS.get(fm, "#888888")


def plot_stages(ax, summary: dict, conditions: list[str]) -> None:
    """Per-stage success rate, grouped bar chart per condition."""
    cells = [c for c in summary["cells"] if c["condition"] in conditions]
    if not cells or not cells[0].get("stages"):
        ax.set_title("Stagewise success rate (no data)")
        ax.set_xticks([])
        return
    stage_ids = list(cells[0]["stages"].keys())
    n_stages = len(stage_ids)
    n_cells = len(cells)
    width = 0.8 / max(n_cells, 1)
    x = np.arange(n_stages)
    for i, cell in enumerate(cells):
        rates = [cell["stages"].get(sid, 0.0) for sid in stage_ids]
        ax.bar(
            x + i * width - 0.4 + width / 2,
            rates, width,
            label=cell["condition"],
            color=_condition_color(cell["condition"], conditions),
        )
    ax.set_ylim(0, 1.05)
    ax.set_xticks(x)
    ax.set_xticklabels(stage_ids, rotation=30, ha="right", fontsize=8)
    ax.set_ylabel("P(stage passed)")
    ax.set_title("Stagewise success rate")
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(axis="y", alpha=0.3)


def plot_failure_modes(ax, summary: dict, conditions: list[str]) -> None:
    """Stacked bar of failure-mode counts per condition."""
    cells = [c for c in summary["cells"] if c["condition"] in conditions]
    if not cells:
        ax.set_title("Failure modes (no data)")
        return
    all_modes = sorted({m for c in cells for m in c.get("failure_modes", {}).keys()})
    bottoms = np.zeros(len(cells))
    x = np.arange(len(cells))
    for mode in all_modes:
        heights = np.array([c.get("failure_modes", {}).get(mode, 0) for c in cells])
        ax.bar(x, heights, bottom=bottoms,
               label=mode, color=_failure_color(mode))
        bottoms += heights
    ax.set_xticks(x)
    ax.set_xticklabels([c["condition"] for c in cells], rotation=0)
    ax.set_ylabel("trials")
    ax.set_title("Failure-mode breakdown")
    ax.legend(fontsize=7, loc="upper right")
    ax.grid(axis="y", alpha=0.3)


def plot_score_vs_cost(ax, trials: list[dict], conditions: list[str]) -> None:
    """Per-trial scatter: cost (x) vs score (y), color = condition."""
    if not trials:
        ax.set_title("Score vs cost (no data)")
        return
    for cond in conditions:
        rows = [t for t in trials if t["condition"] == cond]
        if not rows:
            continue
        costs = [t.get("cost_usd") or 0.0 for t in rows]
        scores = [t["score"] for t in rows]
        ax.scatter(costs, scores, label=cond, s=60,
                   color=_condition_color(cond, conditions),
                   edgecolors="black", linewidth=0.5, alpha=0.8)
    ax.set_xlabel("cost (USD per trial)")
    ax.set_ylabel("score")
    ax.set_ylim(-0.05, 1.05)
    ax.set_title("Per-trial score vs cost")
    ax.legend(fontsize=8, loc="best")
    ax.grid(alpha=0.3)


def plot_wallclock(ax, trials: list[dict], conditions: list[str]) -> None:
    """Per-condition wall-clock distribution as boxplot + jittered points."""
    if not trials:
        ax.set_title("Wall-clock (no data)")
        return
    by_cond = {c: [t["wall_clock_s"] for t in trials if t["condition"] == c]
               for c in conditions}
    by_cond = {c: v for c, v in by_cond.items() if v}
    if not by_cond:
        ax.set_title("Wall-clock (no data)")
        return
    parts = ax.boxplot(list(by_cond.values()),
                       tick_labels=list(by_cond.keys()),
                       widths=0.5, patch_artist=True)
    for patch, cond in zip(parts["boxes"], by_cond.keys()):
        patch.set_facecolor(_condition_color(cond, conditions))
        patch.set_alpha(0.45)
    # Overlay individual points
    for i, (cond, vals) in enumerate(by_cond.items(), start=1):
        jitter = np.random.default_rng(0).uniform(-0.08, 0.08, size=len(vals))
        ax.scatter(np.full(len(vals), i) + jitter, vals,
                   color=_condition_color(cond, list(by_cond.keys())),
                   edgecolors="black", linewidth=0.5, s=30, alpha=0.9, zorder=3)
    ax.set_ylabel("wall-clock (s)")
    ax.set_title("Wall-clock distribution")
    ax.grid(axis="y", alpha=0.3)


def plot_metric_radar(ax, summary: dict, conditions: list[str]) -> None:
    """Polar radar of the (reach_bar_k, pass@k, pass^k) triplet, one polygon per cell."""
    cells = [c for c in summary["cells"] if c["condition"] in conditions]
    if not cells:
        ax.set_title("Metric profile (no data)")
        return
    # Canonical order from the accompanying manuscript §6: productivity,
    # exploration, trustworthiness.
    labels = ["reach", "pass@k", "pass^k"]
    keys = ["reach_bar_k", "pass_at_k", "pass_caret_k"]
    angles = np.linspace(0, 2 * np.pi, len(labels), endpoint=False).tolist()
    angles += angles[:1]
    for cell in cells:
        vals = [float(cell.get(k, 0.0) or 0.0) for k in keys]
        vals += vals[:1]
        color = _condition_color(cell["condition"], conditions)
        ax.plot(angles, vals, color=color, linewidth=2, label=cell["condition"])
        ax.fill(angles, vals, color=color, alpha=0.15)
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_yticks([0.25, 0.5, 0.75, 1.0])
    ax.set_yticklabels(["0.25", "0.5", "0.75", "1.0"], fontsize=7)
    ax.set_ylim(0, 1.0)
    ax.set_title("Metric profile (pass@k, pass^k, reach)", fontsize=10)
    ax.legend(fontsize=7, loc="upper right", bbox_to_anchor=(1.35, 1.1))
    ax.grid(alpha=0.3)


def _bootstrap_metric_triplet(rows: list[dict], k: int,
                              stage_order: list[str] | None = None,
                              weights: list[float] | None = None,
                              n_boot: int = 400, seed: int = 0xC0FFEE
                              ) -> np.ndarray:
    """Return shape (n_boot, 3) array of bootstrap (pass@k, pass^k, reach) samples.

    Pass criterion: every rubric stage passed (boundary case w = e_N).
    """
    n = len(rows)
    if n < 1:
        return np.zeros((0, 3))
    canonical = stage_order
    if canonical is None:
        for r in rows:
            s = r.get("grade", {}).get("stages") or r.get("stages") or {}
            if s:
                canonical = list(s.keys())
                break
    stage_matrix: list[list[int]] = []
    per_row_pass: list[int] = []
    for r in rows:
        s = r.get("grade", {}).get("stages") or r.get("stages") or {}
        if canonical is None:
            stage_matrix.append([])
            per_row_pass.append(0)
        else:
            row_vec = [1 if s.get(sid) else 0 for sid in canonical]
            stage_matrix.append(row_vec)
            per_row_pass.append(1 if row_vec and all(row_vec) else 0)
    rng = np.random.default_rng(seed)
    out = np.empty((n_boot, 3))
    # Columns track METRIC_TRIPLET order: (reach_bar_k, pass@k, pass^k).
    for b in range(n_boot):
        idx = rng.integers(0, n, size=n)
        c_b = int(sum(per_row_pass[i] for i in idx))
        out[b, 0] = reach_bar_k([stage_matrix[i] for i in idx], weights=weights)
        out[b, 1] = pass_at_k(n, c_b, min(k, n))
        out[b, 2] = pass_caret_k(n, c_b, min(k, n))
    return out


def plot_pairwise_bootstrap(axes, trials: list[dict], conditions: list[str],
                            k: int, stage_order: list[str] | None = None,
                            weights: list[float] | None = None) -> None:
    """Three-panel pairwise scatter of bootstrap (pass@k, pass^k, reach) samples.

    Each point is one bootstrap resample of a (model × condition) cell;
    the cloud shape is the joint covariance structure made visible.
    """
    # Three-vector axis order: (reach, pass@k, pass^k).
    pair_specs = [
        (0, 1, "reach", "pass@k"),
        (0, 2, "reach", "pass^k"),
        (1, 2, "pass@k", "pass^k"),
    ]
    by_cond: dict[str, list[dict]] = {}
    for t in trials:
        by_cond.setdefault(t["condition"], []).append(t)
    if not by_cond:
        for ax, (_, _, xlab, ylab) in zip(axes, pair_specs):
            ax.set_title(f"{xlab} vs {ylab} (no data)", fontsize=9)
        return
    cell_boot = {
        cond: _bootstrap_metric_triplet(rows, k,
                                        stage_order=stage_order, weights=weights)
        for cond, rows in by_cond.items()
    }
    for ax, (ix, iy, xlab, ylab) in zip(axes, pair_specs):
        for cond in conditions:
            v = cell_boot.get(cond)
            if v is None or len(v) == 0:
                continue
            ax.scatter(v[:, ix], v[:, iy], s=8, alpha=0.35,
                       color=_condition_color(cond, conditions),
                       edgecolors="none", label=cond)
        ax.set_xlim(-0.05, 1.05)
        ax.set_ylim(-0.05, 1.05)
        ax.set_xlabel(xlab, fontsize=9)
        ax.set_ylabel(ylab, fontsize=9)
        ax.set_title(f"{xlab} vs {ylab}", fontsize=9)
        ax.grid(alpha=0.3)
    # Legend only on the first panel to avoid triplication.
    handles, labels = axes[0].get_legend_handles_labels()
    if handles:
        axes[0].legend(handles, labels, fontsize=7, loc="lower right")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="plot_overview",
                                description="Multi-panel overview plot for an eval run.")
    p.add_argument("--run-dir", required=True, help="eval/runs/<run_id>/")
    p.add_argument("--output", default=None,
                   help="output PNG (default: <run_dir>/overview.png)")
    args = p.parse_args(argv)

    run_dir = Path(args.run_dir)
    summary_path = run_dir / "summary.json"
    trials_path = run_dir / "trials.jsonl"
    if not summary_path.exists():
        print(f"missing: {summary_path}", file=sys.stderr)
        return 2
    summary = json.loads(summary_path.read_text())
    trials = []
    if trials_path.exists():
        trials = [json.loads(l) for l in trials_path.read_text().splitlines() if l.strip()]

    conditions = sorted({c["condition"] for c in summary.get("cells", [])})
    if not conditions:
        print("no cells in summary; nothing to plot", file=sys.stderr)
        return 2

    # 3 rows × 4 cols: top two rows hold the original 2×2 panels (each
    # spanning 2 cols); bottom row holds radar (1 col) + 3 pairwise
    # scatter panels (1 col each).
    fig = plt.figure(figsize=(15, 13))
    gs = fig.add_gridspec(3, 4, height_ratios=[1, 1, 1.0],
                          hspace=0.45, wspace=0.35)
    ax_stages = fig.add_subplot(gs[0, 0:2])
    ax_failure = fig.add_subplot(gs[0, 2:4])
    ax_score = fig.add_subplot(gs[1, 0:2])
    ax_wall = fig.add_subplot(gs[1, 2:4])
    ax_radar = fig.add_subplot(gs[2, 0], projection="polar")
    ax_pair01 = fig.add_subplot(gs[2, 1])
    ax_pair02 = fig.add_subplot(gs[2, 2])
    ax_pair12 = fig.add_subplot(gs[2, 3])

    plot_stages(ax_stages, summary, conditions)
    plot_failure_modes(ax_failure, summary, conditions)
    plot_score_vs_cost(ax_score, trials, conditions)
    plot_wallclock(ax_wall, trials, conditions)
    plot_metric_radar(ax_radar, summary, conditions)

    k_for_corr = summary.get("k") or next(
        (c.get("k") for c in summary.get("cells", []) if c.get("k")),
        summary.get("n_total_trials", 1),
    )
    rw = summary.get("reach_weights") or {}
    plot_pairwise_bootstrap(
        [ax_pair01, ax_pair02, ax_pair12],
        trials, conditions,
        k=int(k_for_corr or 1),
        stage_order=rw.get("stage_order"),
        weights=rw.get("w"),
    )

    n_total = summary.get("n_total_trials", "?")
    spent = summary.get("total_spent_usd", 0.0)
    title = (f"{summary.get('run_id', run_dir.name)}  "
             f"|  {n_total} trials  |  ${spent:.4f} spent")
    fig.suptitle(title, fontsize=10)

    out = Path(args.output) if args.output else run_dir / "overview.png"
    fig.savefig(out, dpi=130, bbox_inches="tight")
    print(f"saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
