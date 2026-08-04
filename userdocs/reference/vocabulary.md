# Vocabulary

The words toolbench uses, and the lines between them. The five composition axes
(benchmark, model, harness, loadout, variant) are deliberately orthogonal, and that
separation is what makes a sweep a clean ablation.

## The layers

The full stack from model to measurement has five layers, each with one job:

| Layer                      | Responsibility                                              | Examples                                  |
|----------------------------|------------------------------------------------------------|-------------------------------------------|
| **Model**                  | LLM inference, called over a provider API.                 | `claude-opus-4-8`, `gpt-5.5`              |
| **Harness**                | The agent runtime that drives the tool-call loop, manages context, and formats wire payloads. | `orchestral`, `claude-code`, `codex-cli` |
| **Toolkit**                | A versioned bundle of tools + deps, isolated in its own env. | served by **toolbase**                   |
| **Tool manager**           | Installs, isolates, and serves toolkits at run time.       | **toolbase** / `tb`                       |
| **Benchmarking framework** | Loads a task, runs trials, grades, reports metrics.       | **toolbench**                             |

## Core terms

Benchmark
:   The task **plus** how to grade it, meaning a user prompt, an optional system prompt, a
    sandbox seed, a rubric, and ground truth. Lives in `examples/<name>/benchmark.yaml`. A
    benchmark references tools and harnesses, but it is not itself a toolbase artifact.

Harness
:   The agent execution framework. Owns the loop policy (retries, error handling), the
    wire format, and the **core tools** (file I/O, shell, run-python, …). Declared per
    benchmark under `harnesses/`. *Not* toolbench itself, and not the model.

Loadout
:   The **domain tools** the agent is equipped with, beyond the harness core. An ordered
    list of *sources*, each either a `python:` module or a `toolbase:` loadout. Loadouts
    are the usual ablation axis. `core_only` vs `full_local` measures what the tools buy
    you.

Source
:   One entry in a loadout's tool list. `python:` imports a module exposing `TOOLS` /
    `make_tools()` (the no-toolbase escape hatch). `toolbase:` resolves a curated set from
    a [toolbase](../guides/toolbase.md) loadout. A tool name may not appear from two
    sources at once.

Variant
:   The **scaffolding** axis, the prompt plus sandbox seed, orthogonal to the tools. Variants
    of one benchmark share the same rubric and ground truth, so a cross-variant score
    delta isolates the cost of *less scaffolding* (e.g. points given directly vs. derived
    from a description).

Rubric
:   The grading spec, a set of ordered, weighted **stages**, each a list of **checks**.
    `type: stagewise` means the trial score is its weighted **reach**: a stage banks its
    weight only if it — and every *gating* stage before it — passed. With the default
    binary + gating stages this is exactly the old prefix product. A rubric-level
    `pass_threshold` can redefine when a trial "passes". See
    [Metrics](metrics.md) and [Rubrics & checks](../authoring/rubrics-and-checks.md).

Continuous stage / partial credit
:   A stage marked `continuous: true` earns *partial* credit ∈ [0,1] (from a check's
    `closeness` metric) instead of all-or-nothing, and stops gating later stages. Built-in
    checks are binary and don't emit `closeness` yet, so `continuous` today behaves as a
    non-gating binary stage — forward-looking, real partial credit lands once a custom
    check returns a `closeness`.

Gating
:   Whether a stage **absorbs** the ones after it. A gating stage (the default) zeroes every
    later stage if it fails, giving the pipeline "how far did the agent get" reading. A
    `gating: false` stage contributes its own credit but blocks nothing — for rubrics whose
    stages are independent quantities rather than a pipeline.

pass_threshold
:   A rubric-level float that redefines a "pass": a trial passes iff its reach ≥ the
    threshold, instead of the default all-stages rule. A grading-time knob, so `regrade` can
    change it without re-running. The manifest records the rule as `pass_criterion`
    (`all_stages` or `reach>=<x>`).

Check
:   One pass/fail predicate evaluated against the sandbox after a trial (e.g.
    `json_with_keys`, `close_to`). Built-in checks plus any benchmark-local ones. Each
    check carries a *role*, either `presence` (did the deliverable get made?) or
    `correctness` (is it right?), which the runner uses for presence-gated nudges without
    leaking the answer.

Judge / LLM judge
:   How a trial is graded. The **rule** judge (deterministic checks) is always primary and
    sets the score. A `rule+llm` (or, via `regrade`, `llm`) judge additionally asks a model
    for an opinion, recorded in `alt_grades` — it never silently overrides the rule score.
    The judge runs through its *own* harness/model (`judge:` block, or `--judge*` flags),
    which may differ from the agent under test.

Integrity leak / quarantine
:   A trial whose tool-call inputs referenced the graded ground-truth answer key. The
    integrity scan flags it, and the trial is **quarantined**: its `score` is set to `0`,
    `failure_mode` becomes `INTEGRITY_LEAK`, and the original score is preserved as
    `score_pre_integrity` (with `integrity_leak` / `integrity_evidence` on the row).

UX-feedback turn
:   One extra, **unscored** turn per trial (opt-in via `--ux-feedback` / `loop.ux_feedback`)
    in which the agent critiques the tools it was given, written to `ux_feedback.md`. It
    never affects the grade — pure tool-design signal.

Trial
:   One execution of a single `(benchmark, harness, loadout, variant, model, seed)` cell.
    Produces a transcript + artifacts, graded to a per-trial reach $R_j \in [0, 1]$.

Run
:   A set of trials over one or more cells, with `--n` seeds each. Produces the aggregated
    `summary.json` and plots under `runs/<run_id>/`.

Cell / condition
:   One `(model × condition)` group that metrics are computed over, where *condition* is
    the swept-axis label (loadout, and harness/variant when those are swept too).

Ground truth
:   The reference answer(s) a rubric's correctness checks compare against, under the
    benchmark's `ground_truth/` dir.

Sandbox
:   The per-trial working directory the agent operates in. Seeded from the variant's
    template, graded in place, then cleaned to a minimal evidence set for `regrade`.

## Scores

reach ($\bar R_k$)
:   Mean rubric-weighted reach across a cell's trials, meaning *how far through the staged
    pipeline the agent gets*, on average. The headline metric.

pass@k
:   Probability that **at least one** of `k` trials passes — all stages by default, or reach ≥
    `pass_threshold` when set — the optimistic "can it ever do it?" view (best-of-k).

pass^k
:   Probability that **all** `k` trials pass, under the same pass rule as pass@k, the
    pessimistic "can it do it *reliably*?" view (worst-of-k).

All three come with bootstrap 95% confidence intervals. They are discussed intuitively in
[Reading results & scores](../guides/reading-results.md) and defined precisely in
[Metrics](metrics.md).

## What lives where

- **toolbench owns** benchmarks, prompts, rubrics, the run loop, grading, and metrics.
- **toolbase owns** toolkits, meaning installing, isolating, curating, and serving tools.
- **The harness owns** the model plus tool-call loop and context management.

A benchmark *references* a toolkit, but a toolkit never references a benchmark.
