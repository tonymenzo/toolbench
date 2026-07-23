# Integrating toolbase

[toolbase](https://toolbase-ai.com) is the package manager and runtime for agent tools.
A loadout's `toolbase:` source resolves a curated set of tools from a toolbase **profile**,
in-process, and hands them to the agent, so you can benchmark exactly the tools your
users would get from `tb serve`. This closes the loop. Author and serve in toolbase, then
measure here.

!!! note "Optional dependency"
    The `toolbase:` backend needs toolbase installed via `pip install
    'toolbench[toolbase]'` (or an editable checkout of toolbase next door). Without it, a
    `toolbase:` source raises a clear error pointing you back to the `python:` escape
    hatch. `python:` loadouts always work with no toolbase at all.

## How it works

When a loadout source names a `toolbase:` profile, toolbench calls toolbase's in-process
orchestral bridge (`toolbase.connect.orchestral.toolbase_tools`). toolbase resolves the
active profile exactly as `tb serve` would, spins up one subprocess per served toolkit
(each in its own isolated env), and yields the tools as orchestral `BaseTool`s. toolbench
unions them with the harness core, runs the trial, and tears the subprocesses down when the
trial ends. You benchmark the *same curated, version-pinned tools* your agents run in
production.

!!! note "CLI runtimes serve the profile out-of-process over MCP"
    The in-process orchestral bridge above is the `orchestral`-runtime path. Under the
    `claude_code` and `codex` [runtimes](harnesses.md#runtimes) the same `toolbase:`
    profile is served **out-of-process over MCP** — the runtime spawns a `toolbase serve
    --profile … --call-timeout …` subprocess (scoped to the trial sandbox) and wires it to
    the coding-agent CLI, rather than importing tools into this process. The served
    toolkit still follows the loadout's `toolbase:` source; only the transport differs.

## Setup

1. **Install a toolkit and activate it into a profile.** toolbench ships a small
   `calculator` toolkit under `examples/calculator` (basic arithmetic plus a scientific
   bundle, enough to compute the `geometry` task's distance and midpoint):

    ```bash
    tb install -e examples/calculator -g -a   # editable install + activate (user scope)
    tb list                                   # calculator  ✓ active
    ```

    `-a` activates it into your user `default` profile. See the
    [toolbase docs](https://toolbase-ai.com/docs/) for authoring your own toolkit.

2. **Point a loadout at the profile.** The example `calc_toolbase` loadout does exactly
   this:

    ```yaml
    # examples/geometry/loadouts/calc_toolbase.yaml
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

    A sibling `select:` carves an ablation arm out of the served set without
    authoring one profile per arm — items match the namespaced name
    (`calculator__add`) or a bare tool name when only one toolkit serves it:

    ```yaml
    sources:
      - toolbase: { profile: default }
        select: [calculator__add, calculator__subtract]
    ```

    A typo'd or ambiguous item is a resolution error, never a silently
    thinner loadout.

3. **Run it.** `python:` and `toolbase:` sources can even be mixed in one loadout:

    ```bash
    toolbench run --benchmark examples/geometry --loadouts calc_toolbase \
        --models claude-haiku-4-5 --n 3 --max-cost-usd 0.50
    ```

    Use `--dry-run` first. The resolution preview lists the exact tools the profile yields
    (namespaced `<toolkit>__<tool>`) before any model is called.

## Version provenance

Every run records *which installed toolkit versions actually served* the trial's tools:
the resolution preview prints them (`toolkit versions: calculator 1.2.0 (toolbase
0.5.0)`), and the same provenance block lands in the manifest's `resolution` section and
each trial's `trial.json` under `config.tools.sources[].provenance`. Versions follow
toolbase's own selection — the project-manifest pin when one exists, else the highest
installed — so a reach delta measured today stays attributable when toolkit versions
move tomorrow.

Under a CLI runtime this served-toolkit provenance is recorded *separately* from the
runtime's own driver version: `claude_code` / `codex` also record their CLI version
under the manifest's `runtime_versions` (see
[Runtime version capture](harnesses.md#runtime-version-capture)). The toolkit versions
here describe *what tools served*; `runtime_versions` describes *what drove the agent*.

## The boundary

toolbase owns *the tools*, meaning installing, isolating, curating, and serving. toolbench
owns *the measurement*, meaning the task, rubric, harness, model, and metrics. A loadout's
`toolbase:` source is the single seam between them. Nothing about the model, prompt, or
grading ever leaks into toolbase, and toolbase never needs to know a benchmark exists.

## Not yet wired

The inline `toolbase: { toolsets: { ... } }` form (declaring toolkit + version + bundles
directly in the loadout, compiled to a throwaway `.toolbase/`) is **not implemented yet**.
It raises a clear error asking you to author a profile and use `profile:` instead. The
example `full_toolbase` and `full_mixed` loadouts use that form and are placeholders until
it lands.
