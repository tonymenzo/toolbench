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

from typing import Any, Callable


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
            if model:
                return cls(model=model, **kwargs)
            return cls(**kwargs)
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
        return gpt

    factory.__name__ = "_factory_litellm"
    register_provider("litellm", factory)


_register_orchestral_passthroughs()
_register_litellm_provider()
