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

from typing import Any, Callable


RuntimeFactory = Callable[..., Any]

_RUNTIMES: dict[str, RuntimeFactory] = {}


def register_runtime(name: str, factory: RuntimeFactory) -> None:
    """Register a runtime factory under `name` (case-insensitive).

    Re-registering the same name silently replaces the prior factory —
    useful for test fakes and adapter overrides.
    """
    _RUNTIMES[name.lower()] = factory


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


def _orchestral_factory(*, llm, tools, tool_hooks, system_prompt,
                        display_hook=None):
    from orchestral import Agent
    return Agent(
        llm=llm, tools=tools, tool_hooks=tool_hooks,
        system_prompt=system_prompt, debug=False,
        display_hook=display_hook,
    )


register_runtime("orchestral", _orchestral_factory)
