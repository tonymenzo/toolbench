"""Harness provider params reach the model call; cache-bust nonce is opt-in.

Two contracts pinned here:

  * everything in a harness's `provider:` block except toolbench's
    control keys (`name`, `cache_bust`) is forwarded as a request kwarg
    on every model call (orchestral: agent.run(**llm_kwargs) →
    llm.get_response(context, **llm_kwargs));
  * the per-trial prompt nonce is appended ONLY when the provider block
    sets `cache_bust: true` — by default the model sees the variant's
    prompt verbatim, with no trial metadata.
"""

import tempfile
import unittest
from pathlib import Path

from tests.helpers import load_geometry
from toolbench.core.budget import Budget
from toolbench.core.harness import Harness
from toolbench.core.llm_factory import register_provider
from toolbench.core.runner import TrialRunner

try:
    import orchestral  # noqa: F401
    from orchestral.context.message import Message
    from orchestral.llm.base.response import Response
    from orchestral.llm.base.usage import Usage
    HAVE = True
except Exception:
    HAVE = False


class _RecordingLLM:
    """Returns an immediate no-tool-call 'done'; records the kwargs of
    every get_response call and the user messages it has seen. The
    response carries a Usage whose model_name is the *dated snapshot*
    (as real providers report), not the configured alias."""

    def __init__(self):
        self.call_kwargs = []
        self.user_messages = []

    def set_tools(self, tools):
        self.tools = tools

    def get_response(self, context, **kw):
        self.call_kwargs.append(dict(kw))
        for m in context.get_messages():
            if getattr(m, "role", None) == "user":
                text = getattr(m, "text", "") or getattr(m, "content", "")
                if text and text not in self.user_messages:
                    self.user_messages.append(text)
        return Response(model="rec",
                        message=Message(role="assistant", text="done",
                                        tool_calls=None),
                        usage=Usage(model_name="rec-20260101",
                                    tokens={"prompt_tokens": 10,
                                            "completion_tokens": 5},
                                    cost=0.0))


@unittest.skipUnless(HAVE, "orchestral not importable")
class TestProviderParamsAndNonce(unittest.TestCase):
    def setUp(self):
        self.bench = load_geometry()
        self.llm = _RecordingLLM()
        register_provider("rectest", lambda model=None, **kw: self.llm)
        from toolbench.core.loadout import discover_loadouts
        self.loadout = discover_loadouts(self.bench.BENCHMARK_DIR)["core_only"]

    def _harness(self, provider_extra=None):
        return Harness.from_dict({
            "runtime": {"name": "orchestral"},
            "provider": {"name": "rectest", **(provider_extra or {})},
            "core": {"tools": []},
            "loop": {"max_iterations": 3},
        }, id="test/rec")

    def _run(self, harness):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runner = TrialRunner(verbose=False)
        return runner.run_trial(
            model_cfg={"provider": "rectest", "model": "x"},
            benchmark=self.bench, harness=harness, loadout=self.loadout,
            variant=self.bench.get_variant(),
            seed=7, trial_id="t0", run_dir=Path(tmp.name), budget=Budget(None),
        )

    def test_provider_params_forwarded_to_model_call(self):
        self._run(self._harness({"max_tokens": 4321}))
        self.assertTrue(self.llm.call_kwargs)
        for kw in self.llm.call_kwargs:
            self.assertEqual(kw.get("max_tokens"), 4321)

    def test_control_keys_not_forwarded(self):
        self._run(self._harness({"max_tokens": 4321, "cache_bust": True}))
        for kw in self.llm.call_kwargs:
            self.assertNotIn("cache_bust", kw)
            self.assertNotIn("name", kw)

    def test_no_nonce_by_default(self):
        self._run(self._harness())
        self.assertTrue(self.llm.user_messages)
        prompt = self.llm.user_messages[0]
        self.assertNotIn("<!-- trial:", prompt)

    def test_nonce_when_cache_bust(self):
        self._run(self._harness({"cache_bust": True}))
        prompt = self.llm.user_messages[0]
        self.assertIn("<!-- trial: t0 seed: 7 -->", prompt)

    def test_resolved_model_snapshot_persisted(self):
        # The provider served a dated snapshot of the configured alias;
        # the trial record must keep proof of exactly what ran.
        res = self._run(self._harness())
        self.assertEqual(res.trajectory.resolved_model, "rec-20260101")
        self.assertEqual(
            res.trajectory.to_metadata_dict()["resolved_model"],
            "rec-20260101")


if __name__ == "__main__":
    unittest.main()
