"""The toolbase-source sandbox-scoping contract.

A toolbase host runs in its own subprocess with its own resolved
base_directory, so the resolver MUST pass the trial sandbox through
``toolbase_tools(config_overrides=...)`` — otherwise the agent's
toolkit tools and harness-core tools operate on different trees and
every sandbox-relative path silently misresolves (found in the first
live gpt-oss shakedown). Tested against a fake toolbase module so no
toolbase install is needed.
"""

import contextlib
import io
import sys
import tempfile
import types
import unittest

from toolbench.core.loadout import Source
from toolbench.core.tool_resolver import resolve_toolbase_source


def _tool(name):
    class _T:
        def get_name(self):
            return name
    return _T()


def _install_fake_toolbase(captured: dict, *, with_overrides: bool):
    """Inject fake toolbase.connect.orchestral with/without the
    config_overrides parameter in toolbase_tools' signature."""
    if with_overrides:
        @contextlib.contextmanager
        def toolbase_tools(*, profile=None, project_root=None, quiet=False,
                           config_overrides=None):
            captured.update(profile=profile, config_overrides=config_overrides)
            yield [_tool("kit__alpha"), _tool("kit__beta")]
    else:
        @contextlib.contextmanager
        def toolbase_tools(*, profile=None, project_root=None, quiet=False):
            captured.update(profile=profile, config_overrides="UNSUPPORTED")
            yield [_tool("kit__alpha")]

    tb = types.ModuleType("toolbase")
    connect = types.ModuleType("toolbase.connect")
    orch = types.ModuleType("toolbase.connect.orchestral")
    orch.toolbase_tools = toolbase_tools
    tb.connect = connect
    connect.orchestral = orch
    saved = {n: sys.modules.get(n) for n in
             ("toolbase", "toolbase.connect", "toolbase.connect.orchestral")}
    sys.modules.update({"toolbase": tb, "toolbase.connect": connect,
                        "toolbase.connect.orchestral": orch})
    return saved


def _restore(saved):
    for name, mod in saved.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


class TestSandboxScoping(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = self._tmp.name
        self.src = Source(backend="toolbase",
                          config={"profile": "p"}, options={})

    def tearDown(self):
        self._tmp.cleanup()
        from toolbench.core.tool_resolver import release_sources
        release_sources(self.sandbox)

    def test_sandbox_passed_as_base_directory_override(self):
        captured: dict = {}
        saved = _install_fake_toolbase(captured, with_overrides=True)
        try:
            tools = resolve_toolbase_source(self.src, self.sandbox)
        finally:
            _restore(saved)
        self.assertEqual(captured["config_overrides"],
                         {"base_directory": self.sandbox})
        self.assertEqual(len(tools), 2)

    def test_old_toolbase_warns_instead_of_crashing(self):
        captured: dict = {}
        saved = _install_fake_toolbase(captured, with_overrides=False)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                tools = resolve_toolbase_source(self.src, self.sandbox)
        finally:
            _restore(saved)
        self.assertEqual(captured["config_overrides"], "UNSUPPORTED")
        self.assertEqual(len(tools), 1)                      # still serves
        self.assertIn("predates config_overrides", buf.getvalue())
        self.assertIn("NOT be scoped", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
