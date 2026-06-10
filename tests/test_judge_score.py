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


if __name__ == "__main__":
    unittest.main()
