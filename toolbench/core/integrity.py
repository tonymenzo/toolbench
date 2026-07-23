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


def scan_transcript(path, markers: list[str], max_hits: int = 8) -> list[dict]:
    """Return the answer-key hits in one trial transcript: for each offending
    tool call, the tool name, the marker matched, and a short snippet of the
    input for a human to verify. Scans tool-call INPUTS only (what the agent
    did), never tool RESULTS."""
    hits: list[dict] = []
    try:
        for r in read_jsonl_gz(path):
            if r.get("type") != "tool_call":
                continue
            blob = json.dumps(r.get("args") or {}, ensure_ascii=False)
            for m in markers:
                idx = blob.find(m)
                if idx >= 0:
                    lo = max(0, idx - 40)
                    hits.append({
                        "tool": r.get("name"),
                        "marker": m,
                        "snippet": blob[lo:idx + len(m) + 40],
                    })
                    break
            if len(hits) >= max_hits:
                break
    except Exception:
        pass
    return hits


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
