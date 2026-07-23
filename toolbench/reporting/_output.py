"""
Output helpers for the reporting plotters: vector+raster figure saving,
and per-figure data sidecars.

Publication runs need (a) vector output for typesetting and (b) the
exact plotted series in a portable form, so a figure can be restyled
without re-running the (expensive) eval and without depending on this
package's plotting internals. Every `render_*` in this subpackage
writes, alongside the conventional `<name>.png`:

    <name>.pdf        vector twin of the figure (typeset from this)
    <name>.data.json  the exact series each curve is drawn from, plus
                      enough metadata (metric labels, stage order,
                      rubric weights) to re-plot the figure standalone

Kept out of `_shared.py` so that module stays pure (no matplotlib, no
I/O), as its docstring promises.
"""

import json
from pathlib import Path


def save_figure(fig, output_path, *, dpi: int = 150) -> list[Path]:
    """Write `fig` as both PNG (raster preview) and PDF (vector).

    `output_path` is the conventional PNG path (`<run_dir>/<name>.png`);
    the PDF twin is `<name>.pdf`. `dpi` applies to the raster only — the
    PDF is resolution-independent and is what publication figures should
    be typeset from. Returns the paths written, in order.
    """
    out = Path(output_path)
    written: list[Path] = []
    for suffix in (".png", ".pdf"):
        p = out.with_suffix(suffix)
        fig.savefig(p, dpi=dpi, bbox_inches="tight")
        written.append(p)
    return written


def write_figure_data(output_path, payload: dict) -> Path:
    """Write the per-figure data sidecar `<name>.data.json`.

    `payload` holds the exact series the figure draws plus the metadata
    needed to restyle it standalone (metric labels, stage order, rubric
    weights, the run id). `default=str` guarantees the write never fails
    on a stray non-JSON-native scalar (e.g. a numpy float); the series
    produced by the metrics layer are already plain Python floats.
    Returns the path written.
    """
    out = Path(output_path).with_suffix(".data.json")
    out.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    return out
