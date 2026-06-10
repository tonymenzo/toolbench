# Harnesses & models

A **harness** is the agent runtime. It drives the tool-call loop, manages context, talks to
a provider, and supplies the **core tools** (file I/O, shell, run-python). The **model** is
chosen separately on the command line. Keeping them apart means you can sweep models under
a fixed runtime, or compare runtimes under a fixed model.

## A harness file

Harnesses live per benchmark under `harnesses/<runtime>/<provider>.yaml`, and the id is the
path minus `.yaml`. The example `geometry` benchmark ships `orchestral/anthropic`:

```yaml
name: orchestral/anthropic
runtime:
  name: orchestral
  version: ">=1.3"
provider:
  name: anthropic
  max_tokens: 8192          # default request params for this provider
core:
  tools: [RunCommandTool, WriteFileTool, ReadFileTool, RunPythonTool, TodoWrite]
loop:
  max_iterations: 150       # agent.run round-trip cap
  max_format_retries: 2     # resume on a malformed tool-call (serialization) crash
  continue_nudges: 0        # presence-gated resumes when a deliverable is missing (0 = strict)
  on_tool_error: retry
  max_retries: 2
```

- **`runtime`** is the agent framework and a PEP 440 version constraint, checked at trial
  setup.
- **`provider`** is the LLM provider plus default request params. The *model id* comes from
  `--models`, not from here.
- **`core`** is either a `tools:` list of runtime primitives **or** `builtin: true` for a
  runtime that ships its own (e.g. `claude-code`). Exactly one of the two.
- **`loop`** is the retry/iteration policy. These are the defaults the CLI's
  `--max-iterations` / `--max-format-retries` / `--continue-nudges` flags override.

## Choosing a harness and model

```bash
toolbench run --benchmark examples/geometry \
    --harness orchestral/anthropic \
    --models claude-haiku-4-5,claude-sonnet-4-6 \
    --loadouts full_local --n 3 --max-cost-usd 2.00
```

Omit `--harness` to use the benchmark's `default_harness`. Sweep `--harnesses` to compare
runtimes (the harness id becomes part of the condition label).

## Providers

The provider named in a harness must be registered. toolbench ships factories for
`anthropic`, `openai`, `google`, `groq`, and `litellm`, plus `stub` (the zero-cost
dry-run LLM). Provider API keys come from the environment, or a `.env` at the repo root.
`litellm` additionally reads a pricing snapshot so cost is reported even when the proxy
doesn't return it. To add your own runtime or provider, see
[Custom tools & providers](../authoring/custom-tools.md).

## Core tools vs. loadout tools

The agent's full toolset is **harness core ∪ loadout**. The harness supplies general
primitives every task needs (write a file, run a script), and the [loadout](loadouts.md)
supplies the *domain* tools you're actually measuring. A tool name may not be provided by
both at once. toolbench errors on a collision so a run is never silently ambiguous.
