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
    ├── audit.txt        # readable transcript + grade audit (always written)
    ├── audit.html       # HTML twin of audit.txt (only with --audit-html)
    ├── ux_feedback.md   # the agent's tool critique (only with --ux-feedback)
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

## The run header: provenance & integrity

Above the cells, `summary.txt` opens with a header that makes the file self-describing:

```console
════════════════════════════════════ RUN  examples/geometry ═══════════════════
  run_id     2026-05-31T14-02-08_geometry_claude-haiku-4-5_run
  task       examples/geometry       k          5   (n per cell)
  trials     10                      cells      2
  budget     $5.00 cap   $0.42 spent
  versions   toolbench 0.6.1  orchestral 0.4.0
  provenance git a1b2c3d4e5   harness orchestral/anthropic
  integrity  CLEAN (10 trials scanned)
```

- **`versions`** pins toolbench *and* the runtime that actually drove the run, so a summary
  reproduces without opening `manifest.json`.
- **`provenance`** tails the code `git <sha>` and the harness id(s) behind the run.
- **`integrity`** is the answer-key firewall. Normally it reads `CLEAN (N trials scanned)`.
  If any trial referenced the graded answer key from outside its sandbox, this turns into a
  loud `** N TRIAL(S) QUARANTINED — reached the answer key **` banner (with an evidence
  snippet). A quarantined trial is **scored 0** with failure mode `INTEGRITY_LEAK`; its
  pre-quarantine score is preserved as `score_pre_integrity` but **excluded from the
  headline** so a leak can never inflate a result.

## Per-trial detail: TRIALS, TOOLS, tokens

Each cell block carries three sections the cell mean would otherwise hide:

```console
  TRIALS
  ------

      scores     1.00  1.00  0.50  1.00  0.50   (min 0.50  max 1.00  spread 0.50)
      UX rating  7  8  6  7  8        (blind, 1-10)
      retries    rate-limit 1   transient 0   nudges 2

  TOOLS
  -----

      adoption   5/5 trials used domain tools  (4 via MCP, 1 via script)
      MCP calls:
        midpoint    9
        distance    7   (1 err)
```

- **TRIALS** — the per-trial `scores` with min/max/spread (so a bimodal cell is visible),
  the blind `UX rating` (1–10) when `--ux-feedback` is on, and a `retries` rollup
  (rate-limit / transient / nudges) as a reliability read.
- **TOOLS** — `adoption N/n trials used domain tools`, split into `X via MCP, Y via script`,
  plus a per-tool call/error breakdown. The MCP-vs-script split is the tell for whether the
  agent actually **drove the served pipeline** or quietly hand-rolled its own scripts.
- **token means** live in the COST section (`mean_tokens`): `initial input` (the starting
  context), cumulative `input` / `output`, and `cache read` / `cache write`, all per trial.

## Reading a sweep as an ablation

Because the axes are orthogonal, the **gap between two rows** is attributable. In the
table above, `full_local − core_only` is +0.46 reach and +0.60 pass^k. That delta *is the
value of the domain tools* for this task, on this model. Swap which axis you list in
`--loadouts` / `--models` / `--variants` / `--harnesses` and the delta means something
different. See the table in [Metrics](../reference/metrics.md#what-a-sweep-tells-you).

For the delta itself, read the **CONDITION DELTAS** section (`paired_deltas` in
`summary.json`) rather than differencing two cells by hand. Conditions share seeds, so
toolbench pairs trials per seed and bootstraps over the *seed dimension* — per-seed noise
cancels in the difference, giving a tighter (and honest) CI than combining two per-cell
CIs. Direction is `condition_b − condition_a` in the order you listed the conditions; a
CI that excludes 0 is a delta your n actually supports.

## Where trials die

`per_stage_k.png` (and the `stages` block in `summary.json`) show the per-stage pass rate.
For the default rubric — every stage binary and gating — this is a **funnel**: a stage can
only pass if every stage before it did. If `midpoint_correct` passes 80% but
`distance_correct` only 30%, the distance step is where runs fall off, so look at a few
failing `trials/<id>/` to see why.

The funnel reading holds *only* for the default gating-binary case. A `continuous: true`
stage contributes partial `[0,1]` credit rather than pass/fail, and a `gating: false` stage
does **not** absorb the stages after it — so with those knobs the stages aren't a strict
prefix product. A continuous stage renders differently in `summary.txt`, showing the credit
and mean distance it came from instead of a plain rate:

```console
      answer_written     5/5       1.00
      area_correct       4/5  credit 0.86   dist 0.030 rel
```

See [Metrics](../reference/metrics.md#continuous-and-independent-stages) for exactly how
each stage feeds the score.

## Cost & budget

Every run takes a hard `--max-cost-usd` cap and aborts the moment spend would exceed it.
`summary.txt` reports total spend and mean cost per trial. A `--dry-run` (with
`--model stub`) exercises the entire pipeline (wiring, grading, summary, plots) for
\$0, which is the right way to validate a new benchmark before spending tokens.

Subscription runtimes (`claude_code`, `codex`) don't expose a per-run charge, so their
observed spend stays \$0 and doesn't consume the API budget cap. When a price snapshot exists
for the model, `summary.txt` also shows a token-derived **API-equivalent (estimated,
subscription)** figure — a counterfactual for comparing a subscription arm against an API arm,
not money billed. `summary.json` keeps it separate as `estimated_api_equivalent_cost_usd`,
with the rates and source in `estimated_cost_basis`, so it can never be mistaken for real spend.

## Re-grading without re-running

Changed a rubric (tightened a tolerance, added a check)? `toolbench regrade --run-id <id>`
re-runs the judge against each trial's preserved `artifacts/` and rewrites the summary,
with no agent and no model calls. Hard process failures (crashes) keep their failure mode,
while everything rubric-derived is recomputed. See [Commands](../reference/commands.md).
