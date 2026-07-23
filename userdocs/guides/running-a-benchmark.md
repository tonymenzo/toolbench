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
| `--judge`                         | Judge selection: `rule` (default) or `rule+llm`. See [Choosing a judge](#choosing-a-judge). |
| `--judge-harness`                 | Harness the judge is called through, e.g. `orchestral/anthropic` or `claude-code/default`. May differ from the harness under test. |
| `--judge-model`                   | Model the judge uses. Defaults to the judge harness's own provider model. |
| `--ux-feedback` / `--no-ux-feedback` | Add an extra, unscored post-trial turn where the agent critiques the served tools → `ux_feedback.md`. A tool-development aid. Default: harness's `loop.ux_feedback`. |
| `--keep-sandbox`                  | Retain the full trial working tree for by-hand auditing instead of tearing it down. |
| `--audit-html` / `--no-audit-html`| Also write an HTML twin of each trial's audit log. Default: harness's `loop.audit_html`. |

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
| `--max-transient-retries`  | `loop.max_transient_retries`, resume on a transient transport/5xx fault before recording `TRANSIENT_API_ERROR`. Default 4. A sibling of the rate-limit knob. |

Rate-limit retries exist so provider throttling — likely under `--parallel` — is
recorded as the operational event it is (`rate_limit_retries` on the trial row, or
`RATE_LIMITED` when exhausted) instead of contaminating the results as a model failure.

`--continue-nudges` only ever fires when a *required deliverable is still absent*. It
never consults a correctness check, so a finished-but-wrong trial is left alone and the
grading oracle never leaks.

## Choosing a judge

Every trial is graded by a **judge**. Two knobs pick which:

- `--judge rule` (the default) is the deterministic rule judge: the rubric checks and
  nothing else. This is what produces the authoritative score.
- `--judge rule+llm` adds an LLM second opinion. It runs **serially, after** the
  authoritative rule grade, and its verdict is attached in `alt_grades` — it **never**
  changes the score. Use it to sanity-check the rule rubric against a model's read of the
  same trial.
- `--judge llm` **alone is rejected on a scored run**: the headline number must stay
  deterministic. To grade purely by LLM (an ablation), apply it after the fact with
  `regrade --judge llm`.

`--judge-harness` lets you judge through a *different* runtime than the one under test — so
a subscription model can judge an API-model run and vice versa (e.g.
`--judge-harness claude-code/default`). `--judge-model` overrides the judge's model.

```bash
toolbench run --benchmark examples/geometry --model claude-haiku-4-5 \
    --loadouts core_only,full_local --n 3 \
    --judge rule+llm --judge-harness orchestral/anthropic --judge-model claude-opus-4-8
```

Because the LLM judge only ever reads preserved artifacts, you don't have to decide it at
run time — `regrade` (below) can apply one retroactively. See
[Metrics](../reference/metrics.md) for how the rule grade becomes the score.

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
  without re-running any agent. Use it after a rubric change (tightened tolerance, added
  check) — and to **apply an LLM judge retroactively** (`regrade --judge rule+llm` or
  `--judge llm`, with `--judge-harness` / `--judge-model`), so judging never has to be
  decided at run time. See [Choosing a judge](#choosing-a-judge).

See [Commands](../reference/commands.md) for the full reference.
