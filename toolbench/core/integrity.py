"""
Benchmark-integrity scan — did a trial reach the graded ground truth?

The claude_code harness does not confine the agent's Bash to its sandbox, so a
trial CAN read the ground-truth answer key (e.g. `soln/truth.json`) that lives
outside the sandbox. This module scans each trial's transcript (the tool-call
INPUTS the agent issued — Bash commands, file reads, greps) for references to
the answer key and flags any trial that touched it, so the result can be
quarantined rather than silently trusted.

Precise by construction: it matches the benchmark's actual ground-truth
directory (from the manifest) plus a few answer-key filename/dir conventions
that never appear among the agent's provided sandbox inputs. Agent-produced
files (results/answer.json, scan.csv) and provided inputs (kappa.csv, spectra)
are NOT matched.
"""

import json
from pathlib import Path

from toolbench.core.store import read_jsonl_gz

# Answer-key markers that only exist in the graded ground truth, never in the
# sandbox the agent is handed. Kept conservative to avoid false positives.
_GENERIC_MARKERS = ("truth.json", "ground_truth", "_ground_truth", "answer_key")


def sensitive_markers(manifest: dict) -> list[str]:
    """The strings whose appearance in a tool-call INPUT means the trial reached
    the answer key: the benchmark's ground-truth dir (absolute) + its basename
    as a path segment, plus the generic answer-key conventions."""
    marks: list[str] = list(_GENERIC_MARKERS)
    gt = ((manifest.get("benchmark_config") or {}).get("ground_truth") or {})
    d = gt.get("dir")
    if d:
        d = str(d).rstrip("/")
        marks.append(d)                      # absolute path
        base = Path(d).name
        if base:
            marks.append(f"{base}/")         # e.g. "soln/" as a path segment
    # De-dup while preserving order.
    seen, out = set(), []
    for m in marks:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


# Tool-call argument keys that carry the body of a file being written or
# edited (Write.content, Edit/MultiEdit old_string/new_string), as opposed to
# a path, a command or code that is executed.
_FILE_BODY_KEYS = frozenset({"content", "old_string", "new_string"})


def _split_args(value, bodies: list[str]):
    """Return `value` with file-body strings removed, appending each removed
    string to `bodies`. Recurses through nested dicts and lists."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in _FILE_BODY_KEYS and isinstance(v, str):
                bodies.append(v)
            else:
                out[k] = _split_args(v, bodies)
        return out
    if isinstance(value, list):
        return [_split_args(v, bodies) for v in value]
    return value


def _is_path_marker(marker: str) -> bool:
    """A marker shaped like a path or file name, as opposed to a bare word."""
    return "/" in marker or marker.endswith(".json")


def scan_transcript(path, markers: list[str], max_hits: int = 8) -> list[dict]:
    """Return the answer-key hits in one trial transcript: for each offending
    tool call, the tool name, the marker matched, where it matched, and a short
    snippet for a human to verify. Scans tool-call INPUTS only (what the agent
    did), never tool RESULTS.

    Commands, paths and executed code are matched against every marker. The
    body of a file the agent writes is matched only against path-shaped
    markers: a script that opens `soln/truth.json` is still caught, but a
    variable or a note that merely says `ground_truth` is not.
    """
    path_markers = [m for m in markers if _is_path_marker(m)]
    hits: list[dict] = []
    try:
        for r in read_jsonl_gz(path):
            if r.get("type") != "tool_call":
                continue
            bodies: list[str] = []
            access = json.dumps(_split_args(r.get("args") or {}, bodies), ensure_ascii=False)
            hit = _first_match(access, markers, "input")
            if hit is None and bodies:
                hit = _first_match("\n".join(bodies), path_markers, "file body")
            if hit is not None:
                hits.append({"tool": r.get("name"), **hit})
            if len(hits) >= max_hits:
                break
    except Exception:
        pass
    return hits


def _first_match(blob: str, markers: list[str], where: str) -> dict | None:
    for m in markers:
        idx = blob.find(m)
        if idx >= 0:
            lo = max(0, idx - 40)
            return {"marker": m, "where": where,
                    "snippet": blob[lo:idx + len(m) + 40]}
    return None


def scan_run(run_dir, trials, manifest: dict) -> dict:
    """Scan every trial in a run. Returns {trial_id: [hits]} for the trials that
    touched the answer key (empty dict when the run is clean)."""
    markers = sensitive_markers(manifest)
    trials_dir = Path(run_dir) / "trials"
    flagged: dict[str, list] = {}
    for t in trials:
        tid = t.get("trial_id")
        if not tid:
            continue
        tp = trials_dir / tid / "transcript.jsonl.gz"
        if not tp.exists():
            continue
        hits = scan_transcript(tp, markers)
        if hits:
            flagged[tid] = hits
    return flagged
