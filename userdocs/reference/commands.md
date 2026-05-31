# Commands

The CLI is `toolbench` (alias `tbe`). Every command prints its own `--help`; this page is
the scannable reference. `toolbench --help` groups the commands; `toolbench <cmd> --help`
lists every flag.

## Running benchmarks

| Command            | Purpose                                                              |
|--------------------|---------------------------------------------------------------------|
| `toolbench run`    | Run a benchmark across the harness × loadout × variant × model grid. |
| `toolbench resume` | Resume an interrupted run — only the seeds that didn't finish.       |
| `toolbench regrade`| Re-judge a finished run's preserved artifacts after a rubric change. |

## `toolbench run`

| Flag                          | Default            | Meaning                                                        |
|-------------------------------|--------------------|----------------------------------------------------------------|
| `--benchmark` / `--task`      | *(required)*       | Benchmark name (dir under `toolbench/benchmarks/`).            |
| `--models` / `--model`        | *(required)*       | Comma-separated model id(s). `stub` is for `--dry-run`.        |
| `--max-cost-usd`              | *(required)*       | Hard budget cap; the run aborts when spend would exceed it.    |
| `--harness` / `--harnesses`   | benchmark default  | Harness id(s), e.g. `orchestral/anthropic`.                   |
| `--loadouts` / `--conditions` | benchmark default  | Loadout name(s).                                               |
| `--variant` / `--variants`    | benchmark default  | Variant name(s).                                               |
| `--n`                         | `3`                | Trials (seeds) per cell.                                       |
| `--seed-base`                 | `1001`             | Base seed; trial seeds are `seed_base + i`.                    |
| `--max-iterations`            | from harness       | Override `loop.max_iterations`.                                |
| `--max-format-retries`        | from harness       | Override `loop.max_format_retries`.                            |
| `--continue-nudges`           | from harness       | Override `loop.continue_nudges` (presence-gated resumes).      |
| `--dry-run`                   | off                | Skip the LLM call; validate wiring + print the resolution preview. |
| `-v` / `--verbose`            | off                | A styled line per tool call; honors `NO_COLOR`.               |
| `--run-label`                 | `run` / `dryrun`   | Suffix for the run id.                                         |

```bash
toolbench run --benchmark geometry --models claude-haiku-4-5 \
    --loadouts core_only,full_local --n 5 --max-cost-usd 1.00
```

## `toolbench resume`

| Flag             | Meaning                                                                |
|------------------|-----------------------------------------------------------------------|
| `--run-id`       | *(required)* the existing run directory under `runs/`.                 |
| `--max-cost-usd` | Override the manifest's budget cap (e.g. widen it). Default: original. |
| `-v`/`--verbose` | Styled per-tool-call output.                                           |

Reads the run's `manifest.json` + `trials.jsonl`, runs only the seeds not yet complete, and
re-aggregates `summary.json` / `summary.txt`.

## `toolbench regrade`

| Flag       | Meaning                                              |
|------------|-----------------------------------------------------|
| `--run-id` | *(required)* the run directory under `runs/`.        |

Re-runs the rule judge against each trial's preserved `artifacts/` with the *current*
rubric — picks up new/changed checks without re-executing any agent. Hard process failures
(crashes) keep their failure mode; rubric-derived modes are recomputed.

## Conventions

- **Comma-separated lists** sweep an axis: `--loadouts a,b` runs both and reports a cell per
  condition.
- **`--dry-run` + `--model stub`** exercises the whole pipeline for \$0 — the right
  pre-flight before any real run.
- **Exit codes:** `0` success, `2` a usage error (unknown benchmark/harness/loadout/etc.).
