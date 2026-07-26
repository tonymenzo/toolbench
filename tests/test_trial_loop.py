"""Trial-loop scheduling: seed-major (round-robin) order, balanced budget
aborts, and the --parallel execution path."""

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from toolbench.cli import _build_work_items, _run_trial_loop
from toolbench.core.budget import Budget


def _h(hid):
    return SimpleNamespace(id=hid, provider_name="stub")


def _named(n):
    return SimpleNamespace(name=n)


class TestBuildWorkItems(unittest.TestCase):
    def test_seed_major_order(self):
        items = _build_work_items(
            harnesses=[_h("h")], loadouts=[_named("A"), _named("B")],
            variants=[_named("v")], models=["m"], seeds=[1, 2],
            completed=set())
        order = [(it["loadout"].name, it["seed"]) for it in items]
        # Both cells run seed 1 before either runs seed 2.
        self.assertEqual(order, [("A", 1), ("B", 1), ("A", 2), ("B", 2)])

    def test_completed_skipped(self):
        items = _build_work_items(
            harnesses=[_h("h")], loadouts=[_named("A")],
            variants=[_named("v")], models=["m"], seeds=[1, 2],
            completed={("h", "A", "v", "m", 1)})
        self.assertEqual([it["seed"] for it in items], [2])

    def test_trial_id_shape_single_axis(self):
        items = _build_work_items(
            harnesses=[_h("h")], loadouts=[_named("A")],
            variants=[_named("v")], models=["m"], seeds=[1001],
            completed=set())
        self.assertEqual(items[0]["trial_id"], "A__n000__seed1001")
        self.assertEqual(items[0]["condition"], "A")


class _FakeRunner:
    """Stands in for TrialRunner: records execution, optionally charges
    the budget per trial so abort behavior can be exercised."""

    def __init__(self, cost_per_trial=0.0, barrier=None, fail_mode=None):
        self.cost = cost_per_trial
        self.ran = []
        self.barrier = barrier
        # When set, every trial returns this failure_mode with score 0 (used to
        # exercise the session-limit abort path).
        self.fail_mode = fail_mode
        self._lock = threading.Lock()

    def run_trial(self, *, model_cfg, benchmark, harness, loadout, variant,
                  seed, trial_id, run_dir, budget):
        if self.barrier is not None:
            # Prove genuine concurrency: every in-flight trial must reach
            # this point before any can proceed.
            self.barrier.wait(timeout=10)
        if self.cost:
            # Slow paid trials down enough that the main thread's abort
            # (cancel_futures) demonstrably beats the queue draining.
            import time
            time.sleep(0.05)
        with self._lock:
            self.ran.append(trial_id)
        aborted = False
        try:
            budget.add(self.cost)
        except Exception:
            aborted = True
        from toolbench.core.task import Grade
        from toolbench.core.trajectory import Trajectory
        if self.fail_mode is not None:
            grade = Grade(score=0.0, stages={}, stage_grades=[],
                          failure_mode=self.fail_mode, judge_kind="rule")
            return SimpleNamespace(
                trial_id=trial_id, ok=False, score=0.0, grade=grade,
                trajectory=Trajectory(), wall_clock_s=0.0, cost_usd=self.cost,
                aborted_by_budget=aborted, error=None, attempts=1, nudges=0,
                rate_limit_retries=0, transient_retries=0)
        grade = Grade(score=1.0, stages={"s0": True}, stage_grades=[],
                      failure_mode="NONE", judge_kind="rule")
        return SimpleNamespace(
            trial_id=trial_id, ok=not aborted, score=1.0, grade=grade,
            trajectory=Trajectory(), wall_clock_s=0.0, cost_usd=self.cost,
            aborted_by_budget=aborted, error=None, attempts=1, nudges=0,
            rate_limit_retries=0, transient_retries=0)


class TestRunTrialLoop(unittest.TestCase):
    def _loop(self, runner, budget, parallel=1, n_loadouts=2, seeds=(1, 2, 3)):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return _run_trial_loop(
            benchmark=SimpleNamespace(name="b"),
            harnesses=[_h("h")],
            loadouts=[_named(c) for c in "AB"[:n_loadouts]],
            variants=[_named("v")], models=["m"], seeds=list(seeds),
            run_dir=Path(tmp.name), runner=runner, budget=budget,
            completed=set(), dry_run=True, parallel=parallel)

    def test_serial_runs_everything(self):
        runner = _FakeRunner()
        records, aborted, reason = self._loop(runner, Budget(None))
        self.assertEqual(len(records), 6)
        self.assertFalse(aborted)
        self.assertIsNone(reason)

    def test_budget_abort_is_balanced_across_cells(self):
        # $1/trial, $3.5 cap → abort fires on trial 4 (seed-major order:
        # A1 B1 A2 B2 ...), leaving each loadout with the SAME number of
        # completed trials instead of one loadout hogging the budget.
        runner = _FakeRunner(cost_per_trial=1.0)
        records, aborted, reason = self._loop(runner, Budget(3.5))
        self.assertTrue(aborted)
        self.assertEqual(reason, "budget")
        per_cell = {}
        for r in records:
            if not r["aborted_by_budget"]:
                per_cell[r["loadout"]] = per_cell.get(r["loadout"], 0) + 1
        counts = sorted(per_cell.values())
        self.assertLessEqual(counts[-1] - counts[0], 1)   # balanced ±1

    def test_parallel_runs_everything_and_is_concurrent(self):
        barrier = threading.Barrier(2)
        runner = _FakeRunner(barrier=barrier)
        records, aborted, reason = self._loop(runner, Budget(None), parallel=2,
                                              n_loadouts=2, seeds=(1, 2, 3))
        # The barrier requires 2 trials in flight simultaneously; if the
        # loop were serial this would deadlock (and time out the wait).
        self.assertEqual(len(records), 6)
        self.assertFalse(aborted)
        self.assertIsNone(reason)
        self.assertEqual(len(runner.ran), 6)

    def test_parallel_budget_abort_stops_launching(self):
        runner = _FakeRunner(cost_per_trial=1.0)
        records, aborted, reason = self._loop(runner, Budget(2.5), parallel=2,
                                              seeds=(1, 2, 3, 4, 5))
        self.assertTrue(aborted)
        self.assertEqual(reason, "budget")
        # 10 trials enumerated; the abort must prevent most of the tail.
        self.assertLess(len(runner.ran), 10)

    def test_session_limit_aborts_and_stops_launching(self):
        # A subscription session/usage-quota termination must halt the queue
        # (every remaining trial would fail identically until the quota
        # resets) and report the reason as "session_limit", not "budget".
        # cost_per_trial slows each trial (as in the budget tests) so the
        # abort demonstrably beats the queue draining; Budget(None) keeps the
        # abort attributable to the session limit, not the budget.
        runner = _FakeRunner(cost_per_trial=1.0, fail_mode="SESSION_LIMIT")
        records, aborted, reason = self._loop(runner, Budget(None), parallel=2,
                                              seeds=(1, 2, 3, 4, 5))
        self.assertTrue(aborted)
        self.assertEqual(reason, "session_limit")
        # 10 trials enumerated; the abort must prevent most of the tail from
        # ever launching (only the already-in-flight one(s) get recorded).
        self.assertLess(len(runner.ran), 10)
        self.assertTrue(all(r["failure_mode"] == "SESSION_LIMIT"
                            for r in records))


if __name__ == "__main__":
    unittest.main()
