"""Runtime registry: orchestral ships registered; unknown names fail fast."""

import unittest

from toolbench.core.runtime import (
    build_agent, check_runtime_version, register_runtime, registered_runtimes,
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


class TestRuntimeVersionCheck(unittest.TestCase):
    """The harness `runtime.version` pin is enforced, not decorative."""

    def test_no_spec_passes(self):
        self.assertIsNone(check_runtime_version("orchestral", None))
        self.assertIsNone(check_runtime_version("orchestral", ""))

    def test_satisfied_spec_passes(self):
        self.assertIsNone(
            check_runtime_version("orchestral", ">=1.3", installed="1.4.0"))

    def test_violated_spec_errors(self):
        err = check_runtime_version("orchestral", ">=1.3", installed="1.2")
        self.assertIsNotNone(err)
        self.assertIn("1.2", err)
        self.assertIn(">=1.3", err)

    def test_compound_spec(self):
        self.assertIsNone(
            check_runtime_version("orchestral", ">=1.3,<2", installed="1.9"))
        self.assertIsNotNone(
            check_runtime_version("orchestral", ">=1.3,<2", installed="2.0"))

    def test_invalid_spec_errors(self):
        err = check_runtime_version("orchestral", "banana", installed="1.0")
        self.assertIsNotNone(err)
        self.assertIn("invalid", err)

    def test_runtime_without_dist_skips_with_none(self):
        # Registered without `dist`: can't enforce — warns, doesn't block.
        register_runtime("distless", lambda **kw: None)
        try:
            import contextlib, io
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                out = check_runtime_version("distless", ">=1.0")
            self.assertIsNone(out)
            self.assertIn("cannot enforce", buf.getvalue())
        finally:
            from toolbench.core import runtime as rt
            rt._RUNTIMES.pop("distless", None)

    def test_real_orchestral_dist_resolves(self):
        # The shipped registration maps to an installed distribution, so
        # an impossible pin must actually fail against the real version.
        err = check_runtime_version("orchestral", ">=999")
        self.assertIsNotNone(err)


if __name__ == "__main__":
    unittest.main()
