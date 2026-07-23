# Harness templates

A harness answers one question: **what drives the model, and how does the
loop behave?** It is the `harness` axis of a run
(`benchmark × harness × loadout × variant × model`).

These are copy-paste starting points, not a registry — nothing in toolbench
reads this directory. Copy a file into a benchmark and edit it:

```
<benchmark>/harnesses/<runtime>/<name>.yaml
```

The path is the harness id: `harnesses/orchestral/groq.yaml` is referred to
as `--harness orchestral/groq`.

## Layout

One directory per **runtime**. Each has a `README.md` documenting every key
that runtime reads, plus runnable templates.

```
claude-code/     default.yaml                       provider-locked (subscription)
codex/           default.yaml                       provider-locked (subscription)
orchestral/      anthropic.yaml  openai.yaml        provider-agnostic:
                 google.yaml     groq.yaml            one template per provider
                 litellm.yaml
```

## Runtime vs. provider

The distinction drives everything else:

- **CLI runtimes** (`claude_code`, `codex`) shell out to a coding agent that
  owns the model request. `provider:` is a credential-free placeholder and
  only `{name, model}` apply — request params like `max_tokens` are inert.
  `core: {builtin: true}` uses the agent's own tools.
- **In-process runtimes** (`orchestral`) call a provider SDK or proxy
  directly. `provider:` is fully live: `name` selects the backend and every
  other key is forwarded as a model-request parameter on each call. `core:`
  is an explicit tool list you curate per arm.

Because orchestral is the provider-agnostic one, it gets a template per
provider. The filename is the **provider key**, not the model family — Gemini
lives in `google.yaml`. Registered providers: `anthropic`, `openai`,
`google`, `groq`, `litellm`, `subscription`. Others (ollama, vllm, custom
routes) register themselves via `register_provider`; see
`toolbench/core/llm_factory.py`.

## Two things that bite

**`cache_bust` on proxy routes.** `provider:` keys are forwarded to the model
*except* the control keys `name` and `cache_bust`. A proxy with response-level
caching hands every trial in a cell the same completion — k trials stop being
independent samples, and pass@k / pass^k silently collapse into reach. Set
`cache_bust: true` on any proxy route (it's on in `litellm.yaml`). Direct
providers don't need it: their prompt/KV caches don't affect sampling.

**`--max-cost-usd` binds only when *something* prices the model.** Cost
resolves through a three-step cascade (`runner.py:_extract_usage`):

1. `usage.cost` on the Response — orchestral's own per-provider pricing model.
   This is where `anthropic`, `openai`, `google` and `groq` all get priced.
2. The LiteLLM proxy's pricing snapshot, fetched at run start — proxy routes
   only, and only if the proxy is reachable at that moment.
3. `toolbench.core.metrics.PRICING_TABLE` — a static last resort.

Step 1 is an **exact model-id string match with no `default` rate**, so an
unrecognized id costs `0.0` rather than raising. Combined with steps 2 and 3
missing, the run reports `$0.00 spent` and the cap never trips — silently.
Always pass the fully-qualified id the provider's pricing table uses
(`openai/gpt-oss-120b`, not `gpt-oss-120b`), and sanity-check the reported
spend on the first run of any new route.

Note that step 3 is reached through a provider *guess* from the model name
(`"claude" -> anthropic`, `"gpt" -> openai`), so `PRICING_TABLE` can never be
consulted for a `groq` or `google` key — adding entries there would not help.
Price a new provider in orchestral's pricing model instead.

## Curating the tool set

`core.tools` is where a harness enforces arm policy, and it is the natural
place to drop tools that would function as an oracle:

- `WebSearchTool` — remove when the answer is published anywhere.
- `RunCommandTool` — a shell is the natural way to `cat`/`find` the ground
  truth; remove it when the task never needs one.

A "closed-book" arm is just a harness whose `core.tools` has no execution
tools at all — file tools only, so the model must work in-context and can
still deposit its answer.
