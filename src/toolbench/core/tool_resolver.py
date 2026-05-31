"""
Tool resolution: harness core + loadout toolkit -> a concrete tool list.

The agent's final tool list is `harness core ∪ loadout toolkit`. The
harness supplies the core/primitive tools (orchestral primitives, via the
`_build_core_tool` factory, or none when the runtime ships its own). The
loadout's toolkit is an ordered list of sources, each routed by backend:

  - `python`  : import a module (dotted name OR filesystem path) that
                exposes `TOOLS` (and optional `BUNDLES`); apply `select:`;
                this is the no-toolbase escape hatch.
  - `toolbase`: resolved by toolbase — NOT available yet; raises a clear
                error pointing back at the `python:` escape hatch.

Invariants: a `select:` item must match a bundle name or a tool name
(else error); a tool name appearing from two sources is an error.
`build_agent_tools` returns `(tools, report)` where `report` is a
structured account of what resolved (for the manifest and `--dry-run`).
"""

import importlib
import importlib.util
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


def resolve_python_source(source: Source, base_directory: str) -> list:
    module = source.config
    mod = _import_module_or_path(module)
    # A module may expose a per-trial factory `make_tools(base_directory,
    # select=...)` — needed when tools require base_directory / external
    # config at construction (e.g. heptapod's grouped tools). The factory
    # owns `select` semantics and returns ready instances.
    make = getattr(mod, "make_tools", None)
    if callable(make):
        return list(make(base_directory, select=source.select,
                         config=_expand_config(source.options.get("config"))))
    # Otherwise the static convention: a `TOOLS` list (+ optional `BUNDLES`).
    all_tools = getattr(mod, "TOOLS", None)
    if all_tools is None:
        raise ValueError(
            f"python source {module!r} exposes neither a `TOOLS` list nor a "
            "`make_tools(base_directory, select=...)` factory."
        )
    bundles = getattr(mod, "BUNDLES", {}) or {}
    tools = _apply_select(all_tools, bundles, source.select, str(module))
    for t in tools:
        _maybe_set_base_directory(t, base_directory)
    return tools


# --------------------------------------------------------------------------
# toolbase source (stub until toolbase's library API ships)
# --------------------------------------------------------------------------
def resolve_toolbase_source(source: Source, base_directory: str) -> list:
    raise RuntimeError(
        "the `toolbase:` source backend is not available yet. Use a `python:` "
        "source instead (the no-toolbase escape hatch) — see "
        "docs/WORKFLOWS_SIMPLE.md (W1). "
        f"Offending source: {source.config!r}"
    )


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
        if nm in seen:
            raise ValueError(
                f"tool name collision: {nm!r} provided by both {seen[nm]} "
                f"and {label}; disable one in the loadout."
            )
        seen[nm] = label
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
        else:  # pragma: no cover - validated upstream
            raise ValueError(f"unknown source backend {src.backend!r}")
        label = f"{src.backend}:{src.config}"
        report["sources"].append({
            "backend": src.backend,
            "config": src.config,
            "select": src.select,
            "tools": [_tool_name(t) for t in stools],
        })
        for t in stools:
            _register(t, label)

    return tools, report
