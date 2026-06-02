# Schemas

The declarative files that make up a benchmark. All are YAML; paths inside them resolve
relative to the file's benchmark directory.

## `benchmark.yaml`

The task + grading spec. Required: `name`, `rubric`, at least one harness/loadout/variant
on disk.

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
```

| Key               | Type   | Notes                                              |
|-------------------|--------|----------------------------------------------------|
| `name`            | str    | `--benchmark` id; defaults to directory name.      |
| `version`         | str    | Free-form; recorded in the manifest.               |
| `description`     | str    | One line.                                          |
| `default_harness` | str    | Harness id used when `--harness` is omitted.       |
| `default_loadout` | str    | Loadout used when `--loadouts` is omitted.         |
| `default_variant` | str    | Variant used when `--variants` is omitted.         |
| `ground_truth.dir`| path   | Reference-answer directory.                         |
| `rubric`          | map    | See [Rubric](#rubric).                              |
| `checks`          | path   | Optional module exposing `CHECKS` (+ `ROLES`).     |

## Rubric

```yaml
rubric:
  type: stagewise                    # only stagewise is supported
  stages:
    - id: answer_written
      description: output/answer.json exists with required keys
      weight: 0.2
      checks:
        - json_with_keys: { file: output/answer.json, required_keys: [distance, midpoint] }
      expected_tool_calls: [add]     # optional, non-graded diagnostic
```

| Stage key            | Type        | Notes                                                  |
|----------------------|-------------|--------------------------------------------------------|
| `id`                 | str         | Unique stage id (appears in the per-stage breakdown).  |
| `description`        | str         | Human label.                                           |
| `weight`             | float       | Stage weight; score is the prefix product, normalized. |
| `checks`             | list        | Each item is `{<check_name>: {<params>}}`. All must pass. |
| `expected_tool_calls`| list[str]   | Diagnostic only — never affects the score.             |

Built-in checks: `json_with_keys` (presence), `close_to` (correctness). Add your own via a
benchmark-local `checks.py`. See [Rubrics & checks](../authoring/rubrics-and-checks.md).

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
  on_tool_error: retry
  max_retries: 2
```

| Key        | Notes                                                                       |
|------------|----------------------------------------------------------------------------|
| `runtime`  | `{name, version}` — PEP 440 version checked at trial setup.                 |
| `provider` | `{name, ...request params}` — provider must be registered. Model ≠ here.    |
| `core`     | Exactly one of `tools: [...]` (runtime primitives) **or** `builtin: true`.  |
| `loop`     | Loop policy; the CLI loop flags override these per run.                     |

The id is the path under `harnesses/` minus `.yaml` (e.g. `orchestral/anthropic`).

## `loadouts/<name>.yaml`

```yaml
name: full_mixed
tools:
  sources:
    - python: tools/dunderkit.py
      select: [additive]              # optional bundle/tool allowlist
    - toolbase: { profile: my-profile }
skills: []
```

| Key            | Notes                                                                |
|----------------|---------------------------------------------------------------------|
| `tools.sources`| Ordered list; each entry names exactly one backend.                 |
| `python:`      | A module import path or filesystem path exposing `TOOLS`/`make_tools`. |
| `toolbase:`    | `{profile, project_root?}` — resolved via toolbase. *(inline `toolsets:` not yet wired.)* |
| `select:`      | Sibling of a source: keep only these bundles/tools.                 |
| `skills:`      | Optional recipe/guide docs exposed to the agent.                    |

## `variants/<name>/variant.yaml`

```yaml
name: direct
description: Points given directly; pure arithmetic.
axes: { input: direct, frame: cartesian }    # free-form labels, recorded in the manifest
user_prompt_file: ./prompts/user.md
system_prompt_file: ./prompts/system.md
sandbox: { template_dir: ./sandbox/template } # omit for an empty sandbox
```

| Key                  | Notes                                                          |
|----------------------|---------------------------------------------------------------|
| `name`               | Must match the directory name.                                 |
| `axes`               | Author-chosen labels; surface in the run manifest.            |
| `user_prompt_file`   | Required — the task prompt.                                    |
| `system_prompt_file` | Optional — falls back to a generic system prompt.             |
| `sandbox.template_dir` | Optional — files copied into each trial's fresh sandbox.    |
