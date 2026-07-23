# `orchestral` harness templates

The orchestral runtime runs the model **in-process** through a provider
SDK or proxy, so unlike the CLI runtimes the `provider:` block is fully
live: `name` selects the backend and every other key is forwarded as a
model-request parameter on each call.

One file per **provider** — the filename is the provider key, not the
model family (Gemini lives in `google.yaml`). Copy the one you need to
`<benchmark>/harnesses/orchestral/<provider>.yaml` and trim `core.tools`
to the arm's tool policy.

Registered providers are `anthropic`, `openai`, `google`, `groq`,
`litellm`, `subscription`. Others (ollama, vllm, custom routes) register
themselves via `register_provider` in an adapter module — see
`toolbench/core/llm_factory.py`.

## Every key this runtime reads

```yaml
# TEMPLATE — orchestral runtime (in-process model via a provider SDK/proxy).
# Copy into a benchmark's harnesses/orchestral/<name>.yaml. Unlike the CLI
# runtimes, provider is FULLY live here: name + model + any request param
# (max_tokens, temperature, ...) are forwarded on every model call.
name: orchestral/<name>
runtime:
  name: orchestral                  # selects this runtime (required)
  version: ">=1.3"                  # enforced against the installed orchestral
provider:
  name: anthropic                   # provider backend: anthropic|openai|litellm|... (control key)
  model: claude-opus-4-8            # request model; --models CLI arg overrides
  max_tokens: 8192                  # forwarded on every model call
  # temperature: 1.0                # any provider request param is forwarded
  # cache_bust: false               # toolbench control key: per-request cache nonce
core:
  # Explicit in-process core toolset (no toolbase builtin). Drop tools an arm bans.
  tools: [RunCommandTool, WriteFileTool, ReadFileTool, EditFileTool,
          FindFilesTool, FileSearchTool, RunPythonTool, TodoRead, TodoWrite]
loop:                               # consumed by the runner
  max_iterations: 150
  max_format_retries: 2
  continue_nudges: 0
  max_rate_limit_retries: 3
  max_transient_retries: 4
  ux_feedback: false                # post-run unscored tool-UX critique (dev aid)

# provider control keys (configure toolbench, NOT sent to the model): name, cache_bust.
# All other provider keys are forwarded verbatim as model-request params.
```
