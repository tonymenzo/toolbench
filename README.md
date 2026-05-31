# toolbench

**toolbench is a platform and CLI for building benchmarks for agentic tools and harnesses.**
Compose a benchmark (a task + a grading rubric) with a *harness* (the agent runtime), a
*loadout* (the tools the agent is given), and a *variant* (the prompt + sandbox), run N
trials per cell against a model, and get reach / pass@k / pass^k metrics with plots.

It is the benchmarking sibling of [toolbase](https://github.com/alexr314/toolbase) — the
package manager and runtime for agent tools. toolbase serves the tools; toolbench measures
how well an agent uses them. Together they close the loop for agentic tool and harness
development.

## Install

```bash
pip install toolbench                 # core framework
pip install 'toolbench[toolbase]'     # + resolve tools from toolbase profiles
```

Requires Python ≥ 3.12. The CLI is available as both `toolbench` and the short alias `tbe`.

## Quickstart

The bundled `geometry` benchmark (Euclidean distance + midpoint between two 2-D points) is a
self-contained, dependency-free example that exercises the whole framework.

```bash
# Validate the wiring with no LLM calls or cost:
toolbench run --benchmark geometry --model stub \
    --loadouts full_local --n 1 --max-cost-usd 0 --dry-run

# A real run (a few cheap trials):
toolbench run --benchmark geometry --model claude-haiku-4-5 \
    --loadouts core_only,full_local --n 3 --max-cost-usd 0.50
```

Each run writes a directory under `runs/<run_id>/` with a `manifest.json`, per-trial
transcripts and artifacts, an aggregated `summary.json` / `summary.txt`, and headline plots
(k-sweep, parallel-coordinates, per-stage breakdown).

## Concepts

A benchmark lives in `src/toolbench/benchmarks/<name>/` and is composed from four declarative
axes — vary any of them on the command line to run an ablation:

| Concept       | What it is                                                            | Where it lives          |
|---------------|----------------------------------------------------------------------|-------------------------|
| **Benchmark** | The task + grading rubric + ground truth.                            | `benchmark.yaml`        |
| **Harness**   | The agent runtime, provider, core tools, and loop policy.           | `harnesses/*.yaml`      |
| **Loadout**   | The domain tools the agent gets (beyond the harness core).          | `loadouts/*.yaml`       |
| **Variant**   | The prompt + sandbox seed (scaffolding axis), orthogonal to tools.  | `variants/<name>/`      |
| **Rubric**    | Ordered, weighted stages of checks; trial score = prefix product.   | inside `benchmark.yaml` |

A **loadout source** is either:

- `python:` — import a module exposing `TOOLS` / `make_tools()` (the no-toolbase escape hatch), or
- `toolbase:` — resolve tools from a [toolbase](https://github.com/alexr314/toolbase) profile,
  in-process (requires `toolbench[toolbase]`):

  ```yaml
  tools:
    sources:
      - toolbase: { profile: my-profile }
  ```

## Metrics

For each (model × condition) cell over `k` trials:

- **reach̄ₖ** — mean rubric-weighted reach (how far through the staged pipeline the agent gets).
- **pass@k** — probability at least one of k trials passes every stage (best-of-k).
- **pass^k** — probability all k trials pass (worst-of-k).

with bootstrap 95% confidence intervals and a metric-correlation matrix.

## Commands

| Command            | What it does                                                          |
|--------------------|----------------------------------------------------------------------|
| `toolbench run`    | Run a benchmark across the harness × loadout × variant × model grid. |
| `toolbench resume` | Resume an interrupted run; run only the seeds not yet completed.      |
| `toolbench regrade`| Re-judge a finished run's preserved artifacts after a rubric change.  |

Run `toolbench --help` (or `tbe --help`) for the full reference.

## License

MIT
