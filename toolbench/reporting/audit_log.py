"""
Per-trial audit log — a full, human-readable trajectory.

Renders every tool call the agent made, with its COMPLETE input fields, result
summary, timing, and pass/fail, plus the graded rubric and the final response /
UX feedback. Text-first (`audit.txt`) so it works on a headless terminal and is
paper-appendix safe; `render_trial_audit_html` produces an optional collapsible
HTML twin from the same data.

Source of truth is the trial transcript (`transcript.jsonl.gz`: one `tool_call`
record per call with the full `args`, plus an `assistant` final and an optional
`ux_feedback`), joined with the trial row for the grade. Pure formatting — no
matplotlib, no network.
"""

import html
import json
from pathlib import Path
from typing import Sequence

_W = 80


def _rule(ch: str = "=") -> str:
    return ch * _W


def _center(s: str) -> str:
    return s.center(_W)


def _fmt_ts(t) -> str:
    """Seconds-from-start -> MM:SS.s."""
    t = float(t or 0.0)
    m = int(t // 60)
    return f"{m:02d}:{t - 60 * m:04.1f}"


def _fmt_field(key: str, value, indent: str, cap: int = 600) -> list[str]:
    """One input field as `key: <json>`, wrapping over-long values onto a
    truncated single line so the log stays scannable but complete-ish."""
    vs = json.dumps(value, ensure_ascii=False)
    if len(vs) > cap:
        vs = vs[:cap] + f" … (+{len(vs) - cap} chars)"
    return [f"{indent}{key}: {vs}"]


def _grade_block(row: dict) -> list[str]:
    stages = row.get("stages") or {}
    credits = row.get("stage_credits") or {}
    dist = row.get("stage_distance") or {}
    dlabel = row.get("stage_distance_label") or {}
    cont = row.get("stage_continuous") or {}
    if not stages:
        return []
    out = ["  GRADE", "  " + "-" * 5, ""]
    longest = max(len(s) for s in stages)
    for sid, passed in stages.items():
        mark = "PASS" if passed else "FAIL"
        extra = ""
        if cont.get(sid):
            c = credits.get(sid)
            d = dist.get(sid)
            lbl = dlabel.get(sid) or ""
            if isinstance(c, (int, float)):
                extra += f"  credit {c:.2f}"
            if isinstance(d, (int, float)):
                extra += f"  dist {d:.3f} {lbl}".rstrip()
        out.append(f"      {sid:<{longest}}  {mark}{extra}")
    return out


def _iter_calls(records: Sequence[dict]):
    for r in records:
        if r.get("type") == "tool_call":
            yield r


def render_trial_audit_text(row: dict, records: Sequence[dict]) -> str:
    """Full plain-text audit for one trial."""
    tid = row.get("trial_id", "?")
    calls = list(_iter_calls(records))
    out: list[str] = [_rule("="), _center(f"TRIAL AUDIT  {tid}"), _rule("=")]

    score = row.get("score")
    score_s = f"{score:.3f}" if isinstance(score, (int, float)) else str(score)
    out += [
        f"  model      {row.get('model', '?'):<28}seed       {row.get('seed', '?')}",
        f"  condition  {row.get('condition', '?'):<28}score      {score_s}  (reach)",
        f"  failure    {row.get('failure_mode') or '-':<28}"
        f"wall       {row.get('wall_clock_s', 0)}s",
        f"  tool calls {len(calls):<28}"
        f"cost       ${row.get('cost_usd') or 0:.2f}",
        _rule("-"),
    ]

    grade = _grade_block(row)
    if grade:
        out += grade + [_rule("-")]

    out += [f"  TRAJECTORY   ({len(calls)} tool calls)", "  " + "-" * 10, ""]
    for i, c in enumerate(calls, 1):
        name = str(c.get("name") or "?")
        ok = c.get("ok", True)
        badge = "OK  " if ok else "FAIL"
        dur = f"({float(c.get('duration_s') or 0.0):.1f}s)"
        out.append(f"  [{i:02d}]  {_fmt_ts(c.get('t'))}  {name:<26} {badge} {dur}")
        args = c.get("args")
        if isinstance(args, dict) and args:
            out.append("        input:")
            for k, v in args.items():
                out += _fmt_field(k, v, indent="          ")
        else:
            out.append("        input: (none)")
        res = str(c.get("result_summary") or "").strip()
        if res:
            if len(res) > 600:
                res = res[:600] + " …"
            out.append(f"        result: {res}")
        out.append("")

    # Final response + UX feedback tail.
    final = next((r.get("content") for r in records
                  if r.get("type") == "assistant" and r.get("content")), None)
    if final:
        out += [_rule("-"), "  FINAL RESPONSE", "  " + "-" * 14, ""]
        out += ["      " + ln for ln in str(final).splitlines()[:40]]
        out.append("")
    ux = next((r for r in records if r.get("type") == "ux_feedback"), None)
    if ux:
        out += [_rule("-"), "  UX FEEDBACK", "  " + "-" * 11, ""]
        for fld in ("blind_rating", "response"):
            val = str(ux.get(fld) or "").strip()
            if val:
                out.append(f"      [{fld}]")
                out += ["      " + ln for ln in val.splitlines()[:60]]
                out.append("")
    out.append(_rule("="))
    return "\n".join(out) + "\n"


def _html_escape(s) -> str:
    return html.escape(str(s), quote=True)


def render_trial_audit_html(row: dict, records: Sequence[dict]) -> str:
    """Collapsible HTML twin of the text audit (self-contained, no external
    assets). Same content; tool-call inputs are <details> so a long run stays
    navigable."""
    tid = _html_escape(row.get("trial_id", "?"))
    calls = list(_iter_calls(records))
    score = row.get("score")
    score_s = f"{score:.3f}" if isinstance(score, (int, float)) else _html_escape(score)

    css = (
        "body{font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;"
        "margin:0;padding:24px;background:#0d1117;color:#c9d1d9}"
        "h1{font-size:16px;margin:0 0 4px}.meta{color:#8b949e;margin-bottom:16px}"
        ".stage{margin:2px 0}.PASS{color:#3fb950}.FAIL{color:#f85149}"
        ".call{border:1px solid #30363d;border-radius:6px;margin:8px 0;"
        "padding:0 12px;background:#161b22}"
        ".call>summary{cursor:pointer;padding:8px 0;list-style:none}"
        ".ok{color:#3fb950}.err{color:#f85149}"
        "pre{white-space:pre-wrap;word-break:break-word;margin:6px 0 10px;"
        "color:#c9d1d9}.k{color:#79c0ff}.sec{margin-top:24px;font-weight:700}"
    )
    parts = [f"<!doctype html><meta charset=utf-8><title>audit {tid}</title>",
             f"<style>{css}</style>",
             f"<h1>TRIAL AUDIT — {tid}</h1>",
             f"<div class=meta>model {_html_escape(row.get('model', '?'))} · "
             f"seed {_html_escape(row.get('seed', '?'))} · "
             f"score {score_s} · {len(calls)} tool calls · "
             f"{_html_escape(row.get('failure_mode') or '-')}</div>"]

    stages = row.get("stages") or {}
    if stages:
        credits = row.get("stage_credits") or {}
        dist = row.get("stage_distance") or {}
        cont = row.get("stage_continuous") or {}
        parts.append("<div class=sec>GRADE</div>")
        for sid, passed in stages.items():
            cls = "PASS" if passed else "FAIL"
            extra = ""
            if cont.get(sid):
                c, d = credits.get(sid), dist.get(sid)
                if isinstance(c, (int, float)):
                    extra += f" credit {c:.2f}"
                if isinstance(d, (int, float)):
                    extra += f" dist {d:.3f}"
            parts.append(f"<div class=stage><span class={cls}>{cls}</span> "
                         f"{_html_escape(sid)}{_html_escape(extra)}</div>")

    parts.append("<div class=sec>TRAJECTORY</div>")
    for i, c in enumerate(calls, 1):
        name = _html_escape(str(c.get("name") or "?"))
        ok = c.get("ok", True)
        badge = ("<span class=ok>OK</span>" if ok
                 else "<span class=err>FAIL</span>")
        dur = f"{float(c.get('duration_s') or 0.0):.1f}s"
        args = c.get("args") if isinstance(c.get("args"), dict) else {}
        body = "\n".join(f'<span class=k>{_html_escape(k)}</span>: '
                         f'{_html_escape(json.dumps(v, ensure_ascii=False))}'
                         for k, v in args.items()) or "(no input fields)"
        res = _html_escape(str(c.get("result_summary") or "").strip()[:800])
        parts.append(
            f"<details class=call><summary>[{i:02d}] {_fmt_ts(c.get('t'))} "
            f"<b>{name}</b> {badge} ({dur})</summary>"
            f"<pre>{body}</pre>"
            + (f"<pre>result: {res}</pre>" if res else "")
            + "</details>")

    ux = next((r for r in records if r.get("type") == "ux_feedback"), None)
    if ux:
        parts.append("<div class=sec>UX FEEDBACK</div>")
        for fld in ("blind_rating", "response"):
            val = _html_escape(str(ux.get(fld) or "").strip())
            if val:
                parts.append(f"<pre><b>{fld}</b>\n{val}</pre>")
    return "".join(parts) + "\n"


def write_trial_audits(summary: dict, trials: Sequence[dict], run_dir,
                       *, html_too: bool = False) -> int:
    """Write `trials/<id>/audit.txt` for every trial with a readable transcript,
    plus `audit.html` when `html_too`. Text is always written (headless-safe);
    the HTML twin is opt-in. Best-effort; returns the count written."""
    from toolbench.core.store import read_jsonl_gz
    trials_dir = Path(run_dir) / "trials"
    written = 0
    for row in trials:
        tid = row.get("trial_id")
        if not tid:
            continue
        tp = trials_dir / tid / "transcript.jsonl.gz"
        if not tp.exists():
            continue
        try:
            records = list(read_jsonl_gz(tp))
        except Exception:
            continue
        try:
            (trials_dir / tid / "audit.txt").write_text(
                render_trial_audit_text(row, records))
            if html_too:
                (trials_dir / tid / "audit.html").write_text(
                    render_trial_audit_html(row, records))
            written += 1
        except Exception:
            continue
    return written
