# Authoring overview

A benchmark is a directory of declarative files, with no Python required for the common
case. This section is for the author building a new task and grading it against LLMs. If
you just want to *run* the example benchmark, start with the
[guides](../guides/running-a-benchmark.md).

## The mental model

You are designing an **experiment**. A benchmark fixes the *task and how to grade it*, and
the four other axes (model, harness, loadout, variant) are the knobs you'll vary to learn
something. Good benchmarks make the rubric a **funnel**, with ordered stages from "did it
produce anything?" to "is it exactly right?", so the score tells you *where* agents fail,
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
2. **Write `benchmark.yaml`**, the task description, the rubric, and the ground-truth
   pointer. See [Define a benchmark](define-a-benchmark.md).
3. **Write the rubric**, ordered and weighted stages of checks.
   See [Rubrics & checks](rubrics-and-checks.md).
4. **Add a variant**, the user/system prompts and (optionally) a sandbox seed.
5. **Pick a loadout.** Start with `core_only` and one tool loadout to ablate.
6. **Dry-run it for \$0.** `--model stub --dry-run` validates wiring, resolution, grading,
   and the summary without any LLM calls.
7. **Run small, then sweep**, a couple of cheap trials, then widen `--n` and the axes.

## Resolving a benchmark

Benchmarks are resolved by path. Any directory with a `benchmark.yaml` is runnable
via `toolbench run --benchmark <path>` (e.g. `examples/geometry`). There's nothing
to register. The benchmark lives wherever you put it, and the CLI loads it directly
from the directory you point at.

## Choosing a judge

A run is graded by the deterministic **rule** judge by default — it evaluates each stage's
`checks:` list, and it's what keeps runs reproducible and `regrade`-able. You override that
per run, without editing the benchmark: a harness may carry an optional `judge:` block, or the
CLI may pass `--judge` / `--judge-harness` / `--judge-model`. **Precedence** (first wins):

1. CLI `--judge` / `--judge-harness` / `--judge-model`
2. the harness's `judge:` block
3. the default `rule`

`kind` is one of `rule`, `rule+llm`, or `llm`. In **`rule+llm`** the rule grade stays
**primary and authoritative** — `score` and the failure mode come from it — and the LLM judge
runs *after* it, its grade attached additively in `alt_grades`. So the headline number stays
deterministic and regradeable while a second opinion rides along. **`llm`-only** is for
ablations (`regrade --judge llm`), never the headline, since the score would then drift with
the judge model's version.

The judge's `harness` names the route the *judge* is called through and may differ from the
agent's harness — a subscription judge can grade an API run and vice versa (subscription
judges are credential-free and unpriced). Allowed keys in a `judge:` block are `kind`,
`harness`, `model`, `max_tokens`, `temperature`, and `artifact_chars` (how much of each
answer/reference file the judge reads — raise it above the 8000-char default for large
deliverables):

```yaml
judge:
  kind: rule+llm
  harness: orchestral/anthropic
  model: claude-opus-4-8
```

## Reproducibility, for free

Every run writes a `manifest.json` with the exact config, the git SHA, and pinned
`toolbench` / `orchestral` (and toolkit) versions. The minimal bundle a collaborator needs
to reproduce your numbers is the benchmark directory, that pin set, the model id, and the
seed. Everything else is recorded.
