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
```

- **`runtime`** is the agent framework and a PEP 440 version constraint, enforced against
  the installed runtime when the run starts (a failing pin aborts before any trial spends
  tokens).
- **`provider`** is the LLM provider plus default request params. The *model id* comes from
  `--models`, not from here.
- **`core`** is either a `tools:` list of runtime primitives **or** `builtin: true` for a
  runtime that ships its own (e.g. `claude-code`). Exactly one of the two.
- **`loop`** is the retry/iteration policy: `max_iterations`, `max_format_retries`,
  `continue_nudges`, `max_rate_limit_retries`, `max_transient_retries` (resume on
  transient API/transport errors), `ux_feedback` (`false` | `true` | `"graded"` — a
  post-run, unscored tool-UX critique), and `audit_html` (bool — render an HTML audit
  twin) — the defaults the corresponding CLI flags override. A `loop:` key the runner
  doesn't consume triggers a loud warning, so a knob that governs nothing can't silently
  mislabel a run's conditions.

A harness may also carry a top-level **`judge:`** block (`{kind, harness, model}`) that
pins the default judge for runs on this harness; `--judge*` overrides it field by field.
See [Choosing a judge](running-a-benchmark.md#choosing-a-judge).

## Choosing a harness and model

```bash
toolbench run --benchmark examples/geometry \
    --harness orchestral/anthropic \
    --models claude-haiku-4-5,claude-sonnet-4-6 \
    --loadouts full_local --n 3 --max-cost-usd 2.00
```

Omit `--harness` to use the benchmark's `default_harness`. Sweep `--harnesses` to compare
runtimes (the harness id becomes part of the condition label).

## Runtimes

toolbench registers three runtimes. The one above, `orchestral`, calls a provider SDK
in-process and drives the tool-call loop itself. The other two shell out to a coding-agent
CLI that owns the model request and ships its own core tools.

- **`orchestral`** — the in-process loop. `provider:` is fully live and `core:` is an
  explicit `tools:` list you curate per arm.
- **`claude_code`** — drives the `claude -p` CLI. Auth is the logged-in Claude CLI
  (subscription), so **no API key** — it strips `ANTHROPIC_API_KEY` from the subprocess
  env, and the run incurs no per-token API cost. Shipped harness id
  `claude-code/default`. `core: {builtin: true}` — the runtime ships its own
  Bash/Write/Edit/Read/Glob/Grep/TodoWrite; a loadout's `toolbase:` loadout is served to
  it over MCP by a `toolbase serve` subprocess (see [Integrating toolbase](toolbase.md)).
- **`codex`** — drives `codex exec --json`. Subscription auth, strips `OPENAI_API_KEY`.
  Shipped harness id `codex/default`. `core: {builtin: true}` uses Codex's own tools; the
  model comes from `--models` on the CLI, and a loadout's `toolbase:` loadout is likewise
  served over MCP.

Both CLI runtimes use the credential-free `subscription` provider: `provider:` is a
placeholder, only `{name, model}` apply, and request params like `max_tokens` are inert.
Start from the copy-paste templates in `harness_templates/` (one directory per runtime,
each with a `README.md` listing every key it reads).

### `runtime:` config keys

Beyond `name` and the `version` pin, each CLI runtime reads a curated set of `runtime.*`
keys. **Unknown keys are silently ignored** (as ever). Correctness-critical flags
(output format, permission mode, allowed tools, MCP wiring) stay hardcoded and are not
overridable.

`claude_code`:

| Key                | Effect                                                                    |
|--------------------|---------------------------------------------------------------------------|
| `call_timeout_s`   | `toolbase serve --call-timeout` (per-MCP-call wall, default 3600)         |
| `env`              | mapping → subprocess env (YAML bools become `"true"`/`"false"`)           |
| `disallowed_tools` | list → `--disallowedTools`                                                |
| `effort`           | `--effort`: `low` \| `medium` \| `high` \| `xhigh` \| `max`               |
| `fallback_model`   | `--fallback-model` when the model is overloaded                           |
| `max_budget_usd`   | `--max-budget-usd` per-session spend cap                                  |
| `add_dir`          | list → `--add-dir` (extra dirs the tools may access)                      |
| `sandbox`          | bool → macOS Seatbelt Bash sandbox confining writes to the sandbox + `/tmp` |
| `sandbox_deny`     | list → extra deny-read paths under the sandbox                            |

`codex`:

| Key                | Effect                                                                    |
|--------------------|---------------------------------------------------------------------------|
| `call_timeout_s`   | `toolbase serve --call-timeout` (per-MCP-call wall, default 3600)         |
| `env`              | mapping → subprocess env                                                  |
| `sandbox`          | `-s/--sandbox`: `read-only` \| `workspace-write` \| `danger-full-access` (default `workspace-write`) |
| `reasoning_effort` | str → `model_reasoning_effort`                                            |

### Protected paths

toolbench passes the benchmark tree — which holds the ground-truth answer key — to the
runtime as **protected paths**. When a CLI runtime's `sandbox` is enabled it deny-reads
them (a Seatbelt `denyRead` for `claude_code`, a native Codex permission profile for
`codex`), so a trial can't read the answer key. This pairs with the post-run integrity
scan, which quarantines any trial whose tool-call inputs reached the answer key.

### Runtime version capture

For a CLI runtime, toolbench shells `claude --version` / `codex --version` at run start and
records the result under `runtime_versions` in the manifest. It surfaces in the summary
provenance line alongside the toolbase toolkit versions and the git SHA, so a result is
traceable to the exact CLI that produced it. (This is the *driver* version — a served
toolbase toolkit reports its own versions separately; see
[Version provenance](toolbase.md#version-provenance).)

## Providers

The provider named in a harness must be registered. toolbench ships factories for
`anthropic`, `openai`, `google`, `groq`, and `litellm`, plus `stub` (the zero-cost
dry-run LLM) and `subscription` (the credential-free placeholder the CLI runtimes use —
see [Runtimes](#runtimes) — where the coding agent, not a provider SDK, owns the model
request). Provider API keys come from the environment, or a `.env` at the repo root.
`litellm` additionally reads a pricing snapshot so cost is reported even when the proxy
doesn't return it. To add your own runtime or provider, see
[Custom tools & providers](../authoring/custom-tools.md).

## Core tools vs. loadout tools

The agent's full toolset is **harness core ∪ loadout**. The harness supplies general
primitives every task needs (write a file, run a script), and the [loadout](loadouts.md)
supplies the *domain* tools you're actually measuring. A tool name may not be provided by
both at once. toolbench errors on a collision so a run is never silently ambiguous.
