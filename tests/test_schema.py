"""Unit tests for harness + loadout schema/discovery."""

import tempfile
import unittest
from pathlib import Path

import yaml

from toolbench.core.harness import Harness, discover_harnesses
from toolbench.core.loadout import Loadout, Source, discover_loadouts


class TestHarness(unittest.TestCase):
    def test_validate_ok(self):
        h = Harness.from_dict({"runtime": {"name": "orchestral"},
                               "provider": {"name": "anthropic"},
                               "core": {"tools": ["X"]}}, id="orchestral/anthropic")
        h.validate()
        self.assertEqual(h.provider_name, "anthropic")
        self.assertEqual(h.runtime_name, "orchestral")

    def test_core_xor_both(self):
        with self.assertRaises(ValueError):
            Harness.from_dict({"runtime": {"name": "o"}, "provider": {"name": "a"},
                               "core": {"tools": ["X"], "builtin": True}}, id="b").validate()

    def test_core_xor_neither(self):
        with self.assertRaises(ValueError):
            Harness.from_dict({"runtime": {"name": "o"}, "provider": {"name": "a"},
                               "core": {}}, id="b").validate()

    def test_builtin_core_ok(self):
        Harness.from_dict({"runtime": {"name": "claude_code"},
                           "provider": {"name": "anthropic"},
                           "core": {"builtin": True}}, id="claude_code").validate()

    def test_discover_nested_and_flat_ids(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "harnesses" / "orchestral").mkdir(parents=True)
            (d / "harnesses" / "orchestral" / "anthropic.yaml").write_text(
                yaml.safe_dump({"runtime": {"name": "orchestral"},
                                "provider": {"name": "anthropic"}, "core": {"tools": ["X"]}}))
            (d / "harnesses" / "claude_code.yaml").write_text(
                yaml.safe_dump({"runtime": {"name": "claude_code"},
                                "provider": {"name": "anthropic"}, "core": {"builtin": True}}))
            hs = discover_harnesses(d)
            self.assertEqual(set(hs), {"orchestral/anthropic", "claude_code"})


class TestLoadout(unittest.TestCase):
    def test_python_with_select(self):
        s = Source.from_entry({"python": "m", "select": ["a"]}, loadout="x")
        self.assertEqual((s.backend, s.config, s.select), ("python", "m", ["a"]))

    def test_toolbase(self):
        s = Source.from_entry({"toolbase": {"toolsets": {}}}, loadout="x")
        self.assertEqual(s.backend, "toolbase")

    def test_no_backend(self):
        with self.assertRaises(ValueError):
            Source.from_entry({"select": ["a"]}, loadout="x")

    def test_two_backends(self):
        with self.assertRaises(ValueError):
            Source.from_entry({"python": "m", "toolbase": {}}, loadout="x")

    def test_discover(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "loadouts").mkdir()
            (d / "loadouts" / "core_only.yaml").write_text(yaml.safe_dump({"tools": {"sources": []}}))
            (d / "loadouts" / "full.yaml").write_text(
                yaml.safe_dump({"tools": {"sources": [{"python": "m"}]}}))
            los = discover_loadouts(d)
            self.assertEqual(set(los), {"core_only", "full"})
            self.assertEqual(len(los["full"].sources), 1)
            self.assertEqual(len(los["core_only"].sources), 0)


if __name__ == "__main__":
    unittest.main()
