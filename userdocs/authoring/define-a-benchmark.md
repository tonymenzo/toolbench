# Define a benchmark

This walks through `benchmark.yaml` and the files around it, using the bundled `geometry`
benchmark as the worked example. Copy that directory and edit.

## `benchmark.yaml`

```yaml
name: geometry
version: 0.1.0
description: Euclidean distance and midpoint between two 2-D points.

# Defaults used when the corresponding CLI flag is omitted.
default_harness: orchestral/anthropic
default_loadout: full_local
default_variant: direct

ground_truth:
  dir: ./ground_truth          # reference answers, resolved relative to this file

rubric:
  type: stagewise              # ordered stages; trial score = prefix product
  stages:
    - id: answer_written
      description: output/answer.json exists with the required keys
      weight: 0.2
      checks:
        - json_with_keys:
            file: output/answer.json
            required_keys: [distance, midpoint]
    - id: midpoint_correct
      description: midpoint is within 1% of ground truth
      weight: 0.3
      checks:
        - close_to:
            file: output/answer.json
            field: midpoint
            reference: ./ground_truth/answer.json
            tolerance_frac: 0.01
      expected_tool_calls: [add, divide]      # optional diagnostic, never graded
    - id: distance_correct
      description: Euclidean distance is within 1% of ground truth
      weight: 0.5
      checks:
        - close_to:
            file: output/answer.json
            field: distance
            reference: ./ground_truth/answer.json
            tolerance_frac: 0.01
```

| Field                | Meaning                                                              |
|----------------------|---------------------------------------------------------------------|
| `name`               | The id used by `--benchmark`. Falls back to the directory name.      |
| `description`        | One line; shown in listings and the manifest.                       |
| `default_*`          | Axis defaults used when the CLI flag is omitted.                     |
| `ground_truth.dir`   | Directory of reference files for correctness checks.                 |
| `rubric`             | The grading spec — see [Rubrics & checks](rubrics-and-checks.md).    |
| `checks`             | *(optional)* path to a benchmark-local `checks.py` (custom checks).  |

## Ground truth

Put the canonical answer(s) under `ground_truth/`. A correctness check's `reference:` path
is resolved relative to the benchmark directory, so the same rubric grades every variant
against the same answer:

```json
// ground_truth/answer.json
{ "distance": 5.0, "midpoint": [1.5, 2.0] }
```

Design variants so they all resolve to this one answer — a constant denominator is what
makes cross-variant reach deltas comparable.

## Prompts and the deliverable

Each variant supplies a user prompt (and optional system prompt). Name the deliverable and
its exact schema in the prompt — the rubric checks a concrete file, so the agent must know
where to write it:

```markdown
<!-- variants/direct/prompts/user.md -->
The two points are in `points.json`. Compute their Euclidean distance and midpoint.
Write the result to `output/answer.json` as:
{"distance": <number>, "midpoint": [<number>, <number>]}
```

Anything you want present at trial start (here, `points.json`) goes in the variant's
`sandbox/template/`; it's copied into a fresh sandbox per trial.

## Wiring the axes

- **Harness** — at least one `harnesses/<runtime>/<provider>.yaml`
  (see [Harnesses](../guides/harnesses.md)).
- **Loadout** — at least one `loadouts/<name>.yaml`; usually `core_only` plus a tool
  condition to ablate (see [Loadouts](../guides/loadouts.md)).
- **Variant** — at least one `variants/<name>/` (see [Variants](../guides/variants.md)).

## Validate

```bash
toolbench run --benchmark <name> --model stub \
    --loadouts core_only --n 1 --max-cost-usd 0 --dry-run
```

A clean dry run means: the benchmark was discovered, the harness/loadout/variant resolved,
the tool list built, and the grader ran against an (empty) sandbox. Now do a small real run
and read the [summary](../guides/reading-results.md).
