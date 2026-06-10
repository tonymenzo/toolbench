"""
Agent-runtime registry.

A harness names its runtime (`runtime: {name: orchestral}`); this module
maps that name to a factory that constructs the agent driving the trial.
Mirrors the provider registry in `llm_factory.py`: the framework ships
the `orchestral` runtime, and adopters register additional runtimes
(claude_code, codex, a domain harness, ...) at import time via
`register_runtime` — the CLI validates every harness's runtime name
against this registry before any trial runs, so a harness claiming an
unregistered runtime fails fast instead of silently running orchestral.

Factory contract
----------------

    factory(*, llm, tools, tool_hooks, system_prompt, display_hook) -> agent

The returned agent must provide:

  - `run(message: str, max_iterations: int = ..., **llm_kwargs)` —
    execute the agent loop, returning a final response object (ideally
    with `.text` and `.tool_calls` attributes; both are read defensively).
    Must be *resumable*: a subsequent `run(new_message)` continues the
    same session/context (the runner uses this for format-crash retries
    and continue-nudges). `**llm_kwargs` carries the harness's provider
    request params (e.g. `max_tokens`) through to the model call.
  - `context.messages` — iterable of messages for token/cost extraction.
    Runtimes whose context is not orchestral-shaped still work; usage
    extraction degrades to a stderr warning and a missing cost.

Runtimes whose core tools are built in (`core: {builtin: true}`) receive
`tools=[]`; `tool_hooks` / `display_hook` may be ignored by runtimes
that have their own transcript capture, but then tool-call trajectories
will be empty in the trial record unless the adapter bridges them.
"""

import sys
from typing import Any, Callable


RuntimeFactory = Callable[..., Any]

_RUNTIMES: dict[str, RuntimeFactory] = {}
# runtime name -> installed distribution that implements it, used by
# `check_runtime_version` to enforce a harness's `runtime.version` spec.
_RUNTIME_DISTS: dict[str, str] = {}


def register_runtime(name: str, factory: RuntimeFactory,
                     dist: str | None = None) -> None:
    """Register a runtime factory under `name` (case-insensitive).

    `dist` names the installed distribution implementing the runtime
    (e.g. "orchestral-ai") so a harness's `runtime.version` spec can be
    enforced against it; omit it and version specs for this runtime are
    skipped with a warning. Re-registering the same name silently
    replaces the prior factory — useful for test fakes and overrides.
    """
    _RUNTIMES[name.lower()] = factory
    if dist:
        _RUNTIME_DISTS[name.lower()] = dist


def registered_runtimes() -> list[str]:
    """Sorted list of currently-registered runtime names."""
    return sorted(_RUNTIMES)


def build_agent(runtime_name: str, *, llm, tools, tool_hooks,
                system_prompt: str, display_hook=None) -> Any:
    """Construct the agent for `runtime_name` via its registered factory.

    Raises ValueError when the runtime isn't registered (the CLI also
    pre-validates, so reaching that error here means a programmatic
    caller skipped validation).
    """
    factory = _RUNTIMES.get(runtime_name.lower())
    if factory is None:
        raise ValueError(
            f"Unknown runtime: {runtime_name!r}. "
            f"Registered: {registered_runtimes()}. "
            f"Add one with `toolbench.core.runtime.register_runtime(name, factory)`."
        )
    return factory(llm=llm, tools=tools, tool_hooks=tool_hooks,
                   system_prompt=system_prompt, display_hook=display_hook)


def check_runtime_version(runtime_name: str, spec: str | None, *,
                          installed: str | None = None) -> str | None:
    """Enforce a harness's `runtime.version` spec (PEP 440, e.g. ">=1.3").

    Returns an error message when the installed runtime distribution
    does not satisfy `spec`, None when it does — or when the check can't
    be performed (no spec; runtime registered without a `dist`; the
    `packaging` library unavailable), in which case a stderr warning
    notes the skip so an unenforced pin is at least visible.

    `installed` overrides the metadata lookup (tests).
    """
    if not spec:
        return None
    name = runtime_name.lower()
    if installed is None:
        dist = _RUNTIME_DISTS.get(name)
        if dist is None:
            print(f"warning: runtime {runtime_name!r} was registered without "
                  f"a `dist`; cannot enforce runtime.version {spec!r}.",
                  file=sys.stderr)
            return None
        try:
            import importlib.metadata
            installed = importlib.metadata.version(dist)
        except Exception:
            return (f"runtime {runtime_name!r}: cannot read the installed "
                    f"version of {dist!r} to enforce runtime.version {spec!r}.")
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version
    except ImportError:
        print(f"warning: `packaging` is unavailable; runtime.version {spec!r} "
              f"for {runtime_name!r} was NOT enforced.", file=sys.stderr)
        return None
    try:
        ok = SpecifierSet(str(spec)).contains(Version(installed),
                                              prereleases=True)
    except Exception as e:
        return (f"runtime {runtime_name!r}: invalid runtime.version spec "
                f"{spec!r} ({type(e).__name__}: {e}).")
    if not ok:
        return (f"runtime {runtime_name!r}: installed version {installed} "
                f"does not satisfy the harness's runtime.version {spec!r}.")
    return None


def _orchestral_factory(*, llm, tools, tool_hooks, system_prompt,
                        display_hook=None):
    from orchestral import Agent
    return Agent(
        llm=llm, tools=tools, tool_hooks=tool_hooks,
        system_prompt=system_prompt, debug=False,
        display_hook=display_hook,
    )


register_runtime("orchestral", _orchestral_factory, dist="orchestral-ai")
