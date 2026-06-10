"""RATE_LIMITED handling in TrialRunner: provider throttling (429/529)
is retried with backoff on the same session instead of being recorded as
a model failure — otherwise parallel runs against rate-limited providers
contaminate the model comparison with operational noise."""

import tempfile
import unittest
from pathlib import Path

from tests.helpers import load_geometry
from toolbench.core import runner as runner_mod
from toolbench.core.budget import Budget
from toolbench.core.failure_modes import RATE_LIMITED
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


class RateLimitError(Exception):
    """Same type name the OpenAI/Anthropic SDKs raise — the classifier
    matches on the name, so no SDK import is needed."""


class _ThrottledLLM:
    """Raises RateLimitError for the first `fail_n` calls, then returns a
    deliberate no-tool-call 'done' response."""

    def __init__(self, fail_n):
        self.fail_n = fail_n
        self.calls = 0

    def set_tools(self, tools):
        self.tools = tools

    def get_response(self, context, **kw):
        self.calls += 1
        if self.calls <= self.fail_n:
            raise RateLimitError("Error code: 429 - rate_limit_error")
        return Response(model="t",
                        message=Message(role="assistant", text="done",
                                        tool_calls=None))


@unittest.skipUnless(HAVE, "orchestral not importable")
class TestRateLimitRetry(unittest.TestCase):
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

    def _run(self, llm, max_rate_limit_retries):
        register_provider("throttletest", lambda model=None, **kw: llm)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runner = TrialRunner(max_iterations=3, verbose=False,
                             max_rate_limit_retries=max_rate_limit_retries)
        res = runner.run_trial(
            model_cfg={"provider": "throttletest", "model": "x"},
            benchmark=self.bench, harness=self.harness, loadout=self.loadout,
            variant=self.bench.get_variant(),
            seed=1, trial_id="t0", run_dir=Path(tmp.name), budget=Budget(None),
        )
        log = (Path(tmp.name) / "trials" / "t0" / "console.log").read_text()
        return res, log

    def test_recovers_within_retry_budget(self):
        res, log = self._run(_ThrottledLLM(fail_n=2), max_rate_limit_retries=3)
        self.assertEqual(res.rate_limit_retries, 2)
        self.assertIsNone(res.error)                       # recovered cleanly
        self.assertNotEqual(res.grade.failure_mode, RATE_LIMITED)
        self.assertEqual(self.sleeps, [10, 30])            # backoff schedule
        self.assertIn("retry 1/3 after RATE_LIMITED", log)
        self.assertIn("backing off 10s", log)

    def test_exhausted_retries_recorded_as_rate_limited(self):
        res, _ = self._run(_ThrottledLLM(fail_n=99), max_rate_limit_retries=2)
        self.assertEqual(res.rate_limit_retries, 2)
        self.assertEqual(res.grade.failure_mode, RATE_LIMITED)
        self.assertIn("throttled", res.grade.judge_notes)

    def test_retries_can_be_disabled(self):
        res, _ = self._run(_ThrottledLLM(fail_n=99), max_rate_limit_retries=0)
        self.assertEqual(res.rate_limit_retries, 0)
        self.assertEqual(res.grade.failure_mode, RATE_LIMITED)
        self.assertEqual(self.sleeps, [])


if __name__ == "__main__":
    unittest.main()
