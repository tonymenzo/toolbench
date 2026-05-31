# Integrating toolbase

[toolbase](https://toolbase-ai.com) is the package manager and runtime for agent tools.
A loadout's `toolbase:` source resolves a curated set of tools from a toolbase **profile**,
in-process, and hands them to the agent — so you can benchmark exactly the tools your
users would get from `tb serve`. This closes the loop: author and serve in toolbase, then
measure here.

!!! note "Optional dependency"
    The `toolbase:` backend needs toolbase installed: `pip install 'toolbench[toolbase]'`
    (or an editable checkout of toolbase next door). Without it, a `toolbase:` source
    raises a clear error pointing you back to the `python:` escape hatch — `python:`
    loadouts always work with no toolbase at all.

## How it works

When a loadout source names a `toolbase:` profile, toolbench calls toolbase's in-process
orchestral bridge (`toolbase.connect.orchestral.toolbase_tools`). toolbase resolves the
active profile exactly as `tb serve` would, spins up one subprocess per served toolkit
(each in its own isolated env), and yields the tools as orchestral `BaseTool`s. toolbench
unions them with the harness core, runs the trial, and tears the subprocesses down when the
trial ends. You benchmark the *same curated, version-pinned tools* your agents run in
production.

## Setup

1. **Install a toolkit and activate it into a profile.** toolbench ships a small
   `calculator` toolkit under `examples/calculator` (basic arithmetic + a scientific
   bundle — enough to compute the `geometry` task's distance and midpoint):

    ```bash
    tb install -e examples/calculator -g -a   # editable install + activate (user scope)
    tb list                                   # calculator  ✓ active
    ```

    `-a` activates it into your user `default` profile. See the
    [toolbase docs](https://toolbase-ai.com/docs/) for authoring your own toolkit.

2. **Point a loadout at the profile.** The bundled `calc_toolbase` loadout does exactly
   this:

    ```yaml
    # benchmarks/geometry/loadouts/calc_toolbase.yaml
    name: calc_toolbase
    tools:
      sources:
        - toolbase: { profile: default }
    ```

    Supported source forms:

    | Form                                          | Resolves…                                  |
    |-----------------------------------------------|--------------------------------------------|
    | `toolbase: { profile: NAME }`                 | the named toolbase profile                 |
    | `toolbase: { profile: NAME, project_root: P }`| profile `NAME`, config resolved against `P`|
    | `toolbase: { project_root: P }`               | `P`'s active/default profile               |

3. **Run it** — `python:` and `toolbase:` sources can even be mixed in one loadout:

    ```bash
    toolbench run --benchmark geometry --loadouts calc_toolbase \
        --models claude-haiku-4-5 --n 3 --max-cost-usd 0.50
    ```

    Use `--dry-run` first: the resolution preview lists the exact tools the profile yields
    (namespaced `<toolkit>__<tool>`) before any model is called.

## The boundary

toolbase owns *the tools* — installing, isolating, curating, serving. toolbench owns
*the measurement* — the task, rubric, harness, model, and metrics. A loadout's
`toolbase:` source is the single seam between them; nothing about the model, prompt, or
grading ever leaks into toolbase, and toolbase never needs to know a benchmark exists.

## Not yet wired

The inline `toolbase: { toolsets: { ... } }` form (declaring toolkit + version + bundles
directly in the loadout, compiled to a throwaway `.toolbase/`) is **not implemented yet**
— it raises a clear error asking you to author a profile and use `profile:` instead. The
bundled `full_toolbase` and `full_mixed` loadouts use that form and are placeholders until
it lands.
