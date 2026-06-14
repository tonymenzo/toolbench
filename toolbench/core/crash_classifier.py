"""
Classify exceptions raised by `agent.run()` into a structured failure
mode + a human-readable reason.

The `RuleJudge` records the failure mode in the rubric grade. Distinct
modes give us first-class signal in the failure-mode taxonomy (see
`toolbench/reporting/plot_overview.py`); a clean reason string keeps the
trial footer / summary readable instead of dumping a Python traceback.

Currently classified:

- `MODEL_FORMAT_CRASH`: `JSONDecodeError` from inside Orchestral's
  OpenAI tool-call parser. gpt-oss (and similar function-calling
  models) periodically emit malformed JSON in their tool-call
  arguments — empty strings, truncated objects, leaked Harmony
  channel markers. Frequency scales with context length.
- `CONTEXT_LENGTH_EXCEEDED`: the conversation outgrew the model's
  context window and the provider rejected the request (HTTP 400 /
  `context_length_exceeded`). Long, tool-heavy trials hit this — the
  full message history + the ~26 tool schemas are resent every turn.
  An operational failure, not a capability one.
- `TRANSIENT_API_ERROR`: a transient transport/server fault on the way
  to the provider — connect/read timeout, dropped connection, or an
  HTTP 5xx (500/502/503/504). Distinct from RATE_LIMITED (429/529, an
  intentional throttle) and from a model failure: the request never got
  a well-formed answer because the endpoint blipped. The runner retries
  these with backoff so one unreachable-endpoint window doesn't zero out
  a whole campaign's worth of trials.
- `AGENT_CRASH`: anything else uncaught from `agent.run()`.

Add new classifications here when a new failure pattern shows up
often enough to deserve its own bucket.
"""

import json
import re

from .failure_modes import (
    AGENT_CRASH, CONTEXT_LENGTH_EXCEEDED, MODEL_FORMAT_CRASH, RATE_LIMITED,
    TRANSIENT_API_ERROR,
)


# Snippet of the raw bad JSON we surface in the reason. Long enough to
# fingerprint the failure, short enough to stay on one console line.
_RAW_SNIPPET_LEN = 120

# Substrings that mark a context-window overflow across providers
# (OpenAI: `context_length_exceeded` / "maximum context length";
# vLLM/litellm: "exceeds model's maximum context length").
_CONTEXT_MARKERS = (
    "maximum context length",
    "context_length_exceeded",
    "exceeds the model's maximum",
    "exceeds model's maximum",
    "reduce the length of the messages",
)

# Markers for provider throttling / load-shedding, matched lowercase.
# Anthropic: 429 `rate_limit_error`, 529 `overloaded_error` (both raised
# as RateLimitError / APIStatusError); OpenAI: 429 `rate_limit_exceeded`
# / `insufficient_quota`; proxies tend to pass the upstream text through.
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "overloaded_error",
    "too many requests",
    "insufficient_quota",
)
# Bare status codes, matched as standalone tokens to avoid firing on an
# unrelated number inside a traceback.
_RATE_LIMIT_STATUS_RE = re.compile(r"\b(?:429|529)\b")

# Exception *type* names (matched lowercase, substring) that always mean
# a transient transport fault, independent of message text. Covers the
# OpenAI SDK (APITimeoutError / APIConnectionError) and the httpx /
# httpcore stack underneath it (ConnectTimeout / ReadTimeout /
# ConnectError / PoolTimeout) plus the stdlib ConnectionError family.
_TRANSIENT_TYPE_MARKERS = (
    "apitimeouterror",
    "apiconnectionerror",
    "connecttimeout",
    "readtimeout",
    "writetimeout",
    "pooltimeout",
    "connecterror",
    "connectionerror",
    "remoteprotocolerror",
)
# Message markers for transient transport / server-side faults, matched
# lowercase. HTTP 5xx is the provider failing to serve a well-formed
# response (distinct from 429/529 throttling, classified above first).
_TRANSIENT_MSG_MARKERS = (
    "timed out",
    "timeout",
    "connection error",
    "connection reset",
    "connection aborted",
    "connection refused",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "server disconnected",
    "server error",
)
# Bare 5xx status tokens, matched standalone so an unrelated number in a
# traceback can't trip the classifier.
_TRANSIENT_STATUS_RE = re.compile(r"\b(?:500|502|503|504)\b")


def classify_crash(exc: BaseException, traceback_str: str) -> tuple[str, str]:
    """Map an `agent.run()` exception to (failure_mode, reason).

    Args:
        exc: the exception object caught at the agent.run() call site.
        traceback_str: the formatted traceback (so the classifier can
            inspect frames from third-party packages without importing
            them).

    Returns:
        (failure_mode, reason). Both go into the trial's grade. The
        failure_mode is one of the constants in
        `toolbench.core.failure_modes`.
    """
    if _is_tool_call_json_decode_error(exc, traceback_str):
        return MODEL_FORMAT_CRASH, _format_tool_call_decode_reason(exc)

    if _is_rate_limit_error(exc, traceback_str):
        return RATE_LIMITED, _rate_limit_reason(exc)

    if _is_context_length_error(exc, traceback_str):
        return CONTEXT_LENGTH_EXCEEDED, _context_length_reason(exc, traceback_str)

    if _is_transient_api_error(exc, traceback_str):
        return TRANSIENT_API_ERROR, _transient_reason(exc)

    return AGENT_CRASH, _short_reason(exc, traceback_str)


def _is_rate_limit_error(exc: BaseException, traceback_str: str) -> bool:
    """True if the provider throttled (429) or shed load (529/overloaded).

    Matched on the exception type name (`RateLimitError` across the
    OpenAI and Anthropic SDKs) plus provider-agnostic message markers,
    so proxied backends classify too.
    """
    if "ratelimit" in type(exc).__name__.lower():
        return True
    blob = f"{exc} {traceback_str}".lower()
    if any(marker in blob for marker in _RATE_LIMIT_MARKERS):
        return True
    return bool(_RATE_LIMIT_STATUS_RE.search(str(exc)))


def _rate_limit_reason(exc: BaseException) -> str:
    head = f"{type(exc).__name__}: {exc}".replace("\n", " ").strip()
    if len(head) > 160:
        head = head[:159] + "…"
    return f"provider throttled the request: {head}"


def _is_transient_api_error(exc: BaseException, traceback_str: str) -> bool:
    """True if the request failed on a transient transport / server fault.

    Matched on the exception type-name chain (so an `APITimeoutError`
    wrapping an httpx `ConnectTimeout` classifies regardless of message)
    plus provider-agnostic message markers and bare 5xx status tokens.
    Called only *after* the rate-limit and context-length checks, so a
    429/529 throttle or a 400 context overflow can never be misfiled
    here.
    """
    names = f"{type(exc).__name__} {traceback_str}".lower()
    if any(marker in names for marker in _TRANSIENT_TYPE_MARKERS):
        return True
    blob = f"{exc} {traceback_str}".lower()
    if any(marker in blob for marker in _TRANSIENT_MSG_MARKERS):
        return True
    return bool(_TRANSIENT_STATUS_RE.search(str(exc)))


def _transient_reason(exc: BaseException) -> str:
    head = f"{type(exc).__name__}: {exc}".replace("\n", " ").strip()
    if len(head) > 160:
        head = head[:159] + "…"
    return f"transient transport/server fault reaching the provider: {head}"


def _is_context_length_error(exc: BaseException, traceback_str: str) -> bool:
    """True if the provider rejected the request for exceeding the context
    window (matched on the error text, so it's provider-agnostic)."""
    blob = f"{exc} {traceback_str}".lower()
    return any(marker in blob for marker in _CONTEXT_MARKERS)


def _context_length_reason(exc: BaseException, traceback_str: str) -> str:
    """Compact reason; surfaces the token counts when the provider gives them."""
    blob = f"{exc} {traceback_str}"
    m = re.search(r"[Ii]nput length \((\d+)\).*?maximum context length \((\d+)\)", blob)
    if m:
        return (f"context window exceeded: input {m.group(1)} tokens "
                f"> limit {m.group(2)}")
    return "context window exceeded (provider rejected request: too many input tokens)"


def _is_tool_call_json_decode_error(exc: BaseException,
                                    traceback_str: str) -> bool:
    """True if this is the gpt-oss tool-call argument decode failure."""
    if not isinstance(exc, json.JSONDecodeError):
        return False
    return any(marker in traceback_str for marker in (
        "parse_tool_calls",
        "openai/parsers",
        "orchestral/llm/openai",
    ))


def _format_tool_call_decode_reason(exc: json.JSONDecodeError) -> str:
    """Compact reason line for a tool-call JSON decode failure."""
    raw = exc.doc or ""
    snippet = raw[:_RAW_SNIPPET_LEN].replace("\n", " ").replace("\r", " ")
    if len(raw) > _RAW_SNIPPET_LEN:
        snippet += "…"
    snippet_part = f"raw={snippet!r}" if raw else "raw=<empty>"
    return (
        f"model emitted malformed tool-call JSON: "
        f"{exc.msg} at char {exc.pos}; {snippet_part}"
    )


def _short_reason(exc: BaseException, traceback_str: str) -> str:
    """Generic fallback reason: exception type + message + first frame."""
    head = f"{type(exc).__name__}: {exc}".replace("\n", " ").strip()
    if len(head) > 200:
        head = head[:199] + "…"
    return f"crash: {head}"
