"""
Tool resolution: harness core + loadout toolkit -> a concrete tool list.

The agent's final tool list is `harness core ∪ loadout toolkit`. The
harness supplies the core/primitive tools (orchestral primitives, via the
`_build_core_tool` factory, or none when the runtime ships its own). The
loadout's toolkit is an ordered list of sources, each routed by backend:

  - `python`  : import a module (dotted name OR filesystem path) that
                exposes `TOOLS` (and optional `BUNDLES`) or a
                `make_tools(base_directory, select=, config=)` factory;
                apply `select:`; this is the no-dependency escape hatch.
                An optional `namespace:` presents the tools as
                `<namespace>__<Tool>` using the same scheme toolbase serves,
                so a bridge to a toolkit reads identically to its `toolbase:`
                source (matching transcripts / `expected_tool_calls`).
  - `toolbase`: resolved in-process via toolbase's orchestral bridge
                (`toolbase.connect.orchestral.toolbase_tools`); the
                source report records each served toolkit's installed
                version as reproducibility provenance.
  - `mcp`     : connect to any MCP server (stdio `command:` or HTTP
                `url:`) via orchestral's MCPClient and serve its tools.

Invariants: a `select:` item must match a bundle name or a tool name
(else error); a tool name appearing from two sources is an error.
`build_agent_tools` returns `(tools, report)` where `report` is a
structured account of what resolved (for the manifest and `--dry-run`).
Sources that hold live connections (toolbase subprocesses, MCP sessions)
stay open for the trial's lifetime; the runner calls
`release_sources(sandbox_dir)` after grading to tear them down.
"""

import contextlib
import copy
import importlib
import importlib.util
import inspect
import os
import sys
from pathlib import Path

from .harness import Harness
from .loadout import Loadout, Source
from .tool_policy import _build_core_tool


def _expand_config(cfg: dict | None) -> dict:
    """Expand ${VAR}/$VAR in a source's `config:` values from the environment
    (the .env-provided per-source config, e.g. paths). Non-strings pass through.
    """
    if not cfg:
        return {}
    return {k: (os.path.expandvars(v) if isinstance(v, str) else v)
            for k, v in cfg.items()}


def _tool_name(tool) -> str:
    # Orchestral tools expose their invocation name via get_name()
    # (e.g. "add", "euclidean_distance") — the same name the agent calls
    # and that shows up in the trajectory.
    get_name = getattr(tool, "get_name", None)
    if callable(get_name):
        try:
            return get_name()
        except Exception:
            pass
    return getattr(tool, "name", None) or type(tool).__name__


def _maybe_set_base_directory(tool, base_directory: str) -> None:
    """Best-effort: scope a tool to the trial sandbox if it carries a
    `base_directory` (orchestral file tools do; `@define_tool` math tools
    do not — those are left untouched)."""
    if hasattr(tool, "base_directory"):
        try:
            setattr(tool, "base_directory", base_directory)
            setup = getattr(tool, "_setup", None)
            if callable(setup):
                setup()
        except Exception:
            pass


# --------------------------------------------------------------------------
# core (harness-provided primitives)
# --------------------------------------------------------------------------
def resolve_core_tools(harness: Harness, base_directory: str) -> list:
    if harness.core.get("builtin"):
        return []  # the runtime (e.g. claude_code) ships its own core tools
    tools: list = []
    for name in harness.core.get("tools") or []:
        try:
            tools.append(_build_core_tool(name, base_directory))
        except Exception as e:  # an optional backend shouldn't tank the trial
            print(f"warning: skipping core tool {name!r}: {e}", file=sys.stderr)
    return tools


# --------------------------------------------------------------------------
# python source (the escape hatch)
# --------------------------------------------------------------------------
def _import_module_or_path(spec: str):
    """Import a module by dotted name, or from a filesystem path (a .py file
    or a package directory). For a directory, its parent is put on sys.path
    so internal imports resolve."""
    p = Path(str(spec))
    if p.exists():
        if p.is_file() and p.suffix == ".py":
            ms = importlib.util.spec_from_file_location(f"_loadout_{p.stem}", p)
            mod = importlib.util.module_from_spec(ms)
            ms.loader.exec_module(mod)
            return mod
        if p.is_dir():
            parent = str(p.resolve().parent)
            if parent not in sys.path:
                sys.path.insert(0, parent)
            return importlib.import_module(p.name)
    return importlib.import_module(str(spec))


def _apply_select(all_tools: list, bundles: dict, select, module: str) -> list:
    """Filter `all_tools` by `select` (bundle names ∪ bare tool names).
    No `select` => all tools. An item matching neither is an error."""
    if not select:
        return list(all_tools)
    by_name = {}
    for t in all_tools:
        nm = _tool_name(t)
        if nm:
            by_name[nm.lower()] = t
    chosen, seen = [], set()

    def _add(t):
        if id(t) not in seen:
            chosen.append(t)
            seen.add(id(t))

    for item in select:
        if item in bundles:
            for t in bundles[item]:
                _add(t)
        elif str(item).lower() in by_name:
            _add(by_name[str(item).lower()])
        else:
            raise ValueError(
                f"python source {module!r}: `select` item {item!r} matches no "
                f"bundle {sorted(bundles)} nor tool {sorted(by_name)}"
            )
    return chosen


def _namespaced_upstream(tool) -> str:
    """The upstream (un-namespaced) name toolbase would serve this tool under.

    We READ it off the tool's own `_mcp_display_name` — the exact field
    toolbase's toolkit host sets and advertises on the wire, and that toolbase
    then namespaces as `<toolkit>__<_mcp_display_name>`. Sourcing the name from
    the instance (rather than reimplementing toolbase's rule here) makes a
    `python:` bridge reflect the SAME name the `toolbase:` source serves *by
    construction* — no second copy of the convention to drift. It matters:
    e.g. `SortByPtTool` carries `_mcp_display_name = "SortByPT"`, which a naive
    class-name strip (`SortByPt`) would get wrong.

    Fallback for a tool that doesn't carry the field: the BaseTool subclass
    name with a trailing `Tool` stripped, PascalCase preserved — toolbase's own
    documented default when no display name is set. (Note: a toolkit.yaml-level
    `display_name:` override is applied by toolbase's host from the YAML and is
    not visible to an in-process bridge; toolkits that use those can't be
    perfectly mirrored by a bridge — use the `toolbase:` source for them.)

    When toolbase is installed we call its canonical `mcp_tool_name` so there is
    exactly one definition of the rule; the inline copy below is only the
    no-toolbase escape-hatch fallback."""
    try:
        from toolbase.naming import mcp_tool_name
        return mcp_tool_name(tool)
    except Exception:
        pass
    display = getattr(tool, "_mcp_display_name", None)
    if isinstance(display, str) and display:
        return display
    name = type(tool).__name__
    return name[:-4] if name.endswith("Tool") else name


def _namespace_tool(tool, namespace: str):
    """Rebind `tool` to present its agent-visible name as
    `<namespace>__<upstream>` (the toolbase scheme). The name an agent calls
    comes from `get_tool_spec().name` (built by orchestral's SchemaGenerator
    from the class name, NOT from `get_name()`), so we override both. Behaviour
    (`execute`/`_run`/validation) is inherited unchanged — only the presented
    name changes."""
    cls = type(tool)
    ns_name = f"{namespace}__{_namespaced_upstream(tool)}"
    overrides = {"get_name": classmethod(lambda c, _n=ns_name: _n)}
    try:
        spec = cls.get_tool_spec()  # original spec (class-name-derived name)
        ns_spec = type(spec)(name=ns_name, description=spec.description,
                             input_schema=spec.input_schema)
        overrides["get_tool_spec"] = classmethod(lambda c, _s=ns_spec: _s)
    except Exception:
        pass  # no derivable spec (e.g. a proxy); get_name override still applies
    ns_cls = type(f"Namespaced_{cls.__name__}", (cls,), overrides)
    try:
        tool.__class__ = ns_cls
    except Exception as e:
        print(f"warning: could not namespace tool {cls.__name__!r} as "
              f"{ns_name!r}: {e}", file=sys.stderr)
    return tool


def _apply_namespace(tools: list, source: Source) -> list:
    """Apply a python source's optional `namespace:` to every returned tool.
    No `namespace:` => tools pass through untouched (bare names, back-compat)."""
    namespace = source.options.get("namespace")
    if not namespace:
        return tools
    return [_namespace_tool(t, str(namespace)) for t in tools]


def resolve_python_source(source: Source, base_directory: str) -> list:
    module = source.config
    mod = _import_module_or_path(module)
    # A module may expose a per-trial factory `make_tools(base_directory,
    # select=...)` — needed when tools require base_directory / external
    # config at construction. The factory owns `select` semantics and
    # returns ready instances.
    make = getattr(mod, "make_tools", None)
    if callable(make):
        return _apply_namespace(
            list(make(base_directory, select=source.select,
                      config=_expand_config(source.options.get("config")))),
            source)
    # Otherwise the static convention: a `TOOLS` list (+ optional `BUNDLES`).
    all_tools = getattr(mod, "TOOLS", None)
    if all_tools is None:
        raise ValueError(
            f"python source {module!r} exposes neither a `TOOLS` list nor a "
            "`make_tools(base_directory, select=...)` factory."
        )
    bundles = getattr(mod, "BUNDLES", {}) or {}
    tools = _apply_select(all_tools, bundles, source.select, str(module))
    # Per-trial copies: dotted-name / package-dir sources are cached in
    # sys.modules, so their TOOLS entries are process-wide singletons.
    # Scoping a shared instance to this trial's sandbox would re-point
    # every concurrent trial using the same source (--parallel) at one
    # sandbox. (.py-file sources are re-exec'd fresh per resolution, but
    # copying uniformly is cheap and keeps the invariant simple.)
    copied = []
    for t in tools:
        try:
            copied.append(copy.deepcopy(t))
        except Exception:
            # Un-copyable tool (live client handle, ...): fall back to the
            # shared instance — correct for serial runs, racy in parallel.
            print(f"warning: python source {module!r}: tool "
                  f"{_tool_name(t)!r} is not deep-copyable; sharing one "
                  "instance across trials (unsafe with --parallel > 1).",
                  file=sys.stderr)
            copied.append(t)
    tools = copied
    for t in tools:
        _maybe_set_base_directory(t, base_directory)
    return _apply_namespace(tools, source)


# --------------------------------------------------------------------------
# per-sandbox lifecycle stack (shared by the toolbase and mcp backends)
# --------------------------------------------------------------------------
# Both backends hold live connections — toolbase spins up one subprocess
# per served toolkit, MCP holds a session (subprocess or HTTP) — that a
# trial needs open for its whole run. Each sandbox's connections live in
# an ExitStack keyed by the sandbox dir; the caller (the runner, or the
# CLI's resolution preview) calls `release_sources(dir)` once it's done.
# This keeps `build_agent_tools` returning the same `(tools, report)` it
# always has — no signature change for callers.
_SOURCE_STACKS: dict[str, contextlib.ExitStack] = {}


def _source_stack(base_directory: str) -> contextlib.ExitStack:
    st = _SOURCE_STACKS.get(base_directory)
    if st is None:
        st = contextlib.ExitStack()
        _SOURCE_STACKS[base_directory] = st
    return st


def release_sources(base_directory: str) -> None:
    """Tear down live source connections (toolbase subprocesses, MCP
    sessions) started for this sandbox. A no-op when none were started,
    so it's always safe to call after a trial / preview."""
    st = _SOURCE_STACKS.pop(base_directory, None)
    if st is not None:
        st.close()


# Back-compat alias (pre-mcp name).
release_toolbase = release_sources


def _reject_profile_key(cfg: dict, source: Source) -> None:
    """Fail loudly on the pre-0.12 `profile:` key.

    toolbase renamed profiles to loadouts and removed the old spellings
    outright, so this key can no longer resolve to anything. Silence would
    be worse than usual here: a benchmark whose tools failed to resolve
    doesn't error, it runs as a *tool-less arm* and grades as a valid
    condition, so a stale config would quietly turn a comparison into a
    measurement of the model alone.
    """
    if "profile" not in cfg:
        return
    name = cfg.get("profile")
    raise RuntimeError(
        "toolbase source: `profile:` was renamed to `loadout:` when toolbase "
        "renamed profiles to loadouts (0.12). Update the source to "
        f"`toolbase: {{loadout: {name!r}}}`. "
        f"Offending source: {source.config!r}"
    )


def resolve_toolbase_source(source: Source, base_directory: str) -> list:
    """Resolve a `toolbase:` loadout source to orchestral tools, in-process.

    `source.config` is a dict. Supported forms:
      - `{loadout: NAME}`                     serve toolbase loadout NAME
      - `{loadout: NAME, project_root: PATH}` resolve config against PATH
      - `{project_root: PATH}`                serve PATH's active/default loadout

    The inline `{toolsets: {...}}` form (compile-to-`.toolbase/`) is not wired
    yet — author a toolbase loadout and reference it with `loadout:` instead.
    Returns orchestral `BaseTool`s (namespaced `<toolkit>__<tool>`), held live
    until `release_toolbase(base_directory)`.

    A benchmark loadout's source names a toolbase loadout: the same idea one
    layer down, which is why they share the word. (toolbase called its own a
    "profile" until 0.12, and this key followed that name.)
    """
    try:
        from toolbase.connect.orchestral import toolbase_tools
    except Exception as e:  # toolbase is an optional dependency
        raise RuntimeError(
            "the `toolbase:` source backend needs toolbase installed "
            "(`pip install 'toolbench[toolbase]'`, or an editable checkout). "
            "Use a `python:` source for the no-toolbase escape hatch. "
            f"(import error: {e})"
        ) from e

    cfg = source.config if isinstance(source.config, dict) else {}
    _reject_profile_key(cfg, source)
    loadout = cfg.get("loadout")
    project_root = cfg.get("project_root")
    if project_root:
        project_root = Path(os.path.expandvars(str(project_root))).expanduser()
    if not loadout and not project_root:
        if cfg.get("toolsets"):
            raise RuntimeError(
                "toolbase source: the inline `toolsets:` spec is not wired yet. "
                "Author a toolbase loadout (`tb loadout create ...`) and reference "
                "it here as `toolbase: {loadout: NAME}`. "
                f"Offending source: {source.config!r}"
            )
        raise RuntimeError(
            "toolbase source: give a `loadout:` (and optional `project_root:`). "
            f"Offending source: {source.config!r}"
        )

    # Scope the served tools to the trial sandbox. toolbase hosts run in
    # their own subprocesses with their own config-resolved
    # base_directory (default: the serve cwd) — without this override,
    # an agent's file-aware toolkit tools read/write a DIFFERENT tree
    # than its harness-core tools, and every sandbox-relative path the
    # agent passes fails. toolbase >= the config_overrides feature
    # accepts the kwarg; older versions get a loud warning because the
    # mismatch corrupts trials silently.
    tb_kwargs: dict = {"loadout": loadout, "project_root": project_root,
                       "quiet": True}
    _tb_params = inspect.signature(toolbase_tools).parameters
    if "config_overrides" in _tb_params:
        tb_kwargs["config_overrides"] = {"base_directory": base_directory}
    else:
        print("warning: installed toolbase predates config_overrides — "
              "toolbase-served tools will NOT be scoped to the trial "
              "sandbox (file-path tool calls will misresolve). Upgrade "
              "toolbase.", file=sys.stderr)
    # Ask toolbase (when new enough) to report how many tools each toolkit
    # dropped, so a serves-1-of-54 misconfiguration is visible here instead of
    # only in serve.log. quiet=True suppresses toolbase's own console warning.
    drop_report: list = []
    if "report" in _tb_params:
        tb_kwargs["report"] = drop_report

    stack = _source_stack(base_directory)
    tools = list(stack.enter_context(toolbase_tools(**tb_kwargs)))
    # A toolbase loadout legitimately serves only a subset of a toolkit's
    # tools (its selected bundles), so `hidden > 0` is normal and NOT worth
    # warning about every trial. Warn only when a toolkit advertised tools
    # but served *none* — the unambiguous "you pointed at a loadout and got
    # nothing" misconfig.
    for r in drop_report:
        if r.get("advertised", 0) > 0 and r.get("served", 0) == 0:
            print(f"warning: toolbase source {source.config!r}: toolkit "
                  f"{r['toolkit']!r} advertised {r['advertised']} tools but "
                  "served 0 — every tool was filtered out by the toolbase "
                  "loadout / bundle selection / config gating. Check the "
                  "loadout's bundles and `tb config`.", file=sys.stderr)
    # The toolbase loadout curates what toolbase serves; a toolbench
    # source-level `select:` carves an ablation arm out of that served set
    # without authoring one toolbase loadout per arm. Items match the namespaced name (`toolkit__tool`)
    # or a bare tool name when unambiguous.
    tools = _select_namespaced(tools, source.select,
                               label=f"toolbase:{source.config!r}")
    # Scope file-aware tools to the sandbox, same as the python: path.
    for t in tools:
        _maybe_set_base_directory(t, base_directory)
    return tools


def _select_namespaced(tools: list, select, *, label: str) -> list:
    """Filter served tools by `select:`. No select => everything.

    An item matches the full namespaced name (`toolkit__tool`, the name
    the agent calls) or, as a convenience, a bare upstream tool name —
    but only when exactly one toolkit serves it. Order follows `select`;
    an item matching nothing (or ambiguously) is an error so a typo'd
    ablation arm fails at resolution, not as a silently-thinner loadout.
    """
    if not select:
        return list(tools)
    by_full = {}
    for t in tools:
        nm = _tool_name(t)
        if nm:
            by_full[nm.lower()] = t
    chosen, seen = [], set()
    for item in select:
        key = str(item).lower()
        tool = by_full.get(key)
        if tool is None:
            suffix_matches = [t for full, t in by_full.items()
                              if full.endswith(f"__{key}")]
            if len(suffix_matches) == 1:
                tool = suffix_matches[0]
            elif len(suffix_matches) > 1:
                names = sorted(_tool_name(t) for t in suffix_matches)
                raise ValueError(
                    f"{label}: `select` item {item!r} is ambiguous — served "
                    f"by {names}; use the namespaced name."
                )
        if tool is None:
            raise ValueError(
                f"{label}: `select` item {item!r} matches no served tool. "
                f"Served: {sorted(by_full)}"
            )
        if id(tool) not in seen:
            chosen.append(tool)
            seen.add(id(tool))
    return chosen


def toolbase_provenance(tools: list) -> dict:
    """Best-effort reproducibility provenance for served toolbase tools.

    Maps each served toolkit (the `<toolkit>__` prefix of the namespaced
    tool names) to the installed version that toolbase's own discovery
    would serve (project-pin aware, else highest installed), plus its
    environment type. Records `"unknown"` rather than raising when the
    lookup fails — provenance must never tank a trial.
    """
    served = sorted({
        nm.split("__", 1)[0]
        for nm in ((_tool_name(t) or "") for t in tools)
        if "__" in nm
    })
    out: dict = {"toolkits": {n: {"version": "unknown"} for n in served}}
    try:
        import importlib.metadata
        out["toolbase_version"] = importlib.metadata.version("toolbase")
    except Exception:
        out["toolbase_version"] = "unknown"
    try:
        # The same discovery the orchestrator served from: cache walk +
        # project-manifest pin, else highest installed version. The slot
        # dir is `cache/<name>/<version>/`, so path.name is the version.
        from toolbase.serve.orchestrator import discover_toolkits
        by_name = {d.name: d for d in discover_toolkits()}
        for name in served:
            d = by_name.get(name)
            if d is None:
                continue
            out["toolkits"][name] = {
                "version": d.path.name,
                "environment": d.meta.get("environment", "unknown"),
            }
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# mcp source (any MCP server, via orchestral's MCPClient)
# --------------------------------------------------------------------------
# Test seam: tests replace this with a factory returning a fake client so
# the backend is testable without the `mcp` package or a live server.
def _default_mcp_client_factory(**kwargs):
    try:
        from orchestral.mcp import MCPClient
    except ImportError as e:
        raise RuntimeError(
            "the `mcp:` source backend needs the MCP SDK "
            "(`pip install 'toolbench[mcp]'`). "
            f"(import error: {e})"
        ) from e
    return MCPClient(**kwargs)


_MCP_CLIENT_FACTORY = _default_mcp_client_factory


def _mcp_safe_config(cfg: dict) -> dict:
    """The config echo for reports/manifests, secrets redacted.

    `headers:` values (auth tokens) and `env:` values (API keys) are
    replaced with `<redacted>`; keys stay visible so a run is still
    auditable for *what* was configured without persisting credentials
    into trial.json / manifest.json.
    """
    safe = dict(cfg)
    for secret_key in ("headers", "env"):
        if isinstance(safe.get(secret_key), dict):
            safe[secret_key] = {k: "<redacted>" for k in safe[secret_key]}
    return safe


def resolve_mcp_source(source: Source, base_directory: str) -> list:
    """Resolve an `mcp:` loadout source to orchestral tools.

    `source.config` is a dict. Supported forms:
      - `{command: [argv...], env: {...}}`   spawn a stdio MCP server
      - `{url: URL, headers: {...}}`         connect to a remote MCP server
      - either form: `timeout: <seconds>`    per-call/connect bound (default 60)

    `${VAR}` in `url`, `command` items, `env` values, and `headers`
    values is expanded from the environment (so tokens live in `.env`,
    not in the loadout yaml). The client session is held open until
    `release_sources(base_directory)` — same lifecycle as toolbase
    subprocesses. State accumulated server-side persists across the
    trial's calls and is torn down with the trial.
    """
    cfg = source.config if isinstance(source.config, dict) else {}
    command = cfg.get("command")
    url = cfg.get("url")
    if bool(command) == bool(url):
        raise RuntimeError(
            "mcp source: give exactly one of `command:` (stdio server argv) "
            f"or `url:` (remote server). Offending source: {cfg!r}"
        )

    def _expand(v):
        return os.path.expandvars(v) if isinstance(v, str) else v

    kwargs: dict = {"timeout": float(cfg.get("timeout", 60.0))}
    if command:
        if not isinstance(command, list):
            raise RuntimeError(
                f"mcp source: `command:` must be an argv list, got {command!r}"
            )
        kwargs["server_command"] = [_expand(c) for c in command]
        if isinstance(cfg.get("env"), dict):
            kwargs["env"] = {k: _expand(v) for k, v in cfg["env"].items()}
    else:
        kwargs["url"] = _expand(url)
        if isinstance(cfg.get("headers"), dict):
            kwargs["headers"] = {k: _expand(v) for k, v in cfg["headers"].items()}

    client = _MCP_CLIENT_FACTORY(**kwargs)
    stack = _source_stack(base_directory)
    # MCPClient is a context manager: connect() on enter (handshake +
    # tool discovery), disconnect() on exit.
    stack.enter_context(client)
    tools = list(client.get_orchestral_tools())
    tools = _select_namespaced(tools, source.select,
                               label=f"mcp:{cfg.get('url') or 'stdio'}")
    for t in tools:
        _maybe_set_base_directory(t, base_directory)
    return tools


# --------------------------------------------------------------------------
# composition
# --------------------------------------------------------------------------
def build_agent_tools(harness: Harness, loadout: Loadout,
                      base_directory: str) -> tuple[list, dict]:
    """Return `(tools, report)` for `harness core ∪ loadout toolkit`,
    erroring on a tool-name collision across sources."""
    tools: list = []
    seen: dict[str, str] = {}  # tool name -> source label
    report: dict = {"harness": harness.id, "loadout": loadout.name,
                    "core": {}, "sources": []}

    def _register(tool, label: str):
        nm = _tool_name(tool)
        # Case-insensitive: Orchestral lower-cases registered tool names,
        # so `Add` and `add` would alias at call time — treat them as the
        # same name here too (matching `select:` and judge matching).
        key = nm.lower()
        if key in seen:
            raise ValueError(
                f"tool name collision: {nm!r} provided by both {seen[key]} "
                f"and {label}; disable one in the loadout."
            )
        seen[key] = label
        tools.append(tool)

    core_tools = resolve_core_tools(harness, base_directory)
    report["core"] = {"builtin": bool(harness.core.get("builtin")),
                      "tools": [_tool_name(t) for t in core_tools]}
    for t in core_tools:
        _register(t, "harness.core")

    for src in loadout.sources:
        if src.backend == "python":
            stools = resolve_python_source(src, base_directory)
        elif src.backend == "toolbase":
            stools = resolve_toolbase_source(src, base_directory)
        elif src.backend == "mcp":
            stools = resolve_mcp_source(src, base_directory)
        else:  # pragma: no cover - validated upstream
            raise ValueError(f"unknown source backend {src.backend!r}")
        label = f"{src.backend}:{src.config}"
        entry = {
            "backend": src.backend,
            # The mcp config can carry credentials (headers/env) — the
            # report lands in trial.json and the manifest, so redact.
            "config": (_mcp_safe_config(src.config)
                       if src.backend == "mcp" and isinstance(src.config, dict)
                       else src.config),
            "select": src.select,
            "tools": [_tool_name(t) for t in stools],
        }
        if src.backend == "toolbase":
            # Reproducibility provenance: which installed toolkit
            # versions actually served this trial's tools.
            entry["provenance"] = toolbase_provenance(stools)
        report["sources"].append(entry)
        for t in stools:
            _register(t, label)

    return tools, report
