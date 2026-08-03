# Install & quickstart

## Install

```bash
pip install toolbench                 # the framework + CLI
pip install 'toolbench[toolbase]'     # + resolve tools from toolbase loadouts
pip install 'toolbench[mcp]'          # + serve any MCP server as a loadout source
pip install 'toolbench[docs]'         # + build these docs locally
```

Requires Python ≥ 3.12. Installing puts two equivalent commands on your path, `toolbench`
and the short alias `tbe`.

```bash
toolbench --help        # sectioned command list
toolbench run --help    # every flag for a command
tbe --version
```

## Set a provider key

Real runs call an LLM, so export the key your harness's provider needs. You can also drop
it in a `.env` at the repo root, which toolbench loads before reading the environment.

```bash
export ANTHROPIC_API_KEY=sk-...
```

Not every harness needs a key: the `claude_code` and `codex` harnesses authenticate through
their logged-in CLI (subscription auth), so they need **no** API key at all. See
[Harnesses](harnesses.md).

A dry run needs no key, since `--model stub` never calls a provider.

## Your first run

With a key set, run the self-contained `geometry` example on a cheap model. This does two
tool conditions × 3 seeds, grades each trial, and writes a run directory under `runs/`:

```bash
toolbench run --benchmark examples/geometry --model claude-haiku-4-5 \
    --loadouts core_only,full_local --n 3 --max-cost-usd 0.50
```

You get a `summary.txt` with reach / pass@k / pass^k per cell, plus plots. Head to
[Reading results & scores](reading-results.md) to interpret them.

!!! tip "Validate the wiring for \$0 first"
    Before spending tokens, dry-run against the stub model to check tool resolution and
    grading with no LLM calls:

    ```bash
    toolbench run --benchmark examples/geometry --model stub \
        --loadouts full_local --n 1 --max-cost-usd 0 --dry-run
    ```

    It prints a **resolution preview** (the exact tools each harness × loadout yields) and
    writes a run directory. The stub writes no answer, so every score reads 0.00 and a
    FAILURES block appears. That is expected. You are checking that everything is wired,
    not measuring the agent.

## Where next

- **Running more.** [Running a benchmark](running-a-benchmark.md) covers sweeping axes,
  resume, and regrade.
- **Authoring.** [Authoring overview](../authoring/overview.md) walks through building your
  own benchmark.
- **Tools from toolbase.** [Integrating toolbase](toolbase.md).
- **The ideas.** [Concepts](../explanation.md).
