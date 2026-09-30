"""
Read-only snapshot of a campaign directory for the dashboard.

A *campaign* is any directory; every `manifest.json` beneath it marks a
toolbench run. For each run the snapshot joins three sources:

  plan.json      every trial the run will attempt (queued work). Runs that
                 predate it are re-enumerated from the manifest.
  trials.jsonl   finished trials, one row each.
  trials/<id>/   a trial directory with no row yet is in flight — or, if the
                 run's process is gone, interrupted.

and classifies the run itself from `status.json` (see
`toolbench.core.run_status`). Nothing here writes to the campaign.

Parsed files are cached by (mtime, size), so polling a large campaign costs
a handful of `stat` calls per run once nothing is changing.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import statistics
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from toolbench.core.failure_modes import EXCLUDED_FROM_METRICS, HARD_PROCESS_FAILURES
from toolbench.core.run_status import STALE_AFTER_S, STATUS_FILE

# Row failure modes that mean the trial never produced a measurement.
_ERROR_MODES = HARD_PROCESS_FAILURES | EXCLUDED_FROM_METRICS | {"resolution_error"}
_LEAK_MODE = "INTEGRITY_LEAK"

# Bytes of a trial's console.log returned to the detail view.
LOG_TAIL_BYTES = 64 * 1024
# Finished trials listed in a run's "recent" feed.
RECENT_TRIALS = 8

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# Per-run tallies. A trial is in exactly one of the states after "planned".
_COUNT_KEYS = ("planned", "queued", "running", "interrupted", "passed", "failed",
               "error", "leak")


class _FileCache:
    """Memoize parsed files keyed on (mtime_ns, size). Thread-safe."""

    def __init__(self) -> None:
        self._entries: dict[Path, tuple[tuple[int, int], object]] = {}
        self._lock = threading.Lock()

    def load(self, path: Path, parse):
        try:
            st = path.stat()
        except OSError:
            return None
        key = (st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._entries.get(path)
        if hit and hit[0] == key:
            return hit[1]
        try:
            value = parse(path)
        except (OSError, ValueError):
            return None
        with self._lock:
            self._entries[path] = (key, value)
        return value


def _parse_json(path: Path):
    return json.loads(path.read_text())


def _parse_rows(path: Path) -> list[dict]:
    """Parse trials.jsonl, skipping unparseable lines (a torn final line from
    a concurrent append is expected while a run is live)."""
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _iso_to_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # legacy local-time stamps
    return dt.timestamp()


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _ctime(path: Path) -> float | None:
    """Creation time where the platform records it, else inode change time."""
    try:
        st = path.stat()
    except OSError:
        return None
    return getattr(st, "st_birthtime", st.st_ctime)


def _safe_name(name: str) -> bool:
    """True for a single path component (no separators, no dot-dirs)."""
    return bool(name) and "/" not in name and "\\" not in name and name not in (".", "..")


def discover_runs(root: Path) -> list[Path]:
    """Every run directory under `root` (inclusive), sorted by path.

    Run directories are not descended into, and the `bundle/` of a
    `toolbench export` (which carries a copy of the manifest) is skipped.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        if "manifest.json" in filenames:
            found.append(Path(dirpath))
            dirnames[:] = []
            continue
        if "run.json" in filenames:
            dirnames[:] = [d for d in dirnames if d != "bundle"]
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
    return sorted(found)


def _plan_from_manifest(manifest: dict) -> list[dict]:
    """Rebuild the trial plan for a run that predates `plan.json`, using the
    same enumerator the runner uses so trial ids match."""
    from toolbench.cli import _build_work_items, _plan_entries

    def ids(key, field):
        return [x[field] if isinstance(x, dict) else x for x in manifest.get(key) or []]

    # The oldest manifests record neither axis. With a single harness or
    # variant the name never enters the trial id, so a placeholder suffices.
    harnesses = ids("harnesses", "id") or [""]
    variants = ids("variants", "name") or [
        (manifest.get("benchmark_config") or {}).get("default_variant") or ""]
    items = _build_work_items(
        harnesses=[SimpleNamespace(id=h) for h in harnesses],
        loadouts=[SimpleNamespace(name=n)
                  for n in manifest.get("loadouts") or manifest.get("conditions") or []],
        variants=[SimpleNamespace(name=n) for n in variants],
        models=ids("models", "model"),
        seeds=manifest.get("seeds") or [],
        completed=set(),
    )
    return _plan_entries(items)


def _trial_state(row: dict | None, has_dir: bool, run_live: bool) -> str:
    if row is None:
        if not has_dir:
            return "queued"
        return "running" if run_live else "interrupted"
    mode = row.get("failure_mode") or ""
    if mode == _LEAK_MODE:
        return "leak"
    if mode in _ERROR_MODES:
        return "error"
    return "passed" if row.get("ok") else "failed"


class CampaignReader:
    """Builds dashboard snapshots of one campaign directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self._cache = _FileCache()

    # ── public API ──────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """The full dashboard state: every run with its cells and trials.

        `mode` is "run" when the root is itself a run directory (the page
        shows that run alone) and "campaign" otherwise (the page offers a
        run picker).
        """
        now = time.time()
        run_dirs = discover_runs(self.root)
        runs = [self._run_state(d, now) for d in run_dirs]
        # Live runs first, then newest first.
        runs.sort(key=lambda r: r["created_at"] or "", reverse=True)
        runs.sort(key=lambda r: r["state"] != "running")
        return {
            "root": str(self.root),
            "name": self.root.name,
            "mode": "run" if run_dirs == [self.root] else "campaign",
            "generated_at": now,
            "live_runs": sum(r["state"] == "running" for r in runs),
            "runs": runs,
        }

    def run_summary(self, run_id: str) -> str | None:
        """The run's rendered `summary.txt`, or None if not yet written."""
        run_dir = self._resolve(run_id)
        path = run_dir / "summary.txt" if run_dir else None
        return path.read_text(errors="replace") if path and path.is_file() else None

    def trial_detail(self, run_id: str, trial_id: str) -> dict | None:
        """A trial's `trials.jsonl` row (if finished) and its console.log tail.

        The log ends with the trial's RESULT block once it finishes; while it
        runs, the tail is the live output.
        """
        run_dir = self._resolve(run_id)
        if run_dir is None or not _safe_name(trial_id):
            return None
        rows = self._cache.load(run_dir / "trials.jsonl", _parse_rows) or []
        row = next((r for r in rows if r.get("trial_id") == trial_id), None)
        log_path = run_dir / "trials" / trial_id / "console.log"
        log, truncated = None, False
        if log_path.is_file():
            with open(log_path, "rb") as fh:
                size = fh.seek(0, os.SEEK_END)
                truncated = size > LOG_TAIL_BYTES
                fh.seek(max(0, size - LOG_TAIL_BYTES))
                log = _ANSI.sub("", fh.read().decode("utf-8", errors="replace"))
        if row is None and log is None:
            return None
        return {"run_id": run_id, "trial_id": trial_id, "row": row,
                "log": log, "log_truncated": truncated}

    # ── internals ───────────────────────────────────────────────────

    def _resolve(self, run_id: str) -> Path | None:
        """Map a run id (path relative to the root) to its directory,
        refusing anything outside the root or not a run."""
        candidate = (self.root / run_id).resolve()
        if not candidate.is_relative_to(self.root):
            return None
        return candidate if (candidate / "manifest.json").is_file() else None

    def _liveness(self, run_dir: Path, now: float) -> dict:
        status = self._cache.load(run_dir / STATUS_FILE, _parse_json)
        if isinstance(status, dict):
            state = status.get("state") or "unknown"
            heartbeat = _iso_to_epoch(status.get("updated_at"))
            if state == "running" and (heartbeat is None or now - heartbeat > STALE_AFTER_S):
                state = "stale"
            return {"state": state, "heartbeat_at": heartbeat,
                    "started_at": _iso_to_epoch(status.get("started_at")),
                    "finished_at": _iso_to_epoch(status.get("finished_at")),
                    "host": status.get("host"), "pid": status.get("pid"),
                    "abort_reason": status.get("abort_reason"),
                    "error": status.get("error")}
        # Runs from before status.json: a written summary means it finalized;
        # otherwise liveness cannot be known.
        finished = (run_dir / "summary.json").is_file()
        return {"state": "finished" if finished else "unknown", "heartbeat_at": None,
                "started_at": None, "finished_at": None, "host": None, "pid": None,
                "abort_reason": None, "error": None}

    def _run_state(self, run_dir: Path, now: float) -> dict:
        manifest = self._cache.load(run_dir / "manifest.json", _parse_json) or {}
        live = self._liveness(run_dir, now)
        run_live = live["state"] == "running"

        plan_doc = self._cache.load(run_dir / "plan.json", _parse_json)
        plan = (plan_doc or {}).get("trials")
        if plan is None:
            try:
                plan = _plan_from_manifest(manifest)
            except Exception:
                plan = []
        rows = {r.get("trial_id"): r
                for r in self._cache.load(run_dir / "trials.jsonl", _parse_rows) or []}
        trials_dir = run_dir / "trials"
        started = ({p.name for p in trials_dir.iterdir() if p.is_dir()}
                   if trials_dir.is_dir() else set())

        # Rows the plan does not know (e.g. a legacy run whose manifest
        # re-enumerates differently) are still shown, appended to their cell.
        planned_ids = {p["trial_id"] for p in plan}
        entries = list(plan) + [
            {"trial_id": tid, "condition": r.get("condition"), "model": r.get("model"),
             "seed": r.get("seed"), "index": None}
            for tid, r in rows.items() if tid and tid not in planned_ids]

        cells: dict[tuple, dict] = {}
        counts = dict.fromkeys(_COUNT_KEYS, 0)
        last_activity = _mtime(run_dir / "trials.jsonl")
        for e in entries:
            tid = e["trial_id"]
            row = rows.get(tid)
            state = _trial_state(row, tid in started, run_live)
            trial = {"trial_id": tid, "seed": e.get("seed"), "index": e.get("index"),
                     "state": state}
            if row is not None:
                trial.update(score=row.get("score"), ok=row.get("ok"),
                             failure_mode=row.get("failure_mode"),
                             cost_usd=row.get("cost_usd"),
                             wall_clock_s=row.get("wall_clock_s"))
            elif state == "running":
                trial["started_at"] = _ctime(trials_dir / tid)
                trial["last_activity"] = _mtime(trials_dir / tid / "console.log")
                if trial["last_activity"]:
                    last_activity = max(last_activity or 0, trial["last_activity"])
            key = (e.get("model"), e.get("condition"))
            cell = cells.setdefault(key, {"model": key[0], "condition": key[1],
                                          "trials": []})
            cell["trials"].append(trial)
            counts[state] += 1
        counts["planned"] = len(entries)

        cell_list = [self._cell_stats(c) for c in cells.values()]
        for c in cell_list:
            c["trials"].sort(key=lambda t: (t["index"] is None, t["index"] or 0,
                                            t["seed"] or 0))

        finished_rows = [r for r in rows.values() if r.get("wall_clock_s")]
        spent = sum(float(r.get("cost_usd") or 0.0) for r in rows.values())
        parallel = int(manifest.get("parallel") or 1)
        remaining = counts["queued"] + counts["running"]
        eta_s = None
        if run_live and remaining and finished_rows:
            median = statistics.median(float(r["wall_clock_s"]) for r in finished_rows)
            eta_s = round(median * remaining / max(1, parallel))

        run_id = run_dir.relative_to(self.root).as_posix() if run_dir != self.root else "."
        return {
            "id": run_id,
            "name": run_dir.name,
            "benchmark": manifest.get("benchmark"),
            "models": [m["model"] if isinstance(m, dict) else m
                       for m in manifest.get("models") or []],
            "created_at": manifest.get("created_at"),
            "n_per_cell": manifest.get("n_per_cell"),
            "parallel": parallel,
            "dry_run": bool(manifest.get("dry_run")),
            "budget_usd": manifest.get("max_cost_usd"),
            "spent_usd": round(spent, 4),
            "eta_s": eta_s,
            "last_activity": last_activity,
            "has_summary": (run_dir / "summary.txt").is_file(),
            "counts": counts,
            "cells": sorted(cell_list, key=lambda c: (str(c["model"]), str(c["condition"]))),
            # Most recent completions first (trials.jsonl is append-ordered).
            "recent": [
                {"trial_id": r.get("trial_id"), "condition": r.get("condition"),
                 "model": r.get("model"), "score": r.get("score"),
                 "failure_mode": r.get("failure_mode"),
                 "wall_clock_s": r.get("wall_clock_s"),
                 "state": _trial_state(r, True, run_live)}
                for r in list(rows.values())[-RECENT_TRIALS:][::-1]],
            **live,
        }

    @staticmethod
    def _cell_stats(cell: dict) -> dict:
        """Running mean reach and pass count over the cell's scored trials
        (errors and quarantined trials excluded, as in the summary)."""
        scored = [t for t in cell["trials"] if t["state"] in ("passed", "failed")]
        cell["n_scored"] = len(scored)
        cell["n_passed"] = sum(t["state"] == "passed" for t in scored)
        cell["mean_reach"] = (round(statistics.fmean(float(t.get("score") or 0.0)
                                                     for t in scored), 4)
                              if scored else None)
        return cell

