# Running a benchmark

`toolbench run` is the workhorse. It expands the
`harness × loadout × variant × model` grid, runs `--n` seeded trials per cell, grades each
against the rubric, and writes an aggregated run directory.

## The shape of a run

```bash
toolbench run \
    --benchmark examples/geometry \
    --models claude-haiku-4-5 \
    --loadouts core_only,full_local \
    --n 3 \
    --max-cost-usd 0.50
```

Anything you don't pass falls back to the benchmark's declared defaults
(`default_harness`, `default_loadout`, `default_variant` in `benchmark.yaml`), so the
shortest real run is just `--benchmark`, `--models`, and `--max-cost-usd`.

## Sweeping axes

Pass a comma-separated list to any axis to sweep it. toolbench runs the full cross-product
and reports one cell per `(model × condition)`:

```bash
# Two loadouts × two models × 5 seeds = 20 trials:
toolbench run --benchmark examples/geometry \
    --models claude-haiku-4-5,claude-sonnet-4-6 \
    --loadouts core_only,full_local \
    --n 5 --max-cost-usd 5.00
```

The swept axis becomes the *condition* label in the results. Sweep one axis at a time when
you want a clean delta (see [Reading results & scores](reading-results.md)).

| Flag                              | Meaning                                                         |
|-----------------------------------|----------------------------------------------------------------|
| `--benchmark` / `--task`          | Path to a benchmark dir (with `benchmark.yaml`), e.g. `examples/geometry`. Required. |
| `--models` / `--model`            | Comma-separated model id(s). Required. `stub` is for `--dry-run`. |
| `--harness` / `--harnesses`       | Harness id(s), e.g. `orchestral/anthropic`. Default: benchmark's. |
| `--loadouts` / `--conditions`     | Loadout name(s). Default: benchmark's `default_loadout`.        |
| `--variant` / `--variants`        | Variant name(s). Default: benchmark's `default_variant`.        |
| `--n`                             | Trials (seeds) per cell. Default 3.                            |
| `--seed-base`                     | Base seed. Trial seeds are `seed_base + i`. Default 1001.      |
| `--max-cost-usd`                  | Hard budget cap. The run aborts when spend would exceed it. Required. |
| `--parallel`                      | Trials in flight at once. Default 1 (serial).                  |

Trials are scheduled *seed-major*: every cell runs its first trial before any cell runs
its second. If the budget aborts the run mid-grid, every condition has (nearly) the same
number of completed trials — k degrades uniformly instead of later cells being dropped
wholesale.

## Parallel trials

`--parallel N` keeps N trials in flight at once. Each trial is fully self-contained (its
own sandbox, agent, LLM client, console.log, and toolbase subprocesses), so trials don't
interact; the practical limits are provider rate limits and the budget check, which
happens as each trial *finishes* — so up to N in-flight trials can complete (and bill)
after the cap is crossed. With `--verbose`, per-tool-call lines from concurrent trials
interleave on stdout; the per-trial `console.log`s stay clean.

## Dry runs (validate for \$0)

Before spending tokens, validate the entire pipeline (tool resolution, grading, summary,
plots) with no LLM calls:

```bash
toolbench run --benchmark examples/geometry --model stub \
    --loadouts full_local --n 1 --max-cost-usd 0 --dry-run
```

`--dry-run` prints a **resolution preview** (the exact tool list each
harness × loadout produces, including any toolbase errors) and then skips the agent call.
It is the fastest way to catch a broken loadout or a misspelled tool before a real run.

## Loop overrides

The retry/loop knobs default to each harness's `loop:` block. Pass a flag to override for
this run:

| Flag                       | Overrides                                                              |
|----------------------------|-----------------------------------------------------------------------|
| `--max-iterations`         | `loop.max_iterations`, the agent's tool-call round-trip cap.          |
| `--max-format-retries`     | `loop.max_format_retries`, resumes on a malformed-tool-call crash.   |
| `--continue-nudges`        | `loop.continue_nudges`, presence-gated "you're not done" resumes.    |
| `--max-rate-limit-retries` | `loop.max_rate_limit_retries`, backoff resumes on provider throttling (429/529). Default 3. |

Rate-limit retries exist so provider throttling — likely under `--parallel` — is
recorded as the operational event it is (`rate_limit_retries` on the trial row, or
`RATE_LIMITED` when exhausted) instead of contaminating the results as a model failure.

`--continue-nudges` only ever fires when a *required deliverable is still absent*. It
never consults a correctness check, so a finished-but-wrong trial is left alone and the
grading oracle never leaks.

## Watching it run

Add `-v` / `--verbose` for a styled line per tool call (`▸` start, `✓`/`✗` end) and a
per-trial header/footer with reach, failure mode, tokens, and cost. Everything printed is
also teed to `runs/<id>/console.log`, so a backgrounded run stays live-tailable. Use
`--run-label <name>` to suffix the run id.

## Resuming and re-grading

- **`toolbench resume --run-id <id>`** picks up an interrupted run. It re-reads the
  manifest and `trials.jsonl`, runs only the seeds that didn't finish, and re-aggregates.
  Widen the budget with `--max-cost-usd` if the original cap is exhausted.
- **`toolbench regrade --run-id <id>`** re-judges a finished run's preserved artifacts
  after a rubric change, without re-running any agent.

See [Commands](../reference/commands.md) for the full reference.
