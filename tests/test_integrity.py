"""Benchmark-integrity scan: flags trials that reached the ground-truth answer
key, and the summary quarantines (zeroes + alerts on) them."""

import gzip
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from toolbench.core.integrity import (scan_run, scan_transcript,
                                      sensitive_markers)
from toolbench.reporting.summary_text import _render_integrity

_MANIFEST = {"benchmark_config":
             {"ground_truth": {"dir": "/x/benchmarks/demo/soln"}}}


def _transcript(path: Path, commands):
    with gzip.open(path, "wt") as f:
        for name, args in commands:
            f.write(json.dumps({"type": "tool_call", "name": name,
                                "args": args}) + "\n")


class TestIntegrity(unittest.TestCase):
    def test_markers_include_ground_truth_dir_and_basename(self):
        m = sensitive_markers(_MANIFEST)
        self.assertIn("truth.json", m)
        self.assertIn("/x/benchmarks/demo/soln", m)
        self.assertIn("soln/", m)

    def test_provided_inputs_not_flagged(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl.gz"
            _transcript(p, [
                ("Bash", {"command": "cat production/kappa.csv"}),
                ("Read", {"file_path": "spectra/spectrum_K_m0.230.csv"}),
                ("Write", {"file_path": "results/answer.json"}),
            ])
            self.assertEqual(scan_transcript(p, sensitive_markers(_MANIFEST)), [])

    def test_answer_key_access_flagged(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl.gz"
            _transcript(p, [
                ("Bash", {"command": "cat ../../../benchmarks/demo/soln/truth.json"}),
            ])
            hits = scan_transcript(p, sensitive_markers(_MANIFEST))
            self.assertTrue(hits)
            self.assertEqual(hits[0]["tool"], "Bash")

    def test_scan_run_flags_only_offending_trial(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            for tid, cmd in (("clean", "ls pythia_cards/"),
                             ("leaky", "python -c 'open(\"/x/benchmarks/demo/"
                                       "soln/truth.json\")'")):
                td = root / "trials" / tid
                td.mkdir(parents=True)
                _transcript(td / "transcript.jsonl.gz",
                            [("Bash", {"command": cmd})])
            trials = [{"trial_id": "clean"}, {"trial_id": "leaky"}]
            flagged = scan_run(root, trials, _MANIFEST)
            self.assertEqual(set(flagged), {"leaky"})

    def test_render_integrity_section(self):
        summary = {"integrity": {"scanned": 3, "flagged": {
            "leaky": {"n_hits": 1,
                      "sample": [{"tool": "Bash", "marker": "truth.json",
                                  "snippet": "cat .../soln/truth.json"}]}}}}
        block = "\n".join(_render_integrity(summary))
        self.assertIn("INTEGRITY ALERT", block)
        self.assertIn("leaky", block)
        # a clean run renders no section
        self.assertEqual(_render_integrity({"integrity": {"scanned": 3,
                                                          "flagged": {}}}), [])


if __name__ == "__main__":
    unittest.main()
