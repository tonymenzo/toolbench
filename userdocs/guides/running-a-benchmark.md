# Running a benchmark

`toolbench run` is the workhorse. It expands the
`harness × loadout × variant × model` grid, runs `--n` seeded trials per cell, grades each
against the rubric, and writes an aggregated run directory.

## The shape of a run

```bash
toolbench run \
    --benchmark geometry \
    --models claude-haiku-4-5 \
    --loadouts core_only,full_local \
    --n 3 \
    --max-cost-usd 0.50
```

Anything you don't pass falls back to the benchmark's declared defaults
(`default_harness`, `default_loadout`, `default_variant` in `benchmark.yaml`), so the
shortest real run is just `--benchmark`, `--models`, and `--max-cost-usd`.

## Sweeping axes

Pass a comma-separated list to any axis to sweep it; toolbench runs the full cross-product
and reports one cell per `(model × condition)`:

```bash
# Two loadouts × two models × 5 seeds = 20 trials:
toolbench run --benchmark geometry \
    --models claude-haiku-4-5,claude-sonnet-4-6 \
    --loadouts core_only,full_local \
    --n 5 --max-cost-usd 5.00
```

The swept axis becomes the *condition* label in the results. Sweep one axis at a time when
you want a clean delta (see [Reading results & scores](reading-results.md)).

| Flag                              | Meaning                                                         |
|-----------------------------------|----------------------------------------------------------------|
| `--benchmark` / `--task`          | Benchmark name (a dir under `toolbench/benchmarks/`). Required. |
| `--models` / `--model`            | Comma-separated model id(s). Required. `stub` is for `--dry-run`. |
| `--harness` / `--harnesses`       | Harness id(s), e.g. `orchestral/anthropic`. Default: benchmark's. |
| `--loadouts` / `--conditions`     | Loadout name(s). Default: benchmark's `default_loadout`.        |
| `--variant` / `--variants`        | Variant name(s). Default: benchmark's `default_variant`.        |
| `--n`                             | Trials (seeds) per cell. Default 3.                            |
| `--seed-base`                     | Base seed; trial seeds are `seed_base + i`. Default 1001.      |
| `--max-cost-usd`                  | Hard budget cap. The run aborts when spend would exceed it. Required. |

## Dry runs: validate for \$0

Before spending tokens, validate the entire pipeline — tool resolution, grading, summary,
plots — with no LLM calls:

```bash
toolbench run --benchmark geometry --model stub \
    --loadouts full_local --n 1 --max-cost-usd 0 --dry-run
```

`--dry-run` prints a **resolution preview** (the exact tool list each
harness × loadout produces, including any toolbase errors) and then skips the agent call.
It's the fastest way to catch a broken loadout or a misspelled tool before a real run.

## Loop overrides

The retry/loop knobs default to each harness's `loop:` block; pass a flag to override for
this run:

| Flag                    | Overrides                                                              |
|-------------------------|-----------------------------------------------------------------------|
| `--max-iterations`      | `loop.max_iterations` — the agent's tool-call round-trip cap.          |
| `--max-format-retries`  | `loop.max_format_retries` — resumes on a malformed-tool-call crash.   |
| `--continue-nudges`     | `loop.continue_nudges` — presence-gated "you're not done" resumes.    |

`--continue-nudges` only ever fires when a *required deliverable is still absent* — it
never consults a correctness check, so a finished-but-wrong trial is left alone and the
grading oracle never leaks.

## Watching it run

Add `-v` / `--verbose` for a styled line per tool call (`▸` start, `✓`/`✗` end) and a
per-trial header/footer with reach, failure mode, tokens, and cost. Everything printed is
also teed to `runs/<id>/console.log`, so a backgrounded run stays live-tailable. Use
`--run-label <name>` to suffix the run id.

## Resuming and re-grading

- **`toolbench resume --run-id <id>`** — pick up an interrupted run: re-reads the manifest
  and `trials.jsonl`, runs only the seeds that didn't finish, and re-aggregates. Widen the
  budget with `--max-cost-usd` if the original cap is exhausted.
- **`toolbench regrade --run-id <id>`** — re-judge a finished run's preserved artifacts
  after a rubric change, without re-running any agent.

See [Commands](../reference/commands.md) for the full reference.
