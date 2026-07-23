# Troubleshooting

Common failures and what they mean. When in doubt, run with `--dry-run --model stub` first.
It reproduces wiring problems for \$0 and prints the resolution preview.

## "Unknown benchmark / harness / loadout / variant"

The benchmark is resolved from the `--benchmark` path (a directory with a
`benchmark.yaml`, e.g. `examples/geometry`). Harnesses, loadouts, and variants come from the
benchmark's own `harnesses/`, `loadouts/`, and `variants/`. The error lists what *is*
available, so check the path, or for a typo or a missing file. Harness ids are the path
minus `.yaml` (e.g. `orchestral/anthropic`).

## "the `toolbase:` source backend needs toolbase installed"

A loadout used a `toolbase:` source but toolbase isn't importable. Either
`pip install 'toolbench[toolbase]'` (or install an editable toolbase), or switch the source
to a `python:` module, the no-toolbase escape hatch. See
[Integrating toolbase](guides/toolbase.md).

## "the `mcp:` source backend needs the MCP SDK"

A loadout used an `mcp:` source but the `mcp` package isn't importable. Install it with
`pip install 'toolbench[mcp]'`. Connection failures (bad `command:`, unreachable `url:`,
rejected auth) surface in the `--dry-run` resolution preview before any model is called.

## "the inline `toolsets:` spec is not wired yet"

The `toolbase: { toolsets: { ... } }` form isn't implemented. Author a toolbase profile and
reference it as `toolbase: { profile: NAME }` instead. The example `full_toolbase` /
`full_mixed` loadouts use the unwired form and are placeholders.

## "tool name collision: 'X' provided by both …"

Two sources (or a source and the harness core) expose the same tool name. The agent's
toolset must be unambiguous, so drop one source, or `select:` a narrower set so the name
appears once.

## "`select` item 'X' matches no bundle nor tool"

A `select:` entry didn't match any bundle in the module's `BUNDLES` or any tool name. The
error lists the available bundles and tools, so fix the typo. (Selects fail loudly on
purpose, so a silent empty toolset never happens.)

## A run aborts immediately on budget

`--max-cost-usd` is a hard cap and the run aborts the moment spend would exceed it. A `0`
cap only makes sense with `--dry-run`. Raise the cap, or lower `--n` / the number of cells.

## No provider key / auth errors

Provider API keys come from the environment (or a `.env` at the repo root), loaded before
anything reads `os.environ`. Set the key your harness's provider needs
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, …). For a wiring check that needs no key, use
`--model stub --dry-run`.

## Plots didn't render

Plot generation is best-effort. A plotting failure prints a warning but never kills the
run, and `summary.json` / `summary.txt` are still written. Confirm `matplotlib` is
installed, then re-render later from the run dir.

## A grade looks wrong after I changed the rubric

You don't need to re-run the agent. `toolbench regrade --run-id <id>` replays the current
rubric against each trial's preserved `artifacts/`. If a check needs evidence that wasn't
preserved, widen what the runner keeps (the `KEEP_*` lists in `core/runner.py`).

## A failure mode I don't recognize

Every completed trial carries exactly one `failure_mode` label (it shows up in `summary.txt`,
plot legends, and each trial's `grade.json`). Most are **operational**, they mean the
provider or transport misbehaved, not that the agent lacked the capability, so don't read them
as a capability signal.

- **`MODEL_FORMAT_CRASH`** — the model emitted malformed tool-call JSON (empty/truncated
  arguments, leaked channel markers, raw source where a string belongs). Covers **both**
  shapes of the same defect: a client-side JSON decode error and a provider-side rejection
  (Groq returns HTTP 400 `tool_use_failed` / echoes `failed_generation`). Auto-retried up to
  the harness's `max_format_retries`; if it still can't parse, the trial ends here.
- **`INTEGRITY_LEAK`** — the trial's tool calls referenced the ground-truth answer key, so it
  is quarantined: scored 0, excluded from the headline, with the pre-quarantine score kept as
  `score_pre_integrity` and the offending calls in `integrity_evidence`. Fix by running under
  a sandbox-confining harness (`sandbox: true`) or keeping ground truth outside any reachable
  path. (This label is assigned as a literal by the run finalizer, not part of the closed
  `failure_modes.py` vocabulary.)
- **`CONTEXT_LENGTH_EXCEEDED`** — the conversation outgrew the model's context window and the
  provider rejected the request. Operational, not a capability signal; shorten the task,
  trim the toolset, or use a longer-context model.
- **`RATE_LIMITED`** — the provider throttled (429) or shed load (529/overloaded) and the
  runner's bounded backoff was exhausted. Raise `--max-rate-limit-retries` or slow the run
  with a lower `--parallel`.
- **`TRANSIENT_API_ERROR`** — a transient transport/server fault reaching the provider
  (connect/read timeout, dropped connection, HTTP 5xx) that survived backoff. Raise
  `--max-transient-retries`; a single endpoint blip shouldn't contaminate a campaign.

The rest of the closed vocabulary, for reference: **`AGENT_CRASH`** (an uncaught, unclassified
exception from the agent loop), **`MODEL_STOPPED_EARLY`** (the model returned a plain message
instead of the next expected tool call, i.e. thought it was done while the rubric was still
incomplete), **`GRADE_ERROR`** (the judge raised while scoring an otherwise-complete
trajectory), **`INCOMPLETE_AT_<STAGE_ID>`** (every stage up to but not including the named one
passed; the named one failed), and **`NONE`** (all rubric stages passed).

## "an LLM judge needs a harness"

You asked for an LLM judge (`--judge llm` / `rule+llm`, or a harness `judge:` block) without
telling it where to run. An LLM judge is addressed as an `(harness, model)` pair, so pass
`--judge-harness` (e.g. `orchestral/anthropic`, `claude-code/default`) or set
`judge: { harness: ... }` in the harness config. The error is raised at setup, before any
trial runs.

## Building the docs

```bash
pip install 'toolbench[docs]'
mkdocs serve          # live preview at http://127.0.0.1:8000
mkdocs build          # static site into ./site
```
