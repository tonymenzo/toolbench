"""
Classify exceptions raised by `agent.run()` into a structured failure
mode + a human-readable reason.

The `RuleJudge` records the failure mode in the rubric grade. Distinct
modes give us first-class signal in the failure-mode taxonomy (see
`eval/reporting/plot_overview.py`); a clean reason string keeps the
trial footer / summary readable instead of dumping a Python traceback.

Currently classified:

- `MODEL_FORMAT_CRASH`: `JSONDecodeError` from inside Orchestral's
  OpenAI tool-call parser. gpt-oss (and similar function-calling
  models) periodically emit malformed JSON in their tool-call
  arguments — empty strings, truncated objects, leaked Harmony
  channel markers. Frequency scales with context length. See
  `eval/docs/model_notes.md`.
- `CONTEXT_LENGTH_EXCEEDED`: the conversation outgrew the model's
  context window and the provider rejected the request (HTTP 400 /
  `context_length_exceeded`). Long, tool-heavy trials hit this — the
  full message history + the ~26 tool schemas are resent every turn.
  An operational failure, not a capability one.
- `AGENT_CRASH`: anything else uncaught from `agent.run()`.

Add new classifications here when a new failure pattern shows up
often enough to deserve its own bucket.
"""

import json
import re

from .failure_modes import (
    AGENT_CRASH, CONTEXT_LENGTH_EXCEEDED, MODEL_FORMAT_CRASH,
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

    if _is_context_length_error(exc, traceback_str):
        return CONTEXT_LENGTH_EXCEEDED, _context_length_reason(exc, traceback_str)

    return AGENT_CRASH, _short_reason(exc, traceback_str)


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
