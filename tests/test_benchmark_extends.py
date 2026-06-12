"""Unit tests for `extends:` — benchmark inheritance (depth-1 overlays)."""

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from toolbench.core.benchmark import YamlBenchmark
from toolbench.core.checks import run_check
from toolbench.core.harness import discover_harnesses, load_harness
from toolbench.core.loadout import discover_loadouts, load_loadout

PARENT_RUBRIC = {
    "type": "stagewise",
    "stages": [
        {"id": "answer_written", "weight": 0.5,
         "checks": [{"json_with_keys": {"file": "output/answer.json",
                                        "required_keys": ["x"]}}]},
        {"id": "value_correct", "weight": 0.5,
         "checks": [{"close_to": {"file": "output/answer.json", "field": "x",
                                  "reference": "./ground_truth/answer.json",
                                  "tolerance_frac": 0.01}}]},
    ],
}

CHILD_RUBRIC = {
    "type": "stagewise",
    "stages": [
        {"id": "shape_only", "weight": 1.0,
         "checks": [{"close_to": {"file": "output/answer.json", "field": "x",
                                  "reference": "./ground_truth/answer.json",
                                  "tolerance_frac": 0.2}}]},
    ],
}

HARNESS = {"runtime": {"name": "orchestral"},
           "provider": {"name": "anthropic"},
           "core": {"tools": ["X"]}}


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))


def _write_variant(bench: Path, name: str) -> None:
    vdir = bench / "variants" / name
    (vdir / "prompts").mkdir(parents=True, exist_ok=True)
    (vdir / "prompts" / "user.md").write_text(f"{bench.name}/{name} prompt")
    _write_yaml(vdir / "variant.yaml",
                {"user_prompt_file": "prompts/user.md"})


def _make_parent(root: Path) -> Path:
    parent = root / "parent"
    _write_yaml(parent / "benchmark.yaml", {
        "name": "parent_family",
        "version": "1.2.3",
        "description": "the self-contained family",
        "default_harness": "orchestral/anthropic",
        "default_loadout": "base",
        "default_variant": "default",
        "ground_truth": {"dir": "./ground_truth"},
        "checks": "./checks/checks.py",
        "rubric": PARENT_RUBRIC,
    })
    (parent / "ground_truth").mkdir(parents=True)
    (parent / "ground_truth" / "answer.json").write_text(json.dumps({"x": 5.0}))
    _write_yaml(parent / "harnesses" / "orchestral" / "anthropic.yaml", HARNESS)
    _write_yaml(parent / "loadouts" / "base.yaml", {})
    (parent / "checks").mkdir(parents=True)
    (parent / "checks" / "checks.py").write_text(
        "def family_check(sandbox, params):\n"
        "    return True, 'ok'\n\n"
        "CHECKS = {'family_check': family_check}\n")
    _write_variant(parent, "default")
    _write_variant(parent, "hard")
    return parent


def _make_child(root: Path, *, cfg: dict | None = None) -> Path:
    child = root / "child"
    base = {"extends": "../parent", "rubric": CHILD_RUBRIC}
    _write_yaml(child / "benchmark.yaml", {**base, **(cfg or {})})
    return child


class TestExtendsResolution(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.parent_dir = _make_parent(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_child_overrides_rubric_inherits_the_rest(self):
        child = YamlBenchmark(_make_child(self.root))
        self.assertEqual([s["id"] for s in child.rubric.stages], ["shape_only"])
        self.assertEqual(child.default_harness, "orchestral/anthropic")
        self.assertEqual(child.default_loadout, "base")
        self.assertEqual(child.default_variant, "default")

    def test_child_inherits_rubric_when_absent(self):
        bench_dir = self.root / "rubricless"
        _write_yaml(bench_dir / "benchmark.yaml", {"extends": "../parent"})
        child = YamlBenchmark(bench_dir)
        self.assertEqual([s["id"] for s in child.rubric.stages],
                         ["answer_written", "value_correct"])

    def test_identity_keys_never_inherited(self):
        child = YamlBenchmark(_make_child(self.root))
        self.assertEqual(child.name, "child")        # dir name, not parent's
        self.assertEqual(child.version, "")
        self.assertEqual(child.description, "")

    def test_ground_truth_anchors_at_declaring_layer(self):
        child = YamlBenchmark(_make_child(self.root))
        self.assertEqual(child.ground_truth_dir,
                         (self.parent_dir / "ground_truth").resolve())
        # A child override re-anchors at the child.
        override_dir = _make_child(self.root, cfg={
            "ground_truth": {"dir": "./ground_truth"}})
        (override_dir / "ground_truth").mkdir()
        child2 = YamlBenchmark(override_dir)
        self.assertEqual(child2.ground_truth_dir,
                         (override_dir / "ground_truth").resolve())

    def test_checks_module_inherited(self):
        child = YamlBenchmark(_make_child(self.root))
        self.assertEqual(child.checks_module_path(),
                         (self.parent_dir / "checks" / "checks.py").resolve())

    def test_variants_union_child_shadows(self):
        child_dir = _make_child(self.root)
        _write_variant(child_dir, "default")
        child = YamlBenchmark(child_dir)
        self.assertEqual(set(child.variants), {"default", "hard"})
        self.assertEqual(child.get_variant("default").read_user_prompt(),
                         "child/default prompt")
        self.assertEqual(child.get_variant("hard").read_user_prompt(),
                         "parent/hard prompt")

    def test_search_dirs_child_first(self):
        child = YamlBenchmark(_make_child(self.root))
        self.assertEqual(child.search_dirs,
                         [child.BENCHMARK_DIR, self.parent_dir.resolve()])
        parent = YamlBenchmark(self.parent_dir)
        self.assertEqual(parent.search_dirs, [parent.BENCHMARK_DIR])

    def test_resolved_config_records_provenance(self):
        child = YamlBenchmark(_make_child(self.root))
        cfg = child.resolved_config()
        self.assertEqual(cfg["extends"], str(self.parent_dir.resolve()))
        self.assertEqual(cfg["ground_truth"]["dir"],
                         str((self.parent_dir / "ground_truth").resolve()))
        self.assertIsNone(YamlBenchmark(self.parent_dir)
                          .resolved_config()["extends"])

    def test_depth_one_enforced(self):
        child_dir = _make_child(self.root)
        grandchild = self.root / "grandchild"
        _write_yaml(grandchild / "benchmark.yaml", {"extends": "../child"})
        with self.assertRaises(ValueError):
            YamlBenchmark(grandchild)
        # The middle layer itself still loads fine.
        YamlBenchmark(child_dir)

    def test_missing_parent_raises(self):
        bench_dir = self.root / "orphan"
        _write_yaml(bench_dir / "benchmark.yaml",
                    {"extends": "../nowhere", "rubric": CHILD_RUBRIC})
        with self.assertRaises(FileNotFoundError):
            YamlBenchmark(bench_dir)

    def test_self_extends_raises(self):
        bench_dir = self.root / "ouroboros"
        _write_yaml(bench_dir / "benchmark.yaml",
                    {"extends": ".", "rubric": CHILD_RUBRIC})
        with self.assertRaises(ValueError):
            YamlBenchmark(bench_dir)


class TestExtendsAssetDiscovery(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.parent_dir = _make_parent(self.root)
        self.child_dir = _make_child(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_harness_discovery_union_and_shadow(self):
        shadow = {**HARNESS, "provider": {"name": "openai"}}
        _write_yaml(self.child_dir / "harnesses" / "orchestral" / "anthropic.yaml",
                    shadow)
        _write_yaml(self.child_dir / "harnesses" / "claude_code.yaml",
                    {"runtime": {"name": "claude_code"},
                     "provider": {"name": "anthropic"},
                     "core": {"builtin": True}})
        dirs = YamlBenchmark(self.child_dir).search_dirs
        hs = discover_harnesses(dirs)
        self.assertEqual(set(hs), {"orchestral/anthropic", "claude_code"})
        self.assertEqual(hs["orchestral/anthropic"].provider_name, "openai")
        self.assertEqual(load_harness(dirs, "orchestral/anthropic").provider_name,
                         "openai")

    def test_loadout_discovery_union_and_fallback(self):
        _write_yaml(self.child_dir / "loadouts" / "extra.yaml", {})
        dirs = YamlBenchmark(self.child_dir).search_dirs
        self.assertEqual(set(discover_loadouts(dirs)), {"base", "extra"})
        # `base` only exists in the parent — load falls through to it.
        self.assertEqual(load_loadout(dirs, "base").name, "base")
        with self.assertRaises(FileNotFoundError):
            load_loadout(dirs, "nope")

    def test_reference_resolves_through_search_dirs(self):
        with tempfile.TemporaryDirectory() as sb:
            sandbox = Path(sb)
            (sandbox / "output").mkdir()
            (sandbox / "output" / "answer.json").write_text(
                json.dumps({"x": 5.0}))
            dirs = YamlBenchmark(self.child_dir).search_dirs
            params = CHILD_RUBRIC["stages"][0]["checks"][0]["close_to"]
            ok, msg = run_check("close_to", sandbox, params,
                                benchmark_dir=dirs)
            self.assertTrue(ok, msg)

    def test_reference_shadowed_by_child(self):
        # The child ships its own ground truth: same relative path, a
        # different value. The search path must pick the child's copy.
        (self.child_dir / "ground_truth").mkdir(parents=True)
        (self.child_dir / "ground_truth" / "answer.json").write_text(
            json.dumps({"x": 100.0}))
        with tempfile.TemporaryDirectory() as sb:
            sandbox = Path(sb)
            (sandbox / "output").mkdir()
            (sandbox / "output" / "answer.json").write_text(
                json.dumps({"x": 5.0}))
            dirs = YamlBenchmark(self.child_dir).search_dirs
            params = CHILD_RUBRIC["stages"][0]["checks"][0]["close_to"]
            ok, _ = run_check("close_to", sandbox, params, benchmark_dir=dirs)
            self.assertFalse(ok)  # graded against the child's 100.0, not 5.0


if __name__ == "__main__":
    unittest.main()
