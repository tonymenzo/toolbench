"""Token rates, effective input, and cost — sourced from orchestral.

WHY NOT A TABLE IN THIS REPO. There were two, and both had rotted. The metered
one was down to six models (`gpt-4o`, `gemini-2.0-flash`); the subscription one
held exactly `gpt-5.5` and `gpt-5.6-sol`, so it returned None for every Claude
model and nothing noticed, because the claude-code runtime happened to report a
cost of its own. Neither carried a cache-WRITE rate at all -- the field whose
absence made a hand computation 11% low.

orchestral ships a `PricingModel` per provider, keyed by exact model id, with
read and write cache rates and dated snapshots covered
(`claude-haiku-4-5-20251001` as well as `claude-haiku-4-5`). toolbench already
depends on orchestral, so reading rates from there is not a new dependency --
it is one fewer copy to forget to update.

TWO SHARP EDGES THIS MODULE EXISTS TO BLUNT.

`PricingModel.get_cost` returns 0.0 for a model it has never heard of, which is
the same value a genuinely free model returns. Membership is therefore checked
against `rates` directly, and an unknown model comes back as None so a caller
can say "no rate source" instead of printing a confident zero.

Providers disagree on field names and on what is billable: Anthropic uses
`cache_creation_input_tokens` / `cache_read_input_tokens`, while OpenAI and groq
use `cached_prompt_tokens` and have no write rate at all, because they do not
charge for writes. That is a rate of zero -- a fact -- not a missing field to be
guessed at, so `lookup_rates` normalizes both shapes and fills 0.0 where a
provider genuinely does not bill.
"""

from __future__ import annotations

from typing import Mapping, Optional

# Provider pricing models, tried in turn. Model ids are distinctive enough
# (`claude-*`, `gpt-*`, `openai/gpt-oss-*`) that a search is unambiguous, and
# it keeps the harness's provider name -- "subscription" for both CLI runtimes
# -- out of the lookup, which would otherwise match nothing.
_PROVIDER_MODULES = (
    "orchestral.llm.anthropic.pricing_model",
    "orchestral.llm.openai.pricing_model",
    "orchestral.llm.groq.pricing_model",
    "orchestral.llm.google.pricing_model",
    "orchestral.llm.mistral.pricing_model",
    "orchestral.llm.vllm.pricing_model",
    "orchestral.llm.ollama.pricing_model",
)

#: orchestral token-type key -> our normalized name. Both spellings of the
#: cache-read rate map to one field; a provider that bills no cache writes
#: simply has no key, and gets 0.0.
_FIELD_ALIASES = {
    "prompt_tokens": "input",
    "completion_tokens": "output",
    "cache_read_input_tokens": "cache_read",
    "cached_prompt_tokens": "cache_read",
    "cache_creation_input_tokens": "cache_write",
}


def _pricing_models():
    import importlib
    for path in _PROVIDER_MODULES:
        try:
            yield importlib.import_module(path).pricing_model
        except Exception:      # a provider orchestral no longer ships
            continue


def lookup_rates(model: str) -> Optional[dict]:
    """Per-million-token rates for `model`, or None if no provider lists it.

    None means "no rate source", which is a different statement from "free" --
    see the module docstring. Keys are always `input`, `output`, `cache_read`
    and `cache_write`, whatever the provider called them.
    """
    if not model:
        return None
    for pm in _pricing_models():
        raw = getattr(pm, "rates", {}).get(model)
        if raw is None:
            continue
        out = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
        for key, value in raw.items():
            field = _FIELD_ALIASES.get(key)
            if field is not None:
                out[field] = float(value)
        return out
    return None


def _raw_input(tokens: Mapping[str, int]) -> int:
    return (int(tokens.get("input", 0) or 0)
            + int(tokens.get("cache_read", 0) or 0)
            + int(tokens.get("cache_creation", 0) or 0))


def effective_input_tokens(
    tokens: Mapping[str, int],
    rates: Optional[Mapping[str, float]],
    *,
    authoritative_cost_usd: Optional[float] = None,
) -> Optional[float]:
    """Input expressed in uncached-equivalent tokens — the figure proportional
    to what the input actually costs.

    With `authoritative_cost_usd` (a figure the runtime itself reported, as the
    claude CLI does), it is SOLVED from that cost rather than recomputed from
    rates. Those disagree in practice: orchestral prices Anthropic cache writes
    at the 5m tier, 1.25x base, while the claude CLI bills 1h writes at 2x, so
    a rate-derived number understates a claude-code run by the same margin a
    hand calculation did. Solving keeps this line and the cost line on one
    basis, so dividing one by the other yields the rate actually charged
    instead of a contradiction.
    """
    if not rates:
        return None
    r_in, r_out = rates.get("input", 0.0), rates.get("output", 0.0)
    if authoritative_cost_usd is not None and r_in:
        output_cost = int(tokens.get("output", 0) or 0) * r_out / 1e6
        return max(0.0, (authoritative_cost_usd - output_cost) * 1e6 / r_in)
    return (int(tokens.get("input", 0) or 0)
            + rates.get("cache_read", 0.0) / r_in * int(tokens.get("cache_read", 0) or 0)
            + rates.get("cache_write", 0.0) / r_in * int(tokens.get("cache_creation", 0) or 0)
            ) if r_in else None


def rate_derived_cost(tokens: Mapping[str, int],
                      rates: Optional[Mapping[str, float]]) -> Optional[float]:
    """Cost from rates, counting each token class at its own rate."""
    if not rates:
        return None
    return (int(tokens.get("input", 0) or 0) * rates.get("input", 0.0)
            + int(tokens.get("cache_read", 0) or 0) * rates.get("cache_read", 0.0)
            + int(tokens.get("cache_creation", 0) or 0) * rates.get("cache_write", 0.0)
            + int(tokens.get("output", 0) or 0) * rates.get("output", 0.0)) / 1e6


def no_cache_upper_bound(tokens: Mapping[str, int],
                         rates: Optional[Mapping[str, float]]) -> Optional[float]:
    """What the same work would cost with no caching at all.

    Needs only the base input/output rates -- the likeliest pair to be
    available -- and brackets the answer from above, which makes the saving
    caching bought legible without asserting anything about cache tiers.
    """
    if not rates:
        return None
    return (_raw_input(tokens) * rates.get("input", 0.0)
            + int(tokens.get("output", 0) or 0) * rates.get("output", 0.0)) / 1e6
