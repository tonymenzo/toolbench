"""
Parallel-coordinates rendering of the three-vector
(reach_bar_k, pass@k, pass^k) — the headline plot from
the accompanying manuscript Figure 1.

Three vertical axes spanning [0, 1], one polyline per
(cell × k) pair. `reach_bar_k` is constant in k, so all polylines for
a given cell share the leftmost anchor and fan to the right; the fan
width is a visual reading of k-sensitivity.

By default we draw the **full sweep** k = 1 ... n. Polylines are
colored by k via a perceptual colormap (viridis), with a shared
colorbar on the right indicating k. For multi-cell runs, one subplot
panel per cell, all panels sharing the colormap.

Usage:
    python -m toolbench.reporting.parallel_coords --run-dir eval/runs/<run_id>
"""

import argparse
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib as mpl

# REPO_ROOT is placed on sys.path by `eval/__init__.py`.
from toolbench.core.metrics import (
    pass_at_k, pass_caret_k, per_trial_reach,
)
from toolbench.reporting._shared import (
    gating_from_rows, pass_count_from_rows,
    short_model_name, stage_matrix_from_rows, subplot_grid,
)
from toolbench.reporting._output import save_figure, write_figure_data


# X positions of the three vertical axes, evenly spaced.
_AXIS_X = (0.0, 4.0, 8.0)

# Y range used for axes (mirrors the LaTeX figure: [0, 5] visually,
# rescaled from the [0, 1] metric range).
_Y_MIN, _Y_MAX = 0.0, 5.0
_TICK_VALUES = (0.0, 0.25, 0.5, 0.75, 1.0)
_TICK_Y      = tuple(v * (_Y_MAX - _Y_MIN) for v in _TICK_VALUES)


# Bottom axis labels (LaTeX-rendered) and top italic interpretive labels.
_AXIS_LABELS = (
    (r"$\overline{\mathrm{reach}}_k^{\,\mathbf{w}}$",
     "productivity",     "reach_bar_k"),
    (r"pass@$k$",
     "exploration",      "pass_at_k"),
    (r"pass$^{\,k}$",
     "trustworthiness",  "pass_caret_k"),
)


# Colormap used to color polylines by k. Viridis is perceptually
# uniform and colorblind-friendly across the full sweep.
_KMAP_NAME = "viridis"

# Common visual constants.
_LINEWIDTH    = 1.8
_MARKER_SIZE  = 5
_ALPHA        = 0.95


def _three_vector_at_k(stage_matrix: list[list[float]],
                       weights: list[float] | None,
                       k: int,
                       gating: list[bool] | None = None,
                       pass_count: int | None = None,
                       ) -> tuple[float, float, float]:
    """Return (reach_bar_k, pass@k, pass^k) for a given k.

    `reach_bar_k` is independent of k in expectation; we still recompute
    each time so callers don't have to pass it separately. Reach uses the
    graded credit matrix with `gating`; the pass family uses `pass_count` (the
    k-independent number of passing trials, per the run's pass criterion). If
    `pass_count` is None it falls back to the binary all-stages count.
    """
    R = per_trial_reach(stage_matrix, weights, gating)
    n = len(R)
    if n == 0:
        return 0.0, 0.0, 0.0
    reach_bar = sum(R) / n
    c = (pass_count if pass_count is not None
         else sum(1 for row in stage_matrix if row and all(row)))
    k = min(k, n)
    return reach_bar, pass_at_k(n, c, k), pass_caret_k(n, c, k)


def render_parallel_coords(summary: dict, trials: list[dict],
                           manifest: dict, output_path: Path,
                           *, title: str | None = None,
                           k_values: list[int] | None = None) -> bool:
    """Render the three-vector parallel-coordinates plot as a k-sweep
    fan (multiple polylines per cell, colored by k).

    Args:
        summary: parsed `summary.json` (gives the cell list + n).
        trials: rows from `trials.jsonl` (used to recompute the
            stage matrix per cell).
        manifest: parsed `manifest.json` (for `reach_weights`).
        output_path: where to write the PNG.
        title: optional figure title.
        k_values: which k values to plot. Defaults to the full sweep
            k = 1 ... n.

    Returns:
        True if a plot was produced, False if there was no data.
    """
    cells = summary.get("cells", [])
    if not cells:
        print("parallel_coords: no cells in summary; nothing to plot",
              file=sys.stderr)
        return False

    by_cell: dict[tuple[str, str], list[dict]] = {}
    for t in trials:
        by_cell.setdefault((t["model"], t["condition"]), []).append(t)

    rw = manifest.get("reach_weights") or {}
    stage_order = rw.get("stage_order")
    weights     = rw.get("w")
    pass_threshold = rw.get("pass_threshold")

    n_per_cell = summary.get("k") or manifest.get("n_per_cell") or 1
    if k_values is None:
        k_values = list(range(1, int(n_per_cell) + 1))
    k_values = sorted({max(1, int(k)) for k in k_values})

    # Discrete colormap with one bin per k value.
    cmap = mpl.colormaps[_KMAP_NAME].resampled(len(k_values))
    norm = mpl.colors.BoundaryNorm(
        boundaries=[k - 0.5 for k in k_values] + [k_values[-1] + 0.5],
        ncolors=cmap.N,
    )

    nrows, ncols = subplot_grid(len(cells))
    fig, axes = plt.subplots(
        nrows, ncols,
        figsize=(7.0 * ncols + 1.5, 5.4 * nrows),
        squeeze=False,
    )
    axes_flat = list(axes.flat)

    series_by_cell: list[dict] = []
    for ax, cell in zip(axes_flat, cells):
        rows = by_cell.get((cell.get("model"), cell.get("condition")), [])
        canonical = (stage_order
                     or (list((rows[0].get("stages") or {}).keys()) if rows else []))
        stage_matrix = stage_matrix_from_rows(rows, canonical) if rows else []
        gating = gating_from_rows(rows, canonical) if rows else None
        pass_count = pass_count_from_rows(rows, pass_threshold) if rows else 0
        cell_data = _draw_cell_panel(ax, cell, stage_matrix, weights,
                                     k_values=k_values, cmap=cmap, norm=norm,
                                     gating=gating, pass_count=pass_count)
        if cell_data is not None:
            series_by_cell.append(cell_data)

    # Hide unused panels.
    for ax in axes_flat[len(cells):]:
        ax.axis("off")

    # Shared colorbar on the right.
    sm = mpl.cm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=axes_flat[:len(cells)],
                        ticks=k_values, fraction=0.025, pad=0.04)
    cbar.set_label(r"$k$  (number of independent trials)", fontsize=10)
    cbar.ax.tick_params(labelsize=9)

    if title is None:
        run_id = summary.get("run_id", "")
        k_lo, k_hi = k_values[0], k_values[-1]
        k_str = (f"k = {k_lo}" if k_lo == k_hi
                 else f"k = {k_lo} … {k_hi}")
        title = (f"Three-vector  ({k_str})"
                 if not run_id else f"{run_id}   ({k_str})")
    fig.suptitle(title, fontsize=11, y=0.995)

    save_figure(fig, output_path, dpi=150)
    plt.close(fig)

    write_figure_data(output_path, {
        "figure": "parallel_coords",
        "run_id": summary.get("run_id", ""),
        "axes": [spec[2] for spec in _AXIS_LABELS],   # reach_bar_k, pass_at_k, pass_caret_k
        "weights": weights,
        "k": k_values,
        "cells": series_by_cell,
    })
    return True


def _draw_cell_panel(ax, cell: dict,
                     stage_matrix: list[list[float]],
                     weights: list[float] | None,
                     *, k_values: list[int],
                     cmap, norm,
                     gating: list[bool] | None = None,
                     pass_count: int | None = None,
                     ) -> dict | None:
    """Draw one (cell) panel with k_values polylines colored by k.

    Returns the exact three-vector polyline per k (for the data
    sidecar), or `None` for an empty panel.
    """
    cell_label = f"{short_model_name(cell.get('model', '?'))}  ×  {cell.get('condition', '?')}"

    if not stage_matrix:
        ax.set_title(f"{cell_label}  (no trials)", fontsize=10)
        ax.axis("off")
        return None

    # Vertical axes.
    for x in _AXIS_X:
        ax.plot([x, x], [_Y_MIN, _Y_MAX],
                color="#999", linewidth=1.5, zorder=1)
    for y, label in zip(_TICK_Y, _TICK_VALUES):
        ax.plot([_AXIS_X[0] - 0.10, _AXIS_X[0] + 0.10], [y, y],
                color="#666", linewidth=1, zorder=2)
        ax.text(_AXIS_X[0] - 0.30, y, f"{label:.2f}",
                ha="right", va="center", fontsize=9)
        for x in _AXIS_X[1:]:
            ax.plot([x - 0.10, x + 0.10], [y, y],
                    color="#666", linewidth=1, zorder=2)

    # Polylines, color = k.
    polylines: dict[int, list[float]] = {}
    for k in k_values:
        color = cmap(norm(k))
        vals = _three_vector_at_k(stage_matrix, weights, k,
                                  gating, pass_count)
        polylines[k] = list(vals)   # (reach_bar_k, pass_at_k, pass_caret_k)
        ys = [v * (_Y_MAX - _Y_MIN) + _Y_MIN for v in vals]
        ax.plot(_AXIS_X, ys,
                color=color, linewidth=_LINEWIDTH,
                alpha=_ALPHA, zorder=3 + k)
        for x, y in zip(_AXIS_X, ys):
            ax.plot(x, y, marker="o",
                    color=color, markersize=_MARKER_SIZE,
                    markeredgecolor="white", markeredgewidth=0.6,
                    alpha=_ALPHA, zorder=4 + k)

    # Top italic interpretive labels.
    for x, (_, annotation, _) in zip(_AXIS_X, _AXIS_LABELS):
        ax.text(x, _Y_MAX + 0.40, annotation,
                ha="center", va="bottom", fontsize=10, fontstyle="italic")

    # Bottom math labels.
    for x, (label, _, _) in zip(_AXIS_X, _AXIS_LABELS):
        ax.text(x, _Y_MIN - 0.55, label,
                ha="center", va="top", fontsize=12)

    ax.set_xlim(-1.6, _AXIS_X[-1] + 1.2)
    ax.set_ylim(_Y_MIN - 1.1, _Y_MAX + 1.0)
    ax.axis("off")
    ax.set_title(cell_label, fontsize=10, pad=18)

    return {
        "model": cell.get("model"),
        "condition": cell.get("condition"),
        "axes": [spec[2] for spec in _AXIS_LABELS],
        "polylines": polylines,   # {k: [reach_bar_k, pass_at_k, pass_caret_k]}
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="parallel_coords",
        description="Three-vector parallel-coordinates plot for an eval run.",
    )
    p.add_argument("--run-dir", required=True,
                   help="eval/runs/<run_id>/  (must contain summary.json, "
                        "manifest.json, trials.jsonl)")
    p.add_argument("--output", default=None,
                   help="output PNG path (default: <run_dir>/parallel_coords.png)")
    p.add_argument("--k-values", default=None,
                   help="comma-separated list of k values to draw "
                        "(default: [1, n/2, n]).")
    p.add_argument("--title", default=None,
                   help="override figure title")
    args = p.parse_args(argv)

    run_dir = Path(args.run_dir)
    summary_path  = run_dir / "summary.json"
    manifest_path = run_dir / "manifest.json"
    trials_path   = run_dir / "trials.jsonl"
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

    k_values = None
    if args.k_values:
        k_values = [int(s) for s in args.k_values.split(",") if s.strip()]

    out = Path(args.output) if args.output else run_dir / "parallel_coords.png"
    if render_parallel_coords(summary, trials, manifest, out,
                              title=args.title, k_values=k_values):
        print(f"saved: {out}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
