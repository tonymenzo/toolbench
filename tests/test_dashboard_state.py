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
        "passed", "failed", "crashed", "leak", "running", "queued"]
    assert r["state"] == "running"
    assert r["counts"]["planned"] == 6
    assert r["spent_usd"] == pytest.approx(2.0)
    assert r["eta_s"] is not None


def test_cell_stats_exclude_only_session_limits(tmp_path):
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


def test_mode_is_run_for_a_run_dir_and_campaign_otherwise(tmp_path):
    run, _ = _make_run(tmp_path)
    assert CampaignReader(run).snapshot()["mode"] == "run"
    assert CampaignReader(run).snapshot()["runs"][0]["id"] == "."
    assert CampaignReader(tmp_path).snapshot()["mode"] == "campaign"


def test_recent_lists_latest_completions_first(tmp_path):
    run, plan = _make_run(tmp_path)
    (run / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        _row(plan[0], ok=True, score=1.0),
        _row(plan[1], ok=False, score=0.3)]))
    recent = CampaignReader(tmp_path).snapshot()["runs"][0]["recent"]
    assert [(t["trial_id"], t["state"]) for t in recent] == [
        (plan[1]["trial_id"], "failed"), (plan[0]["trial_id"], "passed")]


def test_cli_target_defaults_to_latest_run_and_accepts_run_ids(tmp_path, monkeypatch):
    import os
    import toolbench.cli as cli
    import toolbench.dashboard as dashboard

    older, _ = _make_run(tmp_path / "runs", "older")
    newer, _ = _make_run(tmp_path / "runs", "newer")
    os.utime(older / "manifest.json", (1, 1))
    served = []
    monkeypatch.setattr(dashboard, "serve", lambda root, **kw: served.append(Path(root)))
    monkeypatch.setattr(cli, "_OUTPUT_BASE", tmp_path)

    assert cli.main(["dashboard"]) == 0
    assert cli.main(["dashboard", "older"]) == 0
    assert cli.main(["dashboard", str(tmp_path / "runs")]) == 0
    assert served == [newer, (tmp_path / "runs" / "older").resolve(), tmp_path / "runs"]
    assert cli.main(["dashboard", "missing"]) == 1


def test_subscription_run_reports_zero_spend_and_an_api_equivalent_estimate(tmp_path):
    run, plan = _make_run(tmp_path)
    manifest = json.loads((run / "manifest.json").read_text())
    manifest["harnesses"] = [{"id": "claude-code/default", "provider": {"name": "subscription"}}]
    (run / "manifest.json").write_text(json.dumps(manifest))
    (run / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {**_row(plan[0], ok=True, score=1.0, cost=0.0), "estimated_api_equivalent_cost_usd": 0.25},
        {**_row(plan[1], ok=True, score=1.0, cost=0.0), "estimated_api_equivalent_cost_usd": 0.5}]))
    (r,) = CampaignReader(tmp_path).snapshot()["runs"]
    assert r["subscription"] is True
    assert r["spent_usd"] == 0.0
    assert r["api_equivalent_usd"] == pytest.approx(0.75)


def test_metered_run_is_not_subscription(tmp_path):
    _make_run(tmp_path)
    (r,) = CampaignReader(tmp_path).snapshot()["runs"]
    assert r["subscription"] is False and r["api_equivalent_usd"] is None


def _trial_with_sandbox(tmp_path):
    from toolbench.core.trial_start import record_trial_start
    run, plan = _make_run(tmp_path)
    tid = plan[0]["trial_id"]
    tdir = run / "trials" / tid
    sb = tdir / "sandbox"
    (sb / "cards").mkdir(parents=True)
    (sb / "cards" / "flux.cmnd").write_text("seed")
    (sb / "notes.md").write_text("seed")
    (sb / "old.txt").write_text("seed")
    record_trial_start(tdir, sb, system_prompt="SYS", user_prompt="USER")
    # The agent works: modifies one seed file, deletes another, adds files.
    (sb / "notes.md").write_text("seed, then edited")
    (sb / "old.txt").unlink()
    (sb / "output").mkdir()
    (sb / "output" / "answer.json").write_text("{}")
    return run, tid, sb


def test_list_files_marks_against_the_initial_snapshot(tmp_path):
    run, tid, _ = _trial_with_sandbox(tmp_path)
    reader = CampaignReader(tmp_path)
    listing = reader.list_files("run_a", tid)
    assert listing["source"] == "sandbox" and listing["has_init"]
    marks = {e["name"]: e["mark"] for e in listing["entries"]}
    assert marks == {"cards": "same", "output": "added", "notes.md": "modified",
                     "old.txt": "deleted"}
    sub = reader.list_files("run_a", tid, "output")
    assert [(e["path"], e["mark"]) for e in sub["entries"]] == [("output/answer.json", "added")]


def test_files_fall_back_to_artifacts_without_deleted_marks(tmp_path):
    import shutil
    run, tid, sb = _trial_with_sandbox(tmp_path)
    art = sb.parent / "artifacts" / "output"
    art.mkdir(parents=True)
    shutil.copy2(sb / "output" / "answer.json", art / "answer.json")
    shutil.rmtree(sb)
    listing = CampaignReader(tmp_path).list_files("run_a", tid)
    assert listing["source"] == "artifacts"
    assert {e["name"]: e["mark"] for e in listing["entries"]} == {"output": "added"}


def test_read_file_and_prompts(tmp_path):
    run, tid, sb = _trial_with_sandbox(tmp_path)
    (sb / "blob.bin").write_bytes(b"\x00\x01\x02")
    reader = CampaignReader(tmp_path)
    assert reader.read_file("run_a", tid, "notes.md")["text"] == "seed, then edited"
    assert reader.read_file("run_a", tid, "blob.bin")["binary"] is True
    assert reader.trial_prompts("run_a", tid) == {"system": "SYS", "user": "USER"}


@pytest.mark.parametrize("rel", ["../trial.json", "../../../manifest.json", "/etc/passwd", "escape"])
def test_workspace_refuses_paths_outside_the_sandbox(tmp_path, rel):
    import os
    run, tid, sb = _trial_with_sandbox(tmp_path)
    (sb.parent / "trial.json").write_text("{}")
    os.symlink(tmp_path, sb / "escape")
    reader = CampaignReader(tmp_path)
    assert reader.read_file("run_a", tid, rel) is None
    assert reader.list_files("run_a", tid, rel) is None


def test_feedback_from_trial_json(tmp_path):
    run, tid, _ = _trial_with_sandbox(tmp_path)
    reader = CampaignReader(tmp_path)
    assert reader.trial_feedback("run_a", tid) is None
    assert reader.list_files("run_a", tid)["has_feedback"] is False
    (run / "trials" / tid / "trial.json").write_text(json.dumps({"ux_feedback": {
        "blind_rating": "7/10", "response": "The flux tool was clear.", "error": None}}))
    assert reader.trial_feedback("run_a", tid)["blind_rating"] == "7/10"
    assert reader.list_files("run_a", tid)["has_feedback"] is True
    assert reader.list_files("run_a", tid, "cards")["has_feedback"] is False


def test_cell_stats_score_crashes_and_leaks_as_zero_like_the_summary(tmp_path):
    run, plan = _make_run(tmp_path, n=4, loadouts=("tools",))
    (run / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        _row(plan[0], ok=True, score=1.0),
        _row(plan[1], ok=False, score=0.0, mode="AGENT_CRASH"),
        _row(plan[2], ok=False, score=0.0, mode="INTEGRITY_LEAK"),
        _row(plan[3], ok=False, score=0.0, mode="SESSION_LIMIT")]))
    (r,) = CampaignReader(tmp_path).snapshot()["runs"]
    (cell,) = r["cells"]
    assert [t["state"] for t in cell["trials"]] == ["passed", "crashed", "leak", "excluded"]
    assert cell["n_scored"] == 3 and cell["n_passed"] == 1
    assert cell["mean_reach"] == pytest.approx(1 / 3, abs=1e-4)
    assert r["counts"]["crashed"] == 1 and r["counts"]["excluded"] == 1
