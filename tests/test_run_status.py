"""`status.json` lifecycle: a run records running -> finished / aborted /
failed, and the heartbeat keeps `updated_at` fresh while it runs."""

import json
import time

import pytest

from toolbench.core.run_status import STATUS_FILE, RunStatus


def _status(run_dir):
    return json.loads((run_dir / STATUS_FILE).read_text())


def test_running_while_inside_the_block(tmp_path):
    with RunStatus(tmp_path) as status:
        rec = _status(tmp_path)
        assert rec["state"] == "running"
        assert rec["pid"] and rec["host"] and rec["started_at"]
        status.finish(aborted=False)


def test_finish_records_finished(tmp_path):
    with RunStatus(tmp_path) as status:
        status.finish(aborted=False)
    rec = _status(tmp_path)
    assert rec["state"] == "finished"
    assert rec["finished_at"] and rec["abort_reason"] is None


def test_finish_records_abort_reason(tmp_path):
    with RunStatus(tmp_path) as status:
        status.finish(aborted=True, abort_reason="session_limit")
    rec = _status(tmp_path)
    assert rec["state"] == "aborted"
    assert rec["abort_reason"] == "session_limit"


def test_exception_records_failed_with_error(tmp_path):
    with pytest.raises(RuntimeError):
        with RunStatus(tmp_path):
            raise RuntimeError("boom")
    rec = _status(tmp_path)
    assert rec["state"] == "failed"
    assert rec["error"] == "RuntimeError: boom"


def test_leaving_without_finish_records_failed(tmp_path):
    # e.g. cmd_run's early `return 2` on an MCP preflight failure
    with RunStatus(tmp_path):
        pass
    assert _status(tmp_path)["state"] == "failed"


def test_heartbeat_refreshes_updated_at(tmp_path):
    with RunStatus(tmp_path, interval_s=0.05) as status:
        first = (tmp_path / STATUS_FILE).stat().st_mtime_ns
        deadline = time.time() + 2
        while (tmp_path / STATUS_FILE).stat().st_mtime_ns == first:
            assert time.time() < deadline, "heartbeat never rewrote status.json"
            time.sleep(0.02)
        status.finish(aborted=False)


def test_unwritable_run_dir_does_not_fail_the_run(tmp_path):
    missing = tmp_path / "gone"   # never created: every write raises OSError
    with RunStatus(missing) as status:
        status.finish(aborted=False)
