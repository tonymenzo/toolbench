# Changelog

All notable changes to `toolbench` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

---

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

- Campaign runtime & metrics hardening.
