"""
Provider-agnostic LLM constructor with a pluggable registry.

The harness ships with four built-in providers that pass through
directly to Orchestral's stock LLM classes (anthropic, openai,
google, groq) — credentials come from the standard provider env vars
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, ...), no adopter-side
configuration required.

Additional providers (ollama, vllm, litellm, custom routes, ...)
register themselves via `register_provider` at import time. The
registry keeps the harness core free of any deployment-specific
config: an adopter wires their ollama host, litellm proxy, vllm
cluster, etc. in an external adapter module that calls
`register_provider` on import.

Usage
-----

    from toolbench.core.llm_factory import build_llm

    # Built-in passthrough (no adapter needed):
    llm = build_llm(provider="anthropic", model="claude-haiku-4-5")

    # Custom provider:
    from toolbench.core.llm_factory import register_provider

    def my_factory(model=None, **kw):
        return MyCustomLLM(model=model, **kw)

    register_provider("my_route", my_factory)
    llm = build_llm(provider="my_route", model="foo")

`StubLLM` is returned regardless of provider when `dry_run=True`;
the runner short-circuits on it for harness-validation flows that
shouldn't spend tokens.
"""

import json
from typing import Any, Callable

from toolbench.core.json_repair import repair_tool_call_json


# Factory signature: (model, **kwargs) -> LLM instance. The returned
# object must satisfy Orchestral's LLM protocol (call_api /
# call_streaming_api / set_tools / ...).
ProviderFactory = Callable[..., Any]


_PROVIDERS: dict[str, ProviderFactory] = {}


def register_provider(name: str, factory: ProviderFactory) -> None:
    """Register a provider factory under `name` (case-insensitive).

    `factory` is invoked as `factory(model=..., **kwargs)` from
    `build_llm`. Re-registering the same name silently replaces the
    prior factory — useful for test fakes and adapter overrides.
    """
    _PROVIDERS[name.lower()] = factory


def registered_providers() -> list[str]:
    """Sorted list of currently-registered provider names."""
    return sorted(_PROVIDERS)


class StubLLM:
    """Zero-cost stub LLM for `--dry-run` mode.

    Skips network calls. The runner notices a StubLLM and
    short-circuits `agent.run()`, producing a synthetic empty
    trajectory used to validate the harness end-to-end without
    spending tokens.
    """

    is_stub = True
    model = "stub"

    def set_tools(self, tools):  # Mirror the LLM API surface.
        self.tools = tools

    def __repr__(self) -> str:
        return "StubLLM(dry-run)"


def build_llm(provider: str, model: str | None = None,
              dry_run: bool = False, **kwargs) -> Any:
    """Construct an LLM instance via the registered provider factory.

    Args:
        provider: provider name (case-insensitive).
        model: model id passed through to the factory.
        dry_run: if True, return a `StubLLM` regardless of provider.
        **kwargs: forwarded to the factory unchanged.

    Raises:
        ValueError: if `provider` isn't registered. The error message
            includes the list of currently-registered providers so
            the caller can see what's available.
    """
    if dry_run:
        return StubLLM()

    factory = _PROVIDERS.get(provider.lower())
    if factory is None:
        raise ValueError(
            f"Unknown provider: {provider!r}. "
            f"Registered: {registered_providers()}. "
            f"Add a custom provider with "
            f"`toolbench.core.llm_factory.register_provider(name, factory)`."
        )
    return factory(model=model, **kwargs)


def _install_tool_call_json_repair(gpt: Any) -> Any:
    """Wrap an Orchestral ``GPT`` so malformed tool-call argument JSON is
    repaired transparently instead of aborting the trial.

    Orchestral's ``GPT.process_api_response`` -> ``parse_tool_calls`` does
    a bare ``json.loads`` on each tool call's ``arguments`` string. A
    function-calling model (gpt-oss-120b) periodically emits a ``writefile``
    ``data`` blob with raw newlines / control chars / lone backslashes
    (e.g. ``$H_T^\\gamma$`` with a literal U+202F), so that ``json.loads``
    raises a ``JSONDecodeError`` that kills ``agent.run()`` and zeroes the
    trial.

    This wraps the *instance's* ``process_api_response`` (no site-packages
    edit, no behaviour change for any other provider or class). The happy
    path is byte-identical: the wrapper calls the stock method first, and
    only on a ``JSONDecodeError`` does it run the bounded, conservative
    ``repair_tool_call_json`` over each tool call's raw ``arguments`` and
    re-run the *original* parser on the repaired response — reusing all of
    Orchestral's parsing. If repair fails the original exception is
    re-raised unchanged, so the runner's MODEL_FORMAT_CRASH retry/crash
    path and the crash classifier see exactly what they saw before. We
    never accept un-parseable garbage and never evaluate model-authored
    expressions.
    """
    original = gpt.process_api_response

    def process_api_response(api_response):
        try:
            return original(api_response)
        except json.JSONDecodeError:
            if not _repair_api_response_tool_calls(api_response):
                raise  # nothing repairable -> identical failure as before
            # Re-parse the (possibly) repaired response with the stock
            # parser. If it still can't parse, that JSONDecodeError
            # propagates and is classified/retried exactly as before.
            return original(api_response)

    gpt.process_api_response = process_api_response
    return gpt


def _repair_api_response_tool_calls(api_response) -> bool:
    """Best-effort, in-place repair of tool-call ``arguments`` strings on a
    raw OpenAI-shape response. Returns True if any string was changed.

    Pure w.r.t. valid calls: ``repair_tool_call_json`` returns the input
    unchanged when it already parses, so well-formed calls are never
    touched. A call whose arguments are unrepairable (e.g. a Python
    expression) is left as-is, so the subsequent re-parse fails identically.
    """
    changed = False
    choices = getattr(api_response, "choices", None) or []
    for choice in choices:
        message = getattr(choice, "message", None)
        for call in getattr(message, "tool_calls", None) or []:
            fn = getattr(call, "function", None)
            raw = getattr(fn, "arguments", None)
            if not isinstance(raw, str):
                continue
            repaired = repair_tool_call_json(raw)
            if repaired is not None and repaired != raw:
                fn.arguments = repaired
                changed = True
    return changed


def _register_orchestral_passthroughs() -> None:
    """Register the four Orchestral-direct provider factories.

    These are pure passthroughs: no adopter config required, just the
    Orchestral class hierarchy. Credentials come from the standard
    provider env vars on construction.

    The provider's Orchestral class is imported lazily inside its
    factory, not at registration time, so a missing optional provider
    dependency (e.g. `google-genai` for Gemini) only surfaces if that
    provider is actually used. Importing the module or running a stub
    dry-run never pulls in every provider's deps.
    """
    def _passthrough(class_name: str):
        def factory(model: str | None = None, **kwargs):
            import importlib
            cls = getattr(importlib.import_module("orchestral.llm"), class_name)
            llm = cls(model=model, **kwargs) if model else cls(**kwargs)
            # GPT is the only stock class whose parser does a bare
            # json.loads on tool-call arguments; harden just that one.
            if class_name == "GPT":
                llm = _install_tool_call_json_repair(llm)
            return llm
        factory.__name__ = f"_factory_{class_name}"
        return factory

    register_provider("anthropic", _passthrough("Claude"))
    register_provider("openai",    _passthrough("GPT"))
    register_provider("google",    _passthrough("Gemini"))
    register_provider("groq",      _passthrough("Groq"))


def _register_litellm_provider() -> None:
    """Register the `litellm` provider.

    An Orchestral `GPT` (OpenAI chat-completions wire) pointed at a LiteLLM
    proxy by overriding `client.base_url` — exactly what a LiteLLM proxy
    expects, so it routes to whatever backend serves the requested model
    (e.g. `gpt-oss-120b` on Fermilab's litellm.fnal.gov). Reads `LITELLM_HOST`
    (proxy base URL incl. `/v1`) and `LITELLM_API_KEY` from the environment
    (.env). Generic infrastructure — no heptapod dependency.
    """
    def factory(model: str | None = None, **kwargs):
        import os
        import openai
        from orchestral.llm import GPT
        from orchestral.llm.base.llm import LLM

        host = os.getenv("LITELLM_HOST", "https://litellm.fnal.gov/v1")
        api_key = os.getenv("LITELLM_API_KEY", "")
        if not model:
            raise ValueError(
                "the litellm provider needs an explicit model id "
                "(query <LITELLM_HOST>/models for what the proxy routes)."
            )
        gpt = GPT.__new__(GPT)
        LLM.__init__(gpt, tools=None)
        gpt.model = model
        gpt.api_key = api_key
        gpt.client = openai.Client(api_key=api_key, base_url=host, timeout=60.0)
        return _install_tool_call_json_repair(gpt)

    factory.__name__ = "_factory_litellm"
    register_provider("litellm", factory)


class SubscriptionLLM:
    """Placeholder LLM for runtimes that drive their own model process.

    The `claude_code` runtime shells out to the `claude` CLI under the
    user's subscription auth, so the runner's in-process `llm` is never
    called. This needs NO credentials — building it must not require an
    `ANTHROPIC_API_KEY`. It is deliberately NOT a `StubLLM` (which the
    runner treats as a dry-run and short-circuits `agent.run()`); the
    claude_code agent must actually run.
    """

    is_stub = False

    def __init__(self, model: str | None = None, **_):
        self.model = model or "subscription"

    def set_tools(self, tools):  # Mirror the LLM API surface.
        self.tools = tools

    def __repr__(self) -> str:
        return f"SubscriptionLLM(model={self.model!r})"


def _register_subscription_provider() -> None:
    """Register the `subscription` provider.

    A credential-free placeholder for harnesses whose runtime drives an
    external, subscription-authenticated agent process (e.g. `claude_code`
    via the `claude` CLI). `build_llm` constructs it with no env vars, and
    the runtime ignores it. Generic infrastructure — no heptapod dependency.
    """
    def factory(model: str | None = None, **kwargs):
        return SubscriptionLLM(model=model)

    factory.__name__ = "_factory_subscription"
    register_provider("subscription", factory)


_register_orchestral_passthroughs()
_register_litellm_provider()
_register_subscription_provider()
