"""
Metric helpers for the eval harness.

The metrics in this module form a single family. Given a per-trial
score `M_j ∈ [0,1]` measured over `n` independent trials, we report
three k-statistics — each an expectation over `k` iid trials, differing
only in which functional of the k-sample they take:

    M̄_k    = E[ (1/k) Σ M_j ]      mean-of-k     "typical performance"
    M_@k   = E[ max_j M_j ]         best-of-k     "exploration"
    M^k    = E[ min_j M_j ]         worst-of-k    "reliability"

Substituting `M_j = 1{trial j passes}` gives Chen-et-al pass@k
(`pass_at_k`) and τ-bench pass^k (`pass_caret_k`) as the binary
special case. Substituting the partial-credit workflow score
`R_j = (1/W) Σ_i w_i · prod_{l<=i} S[j,l]` (`per_trial_reach`)
gives `reach_bar_k` / `reach_at_k` / `reach_caret_k`.

See the accompanying manuscript for the full reference — definitions,
unbiasedness derivations, worked example, and reporting template.

This module:

- `per_trial_reach`: the per-trial reach R_j with the absorbing
   convention (stage i contributes only if all stages ≤ i passed).
- `reach_bar_k`: M̄_k for partial-credit R_j. The sample mean is
   the unbiased estimator (independent of k by exchangeability).
- `reach_at_k` / `reach_caret_k`: order-statistic unbiased estimators
   of E[max_j R_j] and E[min_j R_j]. Collapse to `pass_at_k` /
   `pass_caret_k` on binary inputs.
- `pass_at_k`: unbiased pass@k estimator (Chen et al. 2021). The
   `M_j = 1{pass}` boundary case of `reach_at_k`.
- `pass_caret_k`: unbiased pass^k estimator (Yao et al. 2024,
   τ-bench), `comb(c, k) / comb(n, k)`. The plug-in `(c/n)^k` form
   is biased downward (Jensen) and is *not* what we report.
- `pearson_corr_matrix`: M×M correlation matrix from a list of
   length-M observation vectors (e.g. bootstrap samples of a metric
   triplet). Off-diagonal entries are None when one variable has zero
   variance.
- `bootstrap_ci`: bootstrap CI for the mean.
- `cost_usd`: pricing-table fallback for providers/models where the
   Orchestral `Usage` object isn't populated. Orchestral's
   `agent.context.get_total_cost()` is the primary cost source; this
   helper exists for dry-run / offline aggregation.
"""

import math
import random
from typing import Sequence


# Fallback prices in USD per million tokens, used only when Orchestral
# does not provide a populated Usage. Format: (input, output, cache_read).
PRICING_TABLE: dict[tuple[str, str], tuple[float, float, float | None]] = {
    ("anthropic", "claude-haiku-4-5"):  (1.00,  5.00,  0.10),
    ("anthropic", "claude-sonnet-4-6"): (3.00, 15.00,  0.30),
    ("anthropic", "claude-opus-4-7"):   (15.00, 75.00, 1.50),
    ("openai",    "gpt-4o-mini"):       (0.15,  0.60,  None),
    ("openai",    "gpt-4o"):            (2.50, 10.00,  None),
    ("google",    "gemini-2.0-flash"):  (0.075, 0.30,  None),
}


def cost_usd(provider: str, model: str,
             input_tokens: int = 0, output_tokens: int = 0,
             cache_read_tokens: int = 0) -> float | None:
    """Compute USD cost from a pricing table; returns None if unknown."""
    rate = PRICING_TABLE.get((provider.lower(), model))
    if rate is None:
        return None
    in_rate, out_rate, cache_rate = rate
    cost = (input_tokens * in_rate + output_tokens * out_rate) / 1e6
    if cache_rate is not None and cache_read_tokens:
        cost += cache_read_tokens * cache_rate / 1e6
    return cost


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased best-of-k estimator on binary M_j (Chen et al. 2021).

    The M_j = 1{trial j passes} special case of `reach_at_k`:
    estimates M_@k = E[max_{j ∈ [k]} M_j] = P(at least one of k iid
    trials passes). Closed form is 1 − C(n−c, k) / C(n, k).

    Args:
        n: total samples drawn for the problem.
        c: number of correct samples.
        k: subset size.
    """
    if k > n:
        raise ValueError(f"k ({k}) cannot exceed n ({n})")
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def pass_caret_k(n: int, c: int, k: int) -> float:
    """Unbiased worst-of-k estimator on binary M_j (τ-bench, 2024).

    The M_j = 1{trial j passes} special case of `reach_caret_k`:
    estimates M^k = E[min_{j ∈ [k]} M_j] = P(every one of k iid
    trials passes). Closed form is C(c, k) / C(n, k); equivalently,
    the fraction of size-k subsets of the n observations that lie
    entirely inside the c successful ones.

    The plug-in form (c/n)^k is biased downward by Jensen and is not
    what we return.

    Args:
        n: total samples drawn for the problem.
        c: number of correct samples.
        k: subset size.
    """
    if k > n:
        raise ValueError(f"k ({k}) cannot exceed n ({n})")
    if c < k:
        return 0.0
    return math.comb(c, k) / math.comb(n, k)


def per_trial_reach(stage_matrix: Sequence[Sequence[float]],
                    weights: Sequence[float] | None = None,
                    gating: Sequence[bool] | None = None) -> list[float]:
    """Per-trial reach values R_j ∈ [0,1].

    R_j = (1/W) * sum_i w_i * prod_{l<=i} S[j, l]. Cumulative product
    enforces the absorbing convention: a stage only contributes if it
    and all prior stages passed (see the accompanying manuscript §2b).

    With `weights=None`, equal weights w_i = 1 are used and R_j becomes
    the (equal-weighted) "depth" — the fraction of the pipeline a
    trial completed before its first failure.

    Returns an empty list if `stage_matrix` is empty, has zero-width
    rows, or `weights` sums to zero.
    """
    rows = [list(r) for r in stage_matrix]
    if not rows:
        return []
    n_stages = len(rows[0])
    if n_stages == 0:
        return []
    w = [1.0] * n_stages if weights is None else list(weights)
    total_w = sum(w)
    if total_w <= 0:
        return []
    out: list[float] = []
    for row in rows:
        cum = 1.0
        r_session = 0.0
        for i, s in enumerate(row):
            gate = True if gating is None else bool(gating[i])
            if gate:
                # Absorbing gate: `s` is a 0/1 pass; a fail zeros all later
                # contributions (the documented prefix-product convention).
                cum = cum if s else 0
                r_session += cum * w[i]
            else:
                # Non-gating continuous stage: contribute its partial credit
                # `s` in [0,1] scaled by the current gate, WITHOUT absorbing the
                # stages after it (so a strict-but-secondary check no longer
                # zeroes the reach bands it precedes).
                r_session += cum * float(s) * w[i]
        out.append(r_session / total_w)
    return out


def reach_bar_k(stage_matrix: Sequence[Sequence[float]],
                weights: Sequence[float] | None = None,
                gating: Sequence[bool] | None = None) -> float:
    """Mean-of-k for partial-credit M_j = R_j: R̄_k = E[(1/k) Σ R_j].

    Unbiased estimator: the sample mean of `per_trial_reach`. The
    estimator is independent of k by exchangeability (averaging
    commutes with the iid expectation). See the accompanying manuscript §3a.

    Returns 0.0 if there are no per-trial reaches.
    """
    R = per_trial_reach(stage_matrix, weights, gating)
    if not R:
        return 0.0
    return sum(R) / len(R)


def reach_at_k(stage_matrix: Sequence[Sequence[float]], k: int,
               weights: Sequence[float] | None = None,
               gating: Sequence[bool] | None = None) -> float:
    """Best-of-k for partial-credit M_j = R_j: M_@k = E[max_j R_j].

    Sorts per-trial reaches R_(1) <= ... <= R_(n) and returns
    sum_{i=k}^{n} C(i-1, k-1)/C(n, k) * R_(i). The weight on R_(i)
    is the probability that R_(i) is the max of a uniformly random
    size-k subset of the n observations. Collapses to `pass_at_k`
    when R_j is binary. See the accompanying manuscript §3a.
    """
    R = per_trial_reach(stage_matrix, weights, gating)
    if not R:
        return 0.0
    n = len(R)
    if k > n:
        raise ValueError(f"k ({k}) cannot exceed n ({n})")
    R.sort()
    denom = math.comb(n, k)
    out = 0.0
    for i in range(k, n + 1):
        out += math.comb(i - 1, k - 1) / denom * R[i - 1]
    return out


def reach_caret_k(stage_matrix: Sequence[Sequence[float]], k: int,
                  weights: Sequence[float] | None = None,
                  gating: Sequence[bool] | None = None) -> float:
    """Worst-of-k for partial-credit M_j = R_j: M^k = E[min_j R_j].

    Symmetric dual of `reach_at_k`: weight on R_(i) is
    C(n-i, k-1)/C(n, k), the probability R_(i) is the min of a
    uniformly random size-k subset. Collapses to `pass_caret_k` on
    binary R. See the accompanying manuscript §3a.
    """
    R = per_trial_reach(stage_matrix, weights, gating)
    if not R:
        return 0.0
    n = len(R)
    if k > n:
        raise ValueError(f"k ({k}) cannot exceed n ({n})")
    R.sort()
    denom = math.comb(n, k)
    out = 0.0
    for i in range(1, n - k + 2):
        out += math.comb(n - i, k - 1) / denom * R[i - 1]
    return out


def pearson_corr_matrix(samples: Sequence[Sequence[float]]
                        ) -> list[list[float | None]]:
    """Pearson correlation matrix from a list of length-M observation vectors.

    Returns an M×M matrix (list-of-lists). Off-diagonal entries where
    either variable has zero variance are returned as None to make the
    degeneracy explicit; diagonal is always 1.0.
    """
    if not samples:
        return []
    M = len(samples[0])
    n = len(samples)
    if n < 2:
        return [[1.0 if i == j else None for j in range(M)] for i in range(M)]
    means = [sum(s[i] for s in samples) / n for i in range(M)]
    variances = [
        sum((s[i] - means[i]) ** 2 for s in samples) / (n - 1)
        for i in range(M)
    ]
    stds = [math.sqrt(v) for v in variances]
    corr: list[list[float | None]] = [[0.0] * M for _ in range(M)]
    for i in range(M):
        for j in range(M):
            if i == j:
                corr[i][j] = 1.0
                continue
            if stds[i] == 0 or stds[j] == 0:
                corr[i][j] = None
                continue
            cov = sum(
                (s[i] - means[i]) * (s[j] - means[j]) for s in samples
            ) / (n - 1)
            corr[i][j] = cov / (stds[i] * stds[j])
    return corr


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def bootstrap_ci(values: Sequence[float], n_bootstrap: int = 1000,
                 ci: float = 0.95, seed: int | None = None) -> tuple[float, float, float]:
    """Bootstrap CI for the mean. Returns (mean, lo, hi)."""
    if not values:
        return (0.0, 0.0, 0.0)
    rng = random.Random(seed)
    arr = list(values)
    means = []
    for _ in range(n_bootstrap):
        sample = [rng.choice(arr) for _ in arr]
        means.append(sum(sample) / len(sample))
    means.sort()
    lo_idx = int(n_bootstrap * (1 - ci) / 2)
    hi_idx = int(n_bootstrap * (1 + ci) / 2) - 1
    return (mean(arr), means[lo_idx], means[hi_idx])
