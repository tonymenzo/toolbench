"""
Bounded, conservative repair for malformed tool-call argument JSON.

Function-calling models (gpt-oss-120b in particular) periodically emit a
tool call whose `arguments` field is *almost* JSON but not quite: a code
or LaTeX blob written into a `writefile`-style `data` argument with raw,
unescaped newlines, control characters, or backslashes (`$H_T^\\gamma$`,
a literal U+202F narrow-no-break-space, an unescaped `\\` before a non-
escape character). Orchestral's OpenAI parser does a bare
`json.loads(arguments)` with no recovery, so one such call raises a
`json.JSONDecodeError` that aborts the whole `agent.run()` and zeroes an
otherwise-good trial.

This module is the recovery path. `repair_tool_call_json` is a PURE
function with NO behaviour change when the input is already valid JSON:
it tries `json.loads` first and returns the input untouched if it
parses. Only on a decode failure does it attempt a small, bounded set of
conservative string-level repairs. The repairs are deliberately limited
to fixing *escaping* inside JSON string values — they never invent
structure, never close unbalanced braces, and never evaluate anything.
In particular a tool argument that is a Python *expression* (e.g.
`"0"*3000`, string concatenation) is NOT valid JSON and is NOT made
valid here: we will not execute model-authored Python, so such a call
falls through to the runner's existing retry/crash path unchanged.

Design constraints:
- Pure: same input -> same output, no I/O, no globals.
- Identity on valid JSON: `repair_tool_call_json(s)` returns `s`
  whenever `json.loads(s)` already succeeds.
- Conservative on failure: returns a repaired string only if that
  string `json.loads`-parses; otherwise returns `None` ("could not
  repair, fall through").
- Model-agnostic: keys only on the JSON text, not on any model name.
"""

import json


def repair_tool_call_json(raw: str) -> str | None:
    """Return a JSON-parseable version of ``raw``, or ``None``.

    Contract:
    - If ``raw`` already parses, it is returned **unchanged** (identity;
      the happy path is never perturbed).
    - Otherwise a bounded sequence of conservative repairs is tried; the
      first repaired candidate that ``json.loads``-parses is returned.
    - If nothing parses, returns ``None`` so the caller can fall through
      to its existing error handling. We never return a string that does
      not parse, and we never fabricate or drop structure.

    The repairs only touch *escaping inside string values*:
      1. escape raw control characters (newline, tab, CR, U+202F and the
         rest of C0/C1 plus the JSON-significant unicode spaces) that
         appear inside a string;
      2. escape lone/invalid backslashes (a ``\\`` not introducing a
         valid JSON escape) inside a string.
    Anything structural (missing braces, trailing commas, Python
    expressions) is left for the caller to reject.
    """
    if not isinstance(raw, str):
        return None
    # Happy path: already valid -> identity, zero behaviour change.
    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError:
        pass

    for candidate in _repair_candidates(raw):
        if candidate == raw:
            continue
        try:
            json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return candidate
    return None


def parse_tool_call_arguments(raw: str):
    """Parse tool-call ``arguments`` JSON, repairing escaping if needed.

    Returns the decoded object on success. Raises the original
    ``json.JSONDecodeError`` (same exception the stock parser would
    raise, with ``.doc``/``.pos`` intact) when neither the raw text nor
    any conservative repair parses — so callers and the crash classifier
    see exactly the failure they expect.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError as first_err:
        repaired = repair_tool_call_json(raw)
        if repaired is not None and repaired != raw:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass
        raise first_err


# --- internals -------------------------------------------------------------

# Valid characters that may follow a backslash inside a JSON string.
_VALID_ESCAPE_NEXT = set('"\\/bfnrtu')


def _repair_candidates(raw: str):
    """Yield repaired candidates in increasing order of aggressiveness.

    Each candidate is produced by a single string-walk that re-escapes
    illegal characters found *inside* string literals while leaving
    everything outside strings (structure, numbers, whitespace) byte-for-
    byte alone. We yield (a) control-char escaping only, then (b) control
    chars + lone backslashes, so the most conservative fix that parses
    wins.
    """
    yield _reescape_strings(raw, fix_backslashes=False)
    yield _reescape_strings(raw, fix_backslashes=True)


def _reescape_strings(raw: str, *, fix_backslashes: bool) -> str:
    """Walk ``raw`` and JSON-escape illegal chars inside string literals.

    A minimal hand-rolled scanner tracks whether we are inside a JSON
    string. Outside strings nothing is altered. Inside a string:
      - raw control characters (U+0000..U+001F) and selected unicode
        spaces (e.g. U+202F narrow-no-break-space, the literal char the
        audit saw leak in from LaTeX) are replaced by their valid JSON
        escape (``\\n``/``\\t``/``\\r`` or ``\\uXXXX``);
      - a backslash that does not introduce a valid JSON escape is, when
        ``fix_backslashes`` is set, doubled to ``\\\\`` (the
        ``$H_T^\\gamma$`` / lone-backslash case); a valid escape pair is
        passed through untouched.
    The result is a pure function of the input.
    """
    out = []
    in_string = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue

        # Inside a string literal.
        if ch == '"':
            out.append(ch)
            in_string = False
            i += 1
            continue

        if ch == '\\':
            nxt = raw[i + 1] if i + 1 < n else ''
            if nxt in _VALID_ESCAPE_NEXT:
                # Valid escape (incl. \\uXXXX) — pass the pair through
                # verbatim so we never corrupt already-correct escaping.
                out.append(ch)
                out.append(nxt)
                i += 2
                continue
            # Lone/invalid backslash.
            if fix_backslashes:
                out.append('\\\\')
            else:
                out.append(ch)
            i += 1
            continue

        code = ord(ch)
        if code < 0x20:
            # Raw control char inside a string: illegal in JSON, escape it.
            out.append(_escape_control(ch))
        elif ch in _ILLEGAL_SPACES:
            # Unicode spaces that confuse some decoders / leaked from
            # LaTeX (narrow-no-break-space, etc.) — escape defensively.
            out.append(f'\\u{code:04x}')
        else:
            out.append(ch)
        i += 1

    return ''.join(out)


# Control chars with a short JSON escape; everything else in C0 gets \\uXXXX.
_SHORT_CONTROL_ESCAPES = {
    '\n': '\\n', '\t': '\\t', '\r': '\\r',
    '\b': '\\b', '\f': '\\f',
}

# Non-ASCII spaces that are not legal as raw JSON whitespace inside a
# string and tend to leak in from LaTeX / copy-paste. U+202F is the
# narrow-no-break-space the audit observed inside `$H_T^\gamma$`.
_ILLEGAL_SPACES = {' ', ' ', ' ', '⁠', '﻿'}


def _escape_control(ch: str) -> str:
    return _SHORT_CONTROL_ESCAPES.get(ch, f'\\u{ord(ch):04x}')
