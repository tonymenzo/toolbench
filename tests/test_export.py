"""Export contract tests.

The export is what leaves the machine, so the two properties worth pinning are
(1) the flat schema stays stable and self-describing, and (2) scrubbing actually
removes machine-specific paths — a silent regression there publishes someone's
home directory.
"""
import gzip
import json
from pathlib import Path

import pytest

from toolbench.core.export import SCHEMA_VERSION, export_run


def _make_run(tmp_path: Path, home_marker: str) -> Path:
    run = tmp_path / "runs" / "2026-01-01T00-00-00_demo_stub_run"
    (run / "trials" / "core_only__n000__seed1001" / "artifacts" / "results").mkdir(
        parents=True)
    trial_dir = run / "trials" / "core_only__n000__seed1001"

    (run / "trials.jsonl").write_text(json.dumps({
        "trial_id": "core_only__n000__seed1001",
        "model": "stub", "harness": "h/default", "loadout": "core_only",
        "variant": "v", "seed": 1001, "ok": True, "score": 0.95,
        "stages": {"a": True}, "stage_credits": {"a": 1.0},
        "wall_clock_s": 12.5, "cost_usd": 0.25, "output_tokens": 100,
        "tool_calls": 3, "tool_errors": 0, "failure_mode": None,
    }) + "\n")
    (run / "manifest.json").write_text(json.dumps({
        "run_id": run.name, "task": "demo", "versions": {"toolbench": "0.3.0"},
    }))
    (run / "summary.json").write_text(json.dumps({
        "reach_weights": {"pass_threshold": 0.9},
    }))
    (run / "summary.txt").write_text(f"summary referencing {home_marker}/secret/path\n")
    (trial_dir / "trial.json").write_text(json.dumps({
        "grade": {"stage_grades": [
            {"id": "a", "weight": 1.0, "credit": 1.0, "evidence": "ok",
             "metrics": {"check_a": {"closeness": 0.5, "distance": 1.25,
                                     "per_item": {"x": 1}}}},
        ]}
    }))
    (trial_dir / "audit.txt").write_text(f"ran in {home_marker}/work/sandbox\n")
    (trial_dir / "artifacts" / "results" / "answer.json").write_text('{"v": 1}')
    with gzip.open(trial_dir / "transcript.jsonl.gz", "wt") as fh:
        fh.write('{"big": "transcript"}\n')
    return run


def test_flat_schema_and_scrub(tmp_path):
    home = str(Path.home())
    run = _make_run(tmp_path, home)
    out = tmp_path / "export"
    rep = export_run(run, out, scrub=True)

    assert rep["n_trials"] == 1
    rows = [json.loads(l) for l in (out / "trials.jsonl").read_text().splitlines()]
    r = rows[0]

    # (1) schema is self-describing and carries the cell coordinates flat
    assert r["schema_version"] == SCHEMA_VERSION
    for key in ("run_id", "trial_id", "harness", "loadout", "variant", "model",
                "seed", "score", "pass_threshold", "passed", "stage_credits",
                "stage_weights", "stage_metrics", "tokens", "provenance"):
        assert key in r, f"missing {key}"

    # pass/fail is resolved from the rubric threshold, not hardcoded
    assert r["pass_threshold"] == 0.9
    assert r["passed"] is True

    # metrics are flattened out of their per-check nesting; bulk dicts dropped
    assert r["stage_metrics"]["a"]["closeness"] == 0.5
    assert "per_item" not in r["stage_metrics"]["a"]

    # (2) no home path survives anywhere in the text output
    for p in list(out.rglob("*")):
        if p.is_file() and p.suffix != ".gz":
            assert home not in p.read_text(), f"unscrubbed home path in {p}"

    # transcripts excluded by default
    assert not list((out / "bundle").rglob("transcript.jsonl.gz"))


def test_transcripts_opt_in_and_no_scrub_flag(tmp_path):
    home = str(Path.home())
    run = _make_run(tmp_path, home)
    out = tmp_path / "export2"
    export_run(run, out, scrub=False, include_transcripts=True)
    assert list((out / "bundle").rglob("transcript.jsonl.gz"))
    # with scrubbing off the raw path is preserved (so the flag is meaningful)
    assert home in (out / "bundle" / "summary.txt").read_text()


def test_rejects_incomplete_run(tmp_path):
    empty = tmp_path / "runs" / "not_a_run"
    empty.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        export_run(empty, tmp_path / "out")
