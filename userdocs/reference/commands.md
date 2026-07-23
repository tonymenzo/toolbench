# Commands

The CLI is `toolbench` (alias `tbe`). Every command prints its own `--help`, and this page
is the scannable reference. `toolbench --help` groups the commands, and `toolbench <cmd>
--help` lists every flag.

## Running benchmarks

| Command            | Purpose                                                              |
|--------------------|---------------------------------------------------------------------|
| `toolbench run`    | Run a benchmark across the harness × loadout × variant × model grid. |
| `toolbench resume` | Resume an interrupted run, only the seeds that didn't finish.        |
| `toolbench regrade`| Re-judge a finished run's preserved artifacts after a rubric change, or apply an LLM judge retroactively. |

## `toolbench run`

| Flag                          | Default            | Meaning                                                        |
|-------------------------------|--------------------|----------------------------------------------------------------|
| `--benchmark` / `--task`      | *(required)*       | Path to a benchmark dir (with `benchmark.yaml`), e.g. `examples/geometry`. |
| `--models` / `--model`        | *(required)*       | Comma-separated model id(s). `stub` is for `--dry-run`.        |
| `--max-cost-usd`              | *(required)*       | Hard budget cap. The run aborts when spend would exceed it.    |
| `--harness` / `--harnesses`   | benchmark default  | Harness id(s), e.g. `orchestral/anthropic`.                   |
| `--loadouts` / `--conditions` | benchmark default  | Loadout name(s).                                               |
| `--variant` / `--variants`    | benchmark default  | Variant name(s).                                               |
| `--n`                         | `3`                | Trials (seeds) per cell.                                       |
| `--seed-base`                 | `1001`             | Base seed. Trial seeds are `seed_base + i`.                    |
| `--max-iterations`            | from harness       | Override `loop.max_iterations`.                                |
| `--max-format-retries`        | from harness       | Override `loop.max_format_retries`.                            |
| `--continue-nudges`           | from harness       | Override `loop.continue_nudges` (presence-gated resumes).      |
| `--max-rate-limit-retries`    | from harness       | Override `loop.max_rate_limit_retries` (backoff resumes on provider 429/529). |
| `--max-transient-retries`     | from harness (`4`) | Override `loop.max_transient_retries`. Resume on a transient transport/5xx fault before recording `TRANSIENT_API_ERROR`. Sibling of `--max-rate-limit-retries`. |
| `--judge`                     | from harness `judge:` | `rule` (default) or `rule+llm`; overrides the harness `judge:` block. `llm` alone is rejected on a scored run (see `regrade`). |
| `--judge-harness`             | judge's harness    | The harness the *judge* is called through, e.g. `orchestral/anthropic`, `claude-code/default`. May differ from the harness under test. |
| `--judge-model`               | judge harness default | Model the judge uses. Defaults to the judge harness's `provider.model`. |
| `--ux-feedback` / `--no-ux-feedback` | `loop.ux_feedback` | One extra *unscored* turn per trial critiquing the tools → `ux_feedback.md` + `trial.json`. Never affects the grade. |
| `--keep-sandbox`              | off                | Don't delete each trial's sandbox after grading (sets `TOOLBENCH_KEEP_SANDBOX=1`); for by-hand auditing. |
| `--audit-html` / `--no-audit-html` | `loop.audit_html` | Also emit a styled HTML twin of each trial's audit log. The plain `audit.txt` is always written. |
| `--parallel`                  | `1`                | Trials in flight at once (each trial is self-contained).       |
| `--dry-run`                   | off                | Skip the LLM call, validate wiring, print the resolution preview. |
| `-v` / `--verbose`            | off                | A styled line per tool call. Honors `NO_COLOR`.               |
| `--run-label`                 | `run` / `dryrun`   | Suffix for the run id.                                         |

```bash
toolbench run --benchmark examples/geometry --models claude-haiku-4-5 \
    --loadouts core_only,full_local --n 5 --max-cost-usd 1.00
```

## `toolbench resume`

| Flag             | Meaning                                                                |
|------------------|-----------------------------------------------------------------------|
| `--run-id`       | *(required)* the existing run directory under `runs/`.                 |
| `--max-cost-usd` | Override the manifest's budget cap (e.g. widen it). Default: original. |
| `--parallel`     | Trials in flight at once. Default: the original run's setting.         |
| `-v`/`--verbose` | Styled per-tool-call output.                                           |

Reads the run's `manifest.json` + `trials.jsonl`, runs only the seeds not yet complete, and
re-aggregates `summary.json` / `summary.txt`.

## `toolbench regrade`

| Flag              | Meaning                                              |
|-------------------|-----------------------------------------------------|
| `--run-id`        | *(required)* the run directory under `runs/`.        |
| `--judge`         | `rule`, `rule+llm`, or `llm`. Unlike `run`, `--judge llm` is allowed here. |
| `--judge-harness` | The harness the *judge* is called through (may differ from the harness under test). |
| `--judge-model`   | Model the judge uses; defaults to the judge harness's `provider.model`. |

Re-runs the rule judge against each trial's preserved `artifacts/` with the *current*
rubric, picking up new/changed checks without re-executing any agent. Hard process failures
(crashes) keep their failure mode, while rubric-derived modes are recomputed. It also **applies
an LLM judge retroactively** (`--judge rule+llm` or `--judge llm`), so judging never has to be
decided at run time — you can run once and later decide how to grade.

## Conventions

- **Comma-separated lists** sweep an axis. `--loadouts a,b` runs both and reports a cell per
  condition.
- **`--dry-run` + `--model stub`** exercises the whole pipeline for \$0, the right
  pre-flight before any real run.
- **Exit codes:** `0` success, `2` a usage error (unknown benchmark/harness/loadout/etc.).
