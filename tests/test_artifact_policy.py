"""ArtifactPolicy parsing + policy-driven sandbox cleanup."""

import tempfile
import unittest
from pathlib import Path

from toolbench.core.artifact_policy import (
    DEFAULT_KEEP_FULL, DEFAULT_POLICY, ArtifactPolicy,
)
from toolbench.core.runner import TrialRunner


class TestFromBlock(unittest.TestCase):
    def test_empty_block_is_defaults(self):
        self.assertEqual(ArtifactPolicy.from_block(None), DEFAULT_POLICY)
        self.assertEqual(ArtifactPolicy.from_block({}), DEFAULT_POLICY)

    def test_keys_replace_defaults_independently(self):
        p = ArtifactPolicy.from_block({"keep_full": [".csv"]})
        self.assertEqual(p.keep_full, (".csv",))
        self.assertEqual(p.truncate, DEFAULT_POLICY.truncate)   # untouched
        self.assertEqual(p.keep_root, DEFAULT_POLICY.keep_root)

    def test_extensions_normalized_to_leading_dot(self):
        p = ArtifactPolicy.from_block({"keep_full": ["csv", ".tsv"]})
        self.assertEqual(p.keep_full, (".csv", ".tsv"))

    def test_truncate_entries(self):
        p = ArtifactPolicy.from_block(
            {"truncate": [{"ext": "jsonl", "max_records": 50}]})
        self.assertEqual(p.truncate, ((".jsonl", 50),))

    def test_unknown_key_errors(self):
        with self.assertRaises(ValueError):
            ArtifactPolicy.from_block({"keepfull": [".csv"]})

    def test_malformed_truncate_errors(self):
        with self.assertRaises(ValueError):
            ArtifactPolicy.from_block({"truncate": [{"ext": ".jsonl"}]})

    def test_non_list_errors(self):
        with self.assertRaises(ValueError):
            ArtifactPolicy.from_block({"keep_full": ".csv"})


class TestCleanupHonorsPolicy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.sandbox = root / "sandbox"
        self.trial = root / "trial"
        self.trial.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel, data=b"x"):
        p = self.sandbox / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def test_custom_keep_full_preserves_nondefault_extension(self):
        # .csv is NOT in the default keep list — a benchmark whose
        # deliverable is a CSV must be able to declare it.
        assert ".csv" not in DEFAULT_KEEP_FULL
        self._write("results/table.csv", b"a,b\n1,2\n")
        self._write("results/scratch.tmp", b"junk")
        policy = ArtifactPolicy.from_block({"keep_full": [".csv"]})
        TrialRunner._cleanup_sandbox(self.sandbox, self.trial,
                                     tool_calls=[], policy=policy)
        art = self.trial / "artifacts"
        self.assertTrue((art / "results" / "table.csv").exists())
        self.assertFalse((art / "results" / "scratch.tmp").exists())
        self.assertFalse(self.sandbox.exists())

    def test_default_policy_used_when_none(self):
        self._write("output/answer.json", b"{}")
        self._write("results/table.csv", b"a,b\n")
        TrialRunner._cleanup_sandbox(self.sandbox, self.trial, tool_calls=[])
        art = self.trial / "artifacts"
        self.assertTrue((art / "output" / "answer.json").exists())   # default kept
        self.assertFalse((art / "results" / "table.csv").exists())   # not in defaults


if __name__ == "__main__":
    unittest.main()
