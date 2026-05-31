"""Resolver tests: harness core + loadout python/toolbase sources.

Requires orchestral (for `define_tool` tools) and the `toolbench.bench_tools`
package on the path; skipped otherwise.
"""

import tempfile
import unittest

try:
    import toolbench.bench_tools.dunderkit  # noqa: F401
    import orchestral  # noqa: F401
    HAVE_DEPS = True
except Exception:
    HAVE_DEPS = False

from toolbench.core.harness import Harness
from toolbench.core.loadout import Loadout


@unittest.skipUnless(HAVE_DEPS, "orchestral / toolbench.bench_tools not importable")
class TestResolver(unittest.TestCase):
    def setUp(self):
        from toolbench.core import tool_resolver
        self.tr = tool_resolver
        self.h = Harness.from_dict(
            {"runtime": {"name": "orchestral"}, "provider": {"name": "anthropic"},
             "core": {"tools": ["RunPythonTool", "WriteFileTool"]}}, id="orchestral/anthropic")

    def _names(self, sources, select=None):
        lo = Loadout.from_dict({"tools": {"sources": sources}}, name="x")
        with tempfile.TemporaryDirectory() as sb:
            tools, report = self.tr.build_agent_tools(self.h, lo, sb)
        return [self.tr._tool_name(t) for t in tools], report

    def test_full_local(self):
        n, _ = self._names([{"python": "toolbench.bench_tools.dunderkit"}, {"python": "toolbench.bench_tools.euclid"}])
        for t in ("add", "subtract", "multiply", "divide", "power", "euclidean_distance"):
            self.assertIn(t, n)

    def test_select_bundle(self):
        n, _ = self._names([{"python": "toolbench.bench_tools.dunderkit", "select": ["additive"]}])
        self.assertIn("add", n)
        self.assertIn("subtract", n)
        self.assertNotIn("power", n)
        self.assertNotIn("divide", n)

    def test_all_metrics(self):
        n, _ = self._names([{"python": "toolbench.bench_tools.dunderkit"}, {"python": "toolbench.bench_tools.euclid"},
                            {"python": "toolbench.bench_tools.taxicab"}, {"python": "toolbench.bench_tools.chebyshev"}])
        for t in ("euclidean_distance", "manhattan_distance", "chebyshev_distance"):
            self.assertIn(t, n)

    def test_collision_errors(self):
        with self.assertRaises(ValueError):
            self._names([{"python": "toolbench.bench_tools.dunderkit"}, {"python": "toolbench.bench_tools.dunderkit"}])

    def test_toolbase_stub_raises(self):
        with self.assertRaises(RuntimeError):
            self._names([{"toolbase": {"toolsets": {"euclid": {}}}}])

    def test_bad_select_errors(self):
        with self.assertRaises(ValueError):
            self._names([{"python": "toolbench.bench_tools.dunderkit", "select": ["nope"]}])

    def test_builtin_core_supplies_nothing(self):
        h = Harness.from_dict({"runtime": {"name": "claude_code"}, "provider": {"name": "anthropic"},
                               "core": {"builtin": True}}, id="claude_code")
        lo = Loadout.from_dict({"tools": {"sources": []}}, name="x")
        with tempfile.TemporaryDirectory() as sb:
            tools, report = self.tr.build_agent_tools(h, lo, sb)
        self.assertEqual(report["core"]["tools"], [])
        self.assertEqual(tools, [])


if __name__ == "__main__":
    unittest.main()
