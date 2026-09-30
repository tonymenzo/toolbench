"""
Run liveness record (`status.json`) for live monitoring.

A run directory alone cannot distinguish a run that is still working from one
whose process was killed: both leave trial directories without a
`trials.jsonl` row. `RunStatus` closes that gap by maintaining a small
`status.json` beside the manifest while `run` / `resume` executes:

    {"state": "running", "pid": 4242, "host": "node01",
     "started_at": "...", "updated_at": "...", "finished_at": null,
     "abort_reason": null, "error": null}

`updated_at` is refreshed by a daemon heartbeat thread every
`HEARTBEAT_INTERVAL_S` seconds. A reader treats a `running` status whose
heartbeat is older than `STALE_AFTER_S` as a dead process. Terminal states
are `finished`, `aborted` (with `abort_reason`) and `failed` (with `error`).

The file is observational only: nothing that runs, grades or aggregates a
trial reads it, it is not part of `toolbench export`, and a failure to write
it never fails the run.
"""

from __future__ import annotations

import datetime
import json
import os
import socket
import threading
from pathlib import Path

STATUS_FILE = "status.json"
HEARTBEAT_INTERVAL_S = 10.0
# Several missed heartbeats, so a briefly stalled filesystem is not misread
# as a dead process.
STALE_AFTER_S = 60.0


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def write_json_atomic(path: Path, obj: dict) -> None:
    """Write JSON via a temp file + rename, so a concurrent reader never
    observes a partially written file."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    os.replace(tmp, path)


class RunStatus:
    """Context manager that maintains `<run_dir>/status.json`.

    Usage::

        with RunStatus(run_dir) as status:
            ...  # execute trials, finalize
            status.finish(aborted=aborted, abort_reason=abort_reason)

    Leaving the block without calling `finish` (an early return or an
    exception) records `failed`, with the exception type and message when
    there is one.
    """

    def __init__(self, run_dir: str | Path,
                 interval_s: float = HEARTBEAT_INTERVAL_S) -> None:
        self._path = Path(run_dir) / STATUS_FILE
        self._interval_s = interval_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._record: dict = {}

    def __enter__(self) -> RunStatus:
        now = _now()
        self._record = {
            "state": "running",
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": now,
            "updated_at": now,
            "finished_at": None,
            "abort_reason": None,
            "error": None,
        }
        self._write()
        self._thread = threading.Thread(target=self._beat, name="toolbench-heartbeat",
                                        daemon=True)
        self._thread.start()
        return self

    def finish(self, *, aborted: bool, abort_reason: str | None = None) -> None:
        """Record the terminal outcome of a run that reached finalization."""
        with self._lock:
            self._record["state"] = "aborted" if aborted else "finished"
            self._record["abort_reason"] = abort_reason if aborted else None

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_s)
        with self._lock:
            if self._record.get("state") == "running":
                self._record["state"] = "failed"
                if exc_type is not None:
                    self._record["error"] = f"{exc_type.__name__}: {exc}"
            self._record["finished_at"] = _now()
        self._write()

    def _beat(self) -> None:
        while not self._stop.wait(self._interval_s):
            self._write()

    def _write(self) -> None:
        with self._lock:
            self._record["updated_at"] = _now()
            record = dict(self._record)
        try:
            write_json_atomic(self._path, record)
        except OSError:
            pass  # observational only: never fail the run over a status write
