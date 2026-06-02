"""
Physics-aware verifications for the rule judge.

Where content predicates (`content_checks.py`) only verify that the
agent produced *some* artifact of the right shape, these predicates
load the underlying numbers and verify the physics. They are exported
via `NUMERIC_CHECKS`, merged into the unified registry in `checks.py`,
and invoked by name from a rubric stage's `checks:` list — typically
alongside a content check (e.g. `plot_nonempty` + `peak_position`),
since a stage passes only when every check in its list passes.

Currently registered:

- `peak_position`: scans every `.npy` in the sandbox, asks whether
  each expected scan-point mass has a histogram peak within tolerance.
  Catches three failure modes the existence-only check misses:
    (a) fake plot from synthetic data — no `.npy` to peak.
    (b) wrong-pairing reconstruction — peaks at the wrong masses.
    (c) wrong-channel reconstruction (e.g., m(ℓℓ) instead of m(ℓj))
        — peaks at the wrong masses.
"""

import json
from pathlib import Path
from typing import Callable


# Snippet length in evidence strings.
_DETAIL_LEN = 200


def check_peak_position(sandbox: Path, params: dict) -> tuple[bool, str]:
    """Verify that the agent's mass arrays peak near each expected mass.

    For every value in `expected_masses`, the check scans all `.npy`
    files in the sandbox and asks: is there a file whose distribution
    has its **dominant peak** within ±`tolerance_frac` of that mass,
    with at least `min_events_per_peak` events in the peak window?
    All expected masses must be matched (each by *some* file).

    Params:
        expected_masses: list[float], scan-point masses in GeV.
        tolerance_frac: float, peak must be within ±this fraction of expected.
            Default 0.10 (±10%).
        min_events_per_peak: int, minimum events in the ±tol window.
            Default 50.
        min_peaks: int, how many of the expected peaks must be found.
            Default len(expected_masses) (i.e. all of them — strict).
            Set lower (e.g. 1) to credit partial reconstructions where
            the agent only handled some scan points.
        n_bins: int, histogram resolution for peak finding. Default 60.
        smoothing: int, boxcar window width (bins) for noise suppression.
            Default 3. Set to 1 to disable.
    """
    try:
        import numpy as np
    except ImportError:
        return False, "peak_position: numpy not available"

    expected = params.get("expected_masses") or []
    if not expected:
        return False, "peak_position: expected_masses must be non-empty"
    expected = [float(m) for m in expected]
    tol_frac        = float(params.get("tolerance_frac", 0.10))
    min_events      = int(params.get("min_events_per_peak", 50))
    min_peaks       = int(params.get("min_peaks", len(expected)))
    min_peaks       = max(1, min(min_peaks, len(expected)))
    n_bins          = int(params.get("n_bins", 60))
    smoothing       = max(1, int(params.get("smoothing", 3)))

    arrays = _collect_mass_arrays(sandbox, np, min_events)
    if not arrays:
        return False, (
            f"peak_position: no .npy with ≥{min_events} finite float events "
            f"(scanned {len(list(sandbox.rglob('*.npy')))} files)"
        )

    found: list[tuple[float, str, float, int]] = []
    missing: list[float] = []
    for m in expected:
        lo, hi = m * (1 - tol_frac), m * (1 + tol_frac)
        best = None
        for rel_path, arr in arrays:
            n_in_window = int(((arr >= lo) & (arr <= hi)).sum())
            if n_in_window < min_events:
                continue
            peak_mass = _dominant_peak_position(arr, np,
                                                n_bins=n_bins,
                                                smoothing=smoothing,
                                                preferred_window=(lo, hi))
            if peak_mass is None:
                continue
            if abs(peak_mass - m) / m <= tol_frac:
                best = (rel_path, peak_mass, n_in_window)
                break
        if best is not None:
            found.append((m, *best))
        else:
            missing.append(m)

    if len(found) >= min_peaks:
        detail = "; ".join(
            f"m={m:.0f}→{path} (peak={pk:.0f}, n={n})"
            for m, path, pk, n in found
        )
        gate = (f"all {len(expected)} peaks within"
                if not missing
                else f"{len(found)}/{len(expected)} peaks within (≥{min_peaks} required)")
        return True, _truncate(f"{gate} ±{tol_frac*100:.0f}%: {detail}",
                               _DETAIL_LEN)

    return False, _truncate(
        f"missing peaks at {missing} GeV (±{tol_frac*100:.0f}%); "
        f"found {len(found)}/{len(expected)} (≥{min_peaks} required)",
        _DETAIL_LEN,
    )


def _collect_mass_arrays(sandbox: Path, np, min_events: int
                         ) -> list[tuple[str, "np.ndarray"]]:
    """Load every .npy as 1-D finite float, drop the empties.

    Returns list of (relative_path_str, array) tuples sorted with
    smaller arrays last, so the search prefers larger-statistics files.
    """
    out: list[tuple[str, "np.ndarray"]] = []
    for p in sandbox.rglob("*.npy"):
        try:
            arr = np.load(p, allow_pickle=False)
        except Exception:
            continue
        if arr.dtype.kind != "f":
            continue
        arr = np.asarray(arr).ravel()
        arr = arr[np.isfinite(arr)]
        if len(arr) < min_events:
            continue
        try:
            rel = str(p.relative_to(sandbox))
        except ValueError:
            rel = p.name
        out.append((rel, arr))
    out.sort(key=lambda x: -len(x[1]))
    return out


def _dominant_peak_position(arr, np, *, n_bins: int, smoothing: int,
                            preferred_window: tuple[float, float] | None
                            ) -> float | None:
    """Return the bin-center mass of the dominant histogram peak.

    "Dominant" = largest local maximum after boxcar smoothing of length
    `smoothing`. If `preferred_window` is given, restricts the histogram
    range to that window's vicinity so a small signal peak isn't
    drowned by a giant combinatoric pile-up at zero.
    """
    if len(arr) == 0:
        return None

    if preferred_window is not None:
        lo, hi = preferred_window
        # Look in a region 2× the tolerance window around the expected
        # mass — wide enough to catch shifted peaks but narrow enough
        # to ignore unrelated background.
        half = (hi - lo) / 2
        center = (hi + lo) / 2
        rng_lo = max(0.0, center - 3 * half)
        rng_hi = center + 3 * half
    else:
        rng_lo = float(arr.min())
        rng_hi = float(arr.max())
    if rng_hi <= rng_lo:
        return None

    hist, edges = np.histogram(arr, bins=n_bins, range=(rng_lo, rng_hi))
    if hist.sum() == 0:
        return None

    if smoothing > 1:
        kernel = np.ones(smoothing) / smoothing
        hist = np.convolve(hist, kernel, mode="same")

    centers = (edges[:-1] + edges[1:]) / 2
    return float(centers[int(np.argmax(hist))])


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n - 1] + "…"


NUMERIC_CHECKS: dict[str, Callable[[Path, dict], tuple[bool, str]]] = {
    "peak_position": check_peak_position,
}
