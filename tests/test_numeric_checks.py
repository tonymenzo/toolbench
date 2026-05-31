import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

from toolbench.core.checks import run_check  # noqa: E402


def run_numeric_check(spec: dict, sandbox) -> tuple[bool, str]:
    """Test adapter: route a legacy `{kind, **params}` fixture through the
    canonical `run_check(name, sandbox, params)` registry entry point."""
    spec = dict(spec)
    name = spec.pop("kind")
    return run_check(name, sandbox, spec)


class _TmpSandbox(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.sandbox = Path(self._td.name)
        (self.sandbox / "analysis").mkdir()

    def tearDown(self):
        self._td.cleanup()

    def write_npy(self, rel: str, arr: np.ndarray) -> Path:
        p = self.sandbox / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        np.save(p, arr, allow_pickle=False)
        return p


class TestPeakPosition(_TmpSandbox):
    """`peak_position` numeric_check."""

    SPEC = {
        "kind": "peak_position",
        "expected_masses": [1000, 1500, 2000],
        "tolerance_frac": 0.10,
        "min_events_per_peak": 50,
    }

    def _good(self, mass: float, n: int = 1000, sigma: float = 50) -> np.ndarray:
        """A clean Gaussian peak near `mass`."""
        return np.random.default_rng(seed=int(mass)).normal(mass, sigma, n)

    def test_three_npys_one_per_scan_point(self):
        # The expected layout: one .npy per scan point, peak at input mass.
        self.write_npy("analysis/lq_1000.npy", self._good(1000))
        self.write_npy("analysis/lq_1500.npy", self._good(1500))
        self.write_npy("analysis/lq_2000.npy", self._good(2000))
        ok, msg = run_numeric_check(self.SPEC, self.sandbox)
        self.assertTrue(ok, msg)
        self.assertIn("peaks within", msg)

    def test_no_npy_files_at_all(self):
        # Faked-PDF case: no .npy → can't measure any peak → fail.
        ok, msg = run_numeric_check(self.SPEC, self.sandbox)
        self.assertFalse(ok)
        self.assertIn("no .npy", msg)

    def test_wrong_pairing_peaks_shifted_low(self):
        # gpt-oss-120b n000/n002 pattern: peaks at ~700/~1100/~1500
        # (well outside ±10% of input).
        self.write_npy("analysis/lq_p1.npy", self._good(700))
        self.write_npy("analysis/lq_p2.npy", self._good(1100))
        self.write_npy("analysis/lq_p3.npy", self._good(1500))
        ok, msg = run_numeric_check(self.SPEC, self.sandbox)
        self.assertFalse(ok)
        self.assertIn("missing peaks", msg)
        # 1500 IS in tolerance for the 1500 scan point — but 700/1100
        # are not. So we should report exactly 2 missing.
        self.assertIn("[1000.0, 2000.0]", msg)

    def test_wrong_channel_dilepton_mass(self):
        # bench-test pattern: m(l,l) instead of m(l,j).
        # All masses pile up around the dilepton invariant mass with
        # a long tail; nothing peaks at the LQ scan masses.
        all_evts = np.random.default_rng(42).exponential(scale=400, size=3000)
        self.write_npy("analysis/lepton_pair_masses.npy", all_evts)
        ok, _ = run_numeric_check(self.SPEC, self.sandbox)
        self.assertFalse(ok)

    def test_stats_starved(self):
        # Each .npy has too few events to clear min_events_per_peak.
        for m in (1000, 1500, 2000):
            self.write_npy(f"analysis/lq_{m}.npy", self._good(m, n=20))
        ok, msg = run_numeric_check(self.SPEC, self.sandbox)
        self.assertFalse(ok)
        # Could be "no .npy with ≥50" or "missing peaks" depending on
        # which path matches first; either is acceptable.
        self.assertTrue("no .npy" in msg or "missing peaks" in msg, msg)

    def test_real_reconstruction_with_combinatoric_pile_up(self):
        # n003/n004/n008 pattern: a real signal peak at the right mass
        # plus a large combinatoric pile-up at low mass. The signal
        # peak should still be findable.
        rng = np.random.default_rng(0)
        for m in (1000, 1500, 2000):
            signal     = rng.normal(m, 80, 600)        # the LQ peak
            background = rng.exponential(150, 1500)    # comb. pile-up at low mass
            self.write_npy(f"analysis/lq_{m}.npy", np.concatenate([signal, background]))
        ok, msg = run_numeric_check(self.SPEC, self.sandbox)
        self.assertTrue(ok, msg)

    def test_combined_npy_with_only_one_peak(self):
        # If the agent saves all events into one .npy and only one
        # scan point's signal is detectable, only one peak is found
        # and the strict check (min_peaks=3) fails.
        self.write_npy("analysis/all_masses.npy", self._good(1000))
        ok, _ = run_numeric_check(self.SPEC, self.sandbox)
        self.assertFalse(ok)

    def test_min_peaks_partial_credit(self):
        # With min_peaks=1, the same single-peak case should PASS —
        # the agent got at least one resonance correct.
        self.write_npy("analysis/all_masses.npy", self._good(1000))
        spec = dict(self.SPEC, min_peaks=1)
        ok, msg = run_numeric_check(spec, self.sandbox)
        self.assertTrue(ok, msg)
        self.assertIn("1/3 peaks", msg)

    def test_min_peaks_zero_peaks_still_fails(self):
        # Even with min_peaks=1, a wrong-channel reconstruction with
        # no correctly-positioned peak must still fail.
        all_evts = np.random.default_rng(1).exponential(scale=400, size=3000)
        self.write_npy("analysis/wrong.npy", all_evts)
        spec = dict(self.SPEC, min_peaks=1)
        ok, _ = run_numeric_check(spec, self.sandbox)
        self.assertFalse(ok)

    def test_unknown_kind(self):
        ok, msg = run_numeric_check({"kind": "no_such"}, self.sandbox)
        self.assertFalse(ok)
        self.assertIn("unknown check", msg)

    def test_empty_expected_list(self):
        ok, msg = run_numeric_check(
            {"kind": "peak_position", "expected_masses": []}, self.sandbox)
        self.assertFalse(ok)
        self.assertIn("expected_masses must be non-empty", msg)


if __name__ == "__main__":
    unittest.main()
