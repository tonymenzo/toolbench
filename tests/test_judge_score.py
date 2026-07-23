"""RuleJudge score semantics: absorbing (prefix-product), normalized.

The docs promise `type: stagewise` ⇒ "trial score is the prefix
product" and "weights need not sum to 1 (the score is normalized)".
These tests pin both properties — in particular that a stage passing
AFTER an earlier failure contributes nothing (where a plain sum of
passed weights would credit it).
"""

import json
import tempfile
import unittest
from pathlib import Path

from toolbench.core.judge import RuleJudge
from toolbench.core.task import Rubric
from toolbench.core.trajectory import Trajectory


def _rubric(weights=(0.2, 0.3, 0.5)):
    return Rubric(stages=[
        {"id": f"s{i}", "weight": w,
         "checks": [{"json_with_keys": {"file": f"out{i}.json",
                                        "required_keys": ["v"]}}]}
        for i, w in enumerate(weights)
    ])


class TestAbsorbingScore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sb = Path(self._tmp.name)
        self.judge = RuleJudge()

    def tearDown(self):
        self._tmp.cleanup()

    def _pass_stage(self, i):
        (self.sb / f"out{i}.json").write_text(json.dumps({"v": 1}))

    def _grade(self, rubric=None):
        return self.judge.grade(Trajectory(), rubric or _rubric(), str(self.sb))

    def test_all_pass(self):
        for i in range(3):
            self._pass_stage(i)
        g = self._grade()
        self.assertEqual(g.score, 1.0)
        self.assertEqual(g.failure_mode, "NONE")

    def test_prefix_only(self):
        self._pass_stage(0)
        self._pass_stage(1)
        g = self._grade()
        self.assertAlmostEqual(g.score, 0.5)   # 0.2 + 0.3

    def test_later_pass_after_failure_contributes_nothing(self):
        # s0 fails; s1 and s2 pass — absorbing convention zeroes them.
        self._pass_stage(1)
        self._pass_stage(2)
        g = self._grade()
        self.assertEqual(g.score, 0.0)
        self.assertTrue(g.stages["s1"])        # still *reported* as passed
        self.assertTrue(g.stages["s2"])
        self.assertEqual(g.failure_mode, "INCOMPLETE_AT_S0")

    def test_score_is_normalized(self):
        # Weights sum to 10; passing the first (weight 2) scores 0.2.
        rubric = _rubric(weights=(2, 3, 5))
        self._pass_stage(0)
        g = self._grade(rubric)
        self.assertAlmostEqual(g.score, 0.2)

    def test_score_matches_per_trial_reach(self):
        from toolbench.core.metrics import per_trial_reach
        self._pass_stage(0)
        self._pass_stage(2)                    # gap at s1
        g = self._grade()
        row = [[1 if g.stages[f"s{i}"] else 0 for i in range(3)]]
        reach = per_trial_reach(row, [0.2, 0.3, 0.5])[0]
        self.assertAlmostEqual(g.score, round(reach, 4))


class TestNonGatingStages(unittest.TestCase):
    """`gating: false` — stages that are independent, not a pipeline.

    Absorption models "stage N presupposes stage N-1". A rubric whose
    stages are separate quantities (three widths in one task) has no
    such dependency, and absorbing there silently zeroes correct work.
    `gating: false` says so without also claiming the partial credit
    that `continuous: true` implies but these binary checks never emit.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sb = Path(self._tmp.name)
        self.judge = RuleJudge()

    def tearDown(self):
        self._tmp.cleanup()

    def _pass_stage(self, i):
        (self.sb / f"out{i}.json").write_text(json.dumps({"v": 1}))

    def _rubric_nongating(self, weights=(0.2, 0.3, 0.5)):
        r = _rubric(weights)
        for s in r.stages[1:]:          # s0 stays the gate (a prerequisite)
            s["gating"] = False
        return r

    def test_failure_does_not_zero_independent_later_stages(self):
        # s1 fails; s0 and s2 pass. Absorbing would score 0.2; here the
        # independent s2 keeps its weight.
        self._pass_stage(0)
        self._pass_stage(2)
        g = self.judge.grade(Trajectory(), self._rubric_nongating(), str(self.sb))
        self.assertAlmostEqual(g.score, 0.7)     # 0.2 + 0.5

    def test_credit_stays_binary(self):
        # Non-gating must not imply partial credit: a failed stage
        # contributes exactly 0, not some closeness.
        self._pass_stage(0)
        g = self.judge.grade(Trajectory(), self._rubric_nongating(), str(self.sb))
        self.assertAlmostEqual(g.score, 0.2)
        by_id = {s.id: s for s in g.stage_grades}
        self.assertEqual(by_id["s1"].credit, 0.0)
        self.assertFalse(by_id["s1"].continuous)
        self.assertFalse(by_id["s1"].gates)
        self.assertTrue(by_id["s0"].gates)

    def test_gate_still_absorbs(self):
        # s0 is the declared gate: its failure zeroes everything, even
        # though the later stages are non-gating.
        self._pass_stage(1)
        self._pass_stage(2)
        g = self.judge.grade(Trajectory(), self._rubric_nongating(), str(self.sb))
        self.assertEqual(g.score, 0.0)

    def test_default_is_unchanged(self):
        # A rubric that sets neither key keeps the original prefix product.
        self._pass_stage(0)
        self._pass_stage(2)
        g = self.judge.grade(Trajectory(), _rubric(), str(self.sb))
        self.assertAlmostEqual(g.score, 0.2)
        self.assertTrue(all(s.gates for s in g.stage_grades))

    def test_continuous_still_implies_non_gating(self):
        r = _rubric()
        r.stages[1]["continuous"] = True
        self._pass_stage(0)
        self._pass_stage(2)
        g = self.judge.grade(Trajectory(), r, str(self.sb))
        self.assertAlmostEqual(g.score, 0.7)


if __name__ == "__main__":
    unittest.main()
