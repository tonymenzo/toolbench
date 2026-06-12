"""Cost extraction in TrialRunner._extract_usage.

LiteLLM proxies report usage.cost = 0.0 for routes they have no pricing
for (e.g. vendor-prefixed aliases like "azure/claude-haiku-4-5"). A $0
cost on millions of tokens must not be trusted as authoritative — it
silently disarms the --max-cost-usd budget cap. The runner should fall
back to the static PRICING_TABLE (stripping the vendor prefix), and only
keep the reported $0 when no fallback knows the model (genuinely free
local routes).
"""

import unittest

from toolbench.core.runner import TrialRunner
from toolbench.core.trajectory import Trajectory

try:
    import orchestral  # noqa: F401
    from orchestral.context.message import Message
    from orchestral.llm.base.response import Response, Usage
    HAVE = True
except Exception:
    HAVE = False


def _fake_agent(model_name: str, cost: float | None,
                prompt_tokens: int = 1_000_000,
                completion_tokens: int = 10_000):
    class _Ctx:
        pass

    class _Agent:
        pass

    usage = Usage(
        model_name=model_name,
        tokens={"prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens},
        cost=cost,
    )
    resp = Response(model=model_name,
                    message=Message(role="assistant", text="done",
                                    tool_calls=None),
                    usage=usage)
    agent = _Agent()
    agent.context = _Ctx()
    agent.context.messages = [resp]
    return agent


def _runner():
    r = TrialRunner.__new__(TrialRunner)
    r.litellm_pricing = None        # proxy snapshot unavailable
    return r


@unittest.skipUnless(HAVE, "orchestral not installed")
class TestCostExtraction(unittest.TestCase):
    def test_zero_cost_unpriced_alias_falls_back_to_static_table(self):
        """azure/claude alias with proxy cost 0.0 → static Claude pricing."""
        traj = Trajectory()
        agent = _fake_agent("azure/claude-haiku-4-5", cost=0.0)
        _runner()._extract_usage(agent, traj,
                                 configured_model="azure/claude-haiku-4-5")
        # 1M input @ $1/M + 10k output @ $5/M = $1.05
        self.assertAlmostEqual(traj.cost_usd, 1.05, places=6)

    def test_positive_provider_cost_is_trusted(self):
        traj = Trajectory()
        agent = _fake_agent("azure/claude-haiku-4-5", cost=2.5)
        _runner()._extract_usage(agent, traj,
                                 configured_model="azure/claude-haiku-4-5")
        self.assertAlmostEqual(traj.cost_usd, 2.5, places=6)

    def test_zero_cost_unknown_model_stays_zero(self):
        """Free local route (no fallback entry) keeps the reported $0."""
        traj = Trajectory()
        agent = _fake_agent("openai/gpt-oss-120b", cost=0.0)
        _runner()._extract_usage(agent, traj,
                                 configured_model="openai/gpt-oss-120b")
        self.assertEqual(traj.cost_usd, 0.0)

    def test_no_cost_field_unknown_model_stays_none(self):
        traj = Trajectory()
        agent = _fake_agent("qwen/qwen35-9b", cost=None)
        _runner()._extract_usage(agent, traj,
                                 configured_model="qwen/qwen35-9b")
        self.assertIsNone(traj.cost_usd)


if __name__ == "__main__":
    unittest.main()
