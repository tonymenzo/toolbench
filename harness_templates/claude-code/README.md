# `claude-code` harness templates

The `claude_code` runtime shells out to `claude -p ... --output-format
stream-json` under a Claude subscription. It is **provider-locked**: the
`provider:` block is a credential-free placeholder and only `{name, model}`
apply — the CLI owns the model request, so `max_tokens` and friends are
inert here (cap output with `env: CLAUDE_CODE_MAX_OUTPUT_TOKENS`).

Copy `default.yaml` to `<benchmark>/harnesses/claude-code/<name>.yaml`.

## Every key this runtime reads

```yaml
# TEMPLATE — claude_code runtime (drives `claude -p` under a subscription).
# Copy into a benchmark's harnesses/claude-code/<name>.yaml and keep only what
# you override. The keys below are the ONLY ones this runtime reads; anything
# else is ignored. Correctness-critical flags are hardcoded (see bottom).
name: claude-code/<name>
runtime:
  name: claude_code                 # selects this runtime (required)
  call_timeout_s: 3600              # per-MCP-call timeout (seconds)
  disallowed_tools: [WebSearch, WebFetch]  # --disallowedTools
  # effort: high                    # --effort: low|medium|high|xhigh|max
  # fallback_model: claude-sonnet-5 # --fallback-model when the model is overloaded
  # max_budget_usd: 5               # --max-budget-usd: per-session spend cap
  # add_dir: [../shared]            # --add-dir: extra dirs the tools may access
  env:                              # any var, set on the claude subprocess
    ENABLE_TOOL_SEARCH: "false"     # eager-load MCP tools; no ToolSearch deferral
    # MAX_THINKING_TOKENS: "31999"          # extended-thinking token budget
    # CLAUDE_CODE_MAX_OUTPUT_TOKENS: "8192" # per-response output-token cap
provider:                           # CLI runtime: only {name, model} apply
  name: subscription                # credential-free placeholder llm (never called)
  model: claude-haiku-4-5           # --model default; --models CLI arg overrides
  # provider.max_tokens and other request params are INERT here — the claude CLI
  # owns the model request. Cap output via env CLAUDE_CODE_MAX_OUTPUT_TOKENS.
core:
  builtin: true                     # use Claude Code's own core tools
loop:                               # consumed by the runner (not the CLI)
  max_iterations: 150               # agent.run() round-trip cap
  max_format_retries: 2             # resume on MODEL_FORMAT_CRASH (bad tool-call JSON)
  continue_nudges: 0                # presence-gated resumes on early stop (0 = strict)
  max_rate_limit_retries: 3         # resume on provider 429/529 with backoff
  max_transient_retries: 4          # resume on transient API/transport errors
  ux_feedback: false                # post-run unscored tool-UX critique; false |
                                    # true | graded (graded shows the agent its
                                    # score + stage pass/fail first, no evidence)

# HARDCODED (not overridable from this file):
#   -p, --output-format stream-json, --verbose    trajectory capture
#   --permission-mode acceptEdits                 non-interactive
#   --allowedTools (builtins + mcp__<server>__*)  oracle hygiene + loadout
#   --mcp-config, --strict-mcp-config, --resume   toolbase wiring + session resume
#   env MCP_TOOL_TIMEOUT, MCP_TIMEOUT; ANTHROPIC_API_KEY stripped
```
