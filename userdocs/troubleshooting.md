# Troubleshooting

Common failures and what they mean. When in doubt, run with `--dry-run --model stub` first
— it reproduces wiring problems for \$0 and prints the resolution preview.

## "Unknown benchmark / harness / loadout / variant"

The benchmark is resolved from the `--benchmark` path (a directory with a
`benchmark.yaml`, e.g. `examples/geometry`); harnesses/loadouts/variants come from the
benchmark's own `harnesses/`, `loadouts/`, `variants/`. The error lists what *is*
available — check the path, or for a typo or a missing file. Harness ids are the path
minus `.yaml` (e.g. `orchestral/anthropic`).

## "the `toolbase:` source backend needs toolbase installed"

A loadout used a `toolbase:` source but toolbase isn't importable. Either
`pip install 'toolbench[toolbase]'` (or install an editable toolbase), or switch the source
to a `python:` module — the no-toolbase escape hatch. See
[Integrating toolbase](guides/toolbase.md).

## "the inline `toolsets:` spec is not wired yet"

The `toolbase: { toolsets: { ... } }` form isn't implemented. Author a toolbase profile and
reference it as `toolbase: { profile: NAME }` instead. The example `full_toolbase` /
`full_mixed` loadouts use the unwired form and are placeholders.

## "tool name collision: 'X' provided by both …"

Two sources (or a source and the harness core) expose the same tool name. The agent's
toolset must be unambiguous — drop one source, or `select:` a narrower set so the name
appears once.

## "`select` item 'X' matches no bundle nor tool"

A `select:` entry didn't match any bundle in the module's `BUNDLES` or any tool name. The
error lists the available bundles and tools — fix the typo. (Selects fail loudly on
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

Plot generation is best-effort — a plotting failure prints a warning but never kills the
run, and `summary.json` / `summary.txt` are still written. Confirm `matplotlib` is
installed; re-render later from the run dir.

## A grade looks wrong after I changed the rubric

You don't need to re-run the agent. `toolbench regrade --run-id <id>` replays the current
rubric against each trial's preserved `artifacts/`. If a check needs evidence that wasn't
preserved, widen what the runner keeps (the `KEEP_*` lists in `core/runner.py`).

## Building the docs

```bash
pip install 'toolbench[docs]'
mkdocs serve          # live preview at http://127.0.0.1:8000
mkdocs build          # static site into ./site
```
