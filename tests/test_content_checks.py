import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

from toolbench.core.checks import run_check  # noqa: E402
from toolbench.core.content_checks import UFO_REQUIRED  # noqa: E402


def run_content_check(spec: dict, sandbox) -> tuple[bool, str]:
    """Test adapter: route a legacy `{kind, **params}` fixture through the
    canonical `run_check(name, sandbox, params)` registry entry point."""
    spec = dict(spec)
    name = spec.pop("kind")
    return run_check(name, sandbox, spec)


class _TmpSandbox(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.sandbox = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def write(self, rel: str, content: bytes | str) -> Path:
        p = self.sandbox / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            p.write_text(content)
        else:
            p.write_bytes(content)
        return p


class TestUfoDir(_TmpSandbox):
    def test_passes_when_canonical_modules_present(self):
        # Agent picks an arbitrary output dir name.
        d = self.sandbox / "feynrules" / "S1_LQ_RR_UFO_v2"
        d.mkdir(parents=True)
        for f in UFO_REQUIRED:
            (d / f).write_text("# stub")
        ok, msg = run_content_check(
            {"kind": "ufo_dir", "under_subpath": "feynrules"}, self.sandbox)
        self.assertTrue(ok, msg)
        self.assertIn("UFO dir", msg)

    def test_fails_when_one_module_missing(self):
        d = self.sandbox / "feynrules" / "broken"
        d.mkdir(parents=True)
        for f in UFO_REQUIRED - {"vertices.py"}:
            (d / f).write_text("# stub")
        ok, _ = run_content_check(
            {"kind": "ufo_dir", "under_subpath": "feynrules"}, self.sandbox)
        self.assertFalse(ok)

    def test_finds_dir_anywhere_when_unscoped(self):
        d = self.sandbox / "some" / "weird" / "place"
        d.mkdir(parents=True)
        for f in UFO_REQUIRED:
            (d / f).write_text("# stub")
        ok, _ = run_content_check({"kind": "ufo_dir"}, self.sandbox)
        self.assertTrue(ok)


class TestLhe(_TmpSandbox):
    def _lhe_with_n_events(self, n: int) -> str:
        body = "<LesHouchesEvents>\n<header></header>\n"
        body += "\n".join(["<event>\n  data\n</event>"] * n)
        body += "\n</LesHouchesEvents>\n"
        return body

    def test_passes_with_enough_events(self):
        self.write("madgraph/Events/run_01/unweighted_events.lhe",
                   self._lhe_with_n_events(15))
        ok, msg = run_content_check(
            {"kind": "lhe_with_events", "min_events": 10}, self.sandbox)
        self.assertTrue(ok, msg)

    def test_fails_below_threshold(self):
        self.write("madgraph/foo.lhe", self._lhe_with_n_events(3))
        ok, _ = run_content_check(
            {"kind": "lhe_with_events", "min_events": 10}, self.sandbox)
        self.assertFalse(ok)

    def test_handles_gzip(self):
        path = self.sandbox / "madgraph" / "x.lhe.gz"
        path.parent.mkdir(parents=True)
        with gzip.open(path, "wt") as f:
            f.write(self._lhe_with_n_events(20))
        ok, _ = run_content_check(
            {"kind": "lhe_with_events", "min_events": 10}, self.sandbox)
        self.assertTrue(ok)

    def test_passes_when_naming_drifts(self):
        # Agent chose a non-default LHE filename.
        self.write("scratch/some_run_42/events_pp_lqlq.lhe",
                   self._lhe_with_n_events(50))
        ok, _ = run_content_check(
            {"kind": "lhe_with_events", "min_events": 10}, self.sandbox)
        self.assertTrue(ok)


class TestJsonlWithKeys(_TmpSandbox):
    def _events_jsonl(self, n: int) -> str:
        return "\n".join(
            json.dumps({"n": 4, "particles": [{"id": 11}] * 4}) for _ in range(n)
        )

    def _jets_jsonl(self, n: int) -> str:
        return "\n".join(
            json.dumps({"algorithm": "antikt", "R": 0.4, "jets": []})
            for _ in range(n)
        )

    def test_pythia_schema_match(self):
        # Agent saved Pythia output under an unexpected name.
        self.write("data/showered_run3.jsonl", self._events_jsonl(150))
        ok, msg = run_content_check(
            {"kind": "jsonl_with_keys",
             "required_keys": ["n", "particles"],
             "min_records": 100},
            self.sandbox,
        )
        self.assertTrue(ok, msg)

    def test_jets_schema_match(self):
        self.write("scratch/clustered.jsonl", self._jets_jsonl(150))
        ok, _ = run_content_check(
            {"kind": "jsonl_with_keys",
             "required_keys": ["algorithm", "jets"],
             "min_records": 100},
            self.sandbox,
        )
        self.assertTrue(ok)

    def test_below_min_records(self):
        self.write("data/events.jsonl", self._events_jsonl(5))
        ok, _ = run_content_check(
            {"kind": "jsonl_with_keys",
             "required_keys": ["n", "particles"],
             "min_records": 100},
            self.sandbox,
        )
        self.assertFalse(ok)

    def test_wrong_keys(self):
        self.write("data/events.jsonl",
                   "\n".join(json.dumps({"foo": 1, "bar": 2}) for _ in range(200)))
        ok, _ = run_content_check(
            {"kind": "jsonl_with_keys",
             "required_keys": ["n", "particles"],
             "min_records": 100},
            self.sandbox,
        )
        self.assertFalse(ok)

    def test_empty_required_keys_rejected(self):
        self.write("data/x.jsonl", "{}\n")
        ok, msg = run_content_check(
            {"kind": "jsonl_with_keys", "required_keys": []}, self.sandbox)
        self.assertFalse(ok)
        self.assertIn("required_keys", msg)


class TestNpyArray(_TmpSandbox):
    def test_passes_for_1d_float(self):
        (self.sandbox / "analysis").mkdir()
        np.save(self.sandbox / "analysis" / "any_name.npy",
                np.linspace(0, 100, 200, dtype=np.float64),
                allow_pickle=False)
        ok, msg = run_content_check(
            {"kind": "npy_array", "ndim": 1, "dtype_kind": "f", "min_len": 50},
            self.sandbox,
        )
        self.assertTrue(ok, msg)

    def test_rejects_wrong_ndim(self):
        (self.sandbox / "a").mkdir()
        np.save(self.sandbox / "a" / "x.npy",
                np.zeros((10, 4), dtype=np.float64),
                allow_pickle=False)
        ok, _ = run_content_check(
            {"kind": "npy_array", "ndim": 1, "min_len": 5}, self.sandbox)
        self.assertFalse(ok)

    def test_rejects_too_short(self):
        (self.sandbox / "a").mkdir()
        np.save(self.sandbox / "a" / "x.npy",
                np.zeros(3, dtype=np.float64),
                allow_pickle=False)
        ok, _ = run_content_check(
            {"kind": "npy_array", "ndim": 1, "min_len": 50}, self.sandbox)
        self.assertFalse(ok)

    def test_rejects_int_when_float_required(self):
        (self.sandbox / "a").mkdir()
        np.save(self.sandbox / "a" / "x.npy",
                np.arange(100, dtype=np.int32),
                allow_pickle=False)
        ok, _ = run_content_check(
            {"kind": "npy_array", "ndim": 1,
             "dtype_kind": "f", "min_len": 50},
            self.sandbox,
        )
        self.assertFalse(ok)


class TestPdfNonempty(_TmpSandbox):
    # Smallest legal-ish PDF: just the magic header + body bytes.
    _PDF_HEADER = b"%PDF-1.4\n"

    def test_passes_for_real_pdf(self):
        self.write("analysis/whatever_the_agent_named_it.pdf",
                   self._PDF_HEADER + b"x" * 6000)
        ok, msg = run_content_check(
            {"kind": "pdf_nonempty",
             "under_subpath": "analysis",
             "min_bytes": 5000},
            self.sandbox,
        )
        self.assertTrue(ok, msg)

    def test_rejects_too_small(self):
        self.write("analysis/tiny.pdf", self._PDF_HEADER + b"x" * 100)
        ok, _ = run_content_check(
            {"kind": "pdf_nonempty", "min_bytes": 5000}, self.sandbox)
        self.assertFalse(ok)

    def test_rejects_renamed_non_pdf(self):
        # File large enough but missing the %PDF magic.
        self.write("analysis/fake.pdf", b"\x89PNG\r\n" + b"x" * 6000)
        ok, _ = run_content_check(
            {"kind": "pdf_nonempty", "min_bytes": 5000}, self.sandbox)
        self.assertFalse(ok)


class TestPlotNonemptyExclude(_TmpSandbox):
    _PNG = b"\x89PNG\r\n\x1a\n"

    def test_excludes_machinery_subpaths_finds_agent_plot(self):
        # MadGraph drops diagnostic plots under HTML/ and SubProcesses/;
        # the agent's real plot is at output/plots/. With exclusion the
        # check must skip the machinery and credit the deliverable.
        self.write("data/mg/mg_output/HTML/card.png", self._PNG + b"x" * 4000)
        self.write("data/mg/mg_output/SubProcesses/P1/matrix11.png",
                   self._PNG + b"x" * 4000)
        self.write("output/plots/mLQmin.png", self._PNG + b"x" * 4000)
        ok, msg = run_content_check(
            {"kind": "plot_nonempty",
             "exclude_subpaths": ["SubProcesses", "HTML"]},
            self.sandbox,
        )
        self.assertTrue(ok, msg)
        self.assertIn("output/plots/mLQmin.png", msg)

    def test_fails_when_only_machinery_plots_present(self):
        self.write("data/mg/mg_output/HTML/card.png", self._PNG + b"x" * 4000)
        ok, _ = run_content_check(
            {"kind": "plot_nonempty",
             "exclude_subpaths": ["SubProcesses", "HTML"]},
            self.sandbox,
        )
        self.assertFalse(ok)

    def test_no_exclusion_matches_machinery(self):
        # Without the param, the vacuous machinery plot still satisfies it
        # (the leniency this param exists to close).
        self.write("data/mg/mg_output/HTML/card.png", self._PNG + b"x" * 4000)
        ok, _ = run_content_check({"kind": "plot_nonempty"}, self.sandbox)
        self.assertTrue(ok)


class TestDispatcher(_TmpSandbox):
    def test_unknown_kind(self):
        ok, msg = run_content_check({"kind": "no_such_check"}, self.sandbox)
        self.assertFalse(ok)
        self.assertIn("unknown check", msg)

    def test_predicate_exception_caught(self):
        # ndim is bogus: int() conversion blows up — must not propagate.
        ok, msg = run_content_check(
            {"kind": "npy_array", "ndim": "not-an-int"}, self.sandbox)
        self.assertFalse(ok)
        # Either it raises and is caught, or returns False cleanly —
        # either way the harness must not crash.
        self.assertFalse(ok)

    def test_under_subpath_does_not_escape(self):
        # ../ should not leak the search outside the sandbox.
        d = self.sandbox / "feynrules" / "M"
        d.mkdir(parents=True)
        for f in UFO_REQUIRED:
            (d / f).write_text("# stub")
        ok, _ = run_content_check(
            {"kind": "ufo_dir", "under_subpath": "../../../"}, self.sandbox)
        # Falls back to the sandbox itself — still finds the UFO dir.
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
