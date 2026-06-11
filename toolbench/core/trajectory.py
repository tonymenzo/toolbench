"""
Trajectory recording for an agent run.

`TrajectoryHook` subclasses `orchestral.tools.hooks.ToolHook` (same
pattern as `examples/shared/tool_logger.py:ToolCallLogger`). It records
every (before_call, after_call) pair as a `ToolCall` on a shared
`Trajectory` instance and emits one column-aligned line per *completed*
call, mirrored to stdout (when verbose) and the per-trial console.log.
The rendering helpers live in `toolbench.reporting.transcript`.
"""

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from orchestral.tools.hooks import ToolHook, ToolHookResult

from toolbench.reporting.transcript import W_TIME, fmt_elapsed, render_call_line


_RESULT_TRUNCATE = 1000
_ERROR_TRUNCATE = 200
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


_USE_COLOR = _supports_color()
_DIM = "\033[2m" if _USE_COLOR else ""
_RESET = "\033[0m" if _USE_COLOR else ""


@dataclass
class ToolCall:
    t: float
    name: str
    args: dict
    duration_s: float
    ok: bool
    result_summary: str

    def to_dict(self) -> dict:
        return {
            "t": round(self.t, 4),
            "name": self.name,
            "args": self.args,
            "duration_s": round(self.duration_s, 4),
            "ok": self.ok,
            "result_summary": self.result_summary,
        }


@dataclass
class Trajectory:
    tool_calls: list[ToolCall] = field(default_factory=list)
    final_response: str = ""
    tokens: dict = field(default_factory=lambda: {"input": 0, "output": 0,
                                                  "cache_read": 0, "cache_creation": 0})
    cost_usd: float | None = None
    # The model id the provider actually served (usage.model_name — for
    # aliases like `claude-haiku-4-5` this is the dated snapshot). An
    # alias can re-route between snapshots mid-campaign, so the trial
    # record keeps proof of exactly what model produced it.
    resolved_model: str | None = None

    def to_metadata_dict(self) -> dict:
        """Compact summary for trial.json — the full tool-call list
        lives in transcript.jsonl.gz, so we don't duplicate it here.
        """
        return {
            "n_tool_calls": len(self.tool_calls),
            "n_tool_errors": sum(1 for tc in self.tool_calls if not tc.ok),
            "final_response": self.final_response,
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
            "resolved_model": self.resolved_model,
        }


def _safe_args(arguments) -> dict:
    try:
        return json.loads(json.dumps(arguments, default=str))
    except Exception:
        return {"_repr": str(arguments)}


def _classify_ok(result) -> bool:
    if isinstance(result, dict):
        if result.get("ok") is False:
            return False
        if "error" in result and result.get("error"):
            return False
    # Orchestral's tool contract returns errors as strings: tool
    # exceptions become "Error: Execution Error\n- Reason: ...", and
    # format_error() produces "Error: <kind>\n- Reason: ...". Without
    # this, every failed orchestral tool call renders as a clean
    # success in the transcript and console.log — 23 consecutive
    # path-resolution failures looked like a healthy trial.
    if isinstance(result, str) and result.lstrip().startswith("Error:"):
        return False
    return True


def _extract_error_msg(result) -> str:
    """Pull a clean one-liner error message from a tool result.

    Tool results commonly take the form of a dict with an `error`
    field; that is surfaced directly. For string results (e.g.
    tracebacks) the last line containing `error` / `exception` /
    `traceback` is returned.
    """
    if isinstance(result, dict):
        for key in ("error", "message", "stderr"):
            v = result.get(key)
            if v:
                msg = str(v).strip()
                if msg:
                    if len(msg) > _ERROR_TRUNCATE:
                        msg = msg[:_ERROR_TRUNCATE - 1] + "…"
                    return msg
    text = str(result).strip()
    # Orchestral error strings: "Error: <kind>" headline plus a
    # "- Reason: <detail>" line. Lead with the reason — it's the
    # actionable part ("run card does not exist") and the console line
    # is width-truncated, so a leading generic headline ("Error:
    # Execution Error - Reason: …") would push it off the edge.
    if text.startswith("Error:"):
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        kind = lines[0][len("Error:"):].strip()
        reason = next((ln[len("- Reason:"):].strip() for ln in lines
                       if ln.startswith("- Reason:")), "")
        msg = f"{kind}: {reason}" if reason else (kind or lines[0])
        if len(msg) > _ERROR_TRUNCATE:
            msg = msg[:_ERROR_TRUNCATE - 1] + "…"
        return msg
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return text[:_ERROR_TRUNCATE]
    last = lines[-1]
    for ln in reversed(lines):
        low = ln.lower()
        if "error" in low or "exception" in low or "traceback" in low:
            last = ln
            break
    if len(last) > _ERROR_TRUNCATE:
        last = last[:_ERROR_TRUNCATE - 1] + "…"
    return last


class TrajectoryHook(ToolHook):
    """Capture tool calls onto a Trajectory and render a styled transcript.

    One column-aligned line is emitted per *completed* tool call (no
    separate ▸-on-start, ✓-on-end pair). Failed calls add an indented
    `|--` continuation with the error reason. Output is mirrored to
    stdout when `verbose=True` and to the trial's console.log when
    `log_path` is set.

    Pairs `before_call` with `after_call` via a small LIFO stack keyed
    by tool name. Sequential agent execution only — Orchestral's runtime
    is sequential.
    """

    def __init__(self, trajectory: Trajectory, verbose: bool = False,
                 log_path: str | Path | None = None):
        self.trajectory = trajectory
        self.verbose = verbose
        self._t0 = time.monotonic()
        self._pending: list[tuple[str, dict, float]] = []
        self._log_fp = None
        if log_path is not None:
            p = Path(log_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._log_fp = open(p, "w", encoding="utf-8")

    def close(self) -> None:
        if self._log_fp is not None:
            self._log_fp.close()
            self._log_fp = None

    def write_to_log(self, text: str) -> None:
        """Append a free-form line (e.g. trial header / footer / crash trace)."""
        if self._log_fp is not None:
            self._log_fp.write(_strip_ansi(text).rstrip() + "\n")
            self._log_fp.flush()

    def _emit(self, line: str) -> None:
        if self.verbose:
            try:
                print(line, flush=True)
            except BrokenPipeError:
                # Caller's stdout pipe is gone (typical when running
                # under a background harness whose output capture
                # closed). Stop trying so a single closed pipe doesn't
                # crash the whole agent loop. The log file still gets
                # written below.
                self.verbose = False
        if self._log_fp is not None:
            try:
                self._log_fp.write(_strip_ansi(line) + "\n")
                self._log_fp.flush()
            except (BrokenPipeError, OSError):
                pass

    def before_call(self, tool_name: str, arguments: dict) -> ToolHookResult:
        args = _safe_args(arguments)
        self._pending.append((tool_name, args, time.monotonic()))
        return ToolHookResult(approved=True)

    def after_call(self, tool_name: str, result) -> ToolHookResult:
        match_idx = None
        for i in range(len(self._pending) - 1, -1, -1):
            if self._pending[i][0] == tool_name:
                match_idx = i
                break
        now = time.monotonic()
        if match_idx is not None:
            _, args, start = self._pending.pop(match_idx)
            duration = now - start
        else:
            args, duration = {}, 0.0

        result_str = str(result)
        if len(result_str) > _RESULT_TRUNCATE:
            result_str = result_str[:_RESULT_TRUNCATE] + "...[truncated]"

        ok = _classify_ok(result)
        t_start = now - duration - self._t0
        self.trajectory.tool_calls.append(ToolCall(
            t=now - self._t0,
            name=tool_name,
            args=args,
            duration_s=duration,
            ok=ok,
            result_summary=result_str,
        ))
        err_msg = "" if ok else _extract_error_msg(result)
        for line in render_call_line(
            t_start=t_start, name=tool_name, args=args,
            duration=duration, ok=ok, err_msg=err_msg,
        ):
            self._emit(line)
        return ToolHookResult(approved=True)


def make_agent_display_hook(traj_hook: TrajectoryHook,
                            text_truncate: int = 600,
                            indent: str = "     ") -> "Callable":  # noqa: F821
    """Return an Orchestral display_hook that prints LLM response text live.

    Orchestral calls the display_hook on every context update. We
    inspect newly-added Response messages, extract their text, and
    surface it in a dim block so the user can watch the agent reason
    between tool calls. Tool calls themselves are still rendered by
    `TrajectoryHook` (this hook does *not* duplicate them).

    Output is also tee'd through the same TrajectoryHook log file so
    the per-trial console.log captures both reasoning and tool calls
    in one stream.
    """
    state = {"last_index": 0}

    def display(context):
        try:
            messages = context.messages
        except AttributeError:
            return
        for i in range(state["last_index"], len(messages)):
            msg = messages[i]
            text = _extract_text(msg)
            if not text:
                continue
            time_col = fmt_elapsed(time.monotonic() - traj_hook._t0).ljust(W_TIME)
            traj_hook._emit(
                f"{time_col}  [agent]  {_truncate(text, text_truncate)}"
            )
        state["last_index"] = len(messages)

    return display


def _extract_text(msg) -> str:
    """Pull the text content out of a Response, if any.

    Filters out system/user/tool-role messages: the system prompt is
    fixed, the user prompt is the task, and tool results have their own
    rendering. We only want the assistant's reasoning/output text.
    """
    inner = getattr(msg, "message", None) or msg
    role = getattr(inner, "role", None)
    if role in {"tool", "system", "user"}:
        return ""
    text = getattr(inner, "text", None) or ""
    return text.strip()


def _truncate(text: str, n: int) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) > n:
        return text[:n - 1] + "…"
    return text
