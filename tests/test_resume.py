"""Resume bookkeeping: the budget cap governs a run's TOTAL spend across
resumes (prior trials are pre-charged), and trials that failed before they
could start (`resolution_error`) are retried instead of frozen at 0."""

import json
from types import SimpleNamespace

import pytest

import toolbench.cli as cli
from toolbench.cli import _partition_resume_rows, cmd_resume
from toolbench.core.budget import Budget, BudgetExceeded
from toolbench.core.store import read_jsonl, write_json

from tests.helpers import GEOMETRY_DIR


# ── units ────────────────────────────────────────────────────────────

def test_partition_splits_resolution_errors():
    rows = [
        {"trial_id": "a", "failure_mode": "INCOMPLETE_AT_X", "cost_usd": 1.0},
        {"trial_id": "b", "failure_mode": "resolution_error"},
        {"trial_id": "c", "failure_mode": "NONE", "cost_usd": 2.0},
    ]
    kept, retryable = _partition_resume_rows(rows)
    assert [r["trial_id"] for r in kept] == ["a", "c"]
    assert [r["trial_id"] for r in retryable] == ["b"]


def test_budget_precharge_counts_toward_cap():
    b = Budget(10.0)
    b.precharge(9.5)
    assert b.spent == 9.5
    with pytest.raises(BudgetExceeded):
        b.add(1.0)


def test_budget_precharge_never_raises_even_over_cap():
    b = Budget(5.0)
    b.precharge(7.0)
    assert b.remaining == -2.0


# ── cmd_resume against the geometry example ─────────────────────────

def _seed_run(tmp_path, *, rows, max_cost_usd, seeds=(1001, 1002)):
    """A minimal-but-valid run dir for cmd_resume: a manifest shaped like
    cmd_run's, plus the given trials.jsonl rows."""
    run_id = "test_resume_run"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    write_json(run_dir / "manifest.json", {
        "run_id": run_id,
        "benchmark": "geometry",
        "benchmark_dir": str(GEOMETRY_DIR),
        "harnesses": [{"id": "orchestral/anthropic",
                       "runtime": {"name": "orchestral"},
                       "provider": {"name": "anthropic"},
                       "core": {"tools": []}, "loop": {}}],
        "loadouts": ["core_only"],
        "variants": [{"name": "direct"}],
        "models": [{"model": "stub"}],
        "task": "geometry",
        "conditions": ["core_only"],
        "n_per_cell": len(seeds),
        "seeds": list(seeds),
        "max_cost_usd": max_cost_usd,
        "dry_run": True,
        "parallel": 1,
    })
    with open(run_dir / "trials.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return run_id, run_dir


def _row(seed, *, failure_mode, cost_usd=None, score=0.0):
    return {
        "trial_id": f"core_only__n{seed - 1001:03d}__seed{seed}",
        "model": "stub", "harness": "orchestral/anthropic",
        "loadout": "core_only", "variant": "direct",
        "condition": "core_only", "seed": seed,
        "ok": False, "score": score, "stages": {},
        "wall_clock_s": 0.1, "cost_usd": cost_usd,
        "failure_mode": failure_mode, "aborted_by_budget": False,
    }


def _resume_args(run_id, max_cost_usd=None):
    return SimpleNamespace(run_id=run_id, max_cost_usd=max_cost_usd,
                           parallel=None, verbose=False)


def test_resume_retries_resolution_error_trials(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_OUTPUT_BASE", tmp_path)
    run_id, run_dir = _seed_run(tmp_path, max_cost_usd=5.0, rows=[
        _row(1001, failure_mode="INCOMPLETE_AT_X", cost_usd=1.0),
        _row(1002, failure_mode="resolution_error", cost_usd=0.0),
    ])
    assert cmd_resume(_resume_args(run_id)) == 0
    rows = read_jsonl(run_dir / "trials.jsonl")
    # The resolution_error row was replaced by a freshly-run trial for the
    # same seed — no duplicate (cell, seed) keys, no frozen zero.
    assert len(rows) == 2
    assert sorted(r["seed"] for r in rows) == [1001, 1002]
    assert all(r["failure_mode"] != "resolution_error" for r in rows)


def test_resume_refuses_exhausted_budget(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_OUTPUT_BASE", tmp_path)
    run_id, run_dir = _seed_run(tmp_path, max_cost_usd=2.0, rows=[
        _row(1001, failure_mode="INCOMPLETE_AT_X", cost_usd=3.0),
    ])
    before = read_jsonl(run_dir / "trials.jsonl")
    assert cmd_resume(_resume_args(run_id)) == 2
    assert "already exhausted" in capsys.readouterr().err
    # Nothing ran, nothing was rewritten.
    assert read_jsonl(run_dir / "trials.jsonl") == before


def test_resume_widened_budget_proceeds(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_OUTPUT_BASE", tmp_path)
    run_id, run_dir = _seed_run(tmp_path, max_cost_usd=2.0, rows=[
        _row(1001, failure_mode="INCOMPLETE_AT_X", cost_usd=3.0),
    ])
    assert cmd_resume(_resume_args(run_id, max_cost_usd=10.0)) == 0
    rows = read_jsonl(run_dir / "trials.jsonl")
    assert sorted(r["seed"] for r in rows) == [1001, 1002]
