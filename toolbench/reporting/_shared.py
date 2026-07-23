"""
Shared helpers for the reporting plotters.

Each plotter (`k_sweep`, `per_stage_k`, `parallel_coords`, ...) needs
the same three primitives: a short display name for the model, a
subplot-grid shape that scales gracefully with cell count, and a 0/1
stage matrix built from a trial list in canonical rubric order. They
live here so the plotters stay in sync.

Pure functions only — no matplotlib, no I/O.
"""

import math
from typing import Sequence


def short_model_name(name: str) -> str:
    """Strip the provider prefix from a fully-qualified model id.

    The provider routing metadata is stored separately in the run
    manifest; the headline plots show the model name itself. So
    `"openai/gpt-oss-120b"` → `"gpt-oss-120b"`, and
    `"claude-haiku-4-5"` → `"claude-haiku-4-5"`.
    """
    if "/" in name:
        return name.rsplit("/", 1)[-1]
    return name


def subplot_grid(n_cells: int) -> tuple[int, int]:
    """Subplot grid shape for `n_cells` panels.

    Layout policy: a single row when there are 1–3 cells (so the legend
    has room on the right), near-square otherwise.

    Returns:
        `(nrows, ncols)` with `nrows * ncols >= n_cells`.
    """
    if n_cells <= 1:
        return 1, 1
    if n_cells <= 3:
        return 1, n_cells
    ncols = math.ceil(math.sqrt(n_cells))
    nrows = math.ceil(n_cells / ncols)
    return nrows, ncols


def stage_matrix_from_rows(rows: Sequence[dict],
                           canonical: Sequence[str]) -> list[list[int]]:
    """Build a 0/1 stage matrix from trial rows in canonical order.

    Each output row is one trial; each column corresponds to a stage
    in `canonical`. A 1 marks "stage passed in this trial". Trial rows
    whose `stages` dict is missing or empty (e.g. GRADE_ERROR rows)
    become a row of zeros — they reached nothing.

    Args:
        rows: trial dicts loaded from `trials.jsonl`. Each must have a
            `stages` field (dict[stage_id, bool]); missing/None is
            treated as the empty dict.
        canonical: canonical stage order (e.g. from
            `manifest["reach_weights"]["stage_order"]`).

    Returns:
        Matrix of shape (len(rows), len(canonical)) with int 0/1
        entries. The matrix is suitable for passing to the reach
        estimators in `toolbench.core.metrics`.
    """
    matrix: list[list[float]] = []
    for r in rows:
        s = r.get("stages") or {}
        credits = r.get("stage_credits") or {}
        # Prefer the continuous per-stage credit when present (a [0,1] closeness
        # for `continuous` stages, 1.0/0.0 for binary); fall back to the binary
        # pass for older rows without credits.
        matrix.append([float(credits[sid]) if sid in credits
                       else (1.0 if s.get(sid) else 0.0)
                       for sid in canonical])
    return matrix


def binary_stage_matrix_from_rows(rows: Sequence[dict],
                                  canonical: Sequence[str]) -> list[list[int]]:
    """Binary 1/0 stage matrix from each row's `stages` dict (never credits).

    The PASS family (pass@k / pass^k, the all-stages count, per-stage absorbing
    probabilities) is defined on the binary stage OUTCOMES, not the continuous
    credits. Feeding the credit matrix into that logic is wrong: a continuous
    stage with credit 0.87 is truthy, so `all(row)` and `if s` treat it as a
    pass even when it missed its binary threshold. Use this matrix for those
    counts and keep `stage_matrix_from_rows` (credits) for the reach estimators.
    """
    matrix: list[list[int]] = []
    for r in rows:
        s = r.get("stages") or {}
        matrix.append([1 if s.get(sid) else 0 for sid in canonical])
    return matrix


def pass_count_from_rows(rows: Sequence[dict],
                         pass_threshold: float | None = None) -> int:
    """Number of trials that "pass", for pass@k / pass^k. Mirrors
    `cli._trial_passed` so the plots agree with the summary: `pass_threshold is
    None` -> binary all-stages criterion; a float -> per-trial reach (the row's
    `score`) >= the threshold (the meaningful definition with continuous
    stages)."""
    c = 0
    for r in rows:
        if pass_threshold is not None:
            if float(r.get("score") or 0.0) >= pass_threshold:
                c += 1
        else:
            s = r.get("stages") or {}
            if s and all(s.values()):
                c += 1
    return c


def gating_from_rows(rows: Sequence[dict],
                     canonical: Sequence[str]) -> list[bool] | None:
    """Per-stage gating mask for the reach estimators, in canonical order.

    `gate[i]` is False for a `continuous` stage (it contributes graded credit
    times the cumulative prefix) and True for a binary stage (absorbing
    prefix-product). Read from any row's `stage_continuous` map. Returns None
    when no row declares continuity, so the estimators fall back to the original
    all-gating (binary-prefix) behaviour.
    """
    cont: dict[str, bool] = {}
    for r in rows:
        for k, v in (r.get("stage_continuous") or {}).items():
            cont[k] = bool(v)
    if not cont:
        return None
    return [not cont.get(sid, False) for sid in canonical]
