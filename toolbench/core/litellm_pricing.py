"""
LiteLLM proxy pricing fetch.

A LiteLLM proxy exposes its admin-configured per-model pricing on
`<host>/v1/model/info`. Querying that once at run start gives us
authoritative input/cache-read/output rates without needing a hand-
maintained pricing table that drifts every time Anthropic or OpenAI
updates their list price.

Returned format (per model):

    {"input": <usd_per_token>,
     "cache_read": <usd_per_token>,
     "output": <usd_per_token>}

Free / locally-hosted models report all three as 0. The runner uses
the table as a fallback when the per-Response `usage.cost` field is
not populated by the proxy (typical for open-weights backends).
"""

import json
import os
import sys
from typing import Optional
from urllib.request import Request, urlopen


# Cache the pricing snapshot for the lifetime of the Python process.
_CACHE: dict[str, dict] | None = None


def _coerce_float(v) -> float:
    """0.0 for None / strings / anything non-numeric."""
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def fetch_litellm_pricing(host: str,
                          api_key: str | None = None,
                          timeout: float = 10.0,
                          force_refresh: bool = False
                          ) -> dict[str, dict[str, float]]:
    """Query `<host>/v1/model/info` and return a pricing dict.

    The result is keyed by `model_name` (e.g. `openai/gpt-oss-120b`,
    `azure/claude-haiku-4-5`) and each value is a dict with keys
    `input`, `cache_read`, `output` — all per-token USD.

    Cached for the process. Pass `force_refresh=True` to re-query.

    Raises on network/HTTP errors so the caller can decide whether to
    fall back to the static `metrics.PRICING_TABLE`.
    """
    global _CACHE
    if _CACHE is not None and not force_refresh:
        return _CACHE

    url = f"{host.rstrip('/')}/model/info"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read())

    out: dict[str, dict[str, float]] = {}
    for entry in payload.get("data", []):
        info = entry.get("model_info") or {}
        out[entry["model_name"]] = {
            "input":      _coerce_float(info.get("input_cost_per_token")),
            "cache_read": _coerce_float(info.get("cache_read_input_token_cost")),
            "output":     _coerce_float(info.get("output_cost_per_token")),
        }
    _CACHE = out
    return out


def fetch_pricing_from_env() -> dict[str, dict[str, float]] | None:
    """Best-effort: pull pricing from the configured litellm proxy.

    The proxy URL is read from the `LITELLM_HOST` environment variable;
    the API key (optional) from `LITELLM_API_KEY`. Use at run start to
    capture a snapshot for the manifest. Returns None when no host is
    configured or the fetch fails — the runner then falls back to the
    static PRICING_TABLE in `metrics.py`.

    Adopters wiring a proxy from a config module should set
    `LITELLM_HOST` at startup, typically from an adapter that reads
    the host out of the host project's config and calls
    `os.environ.setdefault`.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    host = os.getenv("LITELLM_HOST")
    if not host:
        return None

    api_key = os.getenv("LITELLM_API_KEY")
    try:
        return fetch_litellm_pricing(host, api_key=api_key)
    except Exception as e:
        print(f"warning: could not fetch litellm pricing from {host}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return None


def cost_from_proxy(pricing: dict[str, dict[str, float]] | None,
                    model: str,
                    input_tokens: int = 0,
                    output_tokens: int = 0,
                    cache_read_tokens: int = 0) -> Optional[float]:
    """Compute USD cost from a proxy pricing snapshot. Returns None
    when the model isn't in the snapshot.
    """
    if not pricing or model not in pricing:
        return None
    p = pricing[model]
    return (input_tokens * p["input"]
            + cache_read_tokens * p["cache_read"]
            + output_tokens * p["output"])
