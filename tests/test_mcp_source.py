"""The `mcp:` loadout source backend.

Tested through the `_MCP_CLIENT_FACTORY` seam with a fake client, so no
`mcp` package and no live server is needed: config validation, env-var
expansion, select filtering, session lifecycle via release_sources, and
secret redaction in the build report.
"""

import os
import unittest

from toolbench.core import tool_resolver as tr
from toolbench.core.harness import Harness
from toolbench.core.loadout import Loadout


def _tool(name):
    class _T:
        def get_name(self):
            return name
    return _T()


class _FakeMCPClient:
    """Mimics orchestral.mcp.MCPClient's context-manager surface."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.connected = False
        self.disconnected = False

    def __enter__(self):
        self.connected = True
        return self

    def __exit__(self, *exc):
        self.disconnected = True
        return False

    def get_orchestral_tools(self):
        return [_tool("search"), _tool("fetch_page"), _tool("summarize")]


class TestMcpSource(unittest.TestCase):
    def setUp(self):
        self.created: list[_FakeMCPClient] = []

        def factory(**kwargs):
            c = _FakeMCPClient(**kwargs)
            self.created.append(c)
            return c

        self._orig = tr._MCP_CLIENT_FACTORY
        tr._MCP_CLIENT_FACTORY = factory
        self.sandbox = "/tmp/mcp-test-sandbox"

    def tearDown(self):
        tr._MCP_CLIENT_FACTORY = self._orig
        tr.release_sources(self.sandbox)

    def _loadout(self, mcp_cfg, select=None):
        entry = {"mcp": mcp_cfg}
        if select:
            entry["select"] = select
        return Loadout.from_dict(
            {"tools": {"sources": [entry]}}, name="mcp_arm")

    def _resolve(self, mcp_cfg, select=None):
        lo = self._loadout(mcp_cfg, select)
        return tr.resolve_mcp_source(lo.sources[0], self.sandbox)

    def test_stdio_command_form(self):
        tools = self._resolve({"command": ["npx", "@x/server"],
                               "env": {"TOKEN": "abc"}})
        self.assertEqual(len(tools), 3)
        kw = self.created[0].kwargs
        self.assertEqual(kw["server_command"], ["npx", "@x/server"])
        self.assertEqual(kw["env"], {"TOKEN": "abc"})
        self.assertTrue(self.created[0].connected)

    def test_http_url_form_with_select(self):
        tools = self._resolve({"url": "https://h/mcp"}, select=["search"])
        self.assertEqual([t.get_name() for t in tools], ["search"])
        self.assertEqual(self.created[0].kwargs["url"], "https://h/mcp")

    def test_select_typo_errors(self):
        with self.assertRaises(ValueError) as ctx:
            self._resolve({"url": "https://h/mcp"}, select=["serch"])
        self.assertIn("serch", str(ctx.exception))

    def test_env_var_expansion(self):
        os.environ["_TB_TEST_TOKEN"] = "sekrit"
        try:
            self._resolve({"url": "https://h/mcp",
                           "headers": {"Authorization": "Bearer ${_TB_TEST_TOKEN}"}})
        finally:
            del os.environ["_TB_TEST_TOKEN"]
        self.assertEqual(self.created[0].kwargs["headers"],
                         {"Authorization": "Bearer sekrit"})

    def test_both_command_and_url_errors(self):
        with self.assertRaises(RuntimeError):
            self._resolve({"command": ["x"], "url": "https://h"})

    def test_neither_command_nor_url_errors(self):
        with self.assertRaises(RuntimeError):
            self._resolve({"timeout": 5})

    def test_release_sources_disconnects(self):
        self._resolve({"url": "https://h/mcp"})
        client = self.created[0]
        self.assertFalse(client.disconnected)
        tr.release_sources(self.sandbox)
        self.assertTrue(client.disconnected)

    def test_report_redacts_secrets_but_keeps_keys(self):
        harness = Harness.from_dict({
            "runtime": {"name": "orchestral"},
            "provider": {"name": "anthropic"},
            "core": {"tools": []},
        }, id="t")
        lo = self._loadout({"url": "https://h/mcp",
                            "headers": {"Authorization": "Bearer xyz"},
                            "env": {"KEY": "v"}})
        _, report = tr.build_agent_tools(harness, lo, self.sandbox)
        cfg = report["sources"][0]["config"]
        self.assertEqual(cfg["headers"], {"Authorization": "<redacted>"})
        self.assertEqual(cfg["env"], {"KEY": "<redacted>"})
        self.assertEqual(cfg["url"], "https://h/mcp")   # non-secrets visible
        self.assertNotIn("xyz", str(report))


if __name__ == "__main__":
    unittest.main()
