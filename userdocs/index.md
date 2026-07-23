# **<span class="tb-tool">tool</span><span class="tb-bench">bench</span>**

**toolbench is the framework and CLI for building benchmarks for AI agents, tools, and
harnesses.** You describe a task and how to grade it, point it at one or more models, and
toolbench runs the trials, scores them against a rubric, and reports how reliably an agent
gets the job done, with confidence intervals and plots.

It is the benchmarking half of a loop whose other half is
[toolbase](https://toolbase-ai.com), the package manager and runtime for agent tools.

!!! abstract "The loop"
    **toolbase** installs, curates, and serves the tools an agent can call.
    **toolbench** measures how well an agent actually uses them under a given model, a
    given harness, and a given set of tools. Author a tool in toolbase → benchmark it in
    toolbench → see what to fix → repeat. The two are siblings and work fine on their own.
    You can benchmark with hand-written Python tools and never touch toolbase, or use
    toolbase without ever benchmarking.

## Install

```bash
pip install toolbench                 # the framework + CLI
pip install 'toolbench[toolbase]'     # + resolve tools from toolbase profiles
pip install 'toolbench[mcp]'          # + serve any MCP server as a loadout source
```

Python ≥ 3.12. The CLI is available as `toolbench` and the short alias `tbe`.

## Quickstart

toolbench ships one self-contained, dependency-free benchmark called `geometry`, which
computes the Euclidean distance and midpoint between two 2-D points. Use it to see the
whole pipeline without spending a cent.

```bash
# Validate the wiring end-to-end (no LLM calls, no cost)
toolbench run --benchmark examples/geometry --model stub \
    --loadouts full_local --n 1 --max-cost-usd 0 --dry-run

# A real run: 3 trials each in two tool conditions, on a cheap model
toolbench run --benchmark examples/geometry --model claude-haiku-4-5 \
    --loadouts core_only,full_local --n 3 --max-cost-usd 0.50
```

Every run writes a self-contained directory under `runs/<run_id>/`. It holds a
`manifest.json` (the exact config plus pinned versions), per-trial transcripts and
artifacts, an aggregated `summary.json` and `summary.txt`, and headline plots.

## What a benchmark is made of

A benchmark composes four **independent axes**. Hold three fixed and sweep the fourth to
run a clean ablation. Does this loadout help? Is this model better? Does the agent need
the scaffolding?

| Axis          | What it controls                                                    | Sweep it with        |
|---------------|--------------------------------------------------------------------|----------------------|
| **Benchmark** | The task, the rubric, the ground truth.                            | `--benchmark`        |
| **Model**     | The LLM doing the work.                                            | `--models`           |
| **Harness**   | The agent runtime + provider + core tools + loop policy.          | `--harnesses`        |
| **Loadout**   | The domain tools the agent is equipped with.                      | `--loadouts`         |
| **Variant**   | The prompt + sandbox seed (scaffolding), orthogonal to the tools. | `--variants`         |

Grading uses a **rubric**, a set of ordered, weighted stages of checks. A trial's score is
its weighted **reach**, *how far through the task the agent actually got*. In the common
default case (every stage all-or-nothing and gating) this is exactly the **prefix product**:
you only get credit for a stage if every stage before it also passed. Stages can also earn
*partial* credit (`continuous`) or score *independently* of the ones before them
(`gating: false`), so the prefix product is the default, not the only, shape. See
[Metrics](reference/metrics.md) and
[Reading results & scores](guides/reading-results.md).

## Where to go next

<div class="grid cards" markdown>

- :material-play-circle: **[Run a benchmark](guides/running-a-benchmark.md)**.
  Drive the CLI, sweep axes, read a run directory.
- :material-chart-line: **[Reading results & scores](guides/reading-results.md)**.
  Reach, pass@k, and pass^k, explained intuitively.
- :material-pencil-ruler: **[Author a benchmark](authoring/overview.md)**.
  Write your own task, rubric, tools, and variants.
- :material-tools: **[Integrate toolbase](guides/toolbase.md)**.
  Benchmark against tools served from a toolbase profile.

</div>

New to the vocabulary? Start with [Concepts](explanation.md). Looking for a specific flag
or schema field? Jump to the [Reference](reference/commands.md).
