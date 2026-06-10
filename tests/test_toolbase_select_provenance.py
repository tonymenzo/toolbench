"""toolbase-source select: filtering and version provenance.

Both are tested without toolbase installed: `_select_namespaced` is pure
logic over namespaced tool names, and `toolbase_provenance` is exercised
against fake `toolbase.serve.orchestrator` modules injected into
sys.modules (plus the no-toolbase fallback path).
"""

import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from toolbench.core.tool_resolver import _select_namespaced, toolbase_provenance


def _tool(name):
    class _T:
        def get_name(self):
            return name
    return _T()


class TestSelectNamespaced(unittest.TestCase):
    def setUp(self):
        self.tools = [_tool("calculator__add"), _tool("calculator__subtract"),
                      _tool("heptapod__add"), _tool("heptapod__shower")]

    def test_no_select_returns_all(self):
        self.assertEqual(_select_namespaced(self.tools, None, label="t"),
                         self.tools)

    def test_full_namespaced_match(self):
        out = _select_namespaced(self.tools, ["heptapod__shower"], label="t")
        self.assertEqual([t.get_name() for t in out], ["heptapod__shower"])

    def test_bare_name_unambiguous(self):
        out = _select_namespaced(self.tools, ["subtract"], label="t")
        self.assertEqual([t.get_name() for t in out], ["calculator__subtract"])

    def test_bare_name_ambiguous_errors(self):
        # `add` is served by two toolkits — must demand the namespaced name.
        with self.assertRaises(ValueError) as ctx:
            _select_namespaced(self.tools, ["add"], label="t")
        self.assertIn("ambiguous", str(ctx.exception))
        self.assertIn("calculator__add", str(ctx.exception))

    def test_unknown_errors_with_served_list(self):
        with self.assertRaises(ValueError) as ctx:
            _select_namespaced(self.tools, ["nope"], label="t")
        self.assertIn("matches no served tool", str(ctx.exception))

    def test_order_follows_select_and_dedupes(self):
        out = _select_namespaced(
            self.tools, ["shower", "calculator__add", "shower"], label="t")
        self.assertEqual([t.get_name() for t in out],
                         ["heptapod__shower", "calculator__add"])


class TestToolbaseProvenance(unittest.TestCase):
    def _inject_fake_toolbase(self, discoveries):
        """Install fake toolbase.serve.orchestrator modules in sys.modules."""
        tb = types.ModuleType("toolbase")
        serve = types.ModuleType("toolbase.serve")
        orch = types.ModuleType("toolbase.serve.orchestrator")
        orch.discover_toolkits = lambda: discoveries
        tb.serve = serve
        serve.orchestrator = orch
        for name, mod in (("toolbase", tb), ("toolbase.serve", serve),
                          ("toolbase.serve.orchestrator", orch)):
            self._saved[name] = sys.modules.get(name)
            sys.modules[name] = mod

    def setUp(self):
        self._saved = {}

    def tearDown(self):
        for name, mod in self._saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def test_versions_from_discovery(self):
        self._inject_fake_toolbase([
            SimpleNamespace(name="calculator",
                            path=Path("/cache/calculator/1.2.0"),
                            meta={"environment": "venv"}),
            SimpleNamespace(name="unrelated",
                            path=Path("/cache/unrelated/9.9.9"),
                            meta={"environment": "conda"}),
        ])
        prov = toolbase_provenance([_tool("calculator__add")])
        self.assertEqual(prov["toolkits"],
                         {"calculator": {"version": "1.2.0",
                                         "environment": "venv"}})

    def test_unserved_toolkit_not_recorded(self):
        self._inject_fake_toolbase([])
        prov = toolbase_provenance([_tool("calculator__add")])
        # Discovery knows nothing about it → stays "unknown", never raises.
        self.assertEqual(prov["toolkits"],
                         {"calculator": {"version": "unknown"}})

    def test_no_toolbase_installed_degrades_to_unknown(self):
        # No injection: import fails (toolbase isn't in the test env).
        prov = toolbase_provenance([_tool("hep__shower"), _tool("hep__hadronize")])
        self.assertEqual(prov["toolkits"], {"hep": {"version": "unknown"}})

    def test_non_namespaced_tools_ignored(self):
        prov = toolbase_provenance([_tool("plainname")])
        self.assertEqual(prov["toolkits"], {})


if __name__ == "__main__":
    unittest.main()
