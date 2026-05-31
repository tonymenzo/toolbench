# Rubrics & checks

The rubric is where a benchmark earns its keep. A good rubric is a **funnel**: ordered
stages from "did the agent produce anything?" to "is it exactly right?", so the score says
*how far* an agent got and the per-stage breakdown says *where it fell off*.

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
checks pass). With `type: stagewise` the trial score is the **prefix product**:

$$
R_j = \frac{1}{\sum_i w_i}\sum_i w_i \prod_{\ell \le i} x_\ell
$$

A stage banks its weight only if it and every earlier stage passed. Order matters: put the
cheap "did it produce a deliverable" gate first and the demanding correctness checks last.
Weights are yours to assign; they need not sum to 1 (the score is normalized), but a sum of
1 makes reach read as a clean fraction. See [Metrics](../reference/metrics.md) for the math.

!!! tip "Design the funnel deliberately"
    `answer_written (0.2) → midpoint (0.3) → distance (0.5)` means: 0.2 for *trying*
    correctly-shaped output, then most of the credit for the genuinely hard step. If two
    benchmarks weight the same task differently, their reaches aren't comparable — keep
    weights stable across a study.

## Checks

A check is a single mapping keyed by its name, with that check's parameters as the value —
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

The two built-ins you'll use most:

| Check            | Role        | Passes when…                                                       |
|------------------|-------------|-------------------------------------------------------------------|
| `json_with_keys` | presence    | the JSON file exists and contains every `required_keys` entry.     |
| `close_to`       | correctness | a scalar/vector field is within `tolerance_frac` of the reference. |

Each check carries a **role** — `presence` (the deliverable was made) or `correctness`
(it's right). The runner uses presence checks for the optional *continue-nudge*: if a
required deliverable is still absent when the model stops, it can be nudged to keep going —
but a correctness check is **never** consulted for that, so the grading oracle never leaks
and a finished-but-wrong trial is left alone.

`expected_tool_calls:` on a stage is a non-graded diagnostic — it records which tools you'd
expect the agent to call, surfaced in the transcript, but never affects the score.

## Custom checks

When the built-ins aren't enough, add a benchmark-local checks module and point
`benchmark.yaml` at it:

```yaml
# benchmark.yaml
checks: ./checks          # a dir/module exposing CHECKS (and optional ROLES)
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

Local checks merge with the built-ins; a name collision is an error. Now use it in a stage
exactly like a built-in:

```yaml
checks:
  - rows_at_least: { file: output/events.jsonl, min: 100 }
```

## Re-grading after a change

Tightened a tolerance or added a stage? `toolbench regrade --run-id <id>` replays the new
rubric against each trial's preserved `artifacts/` and rewrites the summary — no agent, no
model spend. This is why the runner keeps a minimal evidence set per trial.
