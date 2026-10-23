# External workflows and Conjure

`toolbench external` evaluates final artifacts without requiring a Toolbench
agent harness, transcript or dashboard telemetry. It uses the same `RuleJudge`
and benchmark-local checks as ordinary runs. Scientific scripts, cards, receipts
and references required by the benchmark remain mandatory.

## Prepare and evaluate

```sh
toolbench external prepare /path/to/benchmark /path/to/new-run --variant standard
# Run your workflow in /path/to/new-run/workspace, using prompts.json.
toolbench external evaluate /path/to/new-run --execution-status completed
```

The run directory must be new and outside the benchmark. Only the public variant
seed is copied to `workspace/`; prompts and evaluator metadata stay outside it.
`prompts.json` holds the benchmark system and user prompts. This is separation
of inputs, not an OS security boundary: isolate workers separately if required.
Checks are trusted Python, exactly as in normal Toolbench evaluation.

Use repeated `--dependency-root /path/to/shared-checks` when a benchmark imports
shared validators, contracts or reference assets outside its own directory.
For the Hepbench SM tasks, include `benchmarks/feyntable/sm_pipeline`. The evaluator
rejects changes to benchmark files, declared dependencies, prompts or seeded inputs.
Contained build symlinks (for example MadGraph include files) are recorded by
link target without traversal; a dangling link whose path stays inside the workspace
(MG5 links optional libraries it never built) is recorded as `broken`. Links leaving
the workspace, and replacements of seeded inputs, are rejected. Publication-package validators still enforce their own
stricter no-symlink contract.

It does not automatically discover arbitrary Python import dependencies.

For manual execution, `--execution-status` is an explicit operator declaration,
not proof of execution. Allowed values are completed, failed and timed_out. It
cannot overwrite an adapter's recorded status. A failed/timed-out execution can
receive diagnostic grades but cannot be accepted. Re-evaluate without that flag
to preserve execution provenance; every evaluation is retained separately.

## Conjure adapter

Install Conjure in the same environment, or make its source package importable.
The adapter uses the existing `conjure.config`, `conjure.runner.build_runtime`
and `Runtime` APIs; no Conjure patch is required. Start from
`examples/external/conjure.yaml`, select a model and configure a Toolbase project:

```sh
toolbench external conjure /path/to/new-run --config conjure.yaml --timeout 3600
```

This command starts a real model workflow. Configuration is validated before
launch. The adapter uses the benchmark's prompts and workspace, records the
effective Conjure configuration, and keeps the native session under
`conjure-native/sessions/<id>/` (journal, session manifest, one event log per
agent), the same layout whichever way the trial is launched. After the run it
writes the trial record and transcript from that session (see below).
Do not embed credentials in config; use environment-backed credentials.
Token counts and cost are null unless recorded, never fabricated zeros.

Tool auto-approval defaults to on for adapter-launched runs. Set
`runtime.auto_approve: false` explicitly to retain interactive approvals. This
default is recorded in the effective configuration and does not change other
Conjure sessions or global settings.

Engine selection must be explicit rather than `auto`. For a one-worker comparison,
use max_workers=1, max_depth=0 and no spawning tools. For later multi-agent variants,
grant the appropriate tools and increase those limits. The coordinator must join
its children and assemble the final artifacts before returning; returning while
children remain active or errored fails execution. Root errors, termination and
timeouts are not successful completion. Shutdown precedes evaluation.

Toolbench loadouts are not silently translated into Conjure grants. Configure
Conjure's native Toolbase loadout/authority explicitly and verify tool/skill parity
before comparing systems. The effective config and native traces retain this
information for audit. Scientific scope/workflow instructions should live in a
benchmark variant, not a replacement root prompt hidden in execution config.

## Benchmark-declared Conjure requirements

A benchmark can declare what a Conjure trial of it needs in a `conjure:` block of its
`benchmark.yaml` (paths are relative to that file and survive `extends`):

```yaml
conjure:
  runtime: conjure-runtime.yaml          # mapping layered over the operator's config
  toolbase:
    loadout: heptapod-sm-enumeration     # becomes the root's loadout
    profile: toolbase-loadout.yaml       # the loadout file, with its versions: pins
  preflight:
    module: preflight.py                 # check(config: dict, run: Path, params: dict) -> dict
    params: {}
```

Before any model call the adapter layers the runtime overlay, writes a trial-local
Toolbase project (`<run>/.toolbase`, with the profile as its active loadout), runs from the
run directory (Toolbase resolves version pins from the project above the working
directory, not from the project Conjure is handed), then calls the preflight and writes
`<run>/preflight.json`. A failed preflight ends the trial before the model is called; its
execution is recorded as `failed` with `invalid_trial: true`, a provisioning problem rather
than an agent result. `toolbench external conjure RUN --config C --preflight-only` stops
after the preflight, leaving the run ready to execute. What was applied is recorded under
`execution.benchmark_conjure` in `external-run.json`.

## Records and interpretation

- `external-run.json`: benchmark identity, file hashes, seed hashes, variant,
  execution status/timestamps, configuration identity, declared/observed root
  model metadata, and unknown usage marked null.
- `external-result.json`: latest grade, acceptance flag and artifact hashes.
- `evaluations/*.json`: append-only evaluation records.
- `conjure-native/`: native traces, separate from grading metadata. Tool outputs
  too long for an agent's context, which Conjure writes into the agent's sandbox
  (`.conjure/tool-output/`), are moved here (`conjure-native/tool-output/`) after
  the run, so the graded workspace holds the agents' work alone.
- `trial.json`: what the trial ran and what it cost. Per agent: label, parent and
  depth, engine and model, `access` (`api` or `subscription`), tokens
  (`input` including cached tokens, `output`, `cache_read`, `cache_creation`),
  `cost_usd` (spend) and `api_equivalent_cost_usd`, the toolkits it was spawned
  with and every grant, tool-call counts by name, turns. In total: the same sums,
  with `billing` (`api`, `subscription` or `mixed`); spend counts API-billed agents
  only, and a total is null when any part is unknown. Also the Toolbench version
  and git revision, Conjure and package versions, the pinned profile, the
  toolkits Toolbase served, the preflight report and execution timings.
- `transcript.jsonl.gz`: every agent's events in time order. `tool_call` rows
  carry Toolbench's transcript fields (`name`, `args`, `ok`, `t`,
  `result_summary`) plus `agent`, `call_id`, the full `result` and, when the
  result was cut for the context, where the whole output is. Replies, inputs,
  messages between agents, spawns, grants and terminations are rows too, and a
  Codex agent's web searches carry the results Codex returned (from its rollout
  file). The audit scripts that read a Toolbench trial's transcript read this one.

`toolbench external record RUN` rebuilds `trial.json` and the transcript from
the native session, for runs made before the adapter wrote them. Sessions
recorded by Conjure before it journaled call ids, cache tokens and access are
read too: results pair with calls in order, tokens come from the usage events
(cache unknown), and access is inferred where the engine settles it
(`access_source: inferred`).

**Acceptance is separate from score.** In particular, a zero-weight certification
stage can fail while the diagnostic score is 1.0. Acceptance requires completed
execution and every rubric stage passing. This preserves Hepbench's draft-reference
gates. Missing trajectories make tool-use diagnostics *unobserved*, not unused.

These are standalone external-run records. The existing live Toolbench
dashboard does not yet consume them. The CLI prints
execution status, stage grades, score and acceptance; a future dashboard reader
can consume these small records without loading native agent traces.

The initial tests use Conjure's real runtime with a scripted engine, covering
success, errors and timeout. An archived scalar reference package also passes
through external preparation/evaluation with all seven original rubric stages.
That is an artifact replay, not a newly generated agent result. A live-model
Conjure campaign is the next integration test, not evidence already established.
