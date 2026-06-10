# Variants

A **variant** is the *scaffolding* axis, the prompt the agent sees plus the sandbox it
starts in. Variants are orthogonal to loadouts. A loadout changes the *tools*, while a
variant changes *how much help the prompt and workspace give*. All variants of one
benchmark share the same rubric and ground truth, so a cross-variant score delta cleanly
isolates the cost of less scaffolding.

## A variant file

Variants live under `variants/<name>/`, each with a `variant.yaml`, a `prompts/` dir, and
an optional `sandbox/template/`:

```yaml
name: direct
description: |
  Points are given directly in points.json. The prompt names the file and the
  exact output schema. Pure arithmetic, no derivation.
axes:
  input: direct        # points handed to the agent as data
  frame: cartesian     # straight (x, y), no conversion
user_prompt_file: ./prompts/user.md
system_prompt_file: ./prompts/system.md
sandbox:
  template_dir: ./sandbox/template     # seeded into every trial's sandbox
```

Omit the `sandbox:` block for an empty sandbox (the variant puts everything the agent needs
in the prompt). The `axes:` are free-form labels you choose. They show up in the run
manifest so a sweep's conditions are self-describing.

## One-axis-at-a-time design

The power of variants is in designing each pair to differ in **exactly one axis**, so any
reach delta is attributable. The `geometry` benchmark ships three:

| Variant   | input    | frame     | What it isolates                                          |
|-----------|----------|-----------|----------------------------------------------------------|
| `direct`  | direct   | cartesian | the baseline, points handed over as data                 |
| `derived` | derived  | cartesian | `direct → derived` = the cost of *deriving* the inputs    |
| `polar`   | derived  | polar     | `derived → polar` = the cost of a *coordinate conversion* |

Because all three resolve to the **same** ground-truth answer (distance 5.0, midpoint
`[1.5, 2.0]`), the family-level rubric grades them identically. The denominator is
constant, so cross-variant deltas measure scaffolding only, never a shift in the reference
values.

## Running a variant sweep

```bash
toolbench run --benchmark examples/geometry \
    --variants direct,derived,polar \
    --loadouts full_local --models claude-haiku-4-5 \
    --n 5 --max-cost-usd 2.00
```

Each variant becomes part of the condition label. Read the `direct → derived → polar` reach
curve as a difficulty ladder *you defined by what you withheld*, not by guessing. The
ordering is a measurement, not a claim baked into the names.

## Variants vs. loadouts vs. benchmarks

- Different **tools**, same task → a new **loadout**.
- Same task + rubric, different **prompt/sandbox** → a new **variant**.
- Different **task or rubric** → a new **benchmark**.

Keep them separate and every sweep stays a clean experiment.
