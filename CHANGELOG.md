# Changelog

All notable changes to `toolbench` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

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
