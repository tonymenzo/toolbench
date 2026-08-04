# Changelog

All notable changes to `toolbench` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

## [0.6.0] — 2026-08-03

### Changed (breaking)

- **A `toolbase:` source names a `loadout:`, not a `profile:`.** toolbase renamed profiles to loadouts in its 0.12 and removed the old spellings outright, so every call toolbench made into it raised. Both tool-resolution paths were dead: the in-process orchestral bridge (`toolbase_tools(profile=...)` → `TypeError`) and the MCP subprocess the `claude_code` / `codex` runtimes spawn (`toolbase serve --profile` → `No such option`). Any benchmark with a `toolbase:` source could not resolve its tools at all.

  ```yaml
  sources:
    - toolbase: { loadout: default }    # was: { profile: default }
  ```

  The runtime kwarg and attribute are `loadout` too — toolbase adopted this word *because* it was already toolbench's, so a `toolbase_`-prefixed variant would reintroduce the translation the rename removed.

  No alias: `profile:` raises and names its replacement. Silence would be worse here than usual — a benchmark whose tools fail to resolve doesn't error, it runs as a *tool-less arm* and grades as a valid condition, so a stale config would quietly turn a comparison into a measurement of the model alone.

  Requires `toolbase>=0.12`; the extra was pinned at `>=0.3.0`, which no longer describes anything that works.

### Fixed

- **The toolbase tests validated toolbench against a toolbase that no longer existed.** They run against a fake module injected into `sys.modules`, because toolbase is an optional dependency and the suite has to pass without it. The fake declared the signature toolbench *believed* toolbase had, so when toolbase changed, the two drifted together and the suite stayed green over a completely broken integration — which is how the breakage above went unnoticed through a release.

  The fake now tracks the real signature, and a new contract test asserts against the installed toolbase directly — kwargs the resolver passes by name, flags the CLI runtimes put in argv, and the discovery attributes provenance reads. It skips when toolbase isn't installed, so the no-toolbase path is unchanged. Verified by simulating a regressed toolbase: the contract tests fail, where before nothing did.

- **Setup instructions used a flag that no longer exists.** The toolbase guide told you to run `tb install -e examples/calculator -g -a`; `-g` was removed in toolbase 0.12 in favour of `-u`, and `-a` now activates the *project* rather than the user-level loadout. Both the guide and the manual said otherwise.

## [0.5.0] — 2026-07-28

### Fixed

- **Subscription runs no longer book spend they never incurred.** The `claude`
  CLI prints `total_cost_usd` on every run — an API-equivalent figure, not money
  drawn — and the runner fed it straight into the budget tracker. A five-trial
  haiku cell therefore reported $1.29/trial and an opus cell $5.31/trial against
  caps that nothing was drawing down, and a long enough run could abort on a
  budget it was not consuming. Codex only ever looked correct because its CLI
  emits no cost field at all, so the gap stayed invisible until a claude-code and
  a codex run were compared in the same campaign.

  A harness declaring `provider.name: subscription` now zeroes the trajectory
  cost before the budget sees it and preserves the CLI figure as
  `estimated_api_equivalent_cost_usd` on the trial record, alongside the
  token-based estimate the summary already computed for runtimes reporting no
  cost. Metered API harnesses are unaffected.

### Changed

- **The summary states the billing mode outright** rather than leaving it
  inferable from a `$0.00` spend: `$0.00 spent — SUBSCRIPTION (no metered API
  spend)`, plus the API-equivalent when known. A subscription run and a run that
  happened to cost nothing are different claims, and only one of them is
  reproducible from a budget cap. `summary["subscription_harnesses"]` records
  which harnesses ran unmetered.

- **`--models` / `--model` is now optional** when every selected harness pins its
  own `provider.model`. A campaign defining one harness per (runtime × model)
  puts the model in the manifest as configuration; requiring the flag again asks
  for the same fact twice and invites the two disagreeing — the flag silently
  won, so a harness named for one model could run another. When omitted the model
  is taken from the harness and echoed as `models: <id> (from harness
  provider.model)`. Selecting several harnesses that pin *different* models
  without `--models` is an error rather than a cross product, since sweeping the
  harness and model axes together is the opposite of what a per-model harness
  means. Passing `--models` explicitly sweeps exactly as before.

### Upgrade note

`--max-cost-usd` no longer bounds a run on a subscription harness, because there
is no metered spend to cap. The flag is still required and still bounds metered
harnesses; on subscription harnesses, wall-clock and trial count are the real
limits.

## [0.4.0] — 2026-07-28

### Added

- **`toolbench export`** — turn a completed run into something you can share.
  A run directory is a working artifact: gigabytes of transcripts and
  intermediate data carrying absolute paths from the machine that produced it,
  which is not a thing you can attach to a paper or render a results page from.
  `export` writes two layers instead:

  - `trials.jsonl` — one flat, denormalized, **schema-versioned** row per trial
    (cell coordinates, score, pass/fail against the rubric's own threshold,
    per-stage credits/weights/metrics, telemetry, provenance). Kilobytes.
    Denormalized on purpose so a consumer needs no join logic and no knowledge
    of toolbench's internal layout.
  - `bundle/` — the graded evidence behind those rows: per-trial answer files,
    audit logs, run summaries, manifest. Megabytes.

  Transcripts are excluded by default (`--include-transcripts` opts in): they
  dominate the size and, being binary, are copied verbatim rather than scrubbed.
  Machine-specific absolute paths are rewritten to `${HOME}` / `${RUN}`
  placeholders unless `--no-scrub` is passed. `--archive` also writes a
  `.tar.gz`. On a representative 5-trial run this is 2.7 GB -> 13 MB (1.8 MB
  compressed).

  `schema_version` is the compatibility contract: additive changes bump the
  minor, anything that moves or retypes an existing field bumps the major.

## [0.3.0] — 2026-07-24

### Added

- **MCP preflight verification.** Before any trial runs (and in `--dry-run`), a
  run now verifies that every MCP-serving harness × tools-loadout can actually
  reach its tools: it starts the loadout's `toolbase serve --profile …` exactly
  as the CLI runtime would and completes an MCP `initialize` + `tools/list`
  handshake. If the profile serves zero tools, the handshake times out, or the
  resolved tools are missing, the run **aborts with exit 2 before executing a
  single trial** and prints which cell failed. Previously a `tools` loadout that
  could not reach its tools ran the entire arm silently tool-less and still
  graded as a valid tools result. Verification is gated to the runtimes that
  serve toolbase over MCP (`claude_code`, `codex`) via
  `runtime_serves_toolbase_mcp`; in-process runtimes (orchestral) resolve tools
  directly and are unaffected. New helpers: `verify_toolbase_mcp`,
  `runtime_serves_toolbase_mcp`.

### Fixed

- **`_toolbase_command()` now fails loudly.** When the `toolbase` executable
  cannot be located (neither on `PATH` nor beside the running Python), it raises
  with an actionable message instead of returning a bare `"toolbase"`. The bare
  fallback silently produced a broken `.mcp.json` whose stdio server failed to
  launch (`command not found` on the child's `PATH`), leaving the agent with
  "No such tool available" while the trial still graded as if tools were
  present — a failure mode that surfaced when an env's `toolbase` install was
  in flux. Combined with the preflight above, an unresolvable toolbase is now
  caught before the benchmark begins.

## [0.2.1] — 2026-07-24

Runtime and metrics hardening surfaced by the first publication benchmarking campaign.

### Added

- **`SESSION_LIMIT` failure mode.** A subscription coding-agent CLI (`claude-code` / `codex`) that refuses a request because the logged-in account hit its plan's session/usage quota is no longer misfiled as an `AGENT_CRASH` scored 0. It is classified as a distinct `SESSION_LIMIT` mode (detected from the CLI limit text, checked before `RATE_LIMITED` so the API's `insufficient_quota` still maps to `RATE_LIMITED`), dropped from the scored population (reach / pass@k / stage funnel / paired deltas) via `EXCLUDED_FROM_METRICS`, and re-run on `resume`. The runner aborts the remaining queue on the first one — every later trial would fail identically until the quota resets — and reports the abort reason. Excluded rows stay on disk and surface as per-cell and run-level excluded counts. ([#23](https://github.com/tonymenzo/toolbench/pull/23))
- `gpt-5.6-sol` subscription API-equivalent pricing entry for cost estimation.

### Changed

- **Benchmark agents can no longer read Toolbase toolkit sources off disk.** A trial's `protected_paths` now also deny the agent read access to editable toolkit checkouts (`source_path` in each cached toolkit's `.install_meta.yaml`) and toolkit runtime data directories (`base_directory` in toolkit configs). Without this, an agent in a `core_only` control arm could bypass the MCP interface and import the real tool implementations straight off the filesystem, contaminating the control. MCP server children are not confined by the agent's shell filesystem profile, so declared Toolbase loadouts still execute normally.

### Fixed

- **`claude-code` runtime ignored the `--models` sweep** — every cell ran the harness provider's default model instead of the requested one, so a model axis silently collapsed to a single model. The runtime now honors the swept model.
- **Toolbase MCP command resolution.** Child MCP processes launched a bare `toolbase` command, which failed when Toolbench was started with an absolute virtualenv Python whose `bin/` was not on `PATH`. The command is now resolved to an absolute path (`PATH` → the executable beside the running Python → bare command as a last-resort fallback so the CLI can report a normal MCP startup error).

## [0.2.0] — 2026-06-05

- Continuous / non-gating scoring, `codex` and `claude-code` subscription runtimes, selectable LLM judge, trajectory integrity auditor, subscription cost estimation, and parallel-run hardening.

---

Versions before 0.2.0 predate this changelog; see the git history and release commit subjects for their contents.
