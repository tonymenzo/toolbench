# Rubrics & checks

The rubric is where a benchmark earns its keep. A good rubric is a **funnel** of ordered
stages, running from "did the agent produce anything?" to "is it exactly right?", so the
score says *how far* an agent got and the per-stage breakdown says *where it fell off*.

## Stagewise scoring

```yaml
rubric:
  type: stagewise
  stages:
    - id: answer_written
      description: output/answer.json exists with the required keys
      weight: 0.2
      checks: [ ... ]
    - id: midpoint_correct
      weight: 0.3
      checks: [ ... ]
    - id: distance_correct
      weight: 0.5
      checks: [ ... ]
```

Each stage has an `id`, a `weight`, and a list of `checks` (a stage passes iff **all** its
checks pass). The trial score is the stage's weighted **reach** $R_j$. In the default case —
every stage binary and **gating** (an absorbing pass/fail) — reach reduces exactly to the
**prefix product**: a stage banks its weight only if it *and every stage before it* passed.
The two per-stage knobs below (`continuous`, `gating`) relax that; the full definition, and
what a "pass" means for pass@k, live in [Metrics](../reference/metrics.md#per-trial-reach)
— link there rather than re-deriving the math here.

A stage carries both a binary `passed` (all its checks passed — this is what pass@k counts)
and a scored `credit` $\in [0,1]$ (what it contributes to reach). For a plain binary stage
the two coincide; they diverge **only** for a [`continuous`](#per-stage-options) stage, whose
credit can be a partial value even though `passed` stays binary.

Order matters, so put the cheap "did it produce a deliverable" gate first and the demanding
correctness checks last. Weights are yours to assign and need not sum to 1 (the score is
normalized), but a sum of 1 makes reach read as a clean fraction.

!!! tip "Design the funnel deliberately"
    `answer_written (0.2) → midpoint (0.3) → distance (0.5)` puts 0.2 on *trying*
    correctly-shaped output, then most of the credit on the genuinely hard step. If two
    benchmarks weight the same task differently, their reaches aren't comparable, so keep
    weights stable across a study.

## Per-stage options

Two optional per-stage keys, both **off by default** (leaving the pure prefix-product
funnel above), for rubrics that aren't a strict pipeline. See
[Metrics](../reference/metrics.md#continuous-and-independent-stages) for how each enters the
reach sum.

- **`continuous: true`** — the stage earns *partial* credit $c \in [0,1]$ from a check's
  `closeness` metric instead of an all-or-nothing pass, and (implicitly) stops gating the
  stages after it. Its binary `passed` is unchanged, so pass@k still counts it as pass/fail.

    !!! warning "Forward-looking"
        The built-in checks are **binary** — none of them emit a `closeness` metric yet — so
        today `continuous: true` behaves as a *non-gating binary* stage. It becomes genuine
        partial credit only when a [custom check](#custom-checks) returns a `closeness` in its
        metrics dict.

- **`gating: false`** — the stage contributes its credit but does **not** absorb the stages
  after it. Use it when the stages are *independent quantities* rather than a pipeline, so
  missing one shouldn't zero the rest. (`continuous: true` implies `gating: false`; set
  `gating: false` on a plain binary stage to get independent all-or-nothing stages.)

```yaml
# Three independent quantities — each checked on its own, none gating the others.
rubric:
  type: stagewise
  pass_threshold: 1.0            # a "pass" now means all three, not a pipeline
  stages:
    - id: mean_correct
      weight: 0.333
      gating: false
      checks: [ { close_to: { file: output/stats.json, field: mean,
                              reference: ./ground_truth/stats.json, tolerance_frac: 0.01 } } ]
    - id: variance_correct
      weight: 0.333
      gating: false
      checks: [ { close_to: { file: output/stats.json, field: variance,
                              reference: ./ground_truth/stats.json, tolerance_frac: 0.01 } } ]
    - id: skew_correct
      weight: 0.334
      gating: false
      checks: [ { close_to: { file: output/stats.json, field: skew,
                              reference: ./ground_truth/stats.json, tolerance_frac: 0.01 } } ]
```

A trial that nails two of the three scores $R_j \approx 0.67$ instead of collapsing to the
first failure — the point of `gating: false`.

### `pass_threshold`

By default a trial **passes** (for pass@k / pass^k) iff *every* stage passed. Set a rubric-level

```yaml
rubric:
  type: stagewise
  pass_threshold: 0.8           # a trial passes iff reach ≥ 0.8
  stages: [ ... ]
```

to redefine a pass as **reach ≥ threshold**. Once a rubric has continuous or independent
stages an exact all-stages pass is rarely what you mean, so this is usually how you'd define
the pass rule there. Like every other rubric knob it's a grading-time decision:
`regrade` picks up a changed threshold with no agent and no model spend.

## Checks

A check is a single mapping keyed by its name, with that check's parameters as the value,
the same key-as-discriminator shape loadout sources use:

```yaml
checks:
  - json_with_keys:
      file: output/answer.json
      required_keys: [distance, midpoint]
  - close_to:
      file: output/answer.json
      field: distance
      reference: ./ground_truth/answer.json   # resolved at the benchmark dir
      tolerance_frac: 0.01
```

The two you'll use most on a scalar-answer benchmark like `geometry`:

| Check            | Role        | Passes when…                                                       |
|------------------|-------------|-------------------------------------------------------------------|
| `json_with_keys` | presence    | the JSON file exists and contains every `required_keys` entry.     |
| `close_to`       | correctness | a scalar/vector field is within `tolerance_frac` of the reference. |

### The built-in registry

The full set of built-ins. Most are **presence** checks that verify a deliverable's
*content* (not its filename) so an agent's naming choice can't silently fail a glob; the
physics-specific ones are kept brief — this is a general framework, not a physics one.

| Check             | Role        | Passes when…                                                                               |
|-------------------|-------------|--------------------------------------------------------------------------------------------|
| `json_with_keys`  | presence    | a JSON file (`file:`) exists and holds every `required_keys` entry.                        |
| `close_to`        | correctness | a scalar/vector `field:` is within `tolerance_frac` of the `reference:` value.             |
| `jsonl_with_keys` | presence    | some `.jsonl` has ≥ `min_records` lines whose first record ⊇ `required_keys`.               |
| `record_stream`   | presence    | **(format-agnostic)** a per-event dataset of ≥ `min_records` records exists in *any* form. |
| `npy_array`       | presence    | some `.npy` holds an array matching `ndim` / `min_len` / `dtype_kind`.                      |
| `numeric_array`   | presence    | **(format-agnostic)** any file yields a 1-D numeric array with ≥ `min_len` finite values.  |
| `peak_position`   | correctness | mass arrays peak near each of `expected_masses` within tolerance.                          |
| `ufo_dir`         | presence    | a directory holds the canonical UFO module set. *(physics)*                                |
| `lhe_with_events` | presence    | some `.lhe`/`.lhe.gz` has ≥ `min_events` `<event>` blocks. *(physics)*                      |
| `pdf_nonempty`    | presence    | some `.pdf` ≥ `min_bytes` with a real `%PDF` header (alias of `plot_nonempty` for PDFs).    |
| `plot_nonempty`   | presence    | some `.pdf`/`.png` ≥ `min_bytes` with the right magic header (zero-byte stubs rejected).   |

The **format-agnostic** pair is the counterpart of the strict `.jsonl` / `.npy` checks, for
by-hand agents that save the same physics in whatever format they reach for:

- **`numeric_array`** — a 1-D numeric array across `.npy` / `.csv` / `.tsv` / `.dat` /
  `.txt` / `.json` (every column or field of a tabular/multi-field file is a candidate).
  Params: `min_len`, `under_subpath`. The format-free counterpart of `npy_array`.
- **`record_stream`** — a per-event dataset of ≥ `min_records` records: a `.jsonl` of *any*
  schema, a HepMC event stream, or a tabular/array dump with that many rows. Params:
  `min_records` (default 100), `under_subpath`. The format-free counterpart of
  `jsonl_with_keys`.

And **`peak_position`** now scans every supported numeric format (`.npy` / `.csv` / `.tsv` /
`.dat` / `.txt` / `.json`), asking whether each expected mass has a histogram peak. Params:
`expected_masses` (required), `tolerance_frac` (0.10), `min_events_per_peak` (50),
`min_peaks`, `n_bins` (60), `smoothing` (3).

Each check carries a **role**, either `presence` (the deliverable was made) or
`correctness` (it's right). The runner uses presence checks for the optional
*continue-nudge*. If a required deliverable is still absent when the model stops, it can be
nudged to keep going, but a correctness check is **never** consulted for that, so the
grading oracle never leaks and a finished-but-wrong trial is left alone.

`expected_tool_calls:` on a stage is a non-graded diagnostic. It records which tools you'd
expect the agent to call, surfaced in the transcript, but never affects the score.

## Custom checks

When the built-ins aren't enough, add a benchmark-local checks module and point
`benchmark.yaml` at it. The `checks:` key must be a **`.py` file** — the loader requires a
file and errors on a directory, so point it at the file itself, not the containing dir:

```yaml
# benchmark.yaml
checks: ./checks/checks.py    # a .py FILE exposing CHECKS (and optional ROLES)
```

```python
# checks/checks.py
def rows_at_least(sandbox, params):
    """Each check is (sandbox: Path, params: dict) -> (passed: bool, evidence: str)."""
    path = sandbox / params["file"]
    n = sum(1 for _ in path.open()) if path.exists() else 0
    ok = n >= params["min"]
    return ok, f"{n} rows (need {params['min']})"

CHECKS = {"rows_at_least": rows_at_least}
ROLES  = {"rows_at_least": "correctness"}   # or "presence"
```

Local checks merge with the built-ins, and a name collision is an error. Now use it in a
stage exactly like a built-in:

```yaml
checks:
  - rows_at_least: { file: output/events.jsonl, min: 100 }
```

**Roles.** An untagged custom check defaults to role **`correctness`** — the safe default,
since a correctness check is never consulted for the continue-nudge and so can't leak the
grading oracle. Tag it `presence` in `ROLES` only when it verifies that a deliverable was
*made* (not that it's *right*).

**Returning metrics.** A check may return either `(ok, msg)` or `(ok, msg, metrics)`, where
`metrics` is a dict. Two recognized keys:

- `closeness` (a value in $[0,1]$) — unlocks partial credit for a
  [`continuous: true`](#per-stage-options) stage; without it, `continuous` is just a
  non-gating binary stage.
- `distance` / `distance_label` — a diagnostic distance-to-reference, surfaced in the
  summary (never affects the score).

```python
def within_tol(sandbox, params):
    got, ref, tol = ...          # load answer + reference
    d = abs(got - ref) / max(abs(ref), 1e-9)
    ok = d <= tol
    return ok, f"|Δ|/ref = {d:.3f}", {"closeness": max(0.0, 1 - d / tol),
                                       "distance": d, "distance_label": "rel. error"}
```

## Re-grading after a change

Tightened a tolerance or added a stage? `toolbench regrade --run-id <id>` replays the new
rubric against each trial's preserved `artifacts/` and rewrites the summary, with no agent
and no model spend. This is why the runner keeps a minimal evidence set per trial.
