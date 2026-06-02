"""End-to-end tests for the geometry fixture: the prefix-product R_j
table (no LLM) and the dry-run wiring smoke (StubLLM, no provider key)."""

import json
import tempfile
import unittest
from pathlib import Path

from toolbench.core.checks import load_benchmark_checks, merged_registry
from toolbench.core.judge import RuleJudge
from toolbench.core.metrics import per_trial_reach
from toolbench.core.trajectory import Trajectory
from tests.helpers import GEOMETRY_DIR, load_geometry

try:
    import orchestral  # noqa: F401
    HAVE_DEPS = True
except Exception:
    HAVE_DEPS = False


class TestRubricPrefixProduct(unittest.TestCase):
    """The R_j table from docs/WORKFLOWS_SIMPLE.md (W10), graded for real."""

    def setUp(self):
        self.bench = load_geometry()
        self.reg = merged_registry(load_benchmark_checks(self.bench.checks_module_path()))
        self.order = [s["id"] for s in self.bench.rubric.stages]
        self.weights = [float(s["weight"]) for s in self.bench.rubric.stages]

    def _reach(self, answer):
        with tempfile.TemporaryDirectory() as sb:
            sb = Path(sb)
            (sb / "output").mkdir()
            if answer is not None:
                (sb / "output" / "answer.json").write_text(json.dumps(answer))
            judge = RuleJudge(registry=self.reg, benchmark_dir=str(self.bench.BENCHMARK_DIR))
            grade = judge.grade(Trajectory(), self.bench.rubric, str(sb))
        matrix = [[1 if grade.stages[s] else 0 for s in self.order]]
        return per_trial_reach(matrix, self.weights)[0]

    def test_table(self):
        self.assertAlmostEqual(self._reach(None), 0.0)
        self.assertAlmostEqual(self._reach({"distance": 5.0, "midpoint": [9.0, 9.0]}), 0.2)
        self.assertAlmostEqual(self._reach({"distance": 9.9, "midpoint": [1.5, 2.0]}), 0.5)
        self.assertAlmostEqual(self._reach({"distance": 5.0, "midpoint": [1.5, 2.0]}), 1.0)


@unittest.skipUnless(HAVE_DEPS, "orchestral not importable")
class TestDryRunSmoke(unittest.TestCase):
    def test_dry_run_returns_zero(self):
        import toolbench.cli as cli
        with tempfile.TemporaryDirectory() as runs:
            orig = cli.EVAL_ROOT
            try:
                cli.EVAL_ROOT = Path(runs)
                rc = cli.main(["run", "--benchmark", str(GEOMETRY_DIR),
                               "--loadouts", "full_local",
                               "--harness", "orchestral/anthropic",
                               "--model", "stub", "--n", "1",
                               "--max-cost-usd", "0", "--dry-run"])
                # a manifest was written under the redirected runs dir
                manifests = list(Path(runs).rglob("manifest.json"))
            finally:
                cli.EVAL_ROOT = orig
        self.assertEqual(rc, 0)
        self.assertTrue(manifests)


if __name__ == "__main__":
    unittest.main()
