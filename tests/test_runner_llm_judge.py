"""Post-grade LLM-judge phase in TrialRunner.run_trial.

An opt-in judge runs SERIALLY after the authoritative rule grade,
against the finished sandbox. The properties that matter:
  - the rule grade stays primary (score + failure mode unchanged);
  - the judge's grade is attached additively in grade.alt_grades;
  - a judge failure is recorded there and never disturbs the trial.
Mirrors the discipline of the post-completion UX turn.
"""

import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import load_geometry
from toolbench.core.budget import Budget
from toolbench.core.failure_modes import NONE
from toolbench.core.harness import discover_harnesses
from toolbench.core.llm_factory import register_provider
from toolbench.core.loadout import discover_loadouts
from toolbench.core.runner import TrialRunner
from toolbench.core.task import Grade

try:
    import orchestral  # noqa: F401
    from orchestral.context.message import Message
    from orchestral.llm.base.response import Response
    HAVE = True
except Exception:
    HAVE = False


class _DeliverLLM:
    """Writes a correct geometry deliverable, then stops with no tool call."""

    def set_tools(self, tools):
        self.tools = tools

    def get_response(self, context, **kw):
        # The sandbox is this process's cwd during Agent.run for the geometry
        # benchmark's file tools; write via the tool path used by the harness.
        for t in getattr(self, "tools", []):
            base = getattr(t, "base_directory", None)
            if base:
                out = Path(base) / "output"
                out.mkdir(parents=True, exist_ok=True)
                (out / "answer.json").write_text(
                    json.dumps({"distance": 5.0, "midpoint": [1.5, 2.0]}))
                break
        return Response(model="deliver",
                        message=Message(role="assistant", text="done",
                                        tool_calls=None))


class _FakeJudge:
    """Stand-in LLM judge. Returns a fixed grade, or raises if `boom`."""

    def __init__(self, score=0.5, boom=False):
        self.kind = "llm:fake"
        self._score, self._boom = score, boom
        self.calls = 0

    def grade(self, trajectory, rubric, base_directory):
        self.calls += 1
        if self._boom:
            raise RuntimeError("judge provider down")
        return Grade(score=self._score, stages={}, stage_grades=[],
                     failure_mode=NONE, judge_kind=self.kind)


@unittest.skipUnless(HAVE, "orchestral not importable")
class TestPostGradeLLMJudge(unittest.TestCase):
    def setUp(self):
        register_provider("delivertest", lambda model=None, **kw: _DeliverLLM())
        self.bench = load_geometry()
        bd = self.bench.BENCHMARK_DIR
        self.harness = discover_harnesses(bd)["orchestral/anthropic"]
        self.loadout = discover_loadouts(bd)["core_only"]

    def _run(self, judge):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runner = TrialRunner(max_iterations=2, verbose=False, llm_judge=judge)
        res = runner.run_trial(
            model_cfg={"provider": "delivertest", "model": "x"},
            benchmark=self.bench, harness=self.harness, loadout=self.loadout,
            variant=self.bench.get_variant(),
            seed=1, trial_id="t0", run_dir=Path(tmp.name), budget=Budget(None))
        return res, Path(tmp.name)

    def test_no_judge_leaves_alt_grades_empty(self):
        res, _ = self._run(judge=None)
        self.assertEqual(res.grade.alt_grades, [])
        self.assertEqual(res.grade.judge_kind, "rule")

    def test_judge_grade_is_attached_additively(self):
        judge = _FakeJudge(score=0.42)
        res, run_dir = self._run(judge)
        self.assertEqual(judge.calls, 1)
        # Rule grade stays primary and untouched.
        self.assertEqual(res.grade.judge_kind, "rule")
        rule_score = res.grade.score
        # LLM grade rides along, distinguishable by judge_kind.
        self.assertEqual(len(res.grade.alt_grades), 1)
        alt = res.grade.alt_grades[0]
        self.assertEqual(alt["judge_kind"], "llm:fake")
        self.assertEqual(alt["score"], 0.42)
        self.assertNotEqual(alt["score"], rule_score)
        # And it is persisted to trial.json under grade.alt_grades.
        rec = json.loads((run_dir / "trials" / "t0" / "trial.json").read_text())
        self.assertEqual(rec["grade"]["alt_grades"][0]["score"], 0.42)

    def test_judge_failure_does_not_disturb_the_trial(self):
        judge = _FakeJudge(boom=True)
        res, _ = self._run(judge)
        # Score and failure mode come from the rule grade, not the dead judge.
        self.assertEqual(res.grade.judge_kind, "rule")
        self.assertIsInstance(res.grade.score, float)
        self.assertEqual(len(res.grade.alt_grades), 1)
        self.assertIn("error", res.grade.alt_grades[0])
        self.assertIn("judge provider down", res.grade.alt_grades[0]["error"])


if __name__ == "__main__":
    unittest.main()
