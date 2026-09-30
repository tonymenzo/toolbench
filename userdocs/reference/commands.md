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

## Monitoring

| Command               | Purpose                                                            |
|-----------------------|--------------------------------------------------------------------|
| `toolbench dashboard` | Watch a run live in the browser.                                   |

## Sharing results

| Command             | Purpose                                                             |
|---------------------|---------------------------------------------------------------------|
| `toolbench export`  | Turn a completed run into a portable, schema-versioned dataset you can publish. |

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

The cap governs the run's **total** spend: what the completed trials already
cost is pre-charged against it, so resuming with an unchanged cap only spends
what's left (a run that aborted on budget needs a wider `--max-cost-usd`).
Trials that previously failed before they could start (failure mode
`resolution_error`, e.g. a transient toolbase/MCP connection error) are
**re-run** on resume rather than kept as frozen zero-score rows.

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

## `toolbench export`

| Flag                    | Default                        | Meaning                                        |
|-------------------------|--------------------------------|------------------------------------------------|
| `--run-id`              | *(required)*                   | Existing run directory under `runs/` (nested ids like `campaign/<id>/<run>` resolve too). |
| `--out`                 | `runs/exports/<run-id-leaf>/`  | Destination directory.                          |
| `--include-transcripts` | off                            | Bundle the raw transcripts. They dominate the size and are copied verbatim (binary, so **not** path-scrubbed). |
| `--no-scrub`            | off                            | Skip rewriting machine-specific absolute paths to `${HOME}` / `${RUN}` placeholders. |
| `--archive`             | off                            | Also write `<out>.tar.gz` next to the export directory. |

A run directory is a working artifact — gigabytes of transcripts and intermediate data
carrying absolute paths from the machine that produced it. `export` writes two layers you
can actually publish:

- **`trials.jsonl`** — one flat, denormalized, schema-versioned row per trial (cell
  coordinates, score, pass/fail against the rubric's own threshold, per-stage
  credits/weights/metrics, telemetry, provenance). Kilobytes, and denormalized on purpose so
  a consumer needs no join logic and no knowledge of toolbench's internal layout.
- **`bundle/`** — the graded evidence behind those rows: per-trial answer files, audit logs,
  run summaries, manifest. Megabytes.

`run.json` carries the run-level metadata. `schema_version` (currently `1.0`) is the
compatibility contract: additive changes bump the minor, anything that moves or retypes an
existing field bumps the major.

## `toolbench dashboard`

```bash
toolbench dashboard                  # the most recently started run under ./runs
toolbench dashboard <run-id | dir>   # a specific run, or a directory of runs
```

| Flag     | Default     | Meaning                                                        |
|----------|-------------|----------------------------------------------------------------|
| `--host` | `127.0.0.1` | Interface to bind.                                             |
| `--port` | `8765`      | Port to serve on (`0` picks a free one).                       |
| `--poll` | `3`         | Seconds between browser refreshes.                             |

Serves a read-only live view of one run. At the top are headline figures: trials done,
active slots, passes, mean reach, spend against the budget cap, and an ETA while the run is
live. Below them is the trial matrix: one row per cell, one square per seed index. A square
is queued, running, shaded by reach once finished, errored, integrity-quarantined, or
interrupted. Next to the matrix, a docked inspector shows the newest active trial and its
live `console.log`. Clicking any trial pins the inspector to it, with its result, stage
checklist, and tool calls. Once the run finalizes, a summary view shows `summary.txt`.

Given a directory that holds several runs (a campaign, nested at any depth), the page adds
a run picker; every `manifest.json` beneath the directory counts as a run.

Whether a run is still alive comes from its `status.json` heartbeat. A run that stops
heartbeating is shown as **stale** (its process has most likely exited) and its in-flight
trials as interrupted; `toolbench resume` finishes them. Runs from before `status.json`
existed show as finished once they have a summary, and otherwise as "no heartbeat".

The server only reads the run and never writes to it. To watch a run on a remote machine,
keep the default host and forward the port: `ssh -L 8765:localhost:8765 <host>`.

## Conventions

- **Comma-separated lists** sweep an axis. `--loadouts a,b` runs both and reports a cell per
  condition.
- **`--dry-run` + `--model stub`** exercises the whole pipeline for \$0, the right
  pre-flight before any real run.
- **Exit codes:** `0` success, `2` a usage error (unknown benchmark/harness/loadout/etc.).
