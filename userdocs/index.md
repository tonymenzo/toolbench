# **<span class="tb-tool">tool</span><span class="tb-bench">bench</span>**

**toolbench is the framework and CLI for building benchmarks for AI agents, tools, and
harnesses.** You describe a task and how to grade it, point it at one or more models, and
toolbench runs the trials, scores them against a rubric, and reports how reliably an agent
gets the job done — with confidence intervals and plots.

It is the benchmarking half of a loop whose other half is
[toolbase](https://toolbase-ai.com), the package manager and runtime for agent tools.

!!! abstract "The loop"
    **toolbase** installs, curates, and serves the tools an agent can call.
    **toolbench** measures how well an agent actually uses them — under a given model, a
    given harness, and a given set of tools. Author a tool in toolbase → benchmark it in
    toolbench → see what to fix → repeat. The two are siblings and are useful
    independently: you can benchmark with hand-written Python tools and never touch
    toolbase, or use toolbase without ever benchmarking.

## Install

```bash
pip install toolbench                 # the framework + CLI
pip install 'toolbench[toolbase]'     # + resolve tools from toolbase profiles
```

Python ≥ 3.12. The CLI is available as `toolbench` and the short alias `tbe`.

## Quickstart

toolbench ships one self-contained, dependency-free benchmark — `geometry` (compute the
Euclidean distance and midpoint between two 2-D points). Use it to see the whole pipeline
without spending a cent:

```bash
# Validate the wiring end-to-end — no LLM calls, no cost:
toolbench run --benchmark geometry --model stub \
    --loadouts full_local --n 1 --max-cost-usd 0 --dry-run

# A real run: 3 trials each in two tool conditions, on a cheap model:
toolbench run --benchmark geometry --model claude-haiku-4-5 \
    --loadouts core_only,full_local --n 3 --max-cost-usd 0.50
```

Every run writes a self-contained directory under `runs/<run_id>/`: a `manifest.json`
(exact config + pinned versions), per-trial transcripts and artifacts, an aggregated
`summary.json` / `summary.txt`, and headline plots.

## What a benchmark is made of

A benchmark composes four **independent axes**. Hold three fixed and sweep the fourth to
run a clean ablation — "does this loadout help?", "is this model better?", "does the
agent need the scaffolding?".

| Axis          | What it controls                                                    | Sweep it with        |
|---------------|--------------------------------------------------------------------|----------------------|
| **Benchmark** | The task, the rubric, the ground truth.                            | `--benchmark`        |
| **Model**     | The LLM doing the work.                                            | `--models`           |
| **Harness**   | The agent runtime + provider + core tools + loop policy.          | `--harnesses`        |
| **Loadout**   | The domain tools the agent is equipped with.                      | `--loadouts`         |
| **Variant**   | The prompt + sandbox seed (scaffolding), orthogonal to the tools. | `--variants`         |

Grading is a **rubric**: ordered, weighted stages of checks. A trial's score is the
prefix product — you only get credit for a stage if every stage before it also passed —
so the score reflects *how far through the task the agent actually got*. See
[Reading results & scores](guides/reading-results.md).

## Where to go next

<div class="grid cards" markdown>

- :material-play-circle: **[Run a benchmark](guides/running-a-benchmark.md)** —
  drive the CLI, sweep axes, read a run directory.
- :material-chart-line: **[Reading results & scores](guides/reading-results.md)** —
  reach, pass@k, pass^k, explained intuitively.
- :material-pencil-ruler: **[Author a benchmark](authoring/overview.md)** —
  write your own task, rubric, tools, and variants.
- :material-tools: **[Integrate toolbase](guides/toolbase.md)** —
  benchmark against tools served from a toolbase profile.

</div>

New to the vocabulary? Start with [Concepts](explanation.md). Looking for a specific flag
or schema field? Jump to the [Reference](reference/commands.md).
