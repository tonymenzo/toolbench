# Files & layout

Where everything lives — the package, a benchmark, and a run directory.

## The package

```
toolbench/
├── src/toolbench/
│   ├── cli.py              # the `toolbench` / `tbe` entry point (Click)
│   ├── core/              # framework: runner, judge, rubric, metrics, resolvers, …
│   ├── reporting/         # summary text + plots (k-sweep, parallel-coords, per-stage)
│   ├── bench_tools/       # reusable @define_tool math tools (dunderkit, euclid, …)
│   └── benchmarks/        # the benchmarks themselves (auto-discovered)
│       └── geometry/       # the bundled reference benchmark
└── runs/                  # run output (created on first run; gitignore it)
```

## A benchmark directory

```
benchmarks/<name>/
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

See [Schemas](schemas.md) for each file's fields.

## A run directory

Each `toolbench run` writes one timestamped directory:

```
runs/<timestamp>_<benchmark>_<model>_<label>/
├── manifest.json           # full config, git SHA, pinned versions — the reproducibility record
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
    └── artifacts/          # minimal evidence kept for `regrade`
        └── scripts/        # agent-authored code lifted from the transcript
```

A `trial_id` encodes its cell, e.g. `full_local__n000__seed1001` (and includes the
harness/variant/model when those axes are swept).

## What to commit vs. ignore

- **Commit:** `src/`, `benchmarks/<name>/` (text + small data), `pyproject.toml`, docs.
- **Ignore:** `runs/` (regenerated), `site/` (built docs), caches.

The minimal bundle to reproduce a result is the benchmark directory plus the manifest's pin
set, model id, and seed — everything else is recorded in `manifest.json`.
