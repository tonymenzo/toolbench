# Authoring overview

A benchmark is a directory of declarative files — no Python required for the common case.
This section is for the author building a new task and grading it against LLMs. If you just
want to *run* the example benchmark, start with the [guides](../guides/running-a-benchmark.md).

## The mental model

You are designing an **experiment**. A benchmark fixes the *task and how to grade it*; the
four other axes (model, harness, loadout, variant) are the knobs you'll vary to learn
something. Good benchmarks make the rubric a **funnel** — ordered stages from "did it
produce anything?" to "is it exactly right?" — so the score tells you *where* agents fail,
not just *whether* they did.

## Anatomy of a benchmark

```
examples/<name>/
├── benchmark.yaml          # task metadata + ground truth + rubric  ← the heart
├── ground_truth/           # reference answers the rubric compares against
│   └── answer.json
├── harnesses/              # which runtimes/providers this task supports
│   └── orchestral/anthropic.yaml
├── loadouts/               # tool conditions to ablate
│   ├── core_only.yaml
│   └── full_local.yaml
├── variants/               # prompt + sandbox scaffolding rungs
│   └── direct/
│       ├── variant.yaml
│       ├── prompts/{user,system}.md
│       └── sandbox/template/        # optional: files seeded into each trial
├── tools/                  # optional: benchmark-local @define_tool modules
├── skills/                 # optional: recipe/guide docs a loadout can expose
└── checks/checks.py        # optional: benchmark-local rubric checks
```

Only `benchmark.yaml`, one harness, one loadout, and one variant are strictly required.
The example `geometry` benchmark is the reference to copy.

## The workflow

1. **Scaffold the directory** under `examples/<name>/` (copy `geometry`).
2. **Write `benchmark.yaml`** — the task description, the rubric, the ground-truth pointer.
   See [Define a benchmark](define-a-benchmark.md).
3. **Write the rubric** — ordered, weighted stages of checks.
   See [Rubrics & checks](rubrics-and-checks.md).
4. **Add a variant** — the user/system prompts and (optionally) a sandbox seed.
5. **Pick a loadout** — start with `core_only` and one tool loadout to ablate.
6. **Dry-run it for \$0** — `--model stub --dry-run` validates wiring, resolution, grading,
   and the summary without any LLM calls.
7. **Run small, then sweep** — a couple of cheap trials, then widen `--n` and the axes.

## Resolving a benchmark

Benchmarks are resolved by path: any directory with a `benchmark.yaml` is runnable
via `toolbench run --benchmark <path>` (e.g. `examples/geometry`). There's nothing
to register — the benchmark lives wherever you put it, and the CLI loads it directly
from the directory you point at.

## Reproducibility, for free

Every run writes a `manifest.json` with the exact config, the git SHA, and pinned
`toolbench` / `orchestral` (and toolkit) versions. The minimal bundle a collaborator needs
to reproduce your numbers is: the benchmark directory, that pin set, the model id, and the
seed — everything else is recorded.
