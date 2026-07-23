# Metrics

toolbench reports three numbers per cell, and they answer three different questions. This
page defines them precisely, while [Reading results & scores](../guides/reading-results.md)
walks through them with worked examples. If you only remember one thing:

> **reach** is *how far*, **pass@k** is *can it ever*, **pass^k** is *can it always*.

A *cell* is one `(model × condition)` group. Each cell has `n` trials (one per seed,
`n = --n`), and the order-statistic metrics are reported at `k = n` by default.

## Per-trial reach $R_j$ { #per-trial-reach }

A rubric is ordered, weighted stages $s_1, \dots, s_m$ with weights $w_i$ (summing to 1).
For trial $j$, each stage earns a **credit** $c_{j,i} \in [0,1]$ and is either **gating** (a
failure absorbs every later stage) or not. Reach walks the stages in order, carrying a
running "still on track" factor:

$$
R_j \;=\; \frac{1}{\sum_i w_i}\sum_{i=1}^{m} w_i \, c_{j,i}
          \!\!\prod_{\substack{\ell < i \\ \ell\ \text{gating}}}\!\! x_{j,\ell}
$$

where $x_{j,\ell} \in \{0,1\}$ is whether gating stage $\ell$ passed. Two *independent*
per-stage properties control the shape (both default off, giving the classic behaviour):

- **Credit** $c_{j,i}$. A plain binary stage earns $c = 1$ if it passed, else $0$. A
  [`continuous: true`](#continuous-and-independent-stages) stage earns a partial
  $c \in [0,1]$.
- **Gating.** A gating stage (the default) absorbs: if it fails, every later stage
  contributes $0$ regardless of its own credit. A `gating: false` stage contributes its own
  credit but does **not** block the stages after it.

**With every stage binary and gating — the default — this reduces exactly to the prefix
product**: you bank a stage's weight only if it *and every stage before it* passed. That is
the common case, and the reason reach measures *how far through the task the agent actually
got*, not how many scattered checks happened to pass.

!!! example "geometry rubric (default: binary + gating)"
    Stages `answer_written` (0.2) → `midpoint_correct` (0.3) → `distance_correct` (0.5).

    | What the agent produced                          | reach $R_j$ |
    |--------------------------------------------------|:-----------:|
    | nothing                                          | 0.0         |
    | the file, right keys, wrong values               | 0.2         |
    | file + correct midpoint, wrong distance          | 0.5         |
    | file + correct midpoint + correct distance       | 1.0         |

    `0.0, 0.2, 0.5, 1.0` are the only reachable values: because every stage gates, you can't
    earn the 0.5 distance stage while failing the 0.3 midpoint stage before it.

### What counts as a "pass"

The order-statistic metrics (pass@k / pass^k) need a binary per-trial verdict. By default a
trial **passes iff every stage passed**. A rubric with partial-credit stages can instead set
a [`pass_threshold`](#continuous-and-independent-stages): then a trial passes iff its reach
$R_j \ge$ the threshold. The run manifest records which rule was used as `pass_criterion`
(`all_stages` or `reach>=<x>`); `regrade` can change the threshold without re-running.

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

Both are computed from the same per-trial $R_j$ (so they honour continuous credit and
non-gating stages) and reported uniform-weighted alongside the rubric-weighted $\bar R_k$.
On a fully binary rubric, reach@k collapses to pass@k and reach^k to pass^k.

## Continuous and independent stages

Two rubric knobs, both optional and off by default, let a rubric score things the default
prefix product can't. They are set per stage (and, for the pass rule, per rubric); see
[Rubrics & checks](../authoring/rubrics-and-checks.md) for the authoring side.

**`continuous: true`** — the stage earns *partial* credit $c \in [0,1]$ instead of
all-or-nothing, and (by default) stops gating the stages after it. The credit comes from a
check's recorded `closeness` metric. The built-in checks are binary (they don't emit
`closeness` yet), so `continuous` today behaves as a non-gating binary stage; it becomes true
partial credit when a [custom check](../authoring/rubrics-and-checks.md#custom-checks)
returns a `closeness`. Its binary `passed` is still what pass@k counts.

**`gating: false`** — the stage contributes its credit but does **not** absorb later stages.
Use it for rubrics whose stages are *independent* rather than a pipeline — e.g. three
separate quantities in one task, each worth checking on its own. (`continuous: true` implies
`gating: false`; set `gating: false` on a plain binary stage to get independent all-or-nothing
stages.)

**`pass_threshold: <float in [0,1]>`** (rubric-level) — redefines a "pass" as reach $\ge$ the
threshold instead of all-stages. Once a rubric has continuous or independent stages, an exact
all-stages pass is rarely meaningful, so this is usually how you want pass@k / pass^k defined.
It is a grading-time decision: `regrade --run-id … ` picks up a changed threshold without
re-executing the agent.

!!! example "independent stages"
    A task that asks for three unrelated numbers, each a 1/3-weight stage with `gating: false`
    and `pass_threshold: 1.0`. A trial that nails two of three scores $R_j = 0.67$ and does
    **not** pass — whereas under the default gating rubric, missing the first would have zeroed
    the other two.

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
