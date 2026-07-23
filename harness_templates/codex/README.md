# `codex` harness templates

The `codex` runtime shells out to `codex exec --json` under a ChatGPT
subscription. Like `claude-code` it is **provider-locked**: only
`name` identifies subscription auth; the model comes from `toolbench run
--models`. Request params are inert.

Not plumbed for codex: `disallowed_tools`, `effort`, `fallback_model`,
`max_budget_usd`, `add_dir` — those are `claude_code`-only curated flags.

Copy `default.yaml` to `<benchmark>/harnesses/codex/<name>.yaml`.

## Every key this runtime reads

```yaml
# TEMPLATE — codex runtime (drives `codex exec --json` under a ChatGPT
# subscription). Copy into a benchmark's harnesses/codex/<name>.yaml and keep
# only what you override. The keys below are the ONLY ones this runtime reads.
name: codex/<name>
runtime:
  name: codex                       # selects this runtime (required)
  call_timeout_s: 3600              # per-MCP-call timeout (seconds)
  sandbox: workspace-write          # -s/--sandbox: read-only|workspace-write|danger-full-access
  reasoning_effort: medium          # low|medium|high|xhigh|max; explicit for reproducibility
  env:                              # any var, set on the codex subprocess
    # SOME_VAR: "value"
provider:
  name: subscription                # credential-free placeholder llm (never called)
  # Model comes from `toolbench run --models`; request params are not accepted.
core:
  builtin: true                     # use Codex's own core tools
loop:                               # consumed by the runner (same knobs as claude_code)
  max_format_retries: 2
  continue_nudges: 0
  max_rate_limit_retries: 3
  max_transient_retries: 4
  ux_feedback: false                # post-run unscored tool-UX critique (dev aid)

# NOT plumbed for codex: disallowed_tools, fallback_model, max_budget_usd,
#   add_dir (these are claude_code-only curated flags).
# HARDCODED: --ignore-user-config; codex exec --json; MCP via
#   -c mcp_servers.*; codex exec resume <id>.
```
