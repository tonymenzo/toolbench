# Install & quickstart

## Install

```bash
pip install toolbench                 # the framework + CLI
pip install 'toolbench[toolbase]'     # + resolve tools from toolbase profiles
pip install 'toolbench[docs]'         # + build these docs locally
```

Requires Python ≥ 3.12. Installing puts two equivalent commands on your path — `toolbench`
and the short alias `tbe`.

```bash
toolbench --help        # sectioned command list
toolbench run --help    # every flag for a command
tbe --version
```

## Set a provider key

Real runs call an LLM, so export the key your harness's provider needs (or drop it in a
`.env` at the repo root, which toolbench loads before reading the environment):

```bash
export ANTHROPIC_API_KEY=sk-...
```

No key needed for a dry run — `--model stub` never calls a provider.

## Your first run (\$0)

The bundled `geometry` benchmark is self-contained. Validate the entire pipeline — tool
resolution, the agent loop, grading, the summary, the plots — without spending anything:

```bash
toolbench run --benchmark geometry --model stub \
    --loadouts full_local --n 1 --max-cost-usd 0 --dry-run
```

You'll see a **resolution preview** (the exact tools each harness × loadout yields) and a
run directory written under `runs/`. A clean dry run means everything is wired.

## A real run

```bash
toolbench run --benchmark geometry --model claude-haiku-4-5 \
    --loadouts core_only,full_local --n 3 --max-cost-usd 0.50
```

This runs two tool conditions × 3 seeds, grades each trial, and writes
`runs/<id>/summary.txt` with reach / pass@k / pass^k per cell, plus plots. Head to
[Reading results & scores](reading-results.md) to interpret them.

## Where next

- **Running more:** [Running a benchmark](running-a-benchmark.md) — sweeping axes, resume,
  regrade.
- **Authoring:** [Authoring overview](../authoring/overview.md) — build your own benchmark.
- **Tools from toolbase:** [Integrating toolbase](toolbase.md).
- **The ideas:** [Concepts](../explanation.md).
