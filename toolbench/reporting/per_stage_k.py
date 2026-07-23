"""
Per-stage k-dependence plot.

For each (model × condition) cell, renders the per-stage reach curves
defined in the accompanying manuscript §4 and
`reach_scales_pedagogical.tex` §5 (per-stage view):

    rho_i(k)   := 1 - (1 - P_i)^k      best-of-k reach of stage i  (solid)
    sigma_i(k) := P_i^k                worst-of-k reach of stage i (dotted)

where P_i is the probability of reaching stage i or further (cumulative
absorbing probability). The unbiased estimators are precisely the binary
`pass@k` / `pass^k` evaluated at the per-stage reach indicator
X_{j,i} = 1{trial j reached stage i}; we reuse `pass_at_k(n, c_i, k)`
and `pass_caret_k(n, c_i, k)` with c_i = number of trials reaching
stage i.

The integrated R_{@k} = (1/N) Σ_i rho_i(k) and R^k = (1/N) Σ_i sigma_i(k)
are the height-averaged versions of these. Plotting per-stage exposes
the rate spectrum {1/tau_@^(i), 1/tau^^(i)} directly — useful when the
integrated curve smears multiple bottlenecks together.

Style matches `eval/reporting/k_sweep.py`: one panel per cell, color
encodes stage, linestyle encodes best-vs-worst-of-k. Stages with
identical hat P_i (an absorbing-chain artifact) are dodged
horizontally so all curves remain visible.

Usage:
    python -m toolbench.reporting.per_stage_k --run-dir eval/runs/<run_id>
"""

import argparse
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

# REPO_ROOT is placed on sys.path by `eval/__init__.py`, so this module
# stays importable both as `python -m toolbench.reporting.per_stage_k` and
# as `from toolbench.reporting.per_stage_k import render_per_stage_k` from
# cli.py.
from toolbench.core.metrics import (
    pass_at_k, pass_caret_k, reach_at_k, reach_caret_k,
)
from toolbench.reporting._shared import (
    short_model_name, stage_matrix_from_rows, subplot_grid,
)
from toolbench.reporting._output import save_figure, write_figure_data


# Categorical palette: one color per stage, cycled if N > len(palette).
# Order goes from early pipeline (cool) to late (warm), mirroring the
# typical "early stages reliable, headline stage hard" rubric shape.
_STAGE_PALETTE = [
    "#1F77B4",  # cool blue
    "#2CA02C",  # green
    "#9467BD",  # purple
    "#FF7F0E",  # orange
    "#D62728",  # warm red
    "#8C564B",  # brown
    "#E377C2",  # pink
    "#7F7F7F",  # grey
]
# Distinct marker shape per stage so coincident curves read as separate
# plies even after the horizontal dodge. Cycles past 8 stages.
_STAGE_MARKERS = ["o", "s", "^", "D", "v", "p", "X", "*"]

# Match k_sweep.py's linewidth / marker conventions for stylistic
# consistency across the reporting suite.
_LINEWIDTH        = 2.0
_MARKER_SIZE      = 6
_DASHES_DOT       = (1.2, 2.0)   # worst-of-k linestyle (same as k_sweep)
_JITTER_PER_STAGE = 0.11          # horizontal dodge so coincident curves
                                   # (stages with same hat P_i) sit
                                   # side-by-side instead of stacking.


def render_per_stage_k(summary: dict, trials: list[dict], manifest: dict,
                       output_path: Path, *,
                       title: str | None = None) -> bool:
    """Render the per-stage rho_i(k) / sigma_i(k) figure.

    Args:
        summary: parsed `summary.json` (gives the cell list).
        trials: rows from `trials.jsonl` (used to build the per-cell
            stage matrix).
        manifest: parsed `manifest.json` (provides the canonical stage
            order via `reach_weights.stage_order`).
        output_path: where to write the PNG.
        title: optional figure title.

    Layout: one panel per (model × condition) cell, near-square grid
    above 3 cells (matches k_sweep).

    Returns:
        True if a figure was written, False if there was no data.
    """
    cells = summary.get("cells", [])
    if not cells:
        print("per_stage_k: no cells in summary; nothing to plot",
              file=sys.stderr)
        return False

    by_cell: dict[tuple[str, str], list[dict]] = {}
    for t in trials:
        by_cell.setdefault((t["model"], t["condition"]), []).append(t)

    rw = manifest.get("reach_weights") or {}
    stage_order = rw.get("stage_order")

    nrows, ncols = subplot_grid(len(cells))
    fig, axes = plt.subplots(
        nrows, ncols,
        figsize=(7.2 * ncols, 4.4 * nrows),
        squeeze=False, sharey=True,
    )
    axes_flat = list(axes.flat)

    series_by_cell: list[dict] = []
    for ax, cell in zip(axes_flat, cells):
        rows = by_cell.get((cell.get("model"), cell.get("condition")), [])
        cell_data = _plot_cell(ax, cell, rows, stage_order=stage_order)
        if cell_data is not None:
            series_by_cell.append(cell_data)

    # Hide unused panels (only matters when len(cells) < nrows*ncols).
    for ax in axes_flat[len(cells):]:
        ax.axis("off")

    if title is None:
        title = summary.get("run_id", "")
    if title:
        fig.suptitle(title, fontsize=11, y=0.99)

    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.subplots_adjust(top=0.87)
    save_figure(fig, output_path, dpi=150)
    plt.close(fig)

    write_figure_data(output_path, {
        "figure": "per_stage_k",
        "run_id": summary.get("run_id", ""),
        "metrics": {
            "rho_at_k":     "per-stage best-of-k reach  rho_i(k) = 1-(1-P_i)^k (solid)",
            "sigma_caret_k": "per-stage worst-of-k reach sigma_i(k) = P_i^k     (dotted)",
            "reach_at_k_eqw":    "integrated best-of-k reach, equal-weighted",
            "reach_caret_k_eqw": "integrated worst-of-k reach, equal-weighted",
            "P_hat": "hat P_i = c_reached / n, cumulative absorbing probability",
        },
        "cells": series_by_cell,
    })
    return True


def _plot_cell(ax, cell: dict, rows: list[dict], *,
               stage_order: list[str] | None) -> dict | None:
    """Draw rho_i(k) and sigma_i(k) overlays for one cell on a single panel.

    Returns the exact per-stage and integrated series drawn (for the data
    sidecar), or `None` for an empty panel (no trials / no stages).
    """
    title = f"{short_model_name(cell.get('model', '?'))}  ×  {cell.get('condition', '?')}"

    if not rows:
        ax.set_title(f"{title}  (no trials)", fontsize=10)
        ax.axis("off")
        return None

    canonical = stage_order or list((rows[0].get("stages") or {}).keys())
    if not canonical:
        ax.set_title(f"{title}  (no stages)", fontsize=10)
        ax.axis("off")
        return None

    n = len(rows)
    N = len(canonical)

    stage_matrix = stage_matrix_from_rows(rows, canonical)

    # c_i = number of trials that reached stage i (absorbing convention).
    c_per_stage: list[int] = [0] * N
    for row in stage_matrix:
        reached_so_far = True
        for i, s in enumerate(row):
            if reached_so_far and s:
                c_per_stage[i] += 1
            else:
                reached_so_far = False

    ks = list(range(1, n + 1))
    # Horizontal dodge: center the cloud around the integer k, with
    # stage i offset by (i - (N-1)/2) * eps. Same value of true k for
    # all stages — the offset is purely visual so coincident curves
    # don't hide.
    half = (N - 1) / 2

    # Integrated reach curves (equal-weighted, matching the (1/N)
    # average over the per-stage curves shown). Drawn first, in bold
    # black with no markers, so the colored per-stage curves layer on
    # top and the integrated view reads as a baseline / envelope.
    integ_at    = [reach_at_k(stage_matrix, k, weights=None)    for k in ks]
    integ_caret = [reach_caret_k(stage_matrix, k, weights=None) for k in ks]
    ax.plot(ks, integ_at,
            color="black", linewidth=2.6, alpha=0.85,
            zorder=5, label=r"$R_{@k}$  (integrated, eq-w)")
    ax.plot(ks, integ_caret,
            color="black", linewidth=2.6, dashes=_DASHES_DOT, alpha=0.85,
            zorder=5, label=r"$R^k$  (integrated, eq-w)")
    integ_handles = [
        Line2D([], [], color="black", linewidth=2.6, alpha=0.85),
        Line2D([], [], color="black", linewidth=2.6, dashes=_DASHES_DOT,
               alpha=0.85),
    ]
    integ_labels = [r"$R_{@k}$  integrated (eq-w)",
                    r"$R^k$  integrated (eq-w)"]

    stage_handles: list[Line2D] = []
    stage_labels:  list[str] = []
    stage_series: dict[str, dict] = {}
    for i, sid in enumerate(canonical):
        c_i = c_per_stage[i]
        P_hat = c_i / n if n > 0 else 0.0
        color  = _STAGE_PALETTE[i % len(_STAGE_PALETTE)]
        marker = _STAGE_MARKERS[i % len(_STAGE_MARKERS)]
        dx = (i - half) * _JITTER_PER_STAGE
        ks_dodged = [k + dx for k in ks]

        rho_curve   = [pass_at_k(n, c_i, k)    for k in ks]
        sigma_curve = [pass_caret_k(n, c_i, k) for k in ks]

        stage_series[sid] = {
            "c_reached": c_i,
            "P_hat": P_hat,
            "rho_at_k": rho_curve,
            "sigma_caret_k": sigma_curve,
        }

        # rho_i: solid line (best-of-k).
        ax.plot(ks_dodged, rho_curve,
                color=color, linewidth=_LINEWIDTH,
                marker=marker, markersize=_MARKER_SIZE,
                markeredgecolor="white", markeredgewidth=0.6,
                zorder=4)
        # sigma_i: dotted line (worst-of-k).
        ax.plot(ks_dodged, sigma_curve,
                color=color, linewidth=_LINEWIDTH, dashes=_DASHES_DOT,
                marker=marker, markersize=_MARKER_SIZE,
                markeredgecolor="white", markeredgewidth=0.6,
                zorder=4)

        # Legend handle: color + marker shape (shared by ρ_i and σ_i
        # within a stage). hat P_i is the y-intercept of rho at k=1
        # already, so we don't duplicate it in the label.
        sid_safe = sid.replace("_", r"\_")
        stage_handles.append(Line2D([], [], color=color, linewidth=_LINEWIDTH,
                                    marker=marker, markersize=_MARKER_SIZE,
                                    markeredgecolor="white",
                                    markeredgewidth=0.6))
        stage_labels.append(rf"$\mathrm{{{sid_safe}}}$")

    ax.set_xlim(0.6, n + 0.4)
    ax.set_ylim(-0.03, 1.04)
    ax.set_xlabel(r"$k$  (number of independent trials)")
    ax.set_ylabel(r"per-stage reach  $\rho_i(k),\,\sigma_i(k)$")
    ax.set_xticks(ks)
    ax.set_yticks([0.0, 0.25, 0.5, 0.75, 1.0])
    ax.grid(alpha=0.25, linewidth=0.7, zorder=0)
    ax.set_title(title, fontsize=10, pad=8)

    # Legend: integrated curves on top, per-stage entries in the
    # middle, linestyle key for ρ/σ at the bottom.
    style_handles = [
        Line2D([], [], color="#404040", linewidth=_LINEWIDTH,
               marker="o", markersize=_MARKER_SIZE,
               markeredgecolor="white", markeredgewidth=0.6),
        Line2D([], [], color="#404040", linewidth=_LINEWIDTH,
               dashes=_DASHES_DOT,
               marker="o", markersize=_MARKER_SIZE,
               markeredgecolor="white", markeredgewidth=0.6),
    ]
    style_labels = [r"$\rho_i(k)$  best of $k$",
                    r"$\sigma_i(k)$  worst of $k$"]
    legend_handles = integ_handles + list(stage_handles) + style_handles
    legend_labels  = integ_labels  + list(stage_labels)  + style_labels
    ax.legend(legend_handles, legend_labels,
              fontsize=8, loc="center left",
              bbox_to_anchor=(1.01, 0.5),
              frameon=False, handlelength=2.4,
              handletextpad=0.6, labelspacing=0.55)

    return {
        "model": cell.get("model"),
        "condition": cell.get("condition"),
        "n": n,
        "stage_order": canonical,
        "k": ks,
        "integrated": {
            "reach_at_k_eqw": integ_at,
            "reach_caret_k_eqw": integ_caret,
        },
        "stages": stage_series,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="per_stage_k",
        description="Per-stage rho_i(k) / sigma_i(k) plot for an eval run.",
    )
    p.add_argument("--run-dir", required=True,
                   help="eval/runs/<run_id>/  (must contain summary.json, "
                        "trials.jsonl, manifest.json)")
    p.add_argument("--output", default=None,
                   help="output PNG path (default: <run_dir>/per_stage_k.png)")
    p.add_argument("--title", default=None,
                   help="override figure title")
    args = p.parse_args(argv)

    run_dir = Path(args.run_dir)
    summary_path  = run_dir / "summary.json"
    trials_path   = run_dir / "trials.jsonl"
    manifest_path = run_dir / "manifest.json"
    for required in (summary_path, manifest_path):
        if not required.exists():
            print(f"missing: {required}", file=sys.stderr)
            return 2

    summary  = json.loads(summary_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    trials = []
    if trials_path.exists():
        trials = [json.loads(line)
                  for line in trials_path.read_text().splitlines()
                  if line.strip()]

    out = Path(args.output) if args.output else run_dir / "per_stage_k.png"
    if render_per_stage_k(summary, trials, manifest, out, title=args.title):
        print(f"saved: {out}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
