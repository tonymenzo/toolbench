"""Resolver tests: harness core + loadout python/toolbase sources.

Requires orchestral (for `define_tool` tools); the example tools come from
the geometry benchmark's `tools/` dir, referenced by path. Skipped otherwise.
"""

import tempfile
import unittest
from pathlib import Path

from tests.helpers import GEOMETRY_DIR

try:
    import orchestral  # noqa: F401
    HAVE_DEPS = True
except Exception:
    HAVE_DEPS = False

from toolbench.core.harness import Harness
from toolbench.core.loadout import Loadout


def _tool(name: str) -> str:
    """Absolute path to a geometry example tool module."""
    return str(GEOMETRY_DIR / "tools" / f"{name}.py")


@unittest.skipUnless(HAVE_DEPS, "orchestral not importable")
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
        n, _ = self._names([{"python": _tool("dunderkit")}, {"python": _tool("euclid")}])
        for t in ("add", "subtract", "multiply", "divide", "power", "euclidean_distance"):
            self.assertIn(t, n)

    def test_select_bundle(self):
        n, _ = self._names([{"python": _tool("dunderkit"), "select": ["additive"]}])
        self.assertIn("add", n)
        self.assertIn("subtract", n)
        self.assertNotIn("power", n)
        self.assertNotIn("divide", n)

    def test_all_metrics(self):
        n, _ = self._names([{"python": _tool("dunderkit")}, {"python": _tool("euclid")},
                            {"python": _tool("taxicab")}, {"python": _tool("chebyshev")}])
        for t in ("euclidean_distance", "manhattan_distance", "chebyshev_distance"):
            self.assertIn(t, n)

    def test_collision_errors(self):
        with self.assertRaises(ValueError):
            self._names([{"python": _tool("dunderkit")}, {"python": _tool("dunderkit")}])

    def test_toolbase_stub_raises(self):
        with self.assertRaises(RuntimeError):
            self._names([{"toolbase": {"toolsets": {"euclid": {}}}}])

    def test_bad_select_errors(self):
        with self.assertRaises(ValueError):
            self._names([{"python": _tool("dunderkit"), "select": ["nope"]}])

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


@unittest.skipUnless(HAVE_DEPS, "orchestral not importable")
class TestSkillsResolveWithTools(unittest.TestCase):
    """Skills resolve where tools do, so they reach the preview.

    `build_agent_tools` is called twice: once by the CLI against a temp dir,
    which is what fills `manifest["resolution"]` and fails a mis-wired arm
    before any trial runs, and once per trial against its sandbox. A skill
    resolved anywhere else is absent from the first, so the manifest would
    describe only half the arm and a bad reference would surface twenty
    minutes into a run.
    """

    def setUp(self):
        from toolbench.core import tool_resolver
        self.tr = tool_resolver
        self.h = Harness.from_dict(
            {"runtime": {"name": "orchestral"},
             "provider": {"name": "anthropic"},
             "core": {"tools": ["RunPythonTool"]}}, id="orchestral/anthropic")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.skill = Path(self._tmp.name) / "recipe.md"
        self.skill.write_text("---\nname: recipe\ndescription: d\n---\nbody\n")

    def _report(self, skills):
        lo = Loadout.from_dict(
            {"tools": {"sources": []}, "skills": skills}, name="arm")
        with tempfile.TemporaryDirectory() as sb:
            _tools, report = self.tr.build_agent_tools(self.h, lo, sb)
        return report

    def test_report_carries_resolved_skills(self):
        report = self._report([{"name": "recipe", "file": str(self.skill)}])
        self.assertEqual([r["name"] for r in report["skills"]], ["recipe"])
        self.assertEqual(report["skills"][0]["source"], "benchmark")

    def test_no_skills_is_an_empty_list_not_a_missing_key(self):
        """A consumer reading the manifest should not have to guess."""
        self.assertEqual(self._report([])["skills"], [])

    def test_a_bad_reference_fails_at_resolution(self):
        """Which is the preview, not twenty minutes into the run."""
        with self.assertRaises(FileNotFoundError):
            self._report([{"name": "gone", "file": "/nope/missing.md"}])
