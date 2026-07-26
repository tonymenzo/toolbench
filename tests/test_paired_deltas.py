"""Paired per-seed condition deltas (`_paired_deltas` / aggregate()).

The headline ablation numbers: Δreach / Δpass between two conditions of
the same model, paired over shared seeds so per-seed noise cancels.
"""

import unittest

from toolbench.cli import _paired_deltas, aggregate


STAGE_ORDER = ["s0", "s1"]
WEIGHTS = {"s0": 0.4, "s1": 0.6}


def _row(model, cond, seed, s0, s1, **extra):
    stages = {"s0": bool(s0), "s1": bool(s1)}
    base = {"model": model, "condition": cond, "seed": seed,
            "stages": stages, "score": 0.0, "cost_usd": None,
            "wall_clock_s": 1.0, "failure_mode": "NONE"}
    base.update(extra)
    return base


class TestPairedDeltas(unittest.TestCase):
    def _rows(self):
        rows = []
        for seed in (1, 2, 3, 4):
            rows.append(_row("m", "A", seed, s0=1, s1=0))    # reach 0.4, fail
        for seed in (1, 2, 3):
            rows.append(_row("m", "B", seed, s0=1, s1=1))    # reach 1.0, pass
        rows.append(_row("m", "B", 4, s0=1, s1=0))           # reach 0.4, fail
        return rows

    def test_delta_direction_and_values(self):
        out = _paired_deltas(self._rows(), STAGE_ORDER, WEIGHTS)
        self.assertEqual(len(out), 1)
        d = out[0]
        # B − A in first-appearance (CLI) order.
        self.assertEqual((d["condition_a"], d["condition_b"]), ("A", "B"))
        self.assertEqual(d["n_pairs"], 4)
        # Per-seed reach deltas: 0.6, 0.6, 0.6, 0.0 → mean 0.45.
        self.assertAlmostEqual(d["reach_delta"], 0.45)
        # Per-seed pass deltas: 1, 1, 1, 0 → mean 0.75.
        self.assertAlmostEqual(d["pass_delta"], 0.75)
        lo, hi = d["reach_delta_ci95"]
        self.assertLessEqual(lo, 0.45)
        self.assertGreaterEqual(hi, 0.45)

    def test_no_shared_seeds_no_entry(self):
        rows = [_row("m", "A", 1, 1, 0), _row("m", "B", 2, 1, 1)]
        self.assertEqual(_paired_deltas(rows, STAGE_ORDER, WEIGHTS), [])

    def test_models_not_crossed(self):
        rows = [_row("m1", "A", 1, 1, 0), _row("m2", "B", 1, 1, 1)]
        self.assertEqual(_paired_deltas(rows, STAGE_ORDER, WEIGHTS), [])

    def test_single_shared_seed_has_no_ci(self):
        rows = [_row("m", "A", 1, 1, 0), _row("m", "B", 1, 1, 1)]
        out = _paired_deltas(rows, STAGE_ORDER, WEIGHTS)
        self.assertEqual(out[0]["n_pairs"], 1)
        self.assertIsNone(out[0]["reach_delta_ci95"])

    def test_three_conditions_all_pairs(self):
        rows = []
        for cond in ("A", "B", "C"):
            for seed in (1, 2):
                rows.append(_row("m", cond, seed, 1, cond != "A"))
        out = _paired_deltas(rows, STAGE_ORDER, WEIGHTS)
        pairs = {(d["condition_a"], d["condition_b"]) for d in out}
        self.assertEqual(pairs, {("A", "B"), ("A", "C"), ("B", "C")})

    def test_aggregate_includes_paired_deltas(self):
        summary = aggregate(self._rows(), k=4, stage_order=STAGE_ORDER,
                            stage_weights=WEIGHTS)
        self.assertIn("paired_deltas", summary)
        self.assertEqual(len(summary["paired_deltas"]), 1)
        self.assertAlmostEqual(summary["paired_deltas"][0]["reach_delta"], 0.45)


class TestExcludedFromMetrics(unittest.TestCase):
    """SESSION_LIMIT trials are dropped from the scored population (n / reach /
    pass / paired deltas) but surfaced as an explicit excluded count, so a
    subscription-quota termination never pollutes the metrics as a score-0."""

    def _rows(self):
        from toolbench.core.failure_modes import SESSION_LIMIT
        rows = []
        # cell (m, A): 2 genuine passes (reach 1.0) + 2 quota terminations.
        for seed in (1, 2):
            rows.append(_row("m", "A", seed, 1, 1))
        for seed in (3, 4):
            rows.append(_row("m", "A", seed, 0, 0,
                             failure_mode=SESSION_LIMIT, stages={}, score=0.0))
        # cell (m, B): 2 genuine trials sharing seeds 1,2 with A.
        for seed in (1, 2):
            rows.append(_row("m", "B", seed, 1, 0))
        return rows

    def test_excluded_from_scored_population(self):
        summary = aggregate(self._rows(), k=4, stage_order=STAGE_ORDER,
                            stage_weights=WEIGHTS)
        self.assertEqual(summary["n_total_trials"], 4)      # 2 A + 2 B scored
        self.assertEqual(summary["n_excluded_trials"], 2)   # 2 quota rows
        cell_a = next(c for c in summary["cells"] if c["condition"] == "A")
        self.assertEqual(cell_a["n"], 2)
        self.assertEqual(cell_a["n_excluded"], 2)
        # Reach reflects ONLY the 2 genuine passes, not diluted to 0.5 by the
        # 2 excluded score-0 quota rows.
        self.assertAlmostEqual(cell_a["reach_bar_k"], 1.0)

    def test_excluded_not_in_paired_deltas(self):
        # The A−B delta pairs only scored seeds (1, 2); A's excluded quota
        # rows on seeds 3, 4 must not create phantom pairs or shift the delta.
        summary = aggregate(self._rows(), k=4, stage_order=STAGE_ORDER,
                            stage_weights=WEIGHTS)
        self.assertEqual(len(summary["paired_deltas"]), 1)
        self.assertEqual(summary["paired_deltas"][0]["n_pairs"], 2)


if __name__ == "__main__":
    unittest.main()
