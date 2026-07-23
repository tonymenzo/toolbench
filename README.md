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

The example `geometry` benchmark (Euclidean distance + midpoint between two 2-D points) is a
self-contained, dependency-free example that exercises the whole framework.

```bash
# Validate the wiring with no LLM calls or cost:
toolbench run --benchmark examples/geometry --model stub \
    --loadouts full_local --n 1 --max-cost-usd 0 --dry-run

# A real run (a few cheap trials):
toolbench run --benchmark examples/geometry --model claude-haiku-4-5 \
    --loadouts core_only,full_local --n 3 --max-cost-usd 0.50
```

Each run writes a directory under `runs/<run_id>/` with a `manifest.json`, per-trial
transcripts and artifacts, an aggregated `summary.json` / `summary.txt`, and headline plots
(k-sweep, parallel-coordinates, per-stage breakdown).

## Concepts

A benchmark lives in `examples/<name>/` and is composed from four declarative
axes — vary any of them on the command line to run an ablation:

| Concept       | What it is                                                            | Where it lives          |
|---------------|----------------------------------------------------------------------|-------------------------|
| **Benchmark** | The task + grading rubric + ground truth.                            | `benchmark.yaml`        |
| **Harness**   | The agent runtime (`orchestral`, `claude_code`, `codex`), provider, core tools, and loop policy. | `harnesses/*.yaml` |
| **Loadout**   | The domain tools the agent gets (beyond the harness core).          | `loadouts/*.yaml`       |
| **Variant**   | The prompt + sandbox seed (scaffolding axis), orthogonal to tools.  | `variants/<name>/`      |
| **Rubric**    | Ordered, weighted stages of checks; trial score = weighted reach.   | inside `benchmark.yaml` |

A **loadout source** is one of:

- `python:` — import a module exposing `TOOLS` / `make_tools()` (the no-dependency escape hatch),
- `toolbase:` — resolve tools from a [toolbase](https://github.com/alexr314/toolbase) profile,
  in-process (requires `toolbench[toolbase]`); served toolkit versions are recorded in the
  run manifest as reproducibility provenance, or
- `mcp:` — serve any MCP server's tools, stdio or HTTP (requires `toolbench[mcp]`):

  ```yaml
  tools:
    sources:
      - toolbase: { profile: my-profile }
        select: [calculator__add]          # optional: ablate within the profile
      - mcp: { url: "https://host/mcp", headers: { Authorization: "Bearer ${TOK}" } }
  ```

## Metrics

For each (model × condition) cell over `k` trials:

- $\overline{\text{reach}}_k$ — mean rubric-weighted reach: how far through the task the agent gets, on average.
- $\text{pass@}k$ — probability that at least one of $k$ trials passes (best-of-$k$).
- $\text{pass}^{k}$ — probability that all $k$ trials pass (worst-of-$k$).

with bootstrap 95% confidence intervals and a metric-correlation matrix. A trial *passes*
when it clears the rubric's pass criterion — every stage by default, or reach ≥ a
`pass_threshold` once the rubric uses partial-credit stages. See
[Metrics](https://toolbench-ai.com/docs/reference/metrics/) for the exact estimators.

## Commands

| Command            | What it does                                                          |
|--------------------|----------------------------------------------------------------------|
| `toolbench run`    | Run a benchmark across the harness × loadout × variant × model grid. |
| `toolbench resume` | Resume an interrupted run; run only the seeds not yet completed.      |
| `toolbench regrade`| Re-judge a finished run's preserved artifacts after a rubric change.  |

Run `toolbench --help` (or `tbe --help`) for the full reference.

## Also

- **Runtimes** — besides the API-driven `orchestral` runtime, toolbench drives the
  `claude_code` and `codex` CLIs directly (subscription auth, no API key), with per-turn
  token accounting and a filesystem sandbox. Copy-paste starting points live in
  [`harness_templates/`](harness_templates/).
- **Judges** — grade with the deterministic rule judge (default) or add an LLM second
  opinion (`--judge rule+llm`); the rule grade always stays authoritative. Any judge can be
  applied after the fact with `toolbench regrade --judge …`.
- **Safeguards** — trials that read the ground-truth answer key are quarantined
  (`INTEGRITY_LEAK`, scored 0) so a leak can't inflate the headline; every trial also gets a
  readable `audit.txt` of its full trajectory.

## License

MIT
