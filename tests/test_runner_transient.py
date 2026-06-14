"""TRANSIENT_API_ERROR handling in TrialRunner: a transient transport /
server fault (connect or read timeout, dropped connection, HTTP 5xx) is
retried with backoff on the same session instead of being recorded as a
model failure — otherwise one unreachable-endpoint window zeroes out
every trial it spans (the failure that wiped five colliderbench tasks on
2026-06-13 when the gpt-oss endpoint went unreachable mid-campaign)."""

import tempfile
import unittest
from pathlib import Path

from tests.helpers import load_geometry
from toolbench.core import runner as runner_mod
from toolbench.core.budget import Budget
from toolbench.core.failure_modes import TRANSIENT_API_ERROR
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


class APITimeoutError(Exception):
    """Same type name the OpenAI SDK raises on a connect/read timeout — the
    classifier matches on the name, so no SDK import is needed."""


class _FlakyLLM:
    """Raises APITimeoutError for the first `fail_n` calls, then returns a
    deliberate no-tool-call 'done' response."""

    def __init__(self, fail_n):
        self.fail_n = fail_n
        self.calls = 0

    def set_tools(self, tools):
        self.tools = tools

    def get_response(self, context, **kw):
        self.calls += 1
        if self.calls <= self.fail_n:
            raise APITimeoutError("Request timed out.")
        return Response(model="t",
                        message=Message(role="assistant", text="done",
                                        tool_calls=None))


@unittest.skipUnless(HAVE, "orchestral not importable")
class TestTransientRetry(unittest.TestCase):
    def setUp(self):
        self.bench = load_geometry()
        bd = self.bench.BENCHMARK_DIR
        self.harness = discover_harnesses(bd)["orchestral/anthropic"]
        self.loadout = discover_loadouts(bd)["core_only"]
        # Capture backoff sleeps instead of actually waiting.
        self.sleeps = []
        self._orig_sleep = runner_mod._sleep
        runner_mod._sleep = self.sleeps.append

    def tearDown(self):
        runner_mod._sleep = self._orig_sleep

    def _run(self, llm, max_transient_retries):
        register_provider("flakytest", lambda model=None, **kw: llm)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runner = TrialRunner(max_iterations=3, verbose=False,
                             max_transient_retries=max_transient_retries)
        res = runner.run_trial(
            model_cfg={"provider": "flakytest", "model": "x"},
            benchmark=self.bench, harness=self.harness, loadout=self.loadout,
            variant=self.bench.get_variant(),
            seed=1, trial_id="t0", run_dir=Path(tmp.name), budget=Budget(None),
        )
        log = (Path(tmp.name) / "trials" / "t0" / "console.log").read_text()
        return res, log

    def test_recovers_within_retry_budget(self):
        res, log = self._run(_FlakyLLM(fail_n=2), max_transient_retries=4)
        self.assertEqual(res.transient_retries, 2)
        self.assertIsNone(res.error)                       # recovered cleanly
        self.assertNotEqual(res.grade.failure_mode, TRANSIENT_API_ERROR)
        self.assertEqual(self.sleeps, [15, 45])            # backoff schedule
        self.assertIn("retry 1/4 after TRANSIENT_API_ERROR", log)
        self.assertIn("backing off 15s", log)

    def test_exhausted_retries_recorded_as_transient(self):
        res, _ = self._run(_FlakyLLM(fail_n=99), max_transient_retries=2)
        self.assertEqual(res.transient_retries, 2)
        self.assertEqual(res.grade.failure_mode, TRANSIENT_API_ERROR)
        self.assertIn("transient", res.grade.judge_notes)

    def test_retries_can_be_disabled(self):
        res, _ = self._run(_FlakyLLM(fail_n=99), max_transient_retries=0)
        self.assertEqual(res.transient_retries, 0)
        self.assertEqual(res.grade.failure_mode, TRANSIENT_API_ERROR)
        self.assertEqual(self.sleeps, [])


if __name__ == "__main__":
    unittest.main()
