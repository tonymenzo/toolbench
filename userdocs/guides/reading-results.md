# Reading results & scores

A run drops everything under `runs/<run_id>/`. This guide tours what's there and
**how to actually read the three scores**, the part most people get wrong.

## The run directory

```console
runs/2026-05-31T14-02-08_geometry_claude-haiku-4-5_run/
├── manifest.json        # exact config + git SHA + pinned versions (reproducibility)
├── summary.txt          # the human-readable table (start here)
├── summary.json         # the same numbers, machine-readable
├── trials.jsonl         # one compact line per trial
├── k_sweep.png          # pass@k / pass^k as k grows
├── parallel_coords.png  # the three-vector per cell, side by side
├── per_stage_k.png      # where trials die in the rubric
└── trials/<trial_id>/
    ├── trial.json       # full per-trial record (grade, tokens, cost)
    ├── transcript.jsonl.gz   # every tool call
    ├── console.log      # the styled per-trial log
    └── artifacts/       # the minimal evidence kept for `regrade`
```

Start with `summary.txt`, then drill into a `trials/<id>/` when a number surprises you.

## The three scores, intuitively

`summary.txt` prints one block per cell. For `--n 5` trials of `full_local` it looks like:

```console
══════════════════════ CELL  claude-haiku-4-5  ×  full_local ═══════════════════

  THREE-VECTOR              (k=5)

      reach          0.92         productivity     (rubric-weighted)
      reach (eq-w)   0.95         depth            (equal-weight, no rubric)
      pass@k         1.00         exploration      (best of k)
      pass^k         0.60         trustworthiness  (worst of k)

  STAGES
      answer_written     5/5       1.00
      midpoint_correct   5/5       1.00
      distance_correct   3/5       0.60
```

The three headline numbers (`reach`, `pass@k`, `pass^k`) each answer a different
*question*. Here they are next to the `core_only` baseline:

| condition   | reach $\bar R_k$ | pass@k | pass^k |
|-------------|:----------------:|:------:|:------:|
| `core_only` |       0.46       |  0.40  |  0.00  |
| `full_local`|       0.92       |  1.00  |  0.60  |

Read each as a different *question*:

- **reach = how far, on average.** `core_only` gets ~halfway through the rubric before
  stalling, while `full_local` almost always finishes. This is your headline "how good is
  it".
- **pass@k = can it *ever* do it (best of k).** `full_local` is 1.00. Across 5 tries at
  least one was perfect every time. `core_only` at 0.40 can sometimes stumble all the way
  through without the tools, but not dependably.
- **pass^k = can it do it *every* time (worst of k).** This is the reliability column.
  `full_local` is only 0.60, so even with the tools, run it 5 times and you can't count on
  all 5 being perfect. `core_only` is 0.00, never all-correct across a batch.

!!! tip "The pattern to internalize"
    **High pass@k, low pass^k = capable but flaky.** The agent *can* do the task, it just
    doesn't do it reliably. That's a retry-wrapper or a tightening-the-prompt problem, not
    a "the model can't do this" problem. **Low pass@k = a capability gap.** More tries
    won't help, so change the tools, the model, or the scaffolding.

`pass@1 = pass^1 = c/n`, the raw success rate. As `k` grows the two fan apart, with pass@k
climbing toward 1 and pass^k falling toward 0, and `k_sweep.png` plots exactly that fan.

## Reading a sweep as an ablation

Because the axes are orthogonal, the **gap between two rows** is attributable. In the
table above, `full_local − core_only` is +0.46 reach and +0.60 pass^k. That delta *is the
value of the domain tools* for this task, on this model. Swap which axis you list in
`--loadouts` / `--models` / `--variants` / `--harnesses` and the delta means something
different. See the table in [Metrics](../reference/metrics.md#what-a-sweep-tells-you).

## Where trials die

`per_stage_k.png` (and the `stages` block in `summary.json`) show the per-stage pass rate.
Because grading is a prefix product, this is a **funnel**. If `midpoint_correct` passes 80%
but `distance_correct` only 30%, the distance step is where runs fall off, so look at a few
failing `trials/<id>/` to see why.

## Cost & budget

Every run takes a hard `--max-cost-usd` cap and aborts the moment spend would exceed it.
`summary.txt` reports total spend and mean cost per trial. A `--dry-run` (with
`--model stub`) exercises the entire pipeline (wiring, grading, summary, plots) for
\$0, which is the right way to validate a new benchmark before spending tokens.

## Re-grading without re-running

Changed a rubric (tightened a tolerance, added a check)? `toolbench regrade --run-id <id>`
re-runs the judge against each trial's preserved `artifacts/` and rewrites the summary,
with no agent and no model calls. Hard process failures (crashes) keep their failure mode,
while everything rubric-derived is recomputed. See [Commands](../reference/commands.md).
