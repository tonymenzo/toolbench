"""The toolbase API toolbench actually calls, checked against toolbase itself.

Every other toolbase test here runs against a fake module injected into
`sys.modules`, because toolbase is an optional dependency and the suite
has to pass without it. That is the right default and it has one failure
mode: the fake encodes the signature toolbench *believes* toolbase has,
so when toolbase changed the two drifted and the suite stayed green over
a completely broken integration.

That is what happened at toolbase 0.12, which renamed profiles to
loadouts: `toolbase_tools(profile=...)` became `loadout=`, and
`toolbase serve --profile` became `--loadout`. Both in-process and
MCP-subprocess tool resolution raised on every call, and no test noticed.

So these assert against the real toolbase when it is installed, and skip
when it is not. A skipped test proves nothing, but a failing one names
the drift the moment it appears, which is all that was missing.
"""

import inspect
import subprocess
import unittest

try:
    from toolbase.connect.orchestral import toolbase_tools
    HAVE_TOOLBASE = True
except Exception:
    HAVE_TOOLBASE = False


@unittest.skipUnless(HAVE_TOOLBASE, "toolbase not installed")
class TestToolbaseToolsSignature(unittest.TestCase):
    """`resolve_toolbase_source` builds kwargs by name; a rename is a
    TypeError at the call, not something a type checker would catch."""

    def setUp(self):
        self.params = inspect.signature(toolbase_tools).parameters

    def test_accepts_the_kwargs_the_resolver_passes(self):
        for kw in ("loadout", "project_root", "quiet"):
            with self.subTest(kwarg=kw):
                self.assertIn(
                    kw, self.params,
                    f"toolbase_tools no longer accepts {kw!r}; "
                    "toolbench/core/tool_resolver.py passes it by name",
                )

    def test_the_renamed_kwarg_is_really_gone(self):
        """Guards the other direction: if toolbase ever restored
        `profile`, passing `loadout` might silently do nothing."""
        self.assertNotIn("profile", self.params)

    def test_optional_kwargs_are_feature_detected_not_assumed(self):
        """`config_overrides` and `report` are probed with
        `inspect.signature` before use, so their absence degrades rather
        than raising. This records that they exist today -- if one
        disappears the resolver still works, just more quietly."""
        for kw in ("config_overrides", "report"):
            with self.subTest(kwarg=kw):
                self.assertIn(kw, self.params)


@unittest.skipUnless(HAVE_TOOLBASE, "toolbase not installed")
class TestServeCliContract(unittest.TestCase):
    """The CLI runtimes (claude_code, codex) spawn `toolbase serve` with
    an argv toolbench builds, so a renamed flag breaks them the same way
    a renamed kwarg breaks the in-process path -- and neither the Python
    API test above nor a mocked runtime would see it."""

    def _help(self):
        return subprocess.run(
            ["toolbase", "serve", "--help"],
            capture_output=True, text=True, timeout=60,
        ).stdout

    def test_serve_accepts_the_flags_toolbench_passes(self):
        help_text = self._help()
        for flag in ("--loadout", "--call-timeout"):
            with self.subTest(flag=flag):
                self.assertIn(flag, help_text)

    def test_the_renamed_flag_is_really_gone(self):
        self.assertNotIn("--profile", self._help())


@unittest.skipUnless(HAVE_TOOLBASE, "toolbase not installed")
class TestDiscoveryContract(unittest.TestCase):
    """`toolbase_provenance` reads discovery directly to record which
    build of each toolkit served a trial -- the reproducibility claim a
    benchmark rests on. It swallows exceptions so provenance can never
    tank a run, which also means a rename here degrades silently to
    ``"unknown"`` rather than failing."""

    def test_discovery_still_exposes_what_provenance_reads(self):
        from toolbase.serve.orchestrator import discover_toolkits
        for d in discover_toolkits():
            self.assertTrue(hasattr(d, "name"))
            self.assertTrue(hasattr(d, "path"))    # path.name is the version
            self.assertTrue(hasattr(d, "meta"))    # meta["environment"]
            break

    def test_naming_rule_is_importable(self):
        """The `python:` bridge mirrors toolbase's wire names through
        `mcp_tool_name` rather than reimplementing the rule."""
        from toolbase.naming import mcp_tool_name
        self.assertTrue(callable(mcp_tool_name))


if __name__ == "__main__":
    unittest.main()
