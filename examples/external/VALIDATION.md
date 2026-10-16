# External-run integration record

Implementation date: 2026-10-15.

- Toolbench baseline: b2a90d9398fdcd94699e72e11caf5078b38983f8.
- Conjure source tested: 2eacca7c522450c7efa3a84ff029a5f6901e216b (0.2.5).
- Python environment: existing Hepbench environment; Conjure source supplied on
  PYTHONPATH. No global installations or provider defaults changed.
- Runtime tests exercise Conjure's real driver lifecycle with a scripted engine.
  Another test uses its actual config loader, build_runtime and engine dispatch,
  substituting only the model-facing engine. No LLM calls were made.
- Tests cover input/prompt/reference/dependency drift, symlinks, replay history,
  failure/timeout status, duplicate execution prevention and score-vs-acceptance.
- Runtime integration exposed two assumptions corrected in the test setup:
  Conjure config requires an llms mapping even for a subscription engine; its
  driver wraps tasks in message envelopes rather than forwarding plain text.
- Archived Hepbench scalar reference replay: score 1.0; physics, NDA, diagrams,
  MadGraph, research, reproducibility and FeynTable contract all passed. This
  replay validates grading/handoff only, not new scientific execution or agent
  capability. The preserved reference remains unchanged.
- Live model/tool-grant parity, provider interruption behavior and a measured
  single-worker Conjure campaign remain to be tested before reporting performance.
- The normal live dashboard is unchanged. External-run JSON records are the
  intentional final-score interface; native Conjure traces remain separate.

Final targeted regression: 57 tests passed (external workflow, judge scoring,
builtin checks and benchmark inheritance). Scalar replay was also accepted by
all seven original stages. No live-model campaign was launched.
