"""Retry-on-MODEL_FORMAT_CRASH loop in TrialRunner.run_trial.

A malformed-tool-call-JSON crash is nondeterministic, so the runner
re-attempts the trial from a fresh sandbox up to `max_format_retries`
times. We simulate the crash with a fake LLM and treat any crash as a
format crash (so the test doesn't depend on Orchestral's parser
traceback markers), then assert the loop count, the recorded attempts,
and that all attempts accumulate in one console.log.
"""

import tempfile
import unittest
from pathlib import Path

from toolbench.benchmarks import BENCHMARKS
from toolbench.core import runner as runner_mod
from toolbench.core.budget import Budget
from toolbench.core.failure_modes import MODEL_FORMAT_CRASH
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


_CRASH_MSG = "Expecting ',' delimiter: line 1 column 611 (char 610)"


class _StopLLM:
    """Fake LLM that always returns a deliberate no-tool-call 'done' response
    (so Agent.run returns normally → triggers the nudge decision). If
    `write_deliverable_to` is set, it writes output/answer.json into that
    sandbox on each call — simulating a model that produced its deliverable
    (with WRONG values), so the presence gate should NOT nudge."""

    def __init__(self, write_deliverable_to=None):
        self.calls = 0
        self._write_to = write_deliverable_to

    def set_tools(self, tools):
        self.tools = tools

    def get_response(self, context, **kw):
        self.calls += 1
        if self._write_to is not None:
            import json
            out = Path(self._write_to) / "output"
            out.mkdir(parents=True, exist_ok=True)
            (out / "answer.json").write_text(
                json.dumps({"distance": 99.0, "midpoint": [9.0, 9.0]}))
        return Response(model="stop",
                        message=Message(role="assistant", text="done", tool_calls=None))


class _CrashLLM:
    """Fake LLM whose response always raises — mimicking the gpt-oss
    malformed tool-call JSON decode failure inside Agent.run. Records the
    last user message it was handed on each call so a test can assert the
    concrete parser error is fed back on a resume."""

    def __init__(self):
        self.seen_user_messages = []

    def set_tools(self, tools):
        self.tools = tools

    def get_response(self, context, **kw):
        msgs = [m for m in context.get_messages()
                if getattr(m, "role", None) == "user"]
        if msgs:
            self.seen_user_messages.append(
                getattr(msgs[-1], "text", "") or getattr(msgs[-1], "content", ""))
        raise ValueError(_CRASH_MSG)


@unittest.skipUnless(HAVE, "orchestral not importable")
class TestFormatCrashRetry(unittest.TestCase):
    def setUp(self):
        self.created = []

        def _factory(model=None, **kw):
            llm = _CrashLLM()
            self.created.append(llm)
            return llm

        register_provider("crashtest", _factory)
        self.bench = BENCHMARKS["geometry"]()
        bd = self.bench.BENCHMARK_DIR
        self.harness = discover_harnesses(bd)["orchestral/anthropic"]
        self.loadout = discover_loadouts(bd)["core_only"]
        # Treat every crash as a format crash so the retry path runs without
        # depending on Orchestral's parser traceback markers.
        self._orig = runner_mod.classify_crash
        runner_mod.classify_crash = lambda exc, tb: (MODEL_FORMAT_CRASH, "test")

    def tearDown(self):
        runner_mod.classify_crash = self._orig

    def _run(self, max_format_retries):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runner = TrialRunner(max_iterations=3, verbose=False,
                             max_format_retries=max_format_retries)
        res = runner.run_trial(
            model_cfg={"provider": "crashtest", "model": "x"},
            benchmark=self.bench, harness=self.harness, loadout=self.loadout,
            variant=self.bench.get_variant(),
            seed=1, trial_id="t0", run_dir=Path(tmp.name), budget=Budget(None),
        )
        log = (Path(tmp.name) / "trials" / "t0" / "console.log").read_text()
        return res, log

    def test_retries_then_records_attempts(self):
        res, log = self._run(max_format_retries=2)
        self.assertEqual(res.attempts, 3)              # 1 try + 2 retries
        self.assertEqual(res.grade.failure_mode, MODEL_FORMAT_CRASH)
        self.assertIn("retry 1/2", log)
        self.assertIn("retry 2/2", log)                # both retries logged in one file
        # The resume message handed back to the model includes the concrete
        # parser error, not just a generic "it was bad".
        resume_msgs = self.created[0].seen_user_messages[1:]  # all but the first prompt
        self.assertTrue(resume_msgs)
        self.assertTrue(all(_CRASH_MSG in m for m in resume_msgs))
        self.assertTrue(all("not valid JSON" in m for m in resume_msgs))

    def test_retries_can_be_disabled(self):
        res, _ = self._run(max_format_retries=0)
        self.assertEqual(res.attempts, 1)
        self.assertEqual(res.grade.failure_mode, MODEL_FORMAT_CRASH)


@unittest.skipUnless(HAVE, "orchestral not importable")
class TestContinueNudge(unittest.TestCase):
    """Presence-gated continue-nudge: resume a self-terminated trial only when
    a required deliverable is ABSENT, bounded and recorded; never when the
    deliverable exists (even if wrong)."""

    def setUp(self):
        self.bench = BENCHMARKS["geometry"]()
        bd = self.bench.BENCHMARK_DIR
        self.harness = discover_harnesses(bd)["orchestral/anthropic"]
        self.loadout = discover_loadouts(bd)["core_only"]
        self._sandbox_path = None  # set per-run so _StopLLM can write into it

        def _factory(model=None, **kw):
            return _StopLLM(write_deliverable_to=self._sandbox_path)

        register_provider("stoptest", _factory)

    def _run(self, *, nudges, deliverable_present):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run_dir = Path(tmp.name)
        self._sandbox_path = (run_dir / "trials" / "t0" / "sandbox"
                              if deliverable_present else None)
        runner = TrialRunner(max_iterations=3, verbose=False,
                             max_continue_nudges=nudges)
        res = runner.run_trial(
            model_cfg={"provider": "stoptest", "model": "x"},
            benchmark=self.bench, harness=self.harness, loadout=self.loadout,
            variant=self.bench.get_variant(),
            seed=1, trial_id="t0", run_dir=run_dir, budget=Budget(None),
        )
        log = (run_dir / "trials" / "t0" / "console.log").read_text()
        return res, log

    def test_nudges_when_deliverable_absent_bounded(self):
        # Model keeps stopping with no output produced → nudge up to the bound.
        res, log = self._run(nudges=2, deliverable_present=False)
        self.assertEqual(res.nudges, 2)
        self.assertIn("nudge 1/2", log)
        self.assertIn("nudge 2/2", log)
        self.assertFalse(res.grade.stages.get("answer_written"))  # never produced

    def test_no_nudge_when_deliverable_present_even_if_wrong(self):
        # Model produced output/answer.json (WRONG values) then stopped.
        # Presence passes → NOT nudged → correctness handled by the grade.
        res, _ = self._run(nudges=2, deliverable_present=True)
        self.assertEqual(res.nudges, 0)
        self.assertTrue(res.grade.stages.get("answer_written"))     # present
        self.assertFalse(res.grade.stages.get("distance_correct"))  # but wrong

    def test_nudges_off_by_default(self):
        res, _ = self._run(nudges=0, deliverable_present=False)
        self.assertEqual(res.nudges, 0)


if __name__ == "__main__":
    unittest.main()
