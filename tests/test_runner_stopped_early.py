"""MODEL_STOPPED_EARLY vs INCOMPLETE_AT_<stage>: a model that finishes the
task and gets it wrong is not a model that stopped early.

The runner overrides the judge's failure_mode when the agent loop exits on a
response carrying no tool calls. "The model thought it was done" is equally
true of an agent that quit mid-execution and one that completed the whole
deliverable and got the physics wrong, and only the first is stopping early.

Without a guard the override fired on both, so every confidently-wrong trial
-- the most common outcome in a capability benchmark -- lost the judge's
INCOMPLETE_AT_<rung> label, and with it the footer's evidence line (the footer
prints stage evidence for INCOMPLETE_AT_X but judge_notes for this mode, which
is empty on a clean stop). A three-trial symbolic run reported
MODEL_STOPPED_EARLY x3 and no reason for any of them, while the judge had
correctly identified a different failed rung for each.

The guard is `not any(grade.stages.values())`: any passed stage means the
trial produced gradeable work.
"""

import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import load_geometry
from toolbench.core.budget import Budget
from toolbench.core.failure_modes import MODEL_STOPPED_EARLY, incomplete_at
from toolbench.core.harness import discover_harnesses
from toolbench.core.llm_factory import register_provider
from toolbench.core.loadout import discover_loadouts
from toolbench.core.runner import TrialRunner

try:
    import orchestral  # noqa: F401
    from orchestral.context.message import Message
    from orchestral.llm.base.response import Response
    HAVE = True
except Exception:
    HAVE = False


class _DoneLLM:
    """Returns a no-tool-call 'done' response, optionally depositing a
    deliverable in the sandbox first.

    The deposit stands in for whatever tool calls would have produced the
    file; the classification under test reads the GRADE and the final
    response, not how the artifact got there, so driving real tool calls
    would add scaffolding without adding coverage.
    """

    def __init__(self, sandbox: Path, payload=None):
        self.sandbox = sandbox
        self.payload = payload
        self.calls = 0

    def set_tools(self, tools):
        self.tools = tools

    def get_response(self, context, **kw):
        self.calls += 1
        if self.payload is not None and self.calls == 1:
            out = self.sandbox / "output"
            out.mkdir(parents=True, exist_ok=True)
            (out / "answer.json").write_text(json.dumps(self.payload))
        return Response(model="t",
                        message=Message(role="assistant", text="done",
                                        tool_calls=None))


@unittest.skipUnless(HAVE, "orchestral not importable")
class TestStoppedEarlyVsIncomplete(unittest.TestCase):
    def setUp(self):
        self.bench = load_geometry()
        bd = self.bench.BENCHMARK_DIR
        self.harness = discover_harnesses(bd)["orchestral/anthropic"]
        self.loadout = discover_loadouts(bd)["core_only"]

    def _run(self, payload):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run_dir = Path(tmp.name)
        sandbox = run_dir / "trials" / "t0" / "sandbox"
        llm = _DoneLLM(sandbox, payload)
        register_provider("stoppedtest", lambda model=None, **kw: llm)
        runner = TrialRunner(max_iterations=3, verbose=False)
        return runner.run_trial(
            model_cfg={"provider": "stoppedtest", "model": "x"},
            benchmark=self.bench, harness=self.harness, loadout=self.loadout,
            variant=self.bench.get_variant(),
            seed=1, trial_id="t0", run_dir=run_dir, budget=Budget(None),
        )

    def test_empty_handed_stop_is_stopped_early(self):
        """The original case: the agent quits having produced nothing."""
        res = self._run(payload=None)
        self.assertFalse(any(res.grade.stages.values()),
                         f"expected no stage to pass, got {res.grade.stages}")
        self.assertEqual(res.grade.failure_mode, MODEL_STOPPED_EARLY)

    def test_completed_but_wrong_keeps_its_rung(self):
        """The deliverable exists and is wrong: that is a capability result,
        and the informative label is which rung it fell at."""
        # Right keys (answer_written passes), wrong values (the rest fail).
        res = self._run(payload={"distance": 999.0, "midpoint": [99.0, 99.0]})
        self.assertTrue(res.grade.stages["answer_written"])
        self.assertNotEqual(
            res.grade.failure_mode, MODEL_STOPPED_EARLY,
            "a trial that produced a graded deliverable must not be labelled "
            "as having stopped early")
        self.assertEqual(res.grade.failure_mode,
                         incomplete_at("midpoint_correct"))

    def test_evidence_line_survives(self):
        """The label change is not cosmetic: the footer prints stage evidence
        only for INCOMPLETE_AT_X, so the override also silenced the reason."""
        res = self._run(payload={"distance": 999.0, "midpoint": [99.0, 99.0]})
        first_failed = next(s for s in res.grade.stage_grades if not s.passed)
        self.assertTrue(first_failed.evidence,
                        "the failed stage should carry evidence to report")

    def test_partial_progress_reports_the_deepest_rung_reached(self):
        """Two trials that both stop voluntarily but get different distances
        through the rubric must not collapse to the same label."""
        wrong_both = self._run(
            payload={"distance": 999.0, "midpoint": [99.0, 99.0]})
        right_midpoint = self._run(
            payload={"distance": 999.0, "midpoint": [1.5, 2.0]})
        self.assertEqual(wrong_both.grade.failure_mode,
                         incomplete_at("midpoint_correct"))
        self.assertEqual(right_midpoint.grade.failure_mode,
                         incomplete_at("distance_correct"))
        self.assertNotEqual(wrong_both.grade.failure_mode,
                            right_midpoint.grade.failure_mode)


if __name__ == "__main__":
    unittest.main()
