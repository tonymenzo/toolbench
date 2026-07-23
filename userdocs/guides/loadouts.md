# Loadouts & tools

A **loadout** is the set of domain tools the agent is equipped with, beyond the harness
core. It is the most common ablation axis. Hold everything else fixed and swap the
loadout to learn *what the tools are worth*.

## A loadout file

Loadouts live per benchmark under `loadouts/<name>.yaml`, and the name is the filename
stem. A loadout is an ordered list of **sources**:

```yaml
name: full_local
tools:
  sources:
    - python: tools/dunderkit.py    # all of a module's tools
    - python: tools/euclid.py
skills: []
```

The empty loadout, only the harness core, is the natural baseline:

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
    hatch**. Author tools as plain `@define_tool` functions and point a loadout straight
    at the module:

    ```yaml
    tools:
      sources:
        - python: tools/dunderkit.py
          select: [additive]        # optional: only the `additive` bundle (or tool names)
    ```

    The value is either a **relative filesystem path** (resolved against the benchmark
    directory, so `tools/dunderkit.py` points at the benchmark's own `tools/`) or an
    importable **dotted module path** (e.g. `mypkg.tools.dunderkit`) for an installed package.

=== "toolbase:"

    Resolve a curated set from a [toolbase](toolbase.md) profile, served in-process
    (requires `pip install 'toolbench[toolbase]'`). The served toolkit versions are
    recorded in the run manifest and each trial as reproducibility provenance:

    ```yaml
    tools:
      sources:
        - toolbase: { profile: geometry-tools }
    ```

    See [Integrating toolbase](toolbase.md) for the full setup.

=== "mcp:"

    Connect to **any MCP server** — stdio or HTTP — via orchestral's MCP client
    (requires `pip install 'toolbench[mcp]'`). The session stays open for the
    trial and is torn down with it. MCP wiring differs by [runtime](harnesses.md#runtimes):
    this in-process client is the `orchestral` path — under `claude_code` / `codex` the
    runtime itself wires MCP (a `toolbase serve` subprocess handed to the coding-agent CLI):

    ```yaml
    tools:
      sources:
        - mcp: { command: ["npx", "@some/mcp-server"] }       # stdio
        - mcp:
            url: "https://host/mcp"                            # HTTP
            headers: { Authorization: "Bearer ${MY_TOKEN}" }
    ```

    `${VAR}` in config values expands from the environment, so tokens live in
    `.env`, not in the yaml — and `headers:`/`env:` values are redacted in
    everything the run persists.

## Narrowing a source with `select:`

A `select:` list keeps only the named items from its source — so one source can feed
several ablation arms without re-authoring it per arm. An item that matches nothing is an
error, so a typo fails loudly instead of silently thinning the loadout. With no `select:`,
you get everything the source serves. Matching per backend:

| Backend     | A `select:` item matches…                                            |
|-------------|----------------------------------------------------------------------|
| `python:`   | a **bundle** name (from `BUNDLES`) or a tool name.                   |
| `toolbase:` | the namespaced name (`calculator__add`) or an unambiguous bare tool name. |
| `mcp:`      | the served tool name.                                                |

## Mixing sources

Sources compose, and the agent's final toolset is `harness core ∪ all sources`. The same
tool name may not come from two sources. toolbase errors on a collision rather than
silently picking one. A mixed loadout is fine:

```yaml
name: full_mixed
tools:
  sources:
    - python: tools/dunderkit.py   # local primitives
    - toolbase: { profile: geometry-tools }     # served domain tools
```

## Skills

A loadout may also expose **skills**, short recipe/guide documents delivered to the agent —
the third leg of a domain harness (tools + skills + prompts):

```yaml
skills:
  - name: distance_recipe
    file: ./skills/distance_recipe.md   # benchmark-relative
    mode: on_demand                     # or: inline
```

- **`on_demand`** (default): the file is copied into the trial sandbox at
  `skills/<name>.md` and the system prompt gains a one-line pointer; the agent reads it
  when it judges it relevant. Costs ~no context until consulted — but the harness core
  needs a way to read files (`ReadFileTool` / `RunCommandTool`).
- **`inline`**: the full content is embedded in the system prompt. Always visible, costs
  context every turn; right for short recipes or cores without file tools.

Skills are part of the measured configuration: a declared skill whose file is missing
fails trial setup loudly, and each trial records its skill names in `trial.json`. The
`geometry` benchmark's `guided` loadout pairs the basic arithmetic tools with a
`distance_recipe` skill that teaches assembling a distance from primitives, so you can
measure whether *guidance* (rather than a dedicated tool) is enough.

## The geometry loadouts

The example benchmark ships a spread of loadouts so you can see the axis in action:

| Loadout         | Tools                                              | Measures…                          |
|-----------------|---------------------------------------------------|------------------------------------|
| `core_only`     | none (harness core only)                          | the baseline                       |
| `primitives_only` | arithmetic primitives, no distance tool         | can it assemble the answer?        |
| `full_local`    | primitives + a Euclidean distance tool (Python)   | the with-tools ceiling             |
| `guided`        | primitives + a distance *skill*                   | guidance vs. a dedicated tool      |
| `all_metrics`   | Euclidean + Manhattan + Chebyshev distance tools  | does it pick the *right* tool?     |

Run a couple side by side: `--loadouts core_only,full_local`.
