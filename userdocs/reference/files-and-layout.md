# Files & layout

Where everything lives, across the package, a benchmark, and a run directory.

The package ships the framework only. Benchmarks are repo content the CLI
resolves by path (`--benchmark <dir>`), not bundled into the package.

```
toolbench/
├── toolbench/             # the package (imported as `toolbench`)
│   ├── cli.py              # the `toolbench` / `tbe` entry point (Click)
│   ├── core/              # framework: runner, judge, rubric, metrics, resolvers, …
│   └── reporting/         # summary text + plots (k-sweep, parallel-coords, per-stage)
├── examples/             # example benchmarks (resolved by path, not packaged)
│   └── geometry/          # the reference benchmark (with its own tools/)
└── runs/                  # run output (created on first run, gitignore it)
```

## A benchmark directory

```
examples/<name>/
├── benchmark.yaml          # task + rubric + ground-truth pointer
├── ground_truth/           # reference answers
├── harnesses/<runtime>/<provider>.yaml
├── loadouts/<name>.yaml
├── variants/<name>/
│   ├── variant.yaml
│   ├── prompts/{user,system}.md
│   └── sandbox/template/   # optional: seeded into each trial
├── tools/                  # optional: benchmark-local @define_tool modules
├── skills/                 # optional: recipe docs a loadout can expose
└── checks/checks.py        # optional: benchmark-local rubric checks
```

A sibling benchmark that re-grades the same task can be a one-file overlay —
`benchmark.yaml` with `extends: ../<parent>` plus whatever it overrides —
inheriting everything else from the parent directory
([Extends](schemas.md#extends)).

See [Schemas](schemas.md) for each file's fields.

## A run directory

Each `toolbench run` writes one timestamped directory under `runs/` in your current working
directory, so output lands next to the benchmark you ran rather than inside the installed
package:

```
runs/<timestamp>_<benchmark>_<model>_<label>/
├── manifest.json           # full config, git SHA, pinned versions, the reproducibility record
├── console.log             # the whole run's output, ANSI-stripped, live-tailable
├── trials.jsonl            # one compact line per trial
├── summary.json            # aggregated per-cell metrics
├── summary.txt             # the human-readable summary table
├── k_sweep.png             # pass@k / pass^k vs k
├── parallel_coords.png     # the three-vector per cell
├── per_stage_k.png         # per-stage pass rate (the funnel)
└── trials/<trial_id>/
    ├── trial.json          # full per-trial record (grade, tokens, cost, config)
    ├── transcript.jsonl.gz # every tool call (gzipped)
    ├── console.log         # this trial's styled log
    ├── audit.txt           # always written: full trajectory + every tool input
    ├── audit.html          # only with --audit-html / loop.audit_html: styled twin of audit.txt
    ├── ux_feedback.md      # only with --ux-feedback: the trial's unscored UX critique
    └── artifacts/          # minimal evidence kept for `regrade`
        └── scripts/        # agent-authored code lifted from the transcript
```

A `trial_id` encodes its cell, e.g. `full_local__n000__seed1001` (and includes the
harness/variant/model when those axes are swept).

### `summary.json` fields

Beyond the per-cell metrics, the top level of `summary.json` records:

| Field           | Shape                                                     | Notes                                          |
|-----------------|----------------------------------------------------------|------------------------------------------------|
| `integrity`     | `{scanned, flagged: {tid: {n_hits, sample}}}`            | Integrity-scan tally and any quarantined trials. |
| `provenance`    | `{git_sha, versions, harnesses}`                          | Reproducibility record for the whole run.       |
| `pass_criterion`| `"all_stages"` or `"reach>=<x>"`                          | How a "pass" was defined for pass@k / pass^k.   |
| `reach_weights` | `{stage_order, w, pass_threshold}`                        | The rubric's stage order, weights, and threshold. |
| `estimated_api_equivalent_cost_usd` | float                                | Only for subscription runs: token-derived API-equivalent estimate (not real spend). |
| `estimated_cost_basis` | `{basis, model, rates_usd_per_million_tokens, source, …}` | The rates/source behind that estimate.  |

Each per-cell block additionally carries:

| Field           | Notes                                                                         |
|-----------------|-------------------------------------------------------------------------------|
| `mean_tokens`   | `{initial_input, input, output, cache_read, cache_creation}`.                 |
| `mean_estimated_api_equivalent_cost_usd` | Mean API-equivalent estimate per trial (subscription runs only).    |
| `trial_scores`  | The cell's per-trial reaches, sorted.                                         |
| `stage_display` | Per-stage continuous-credit / distance breakdown.                             |
| `tool_usage`    | Per-tool call/error counts + MCP-vs-script adoption.                          |
| `ux_ratings`    | Aggregated UX-feedback ratings (present only when UX feedback ran).           |
| `pass_threshold`| The threshold in force for the cell.                                          |
| `retries`       | Retry tallies for the cell.                                                   |

### `trials.jsonl` fields

Each per-trial row adds, alongside its grade:

| Field                  | Notes                                                                  |
|------------------------|------------------------------------------------------------------------|
| `stage_credits`        | Per-stage credit ∈ [0,1] (what the stage contributed to reach).        |
| `stage_continuous`     | Per-stage `continuous` flag (whether that stage scored continuously).  |
| `stage_distance`       | Per-stage distance-to-reference, when a check recorded one.            |
| `initial_input_tokens` | Tokens in the first request.                                          |
| `cache_creation_tokens`| Cache-creation token count.                                           |

On a **quarantined** trial the row also carries `integrity_leak`, `integrity_evidence`, and
`score_pre_integrity` — the original score is kept there while `score` is set to `0` and
`failure_mode` becomes `INTEGRITY_LEAK`.

## What to commit vs. ignore

- **Commit** `toolbench/`, `examples/<name>/` (text and small data), `pyproject.toml`, and
  docs.
- **Ignore** `runs/` (regenerated), `site/` (built docs), and caches.

The minimal bundle to reproduce a result is the benchmark directory plus the manifest's pin
set, model id, and seed. Everything else is recorded in `manifest.json`.
