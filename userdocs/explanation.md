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

## Reach, and why its default is a prefix product

Most "score" definitions count how many checks passed. toolbench instead grades a rubric as
ordered, weighted stages and reports **reach**: weighted progress under an *absorbing-gating*
convention. Each stage earns a **credit** (binary by default, or continuous partial credit in
`[0,1]`) and is **gating** by default (a failure absorbs every later stage) unless declared
independent with `gating: false`.

With every stage binary and gating, reach is *exactly* the **prefix product**: you bank a
stage's weight only if it and every stage before it passed. That is the special case worth
keeping in your head, because it makes the score answer the question people actually care
about, *how far through the task did the agent get?*, rather than rewarding an agent that
nails scattered late checks while skipping the foundation. It also turns the per-stage pass
rates into a **funnel** that shows exactly where agents fall off, which is usually more
actionable than the headline number. Continuous and non-gating stages generalise this for
rubrics that want partial credit or independent quantities; the exact formula lives in
[Metrics](reference/metrics.md).

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
leak the grading oracle and inflate scores. Correctness is judged once, at the end. The
integrity quarantine below guards the *other* direction of leakage, an agent that reaches the
answer key itself.

## Integrity quarantine

The `claude_code` runtime deliberately doesn't confine the agent's Bash to the sandbox, so a
trial *could* `cat` the ground-truth answer key that lives outside it. Two things stop that
from silently inflating a score. The CLI runtimes **deny-read** the ground-truth paths up
front (`protected_paths`), and as a backstop a post-run scan reads each trial's tool-call
*inputs* (Bash commands, file reads, greps, never tool results) for references to the answer
key, markers like `truth.json`, `ground_truth`, `answer_key`, and the benchmark's own
ground-truth directory. A flagged trial is scored **0**, marked `INTEGRITY_LEAK`, has its
original score preserved as `score_pre_integrity`, and is **excluded from the headline**. It
is the enforcement behind "no oracle leakage": neither the grader nor the agent gets to peek.

## UX-feedback turn

An opt-in, **unscored** post-completion turn (harness `loop.ux_feedback: true`) that asks the
agent to critique the tools it just used. It runs *after* the task loop ends and *before*
teardown, never overwrites `trajectory.final_response`, and instructs the agent to touch no
files, so grading (which reads only sandbox files) sees exactly the same state either way. It
cannot move the score by construction. Treat it as a tool-development signal, surfacing the
interface and documentation friction the agent actually hit, not as a benchmark metric.

## The toolbase boundary

toolbench measures, while [toolbase](guides/toolbase.md) manages tools. The line is
deliberate and one-directional. A benchmark *references* a toolkit (via a `toolbase:`
loadout source), but a toolkit never knows a benchmark exists. Nothing about the model,
prompt, or grading crosses into toolbase. This keeps both tools useful alone. You can
benchmark with plain Python tools and no toolbase, or use toolbase to serve tools you never
benchmark.

## Judges

Grading is done by the deterministic **rule judge** by default. You can optionally attach an
**LLM judge**, but it runs *after* the rule judge as a non-authoritative second opinion,
never in its place: its verdict is stored in the trial's `alt_grades` and the rule grade
stays primary. That ordering is deliberate. Because the score and every metric derived from
it come from a deterministic rule, a run stays **reproducible** and `regrade`-able forever,
whereas a headline number owned by an LLM judge would drift with the judge model's version.
The LLM grade rides along for comparison and ablation; it never becomes the number. (An LLM
judge needs somewhere to run, so configuring one without a `--judge-harness` / harness
`judge.harness` fails loudly at setup.)

## Reproducibility by default

Every run records a `manifest.json` holding the exact config, the git SHA, and pinned
`toolbench` / `orchestral` / toolkit versions. Trials keep a minimal evidence set so the
judge can be **replayed** (`regrade`) after a rubric change without spending a single token.
The smallest bundle that reproduces a result is the benchmark directory plus that pin set, a
model id, and a seed, so a collaborator gets your numbers up to model nondeterminism. The
rule-primary judge above is what makes this hold: replaying a deterministic rubric always
reproduces the same score, LLM second opinions notwithstanding.
