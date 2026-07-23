"""
k-dependence plot for a single eval run.

Renders, per (model × condition) cell, the k-dependent metric family
on one panel. The family is defined in the accompanying manuscript:
every curve is an expectation over k iid trials of a per-trial score
M_j, evaluated either on binary M_j = 1{trial j passes} (the "pass"
family) or on partial-credit M_j = R_j ∈ [0,1] (the "reach" family).

- `reach@k(k)`  best-of-k, rubric-weighted    (solid, reach color)
- `reach^k(k)`  worst-of-k, rubric-weighted   (dotted, reach color)
- `pass@k(k)`   best-of-k, binary             (solid, pass color)
- `pass^k(k)`   worst-of-k, binary            (dotted, pass color)
- `reach_bar_k` mean reach (k-independent)    (dashed, neutral)

Plus two filled bands: the *reach fan* (between `reach@k` and
`reach^k`) and the *pass fan* (between `pass@k` and `pass^k`). The two
fans collapse to a single point at k=1 and widen as k grows; the gap
between fans is the partial-credit benefit (rubric vs binary).

Usage:
    python -m toolbench.reporting.k_sweep --run-dir eval/runs/<run_id>
"""

import argparse
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt

# REPO_ROOT is placed on sys.path by `eval/__init__.py`, so this module
# stays importable both as `python -m toolbench.reporting.k_sweep` and as
# `from toolbench.reporting.k_sweep import render_k_sweep` from cli.py.
from toolbench.core.metrics import (
    pass_at_k, pass_caret_k, per_trial_reach,
    reach_at_k, reach_caret_k,
)
from toolbench.reporting._shared import (
    short_model_name, stage_matrix_from_rows, subplot_grid,
)
from toolbench.reporting._output import save_figure, write_figure_data


# Reach (graded) and pass (boundary) get distinct hues. Linestyle
# distinguishes best-of-k (solid) from worst-of-k (dashed) within
# each family.
_REACH_COLOR    = "#1F77B4"   # cool blue
_PASS_COLOR     = "#D62728"   # warm red
_REACH_BAR_COLOR = "#404040"  # neutral grey
_BAND_ALPHA      = 0.14
_LINEWIDTH       = 2.0
_MARKER_SIZE     = 5


def render_k_sweep(summary: dict, trials: list[dict], manifest: dict,
                   output_path: Path, *, title: str | None = None) -> bool:
    """Render the k-sweep figure.

    Args:
        summary: parsed `summary.json` (gives the cell list + n + k).
        trials: rows from `trials.jsonl` (used to recompute the
            per-session reach R_j and the stage matrix per cell).
        manifest: parsed `manifest.json` (provides the rubric weights
            and the canonical stage order via `reach_weights`).
        output_path: where to write the PNG.
        title: optional figure title.

    Returns:
        True if a figure was written, False if there was no data.
    """
    cells = summary.get("cells", [])
    if not cells:
        print("k_sweep: no cells in summary; nothing to plot",
              file=sys.stderr)
        return False

    by_cell: dict[tuple[str, str], list[dict]] = {}
    for t in trials:
        by_cell.setdefault((t["model"], t["condition"]), []).append(t)

    rw = manifest.get("reach_weights") or {}
    stage_order = rw.get("stage_order")
    weights     = rw.get("w")

    nrows, ncols = subplot_grid(len(cells))
    fig, axes = plt.subplots(
        nrows, ncols,
        figsize=(5.6 * ncols, 4.4 * nrows),
        squeeze=False, sharey=True,
    )
    axes_flat = list(axes.flat)

    series_by_cell: list[dict] = []
    for ax, cell in zip(axes_flat, cells):
        rows = by_cell.get((cell.get("model"), cell.get("condition")), [])
        cell_data = _plot_cell(ax, cell, rows,
                               stage_order=stage_order, weights=weights)
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

    # Portable data sidecar: the exact curves drawn above, so the figure
    # can be restyled without recomputing from trials.jsonl.
    write_figure_data(output_path, {
        "figure": "k_sweep",
        "run_id": summary.get("run_id", ""),
        "metrics": {
            "reach_at_k":    "best-of-k, rubric-weighted reach  (solid)",
            "reach_caret_k": "worst-of-k, rubric-weighted reach (dotted)",
            "pass_at_k":     "best-of-k, binary all-stages pass  (solid)",
            "pass_caret_k":  "worst-of-k, binary all-stages pass (dotted)",
            "reach_bar_k":   "mean per-trial reach (k-independent baseline)",
        },
        "weights": weights,
        "cells": series_by_cell,
    })
    return True


def _plot_cell(ax, cell: dict, rows: list[dict], *,
               stage_order: list[str] | None,
               weights: list[float] | None) -> dict | None:
    """Draw one panel: 4 curves + 2 bands + the reach_bar baseline.

    Returns the exact series drawn (for the data sidecar), or `None`
    for an empty panel (no trials / no stages).
    """
    title = f"{short_model_name(cell.get('model', '?'))}  ×  {cell.get('condition', '?')}"

    if not rows:
        ax.set_title(f"{title}  (no trials)", fontsize=10)
        ax.axis("off")
        return None

    canonical = stage_order or list((rows[0].get("stages") or {}).keys())
    stage_matrix = stage_matrix_from_rows(rows, canonical)

    n = len(stage_matrix)
    if n == 0 or not canonical:
        ax.set_title(f"{title}  (no stages)", fontsize=10)
        ax.axis("off")
        return None

    # Per-session reach + pass counts.
    R = per_trial_reach(stage_matrix, weights) or [0.0] * n
    c = sum(1 for row in stage_matrix if row and all(row))
    reach_bar = sum(R) / len(R)

    ks = list(range(1, n + 1))
    reach_at_curve    = [reach_at_k(stage_matrix, k, weights)    for k in ks]
    reach_caret_curve = [reach_caret_k(stage_matrix, k, weights) for k in ks]
    pass_at_curve     = [pass_at_k(n, c, k)                      for k in ks]
    pass_caret_curve  = [pass_caret_k(n, c, k)                   for k in ks]

    # Filled fans first so curves draw on top.
    ax.fill_between(ks, reach_caret_curve, reach_at_curve,
                    color=_REACH_COLOR, alpha=_BAND_ALPHA,
                    linewidth=0, zorder=1)
    ax.fill_between(ks, pass_caret_curve, pass_at_curve,
                    color=_PASS_COLOR, alpha=_BAND_ALPHA,
                    linewidth=0, zorder=1)

    # Linestyle categorizes the aggregation type:
    #   solid           — best-of-k       (reach@k, pass@k)
    #   dotted (tight)  — worst-of-k      (reach^k, pass^k)
    #   dashed (long)   — mean (no k-dep) (reach_bar_k)
    _DASHES = (5, 3)
    _DASHES_DOT = (1.2, 2.0)

    # reach_bar baseline (dashed, grey).
    ax.axhline(reach_bar, color=_REACH_BAR_COLOR, linewidth=1.2,
               dashes=_DASHES, zorder=2,
               label=r"$\overline{\mathrm{reach}}_k^{\,\mathbf{w}}$")

    # Reach family.
    ax.plot(ks, reach_at_curve,
            color=_REACH_COLOR, linewidth=_LINEWIDTH,
            marker="o", markersize=_MARKER_SIZE,
            markeredgecolor="white", markeredgewidth=0.6,
            zorder=4, label=r"reach@$k$")
    ax.plot(ks, reach_caret_curve,
            color=_REACH_COLOR, linewidth=_LINEWIDTH, dashes=_DASHES_DOT,
            marker="o", markersize=_MARKER_SIZE,
            markeredgecolor="white", markeredgewidth=0.6,
            zorder=4, label=r"reach$^{\,k}$")

    # Pass family.
    ax.plot(ks, pass_at_curve,
            color=_PASS_COLOR, linewidth=_LINEWIDTH,
            marker="s", markersize=_MARKER_SIZE,
            markeredgecolor="white", markeredgewidth=0.6,
            zorder=4, label=r"pass@$k$")
    ax.plot(ks, pass_caret_curve,
            color=_PASS_COLOR, linewidth=_LINEWIDTH, dashes=_DASHES_DOT,
            marker="s", markersize=_MARKER_SIZE,
            markeredgecolor="white", markeredgewidth=0.6,
            zorder=4, label=r"pass$^{\,k}$")

    # Cosmetic frame.
    ax.set_xlim(0.6, n + 0.4)
    ax.set_ylim(-0.03, 1.04)
    ax.set_xlabel(r"$k$  (number of independent trials)")
    ax.set_ylabel(r"expected per-trial score  $\mathbb{E}[\cdot]$ over $k$ iid trials")
    ax.set_xticks(ks)
    ax.set_yticks([0.0, 0.25, 0.5, 0.75, 1.0])
    ax.grid(alpha=0.25, linewidth=0.7, zorder=0)
    ax.set_title(title, fontsize=10, pad=8)

    # Compact legend — order it so the reach family sits above pass,
    # reach_bar reference last.
    handles, labels = ax.get_legend_handles_labels()
    order = [labels.index(name) for name in (
        r"reach@$k$", r"reach$^{\,k}$",
        r"pass@$k$",  r"pass$^{\,k}$",
        r"$\overline{\mathrm{reach}}_k^{\,\mathbf{w}}$",
    ) if name in labels]
    ax.legend([handles[i] for i in order], [labels[i] for i in order],
              fontsize=8, loc="center left", bbox_to_anchor=(1.01, 0.5),
              frameon=False,
              handlelength=3.5,   # longer line segments → dashes visible
              handletextpad=0.6,
              labelspacing=0.55)

    return {
        "model": cell.get("model"),
        "condition": cell.get("condition"),
        "n": n,
        "c_all_stages": c,          # # trials passing every stage
        "stage_order": canonical,
        "k": ks,
        "reach_at_k": reach_at_curve,
        "reach_caret_k": reach_caret_curve,
        "pass_at_k": pass_at_curve,
        "pass_caret_k": pass_caret_curve,
        "reach_bar_k": reach_bar,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="k_sweep",
        description="k-dependence plot for an eval run.",
    )
    p.add_argument("--run-dir", required=True,
                   help="eval/runs/<run_id>/  (must contain summary.json, "
                        "trials.jsonl, manifest.json)")
    p.add_argument("--output", default=None,
                   help="output PNG path (default: <run_dir>/k_sweep.png)")
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

    out = Path(args.output) if args.output else run_dir / "k_sweep.png"
    if render_k_sweep(summary, trials, manifest, out, title=args.title):
        print(f"saved: {out}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
