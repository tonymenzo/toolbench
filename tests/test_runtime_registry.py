"""Runtime registry: orchestral ships registered; unknown names fail fast."""

import unittest

from toolbench.core.runtime import (
    build_agent, register_runtime, registered_runtimes,
)


class TestRuntimeRegistry(unittest.TestCase):
    def test_orchestral_is_registered(self):
        self.assertIn("orchestral", registered_runtimes())

    def test_unknown_runtime_raises_with_known_list(self):
        with self.assertRaises(ValueError) as ctx:
            build_agent("claude_code", llm=None, tools=[], tool_hooks=[],
                        system_prompt="x")
        self.assertIn("claude_code", str(ctx.exception))
        self.assertIn("orchestral", str(ctx.exception))

    def test_registered_factory_receives_contract_kwargs(self):
        seen = {}

        def factory(**kw):
            seen.update(kw)
            return "agent-sentinel"

        register_runtime("FakeRT", factory)
        try:
            out = build_agent("fakert",   # case-insensitive
                              llm="L", tools=["t"], tool_hooks=["h"],
                              system_prompt="sp", display_hook="d")
            self.assertEqual(out, "agent-sentinel")
            self.assertEqual(seen, {"llm": "L", "tools": ["t"],
                                    "tool_hooks": ["h"], "system_prompt": "sp",
                                    "display_hook": "d"})
        finally:
            # Don't leak the fake into other tests' registered_runtimes().
            from toolbench.core import runtime as rt
            rt._RUNTIMES.pop("fakert", None)


if __name__ == "__main__":
    unittest.main()
