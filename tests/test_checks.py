"""Unit tests for the unified check registry (toolbench/core/checks.py)."""

import json
import tempfile
import unittest
from pathlib import Path

from toolbench.core.checks import (
    BUILTIN_CHECKS, close_to, json_with_keys, load_benchmark_checks,
    load_benchmark_roles, merged_registry, merged_roles, missing_presence,
    run_check,
)
from toolbench.benchmarks import BENCHMARKS


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sb = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, obj):
        p = self.sb / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj))
        return p


class TestMissingPresence(_Tmp):
    """The presence-gate for the continue-nudge. Must flag an absent
    deliverable, and must NOT flag a present-but-WRONG one (which would leak
    the correctness oracle)."""

    def setUp(self):
        super().setUp()
        self.b = BENCHMARKS["geometry"]()
        cm = self.b.checks_module_path()
        self.reg = merged_registry(load_benchmark_checks(cm))
        self.roles = merged_roles(load_benchmark_roles(cm))

    def _missing(self):
        return missing_presence(self.b.rubric, self.sb, registry=self.reg,
                                roles=self.roles,
                                benchmark_dir=str(self.b.BENCHMARK_DIR))

    def test_absent_deliverable_is_flagged(self):
        # No output/answer.json at all → presence check fails → would nudge.
        self.assertTrue(self._missing())

    def test_present_but_wrong_is_not_flagged(self):
        # Deliverable exists (keys present) but the VALUES are wrong. The
        # presence check passes; the correctness check (close_to) is never
        # consulted — so no nudge, no oracle leakage.
        self.write("output/answer.json", {"distance": 99.0, "midpoint": [9.0, 9.0]})
        self.assertEqual(self._missing(), "")


class TestJsonWithKeys(_Tmp):
    def test_pass(self):
        self.write("output/answer.json", {"distance": 5.0, "midpoint": [1.5, 2.0]})
        ok, _ = json_with_keys(self.sb, {"file": "output/answer.json",
                                         "required_keys": ["distance", "midpoint"]})
        self.assertTrue(ok)

    def test_missing_file(self):
        ok, msg = json_with_keys(self.sb, {"file": "nope.json", "required_keys": ["x"]})
        self.assertFalse(ok)
        self.assertIn("missing", msg)

    def test_missing_key(self):
        self.write("a.json", {"distance": 5.0})
        ok, msg = json_with_keys(self.sb, {"file": "a.json",
                                           "required_keys": ["distance", "midpoint"]})
        self.assertFalse(ok)
        self.assertIn("midpoint", msg)

    def test_bad_json(self):
        (self.sb / "b.json").write_text("{not json")
        ok, msg = json_with_keys(self.sb, {"file": "b.json", "required_keys": ["x"]})
        self.assertFalse(ok)
        self.assertIn("invalid JSON", msg)


class TestCloseTo(_Tmp):
    def _ref(self, obj):
        return str(self.write("gt.json", obj))

    def test_scalar_pass(self):
        self.write("out.json", {"d": 5.02})
        ok, _ = close_to(self.sb, {"file": "out.json", "field": "d",
                                   "reference": self._ref({"d": 5.0}), "tolerance_frac": 0.01})
        self.assertTrue(ok)

    def test_scalar_fail(self):
        self.write("out.json", {"d": 6.0})
        ok, _ = close_to(self.sb, {"file": "out.json", "field": "d",
                                   "reference": self._ref({"d": 5.0}), "tolerance_frac": 0.01})
        self.assertFalse(ok)

    def test_vector_pass(self):
        self.write("out.json", {"m": [1.5, 2.0]})
        ok, _ = close_to(self.sb, {"file": "out.json", "field": "m",
                                   "reference": self._ref({"m": [1.5, 2.0]}), "tolerance_frac": 0.01})
        self.assertTrue(ok)

    def test_vector_shape_mismatch(self):
        self.write("out.json", {"m": [1.5]})
        ok, msg = close_to(self.sb, {"file": "out.json", "field": "m",
                                     "reference": self._ref({"m": [1.5, 2.0]}), "tolerance_frac": 0.01})
        self.assertFalse(ok)
        self.assertIn("shape", msg)

    def test_zero_reference_guard(self):
        self.write("out.json", {"d": 0.0001})
        ok, _ = close_to(self.sb, {"file": "out.json", "field": "d",
                                   "reference": self._ref({"d": 0.0}), "tolerance_frac": 0.01})
        self.assertFalse(ok)  # 0.0001 > 0.01 * max(0, 1e-9)


class TestRegistry(_Tmp):
    def test_reference_resolved_against_benchmark_dir(self):
        (self.sb / "gt").mkdir()
        (self.sb / "gt" / "a.json").write_text(json.dumps({"d": 5.0}))
        sbx = self.sb / "sbx"
        sbx.mkdir()
        (sbx / "out.json").write_text(json.dumps({"d": 5.0}))
        ok, _ = run_check("close_to", sbx,
                          {"file": "out.json", "field": "d",
                           "reference": "gt/a.json", "tolerance_frac": 0.01},
                          benchmark_dir=str(self.sb))
        self.assertTrue(ok)

    def test_unknown_check(self):
        ok, msg = run_check("nope", self.sb, {})
        self.assertFalse(ok)
        self.assertIn("unknown check", msg)

    def test_legacy_kinds_present(self):
        for k in ("ufo_dir", "jsonl_with_keys", "npy_array", "plot_nonempty", "peak_position"):
            self.assertIn(k, BUILTIN_CHECKS)

    def test_merged_collision_errors(self):
        with self.assertRaises(ValueError):
            merged_registry({"close_to": lambda s, p: (True, "")})

    def test_merged_adds_local(self):
        reg = merged_registry({"my_check": lambda s, p: (True, "ok")})
        self.assertIn("my_check", reg)
        self.assertIn("close_to", reg)

    def test_load_benchmark_checks_none(self):
        self.assertEqual(load_benchmark_checks(None), {})


if __name__ == "__main__":
    unittest.main()
