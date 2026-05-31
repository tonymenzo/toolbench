# Vocabulary

The words toolbench uses, and the lines between them. The five composition axes
(benchmark, model, harness, loadout, variant) are deliberately orthogonal — that
separation is what makes a sweep a clean ablation.

## The layers

The full stack from model to measurement has five layers, each with one job:

| Layer                      | Responsibility                                              | Examples                                  |
|----------------------------|------------------------------------------------------------|-------------------------------------------|
| **Model**                  | LLM inference, called over a provider API.                 | `claude-opus-4-8`, `gpt-5.5`              |
| **Harness**                | The agent runtime: drives the tool-call loop, manages context, formats wire payloads. | `orchestral`, `claude-code`, `codex-cli` |
| **Toolkit**                | A versioned bundle of tools + deps, isolated in its own env. | served by **toolbase**                   |
| **Tool manager**           | Installs, isolates, and serves toolkits at run time.       | **toolbase** / `tb`                       |
| **Benchmarking framework** | Loads a task, runs trials, grades, reports metrics.       | **toolbench**                             |

## Core terms

Benchmark
:   The task **plus** how to grade it: a user prompt, an optional system prompt, a sandbox
    seed, a rubric, and ground truth. Lives in `benchmarks/<name>/benchmark.yaml`. A
    benchmark references tools and harnesses; it is not itself a toolbase artifact.

Harness
:   The agent execution framework — owns the loop policy (retries, error handling), the
    wire format, and the **core tools** (file I/O, shell, run-python, …). Declared per
    benchmark under `harnesses/`. *Not* toolbench itself, and not the model.

Loadout
:   The **domain tools** the agent is equipped with, beyond the harness core. An ordered
    list of *sources*, each either a `python:` module or a `toolbase:` profile. Loadouts
    are the usual ablation axis: `core_only` vs `full_local` measures what the tools buy
    you.

Source
:   One entry in a loadout's tool list. `python:` imports a module exposing `TOOLS` /
    `make_tools()` (the no-toolbase escape hatch); `toolbase:` resolves a curated set from
    a [toolbase](../guides/toolbase.md) profile. A tool name may not appear from two
    sources at once.

Variant
:   The **scaffolding** axis: the prompt + sandbox seed, orthogonal to the tools. Variants
    of one benchmark share the same rubric and ground truth, so a cross-variant score
    delta isolates the cost of *less scaffolding* (e.g. points given directly vs. derived
    from a description).

Rubric
:   The grading spec: ordered, weighted **stages**, each a list of **checks**. `type:
    stagewise` means the trial score is the prefix product — a stage contributes only if
    every earlier stage passed. See [Rubrics & checks](../authoring/rubrics-and-checks.md).

Check
:   One pass/fail predicate evaluated against the sandbox after a trial (e.g.
    `json_with_keys`, `close_to`). Built-in checks plus any benchmark-local ones. Each
    check carries a *role* — `presence` (did the deliverable get made?) vs `correctness`
    (is it right?) — which the runner uses for presence-gated nudges without leaking the
    answer.

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
    template; graded in place, then cleaned to a minimal evidence set for `regrade`.

## Scores

reach ($\bar R_k$)
:   Mean rubric-weighted reach across a cell's trials — *how far through the staged
    pipeline the agent gets*, on average. The headline metric.

pass@k
:   Probability that **at least one** of `k` trials passes every stage (best-of-k) — the
    optimistic "can it ever do it?" view.

pass^k
:   Probability that **all** `k` trials pass (worst-of-k) — the pessimistic "can it do it
    *reliably*?" view.

All three come with bootstrap 95% confidence intervals. They are discussed intuitively in
[Reading results & scores](../guides/reading-results.md) and defined precisely in
[Metrics](metrics.md).

## What lives where

- **toolbench owns:** benchmarks, prompts, rubrics, the run loop, grading, metrics.
- **toolbase owns:** toolkits — installing, isolating, curating, and serving tools.
- **The harness owns:** the model + tool-call loop and context management.

A benchmark *references* a toolkit; a toolkit never references a benchmark.
