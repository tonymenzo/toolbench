# Metrics

toolbench reports three numbers per cell, and they answer three different questions. This
page defines them precisely, while [Reading results & scores](../guides/reading-results.md)
walks through them with worked examples. If you only remember one thing:

> **reach** is *how far*, **pass@k** is *can it ever*, **pass^k** is *can it always*.

A *cell* is one `(model × condition)` group. Each cell has `n` trials (one per seed,
`n = --n`), and the order-statistic metrics are reported at `k = n` by default.

## Per-trial reach $R_j$

A rubric is ordered, weighted stages $s_1, \dots, s_m$ with weights $w_i$
(summing to 1). For trial $j$, let $x_{j,i} \in \{0,1\}$ be whether stage $i$ passed.
With `type: stagewise` the score is the **prefix product**, where you bank a stage's weight
only if it *and every stage before it* passed:

$$
R_j \;=\; \frac{1}{\sum_i w_i}\sum_{i=1}^{m} w_i \prod_{\ell \le i} x_{j,\ell}
$$

So a trial that writes the answer file (stage 1) but gets the value wrong (stage 2) earns
stage 1's weight and nothing after it. This is the whole point. **The score measures how
far through the task the agent actually got**, not how many scattered checks happened to
pass. A trial "passes" (for pass@k / pass^k) iff $R_j = 1$, i.e. every stage passed.

!!! example "geometry rubric"
    Stages `answer_written` (0.2) → `midpoint_correct` (0.3) → `distance_correct` (0.5).

    | What the agent produced                          | reach $R_j$ |
    |--------------------------------------------------|:-----------:|
    | nothing                                          | 0.0         |
    | the file, right keys, wrong values               | 0.2         |
    | file + correct midpoint, wrong distance          | 0.5         |
    | file + correct midpoint + correct distance       | 1.0         |

    Note 0.2 + 0.5 → 1.0 are the only reachable values: because it's a prefix product, you
    can't earn the 0.5 distance stage while failing the 0.3 midpoint stage before it.

## reach $\bar R_k$ (*how far, on average*)

The headline metric, the mean per-trial reach across the cell.

$$
\bar R_k \;=\; \frac{1}{n}\sum_{j=1}^{n} R_j
$$

Reported with a bootstrap 95% CI. A cell at $\bar R_k = 0.5$ is, on average, getting
halfway (by rubric weight) through the task. toolbench also reports a **uniform-weighted**
twin (all stages weighted equally) so you can read "depth of pipeline reached" without
knowing the headline weights.

## pass@k (*can it ever?*)

Given $n$ trials of which $c$ fully passed, pass@k is the probability that a random subset
of $k$ trials contains **at least one** full pass, the unbiased best-of-k estimator
(the [HumanEval](https://arxiv.org/abs/2107.03374) estimator):

$$
\text{pass@}k \;=\; 1 - \frac{\dbinom{n-c}{k}}{\dbinom{n}{k}}
$$

In other words, if you let the agent try k times and kept the best, how often would you get
a fully-correct result? High pass@k with low reach means the capability is **there but
unreliable**, worth a retry wrapper.

## pass^k (*can it always?*)

The probability that a random subset of $k$ trials are **all** full passes, the
worst-of-k estimator:

$$
\text{pass\^}k \;=\; \frac{\dbinom{c}{k}}{\dbinom{n}{k}}
$$

In other words, if you ran the agent k times, how often would every single run succeed?
This is the number to watch for **production reliability**. An agent you can trust
unattended needs pass^k near 1, not just pass@k.

`pass@1 = pass^1 = c/n` (the raw success rate). The two metrics fan out from there as
`k` grows, pass@k upward and pass^k downward.

## reach order statistics

The reach analogues of the pass metrics, for tasks where partial credit matters:

- **reach@k**, expected reach of the *best* of `k` trials (best-of-k).
- **reach^k**, expected reach of the *worst* of `k` trials (worst-of-k).

Both are reported uniform-weighted alongside the rubric-weighted $\bar R_k$.

## Correlation matrix

For each cell toolbench bootstrap-resamples the three-vector
$(\bar R_k, \text{pass@}k, \text{pass\^}k)$ and reports their 3×3 Pearson correlation,
a quick read on whether, *for this cell*, the three views agree or diverge (e.g. reach
high but pass^k low ⇒ many near-misses).

## What a sweep tells you

Because the axes are orthogonal, the **delta** between two cells attributes a capability:

| Sweep                          | The delta measures…                          |
|--------------------------------|----------------------------------------------|
| `--loadouts core_only,full`    | what the domain tools buy you                |
| `--models a,b`                 | model capability, tools/harness fixed        |
| `--variants direct,derived`    | the cost of less scaffolding (derivation)    |
| `--harnesses h1,h2`            | the runtime/loop policy's effect             |

See [Reading results & scores](../guides/reading-results.md) for how to read these deltas
off a real `summary.txt`.
