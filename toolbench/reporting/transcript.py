"""
Per-trial transcript rendering.

Pure-string formatting helpers for the trial console.log / trial.txt:
header, single-line call records, and the END block. The live
`TrajectoryHook` in `eval/core/trajectory.py` pipes its events through
these helpers so the on-disk transcript and the live terminal output
stay identical.

Layout (plain ASCII, paper-appendix safe):

    ================================================================
                     TRIAL  <trial_id>
    ================================================================
      model     <model>            provider   <provider>
      task      <task>             seed       <seed>
      start     <YYYY-MM-DD HH:MM:SS>   condition  <condition>
    ----------------------------------------------------------------

    MM:SS.s   tool_name                 (X.Ys)  <summary>
    MM:SS.s   tool_name      FAIL       (X.Ys)  <summary>
              |-- <error reason>

    ----------------------------------------------------------------
      MM:SS.s  END
    ================================================================

      RESULT
      ------
          reach     <0..1>
          pass      yes|no
          failure   <FAILURE_MODE>
                    <one-line reason>

      COST
      ----
          cost      <$X.XX>     [(local via litellm) when zero]
          tokens    <in> in / <out> out / <cache_read> cache_read
          wall      <Xm Ys>

    ================================================================
"""

from datetime import datetime

from toolbench.core.failure_modes import NONE

# Column widths (kept here so the live hook and the post-hoc renderer
# agree). All counted in characters.
W_TIME      = 8
W_TOOL      = 24
W_STATUS    = 6   # "FAIL  " or "      "
W_DURATION  = 8

LINE_WIDTH  = 64
RULE_DOUBLE = "=" * LINE_WIDTH
RULE_SINGLE = "-" * LINE_WIDTH


# Args we never want to surface in the SUMMARY column — these are
# either schema boilerplate (timeout, debug, log_dir) or always-on
# defaults (cluster_all=True, mass_option=1) that add noise without
# information.
_NOISY_ARG_KEYS = {
    "timeout", "timeout_sec", "log_dir", "debug",
    "limit", "offset",
}


# Per-tool summary heuristics. For each tool name (case-insensitive,
# matching the wire-format orchestral lowercases to), the entry is a
# function (args_dict) -> str picking the most informative single
# argument or derived display. Tools not listed fall back to the
# generic "first non-noisy kwarg" heuristic.
def _summary_runcommand(args: dict) -> str:
    cmd = str(args.get("command", "")).strip()
    return _truncate_word(cmd, 60)


def _summary_readfile(args: dict) -> str:
    return str(args.get("path", ""))


def _summary_writefile(args: dict) -> str:
    return str(args.get("path", args.get("file_path", "")))


def _summary_editfile(args: dict) -> str:
    return str(args.get("path", args.get("file_path", "")))


def _summary_findfiles(args: dict) -> str:
    return str(args.get("pattern", args.get("path", "")))


def _summary_filesearch(args: dict) -> str:
    return str(args.get("query", args.get("pattern", "")))


def _summary_runpython(args: dict) -> str:
    code = str(args.get("code", "")).strip().splitlines()
    return _truncate_word(code[0] if code else "", 60)


def _summary_websearch(args: dict) -> str:
    return str(args.get("query", ""))


def _summary_todowrite(args: dict) -> str:
    body = str(args.get("todos", ""))
    n = sum(1 for ln in body.splitlines() if ln.lstrip().startswith("- ["))
    return f"{n} items"


def _summary_todoread(args: dict) -> str:
    return ""


def _summary_feynrules(args: dict) -> str:
    return str(args.get("model_path", args.get("output_dir", "")))


def _summary_madgraph(args: dict) -> str:
    return str(args.get("command_card", args.get("data_dir", "")))


def _summary_pythia(args: dict) -> str:
    return str(args.get("data_dir", args.get("cmnd_path", "")))


def _summary_jets(args: dict) -> str:
    algo = args.get("algorithm", "antikt")
    R = args.get("R", "")
    return f"R={R} {algo}".strip()


def _summary_gethardestn(args: dict) -> str:
    n = args.get("n_hardest", "?")
    inp = args.get("input_path", args.get("events_path", ""))
    return f"n={n} {inp}".strip()


def _summary_resonance(args: dict) -> str:
    tmpl = args.get("template")
    if tmpl:
        return f"template={tmpl}"
    return str(args.get("output_prefix", ""))


_TOOL_SUMMARIES = {
    "runcommand":              _summary_runcommand,
    "readfile":                _summary_readfile,
    "writefile":               _summary_writefile,
    "editfile":                _summary_editfile,
    "findfiles":               _summary_findfiles,
    "filesearch":              _summary_filesearch,
    "runpython":               _summary_runpython,
    "websearch":               _summary_websearch,
    "todowrite":               _summary_todowrite,
    "todoread":                _summary_todoread,
    "feynrulestoufo":          _summary_feynrules,
    "madgraphfromruncard":     _summary_madgraph,
    "pythiafromruncard":       _summary_pythia,
    "jetclusterslowjet":       _summary_jets,
    "gethardestn":             _summary_gethardestn,
    "resonancereconstruction": _summary_resonance,
}


def _truncate_word(s: str, n: int) -> str:
    """Truncate `s` to <= n chars on a word boundary, ellipsis if cut."""
    if len(s) <= n:
        return s
    cut = s.rfind(" ", 0, n - 1)
    if cut <= 0:
        cut = n - 1
    return s[:cut] + "…"


def call_summary(tool_name: str, args: dict) -> str:
    """Single-line SUMMARY column for a tool call."""
    fn = _TOOL_SUMMARIES.get(tool_name.lower())
    if fn is not None:
        try:
            return fn(args or {})
        except Exception:
            pass
    # Generic fallback: first non-noisy non-private kwarg.
    for k, v in (args or {}).items():
        if k.startswith("_") or k in _NOISY_ARG_KEYS:
            continue
        s = str(v)
        if len(s) > 60:
            s = s[:59] + "…"
        return f"{k}={s}"
    return ""


def fmt_elapsed(seconds: float) -> str:
    """MM:SS.s, or HH:MM:SS for runs over an hour. Width = W_TIME."""
    if seconds < 0:
        seconds = 0.0
    if seconds >= 3600:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    m = int(seconds // 60)
    s = seconds - 60 * m
    return f"{m:02d}:{s:04.1f}"


def fmt_wall(seconds: float) -> str:
    """Human-readable wall time: e.g. '4m30s', '0m21s', '1h05m'."""
    if seconds < 60:
        return f"0m{int(seconds):02d}s"
    m = int(seconds // 60)
    s = int(seconds % 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h = m // 60
    m = m % 60
    return f"{h}h{m:02d}m"


def fmt_duration(seconds: float) -> str:
    """Formatted call duration column, e.g. '(0.6s)' or '(17.6s)'.
    Width = W_DURATION characters when right-padded."""
    return f"({seconds:.1f}s)"


def render_header(*, trial_id: str, model: str, provider: str, task: str,
                  seed: int, condition: str,
                  start_dt: datetime | None = None) -> str:
    """The TRIAL banner emitted before any tool calls."""
    start = (start_dt or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    title = f"TRIAL  {trial_id}".center(LINE_WIDTH)
    lines = [
        RULE_DOUBLE,
        title,
        RULE_DOUBLE,
        f"  model     {model:<20} provider   {provider}",
        f"  task      {task:<20} seed       {seed}",
        f"  start     {start:<20} condition  {condition}",
        RULE_SINGLE,
    ]
    return "\n".join(lines)


# Multi-line tool arguments (Bash heredocs, file contents, code blobs) are
# rendered as a clearly-labelled, bounded block beneath the call line rather
# than spilling raw newlines into the column-aligned stream.
_BODY_MAX_LINES = 12        # lines shown before truncation
_BODY_MAX_CHARS = 1400      # total chars shown before truncation
_BODY_KEYS = ("command", "content", "new_string", "old_string", "data",
              "code", "file_text", "card_text", "query", "input")


def _oneline(s: str) -> str:
    """Collapse all whitespace (incl. newlines) to single spaces."""
    return " ".join(str(s).split())


def _primary_body(args: dict):
    """Return (label, text) for the most informative multi-line / long string
    argument worth showing as a block, or None. Prefers known body keys."""
    if not args:
        return None
    def _big(v):
        return isinstance(v, str) and ("\n" in v or len(v) > 160)
    for k in _BODY_KEYS:
        v = args.get(k)
        if _big(v):
            return k, v
    for k, v in args.items():
        if not k.startswith("_") and k not in _NOISY_ARG_KEYS and _big(v):
            return k, v
    return None


def render_call_line(*, t_start: float, name: str, args: dict,
                     duration: float, ok: bool, err_msg: str = "") -> list[str]:
    """One column-aligned line per completed tool call.

    Returns a list of rendered lines: the call line (always single-line, with
    whitespace collapsed so nothing spills), then — for a multi-line / long
    argument such as a Bash command or a written file — a clearly-labelled
    indented block (`+-- <arg> (N lines):` … `|   …` … `+-- (+K more)`),
    bounded by line and character caps. A failed call adds an indented
    `|-- error` continuation.
    """
    time_col   = fmt_elapsed(t_start).ljust(W_TIME)
    tool_col   = name.ljust(W_TOOL)
    status_col = ("FAIL" if not ok else "").ljust(W_STATUS)
    dur_col    = fmt_duration(duration).ljust(W_DURATION)
    summary    = _oneline(call_summary(name, args))
    line = f"{time_col}  {tool_col}  {status_col}{dur_col}  {summary}".rstrip()
    out = [line]

    indent = " " * (W_TIME + 2)
    body = _primary_body(args)
    if body is not None:
        label, text = body
        body_lines = text.splitlines() or [text]
        n = len(body_lines)
        out.append(f"{indent}+-- {label} ({n} line{'s' if n != 1 else ''}):")
        shown = chars = 0
        bp = indent + "|   "
        for bl in body_lines[:_BODY_MAX_LINES]:
            bl = bl.rstrip()
            if chars + len(bl) > _BODY_MAX_CHARS:
                bl = bl[:max(0, _BODY_MAX_CHARS - chars)] + "…"
            out.append(bp + _truncate_word(bl, LINE_WIDTH - len(bp)))
            shown += 1
            chars += len(bl)
            if chars >= _BODY_MAX_CHARS:
                break
        rem = n - shown
        if rem > 0:
            out.append(f"{indent}+-- (+{rem} more line{'s' if rem != 1 else ''}, truncated)")

    if (not ok) and err_msg:
        prefix = indent + "|-- "
        out.append(prefix + _truncate_word(_oneline(err_msg), LINE_WIDTH - len(prefix)))
    return out


def render_footer(*, end_t: float, reach: float, passed: bool,
                  failure_mode: str, failure_reason: str = "",
                  cost_usd: float | None, tokens: dict | None,
                  wall_s: float, cost_note: str = "") -> str:
    """The END / RESULT / COST block emitted after the last tool call."""
    end_label = f"  {fmt_elapsed(end_t)}  END"
    pass_str = "yes" if passed else "no"

    cost_line = (
        f"      cost      ${cost_usd:.2f}"
        if cost_usd is not None
        else "      cost      n/a"
    )
    if cost_note:
        cost_line = f"{cost_line:<32}{cost_note}"

    tk = tokens or {}
    tok_in = int(tk.get("input", 0))
    tok_out = int(tk.get("output", 0))
    tok_cache = int(tk.get("cache_read", 0))

    result_lines = [
        "  RESULT",
        "  ------",
        "",
        f"      reach     {reach:.2f}",
        f"      pass      {pass_str}",
    ]
    if failure_mode and failure_mode != NONE:
        result_lines.append("")
        result_lines.append(f"      failure   {failure_mode}")
        if failure_reason:
            wrapped = _wrap_under_label(failure_reason, indent=16,
                                        width=LINE_WIDTH)
            result_lines.extend(wrapped)
    cost_lines = [
        "  COST",
        "  ----",
        "",
        cost_line,
        f"      tokens    {tok_in:,} in  /  {tok_out:,} out  /  {tok_cache:,} cache_read",
        f"      wall      {fmt_wall(wall_s)}",
    ]

    out = [
        RULE_SINGLE,
        end_label,
        RULE_DOUBLE,
        "",
        *result_lines,
        "",
        *cost_lines,
        "",
        RULE_DOUBLE,
    ]
    return "\n".join(out)


def _wrap_under_label(text: str, *, indent: int, width: int) -> list[str]:
    """Wrap `text` so that each emitted line begins with `indent` spaces
    and stays within `width` characters total.
    """
    pad = " " * indent
    avail = max(20, width - indent)
    words = text.split()
    out: list[str] = []
    cur = ""
    for w in words:
        candidate = (cur + " " + w).strip() if cur else w
        if len(candidate) <= avail:
            cur = candidate
        else:
            out.append(pad + cur)
            cur = w
    if cur:
        out.append(pad + cur)
    return out
