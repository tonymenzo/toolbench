"""Dashboard snapshot: trial and run classification from run-directory files,
legacy-run fallbacks, and the read-only endpoints' path safety."""

import datetime
import json
from pathlib import Path

import pytest

from toolbench.core.run_status import write_json_atomic
from toolbench.dashboard.state import CampaignReader, discover_runs
from tests.helpers import GEOMETRY_DIR


def _now_iso(minutes_ago=0.0):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    return t.isoformat(timespec="seconds")


def _make_run(root: Path, name="run_a", *, n=3, loadouts=("tools", "core"),
              status_state="running", heartbeat_minutes_ago=0.0):
    run = root / name
    (run / "trials").mkdir(parents=True)
    plan = [{"trial_id": f"{lo}__n{i:03d}__seed{1001 + i}", "condition": lo,
             "harness": "h", "loadout": lo, "variant": "v", "model": "m",
             "seed": 1001 + i, "index": i}
            for i in range(n) for lo in loadouts]
    (run / "manifest.json").write_text(json.dumps({
        "run_id": name, "created_at": "2026-09-30T12:00:00", "benchmark": "b",
        "models": [{"model": "m"}], "loadouts": list(loadouts), "n_per_cell": n,
        "parallel": 2, "max_cost_usd": 5.0, "seeds": [1001 + i for i in range(n)]}))
    (run / "plan.json").write_text(json.dumps({"trials": plan}))
    if status_state:
        write_json_atomic(run / "status.json", {
            "state": status_state, "pid": 1, "host": "x",
            "started_at": _now_iso(30), "updated_at": _now_iso(heartbeat_minutes_ago)})
    return run, plan


def _row(t, *, ok, score, mode="NONE", cost=0.5):
    return {**t, "ok": ok, "score": score, "failure_mode": mode,
            "wall_clock_s": 60, "cost_usd": cost}


def _states(run_state):
    return {t["trial_id"]: t["state"] for c in run_state["cells"] for t in c["trials"]}


def test_trial_states_from_rows_dirs_and_plan(tmp_path):
    run, plan = _make_run(tmp_path)
    rows = [_row(plan[0], ok=True, score=1.0),
            _row(plan[1], ok=False, score=0.4, mode="INCOMPLETE_AT_X"),
            _row(plan[2], ok=False, score=0.0, mode="AGENT_CRASH"),
            _row(plan[3], ok=False, score=0.0, mode="INTEGRITY_LEAK")]
    # A torn final line (a concurrent append) must be skipped, not fatal.
    (run / "trials.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows) + '{"trial_id": "tor')
    (run / "trials" / plan[4]["trial_id"]).mkdir()

    snap = CampaignReader(tmp_path).snapshot()
    (r,) = snap["runs"]
    states = _states(r)
    assert [states[t["trial_id"]] for t in plan] == [
        "passed", "failed", "error", "leak", "running", "queued"]
    assert r["state"] == "running"
    assert r["counts"]["planned"] == 6
    assert r["spent_usd"] == pytest.approx(2.0)
    assert r["eta_s"] is not None


def test_cell_stats_exclude_errors(tmp_path):
    run, plan = _make_run(tmp_path, loadouts=("tools",))
    (run / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        _row(plan[0], ok=True, score=1.0),
        _row(plan[1], ok=False, score=0.5),
        _row(plan[2], ok=False, score=0.0, mode="SESSION_LIMIT")]))
    (cell,) = CampaignReader(tmp_path).snapshot()["runs"][0]["cells"]
    assert cell["n_scored"] == 2 and cell["n_passed"] == 1
    assert cell["mean_reach"] == pytest.approx(0.75)


def test_missed_heartbeat_is_stale_and_in_flight_trials_interrupted(tmp_path):
    run, plan = _make_run(tmp_path, heartbeat_minutes_ago=10)
    (run / "trials" / plan[0]["trial_id"]).mkdir()
    (r,) = CampaignReader(tmp_path).snapshot()["runs"]
    assert r["state"] == "stale"
    assert _states(r)[plan[0]["trial_id"]] == "interrupted"
    assert r["eta_s"] is None


def test_legacy_run_without_status_or_plan(tmp_path):
    run, _ = _make_run(tmp_path, status_state=None)
    (run / "plan.json").unlink()
    (r,) = CampaignReader(tmp_path).snapshot()["runs"]
    assert r["state"] == "unknown"
    # Plan re-enumerated from the manifest with the runner's own trial ids.
    assert r["counts"]["queued"] == 6
    assert "tools__n000__seed1001" in _states(r)
    (run / "summary.json").write_text("{}")
    assert CampaignReader(tmp_path).snapshot()["runs"][0]["state"] == "finished"


def test_discovery_nests_and_skips_export_bundles(tmp_path):
    _make_run(tmp_path / "campaign" / "trials" / "claude-code", "run_a")
    _make_run(tmp_path / "campaign", "run_b")
    export = tmp_path / "exports" / "run_a"
    (export / "bundle").mkdir(parents=True)
    (export / "run.json").write_text("{}")
    (export / "bundle" / "manifest.json").write_text("{}")
    found = [p.relative_to(tmp_path).as_posix() for p in discover_runs(tmp_path)]
    assert found == ["campaign/run_b", "campaign/trials/claude-code/run_a"]


def test_summary_and_trial_detail(tmp_path):
    run, plan = _make_run(tmp_path)
    reader = CampaignReader(tmp_path)
    assert reader.run_summary("run_a") is None
    (run / "summary.txt").write_text("RUN summary\n")
    assert reader.run_summary("run_a") == "RUN summary\n"

    tdir = run / "trials" / plan[0]["trial_id"]
    tdir.mkdir()
    (tdir / "console.log").write_text("\x1b[1mstep\x1b[0m one\n")
    d = reader.trial_detail("run_a", plan[0]["trial_id"])
    assert d["row"] is None and d["log"] == "step one\n"


@pytest.mark.parametrize("run_id,trial_id", [
    ("../outside", "t"), ("run_a", "../../manifest.json"), ("run_a", ".."),
])
def test_endpoints_refuse_paths_outside_the_campaign(tmp_path, run_id, trial_id):
    root = tmp_path / "campaign"
    _make_run(root)
    _make_run(tmp_path, "outside")
    (tmp_path / "outside" / "summary.txt").write_text("secret")
    reader = CampaignReader(root)
    assert reader.run_summary(run_id) is None
    assert reader.trial_detail(run_id, trial_id) is None


def test_dry_run_writes_plan_and_status_the_dashboard_reads(tmp_path):
    pytest.importorskip("orchestral")
    import toolbench.cli as cli
    orig = cli._OUTPUT_BASE
    try:
        cli._OUTPUT_BASE = tmp_path
        rc = cli.main(["run", "--benchmark", str(GEOMETRY_DIR),
                       "--loadouts", "core_only,full_local",
                       "--harness", "orchestral/anthropic", "--model", "stub",
                       "--n", "2", "--max-cost-usd", "0", "--dry-run"])
    finally:
        cli._OUTPUT_BASE = orig
    assert rc == 0
    (r,) = CampaignReader(tmp_path / "runs").snapshot()["runs"]
    assert r["state"] == "finished"
    assert r["counts"]["planned"] == 4
    assert r["counts"]["queued"] == r["counts"]["running"] == 0
    assert r["has_summary"]
