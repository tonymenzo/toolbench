# Concepts

Why toolbench is shaped the way it is. The [vocabulary](reference/vocabulary.md) defines the
terms, while this page explains the design decisions behind them.

## Orthogonal axes

A benchmark run is an experiment, and an experiment needs controls. toolbench separates five
things that are often tangled together (the **task**, the **model**, the **harness**, the
**tools**, and the **scaffolding**) into independent axes you set on the command line. The
payoff is that the *delta* between two runs is attributable. Change only the loadout and the
score difference *is* the value of those tools. Change only the variant and it *is* the cost
of less hand-holding. Tangle two axes and you can't say which caused the change.

## Reach as a prefix product

Most "score" definitions count how many checks passed. toolbench instead grades a rubric as
ordered stages and takes the **prefix product**. You bank a stage's weight only if it and
every stage before it passed. This makes the score answer the question people actually care
about, *how far through the task did the agent get?*, rather than rewarding an agent that
nails scattered late checks while skipping the foundation. It also turns the per-stage pass
rates into a **funnel** that shows exactly where agents fall off, which is usually more
actionable than the headline number.

## Three numbers, three questions

A single success rate hides the most important distinction in agent evaluation, *capable*
vs. *reliable*. toolbench reports three views so the distinction is visible:

- **reach**, how far, on average (partial credit).
- **pass@k**, can it *ever* get it fully right (best of k)?
- **pass^k**, can it get it right *every* time (worst of k)?

High pass@k with low pass^k is the signature of a capable-but-flaky agent, a retry or
prompt problem, not a capability gap. Low pass@k is a capability gap, where more tries won't
help. You can't see that difference from one number. See [Metrics](reference/metrics.md).

## No oracle leakage

Agents are sometimes "nudged" to keep working when they stop early. toolbench's nudge is
**presence-gated**. It fires only when a *required deliverable is still absent*, and it
**never** consults a correctness check. A finished-but-wrong trial is therefore left
exactly alone. The agent is never implicitly told "that's wrong, try again," which would
leak the grading oracle and inflate scores. Correctness is judged once, at the end.

## The toolbase boundary

toolbench measures, while [toolbase](guides/toolbase.md) manages tools. The line is
deliberate and one-directional. A benchmark *references* a toolkit (via a `toolbase:`
loadout source), but a toolkit never knows a benchmark exists. Nothing about the model,
prompt, or grading crosses into toolbase. This keeps both tools useful alone. You can
benchmark with plain Python tools and no toolbase, or use toolbase to serve tools you never
benchmark.

## Reproducibility by default

Every run records a `manifest.json` holding the exact config, the git SHA, and pinned
`toolbench` / `orchestral` / toolkit versions. Trials keep a minimal evidence set so the
judge can be **replayed** (`regrade`) after a rubric change without spending a single token.
The smallest bundle that reproduces a result is the benchmark directory plus that pin set, a
model id, and a seed, so a collaborator gets your numbers up to model nondeterminism.
