# Schemas

The declarative files that make up a benchmark. All are YAML, and paths inside them resolve
relative to the file's benchmark directory.

## `benchmark.yaml`

The task and grading spec. Requires `name`, `rubric`, and at least one
harness/loadout/variant on disk.

```yaml
name: geometry                       # id for --benchmark (falls back to dir name)
version: 0.1.0
description: Euclidean distance and midpoint between two 2-D points.
default_harness: orchestral/anthropic
default_loadout: full_local
default_variant: direct
ground_truth:
  dir: ./ground_truth
rubric: { type: stagewise, stages: [ ... ] }
checks: ./checks                     # optional: benchmark-local checks module
artifacts:                           # optional: what sandbox cleanup preserves
  keep_full: [".pdf", ".png", ".csv", ".json"]
  truncate: [ { ext: ".jsonl", max_records: 200 } ]
```

| Key               | Type   | Notes                                              |
|-------------------|--------|----------------------------------------------------|
| `name`            | str    | `--benchmark` id, defaults to directory name.      |
| `version`         | str    | Free-form, recorded in the manifest.               |
| `description`     | str    | One line.                                          |
| `default_harness` | str    | Harness id used when `--harness` is omitted.       |
| `default_loadout` | str    | Loadout used when `--loadouts` is omitted.         |
| `default_variant` | str    | Variant used when `--variants` is omitted.         |
| `ground_truth.dir`| path   | Reference-answer directory.                         |
| `rubric`          | map    | See [Rubric](#rubric).                              |
| `checks`          | path   | Optional module exposing `CHECKS` (+ `ROLES`).     |
| `artifacts`       | map    | See [Artifacts](#artifacts).                        |
| `extends`         | path   | Optional parent benchmark dir to inherit from. See [Extends](#extends). |

## Extends

A benchmark may overlay another with `extends: <path-to-parent-dir>`,
inheriting every asset it doesn't declare. Use it when sibling benchmarks
share one underlying task but grade different deliverables — a shape-only
or yield-only rubric over the same simulation, a stricter tolerance ladder
— without duplicating the directory tree.

```yaml
# sus-16-046_T5Wg_shape/benchmark.yaml — a complete overlay
name: sus-16-046_T5Wg_shape
description: Shape-only grading; normalization ungraded.
extends: ../sus-16-046_T5Wg
rubric: { type: stagewise, stages: [ ... ] }
```

Semantics:

- The child is a **distinct benchmark** — own name, version, and run
  cells. Variants stay the scaffolding axis *within* each benchmark, so
  cross-variant reach deltas never cross a rubric boundary.
- Top-level keys merge whole: a key the child declares replaces the
  parent's; one it omits is inherited (`rubric`, `ground_truth`, `checks`,
  `artifacts`, `default_*`). Identity keys (`name`, `version`,
  `description`) are never inherited.
- Discovery-based assets (`harnesses/`, `loadouts/`, `variants/`) are the
  union of both dirs, child shadowing by name. Inherited paths keep
  anchoring at the file that declared them: an inherited rubric's
  `reference:` finds the parent's `ground_truth/`, an inherited loadout's
  `python:` source still points at the parent's `tools/`.
- Inheritance is **depth-1**: a parent must be self-contained; extending
  an overlay is an error.
- The run manifest records `benchmark_extends` and the post-merge config
  (`benchmark_config`), so an overlay run is reproducible even if the
  parent later changes.

## Artifacts

After grading, each trial's sandbox is deleted; only files matched by the
`artifacts:` policy are copied into `trials/<id>/artifacts/` — and those are
all `toolbench regrade` can ever see. **The policy must keep every file the
rubric's checks read.** The runner audits this after every trial and warns if
a stage that just passed would flip against the preserved artifacts.

| Key                | Default                                          | Notes                                  |
|--------------------|--------------------------------------------------|----------------------------------------|
| `keep_full`        | `.pdf .png .npy .py .lhe .lhe.gz .json`          | Extensions copied verbatim.            |
| `truncate`         | `[{ext: .jsonl, max_records: 200}]`              | Record-oriented files, first N records. |
| `keep_root`        | `[todos.md]`                                     | Bare-name files at the sandbox root.   |
| `exclude_segments` | `[bin/internal]`                                 | Path segments pruned (third-party machinery). |

Each key *replaces* its default when present. Agent-authored code passed to
code-running tools is always preserved under `artifacts/scripts/`.

## Rubric

```yaml
rubric:
  type: stagewise                    # only stagewise is supported
  pass_threshold: null               # null ⇒ all-stages pass; float ⇒ pass iff reach ≥ it
  stages:
    - id: answer_written
      description: output/answer.json exists with required keys
      weight: 0.2
      checks:
        - json_with_keys: { file: output/answer.json, required_keys: [distance, midpoint] }
      expected_tool_calls: [add]     # optional, non-graded diagnostic
```

| Rubric key       | Type          | Notes                                                       |
|------------------|---------------|-------------------------------------------------------------|
| `type`           | str           | Only `stagewise` is supported.                              |
| `pass_threshold` | float \| null | Rubric-level. `null` (default) ⇒ a trial passes iff every stage passed; a float ⇒ it passes iff its reach ≥ the threshold. A grading-time decision, changeable with `regrade`. |
| `stages`         | list          | See below.                                                  |

| Stage key            | Type        | Notes                                                  |
|----------------------|-------------|--------------------------------------------------------|
| `id`                 | str         | Unique stage id (appears in the per-stage breakdown).  |
| `description`        | str         | Human label.                                           |
| `weight`             | float       | Stage weight. The trial score is the weighted **reach**, normalized (see [Metrics](metrics.md)). |
| `checks`             | list        | Each item is `{<check_name>: {<params>}}`. All must pass. |
| `continuous`         | bool        | Optional. `true` ⇒ the stage earns partial credit ∈ [0,1] from a check's `closeness` (and stops gating). Built-in checks are binary, so today this is a non-gating binary stage — forward-looking (see [Metrics](metrics.md)). |
| `gating`             | bool        | Optional, default `true`. `false` ⇒ the stage contributes its credit but does **not** absorb later stages (independent, not a pipeline). |
| `expected_tool_calls`| list[str]   | Diagnostic only, never affects the score.              |

The built-in checks are `json_with_keys` (presence) and `close_to` (correctness). Add your
own via a benchmark-local `checks.py`. See
[Rubrics & checks](../authoring/rubrics-and-checks.md).

## `harnesses/<runtime>/<provider>.yaml`

```yaml
name: orchestral/anthropic
runtime: { name: orchestral, version: ">=1.3" }
provider: { name: anthropic, max_tokens: 8192 }
core: { tools: [RunCommandTool, WriteFileTool, ReadFileTool, RunPythonTool, TodoWrite] }
loop:
  max_iterations: 150
  max_format_retries: 2
  continue_nudges: 0
  max_rate_limit_retries: 3   # backoff resumes on provider 429/529
  max_transient_retries: 4    # resume on transient transport/5xx before TRANSIENT_API_ERROR
  ux_feedback: false          # false | true | "graded"
  audit_html: false           # also emit the styled HTML audit twin
judge:                        # optional; how trials are graded
  kind: rule+llm              # rule | rule+llm | llm
  harness: orchestral/anthropic
  model: claude-opus-4-8
```

| Key        | Notes                                                                       |
|------------|----------------------------------------------------------------------------|
| `runtime`  | `{name, version}`. The name must be a registered runtime, and `version` is a PEP 440 spec enforced against the installed runtime at run start. Besides `orchestral`, the runtimes `claude_code` and `codex` ship, each with its own `runtime:` keys — see [Harnesses](../guides/harnesses.md) for the full list. Add more via `toolbench.core.runtime.register_runtime`. |
| `provider` | `{name, ...request params}`, and the provider must be registered. Model ≠ here. |
| `core`     | Exactly one of `tools: [...]` (runtime primitives) **or** `builtin: true`.  |
| `loop`     | Loop policy. The CLI loop flags override these per run.                     |
| `judge`    | Optional. See [Judge](#judge).                                              |

### Loop keys

| Key                     | Notes                                                                    |
|-------------------------|--------------------------------------------------------------------------|
| `max_iterations`        | Tool-call loop budget.                                                    |
| `max_format_retries`    | Retries on a malformed tool call before failing.                         |
| `continue_nudges`       | Presence-gated resume nudges.                                            |
| `max_rate_limit_retries`| Backoff resumes on provider 429/529.                                     |
| `max_transient_retries` | Resumes on a transient transport/5xx fault before recording `TRANSIENT_API_ERROR` (default `4`). |
| `ux_feedback`           | `false` (default), `true`, or `"graded"`. Adds one unscored UX-critique turn per trial. |
| `audit_html`            | bool. Also emit a styled HTML twin of each trial's audit log.           |

### Judge

The optional top-level `judge:` block selects how trials are graded. It is validated at load,
and unknown keys are rejected.

```yaml
judge: { kind: rule+llm, harness: orchestral/anthropic, model: claude-opus-4-8 }
```

| Key          | Notes                                                                       |
|--------------|-----------------------------------------------------------------------------|
| `kind`       | `rule` (deterministic checks only), `rule+llm` (rule primary + an LLM opinion), or `llm`. |
| `harness`    | The harness the *judge* is called through — **not** the agent under test.   |
| `model`      | Model the judge uses.                                                        |
| `max_tokens` | Optional request param for the judge call.                                   |
| `temperature`| Optional request param for the judge call.                                   |
| `artifact_chars`| Optional. Max characters of each answer/reference file the judge reads (default 8000). Raise it for large deliverables or references. |

`--judge` / `--judge-harness` / `--judge-model` on `run` and `regrade` override this per run.

Everything in `provider:` besides `name` and `cache_bust` is forwarded as a
request parameter on every model call (`max_tokens`, `temperature`, ...).
`cache_bust: true` appends a per-trial nonce to the prompt — needed only for
routes with *response-level* caching (e.g. a LiteLLM proxy with caching
enabled), where identical requests can return identical completions and the
k trials of a cell would stop being independent samples. Direct providers'
prompt/KV caches don't affect sampling, so the default is off and the model
sees the variant's prompt verbatim.

The id is the path under `harnesses/` minus `.yaml` (e.g. `orchestral/anthropic`).

## `loadouts/<name>.yaml`

```yaml
name: full_mixed
tools:
  sources:
    - python: tools/dunderkit.py
      select: [additive]              # optional bundle/tool allowlist
    - toolbase: { profile: my-profile }
      select: [calculator__add]       # ablate within the served profile
    - mcp: { command: ["npx", "@some/mcp-server"], env: { TOKEN: "${MY_TOKEN}" } }
    - mcp: { url: "https://host/mcp", headers: { Authorization: "Bearer ${TOK}" } }
skills: []
```

| Key            | Notes                                                                |
|----------------|---------------------------------------------------------------------|
| `tools.sources`| Ordered list. Each entry names exactly one backend.                 |
| `python:`      | A module import path or filesystem path exposing `TOOLS`/`make_tools`. |
| `toolbase:`    | `{profile, project_root?}`, resolved via toolbase; served toolkit versions are recorded as provenance. *(inline `toolsets:` not yet wired.)* |
| `mcp:`         | Any MCP server: `{command: [argv...], env?}` (stdio) or `{url, headers?}` (HTTP), `timeout?` seconds. Needs `toolbench[mcp]`. |
| `select:`      | Sibling of a source, keeping only these bundles/tools (toolbase/mcp: namespaced or unambiguous bare names). |
| `skills:`      | `[{name, file, mode?}]` — guide docs delivered to the agent: `on_demand` (default) copies the file into the sandbox `skills/` dir with a system-prompt pointer; `inline` embeds it in the system prompt. `file:` is benchmark-relative; a missing file fails the trial setup loudly. |

`${VAR}` in `mcp:` config values expands from the environment, so tokens live in `.env`,
not in the loadout yaml — and `headers:`/`env:` values are redacted in everything the run
persists (manifest, trial.json).

## `variants/<name>/variant.yaml`

```yaml
name: direct
description: Points given directly, pure arithmetic.
axes: { input: direct, frame: cartesian }    # free-form labels, recorded in the manifest
user_prompt_file: ./prompts/user.md
system_prompt_file: ./prompts/system.md
sandbox: { template_dir: ./sandbox/template } # omit for an empty sandbox
```

| Key                  | Notes                                                          |
|----------------------|---------------------------------------------------------------|
| `name`               | Must match the directory name.                                 |
| `axes`               | Author-chosen labels that surface in the run manifest.        |
| `user_prompt_file`   | Required, the task prompt.                                     |
| `system_prompt_file` | Optional, falls back to a generic system prompt.              |
| `sandbox.template_dir` | Optional, files copied into each trial's fresh sandbox.     |
