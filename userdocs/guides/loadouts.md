# Loadouts & tools

A **loadout** is the set of domain tools the agent is equipped with, beyond the harness
core. It is the most common ablation axis: holding everything else fixed and swapping the
loadout tells you *what the tools are worth*.

## A loadout file

Loadouts live per benchmark under `loadouts/<name>.yaml`; the name is the filename stem.
A loadout is an ordered list of **sources**:

```yaml
name: full_local
tools:
  sources:
    - python: toolbench.bench_tools.dunderkit    # all of a module's tools
    - python: toolbench.bench_tools.euclid
skills: []
```

The empty loadout — only the harness core — is the natural baseline:

```yaml
name: core_only
tools:
  sources: []          # the agent has only the harness's core tools
```

## Source backends

Each source names exactly one backend:

=== "python:"

    Import a module that exposes a `TOOLS` list (and optional `BUNDLES`), or a
    `make_tools(base_directory, select=...)` factory. This is the **no-toolbase escape
    hatch** — author tools as plain `@define_tool` functions and point a loadout straight
    at the module:

    ```yaml
    tools:
      sources:
        - python: toolbench.bench_tools.dunderkit
          select: [additive]        # optional: only the `additive` bundle (or tool names)
    ```

=== "toolbase:"

    Resolve a curated set from a [toolbase](toolbase.md) profile, served in-process
    (requires `pip install 'toolbench[toolbase]'`):

    ```yaml
    tools:
      sources:
        - toolbase: { profile: geometry-tools }
    ```

    See [Integrating toolbase](toolbase.md) for the full setup.

## `select:` — narrowing a source

A `select:` list keeps only the named **bundles** or **tools** from a `python:` source. A
bundle is a named group a module declares in `BUNDLES`; an item that matches neither a
bundle nor a tool name is an error (so a typo fails loudly). With no `select:`, you get
everything the module exposes.

## Mixing sources

Sources compose, and the agent's final toolset is `harness core ∪ all sources`. The same
tool name may not come from two sources — toolbash errors on a collision rather than
silently picking one. A mixed loadout is fine:

```yaml
name: full_mixed
tools:
  sources:
    - python: toolbench.bench_tools.dunderkit   # local primitives
    - toolbase: { profile: geometry-tools }     # served domain tools
```

## Skills

A loadout may also expose **skills** — short recipe/guide documents the agent can consult.
The `geometry` benchmark's `guided` loadout pairs the basic arithmetic tools with a
`distance_recipe` skill that teaches assembling a distance from primitives, so you can
measure whether the *guidance* (rather than a dedicated tool) is enough.

## The geometry loadouts

The bundled benchmark ships a spread of loadouts so you can see the axis in action:

| Loadout         | Tools                                              | Measures…                          |
|-----------------|---------------------------------------------------|------------------------------------|
| `core_only`     | none (harness core only)                          | the baseline                       |
| `primitives_only` | arithmetic primitives, no distance tool         | can it assemble the answer?        |
| `full_local`    | primitives + a Euclidean distance tool (Python)   | the with-tools ceiling             |
| `guided`        | primitives + a distance *skill*                   | guidance vs. a dedicated tool      |
| `all_metrics`   | Euclidean + Manhattan + Chebyshev distance tools  | does it pick the *right* tool?     |

Run a couple side by side: `--loadouts core_only,full_local`.
