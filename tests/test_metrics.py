import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from toolbench.core.metrics import (  # noqa: E402
    reach_bar_k, bootstrap_ci, cost_usd, mean,
    pass_at_k, pass_caret_k, pearson_corr_matrix,
    per_trial_reach, reach_at_k, reach_caret_k,
    subscription_api_equivalent_cost,
)


class TestPassAtK(unittest.TestCase):
    def test_all_correct(self):
        self.assertEqual(pass_at_k(n=10, c=10, k=1), 1.0)
        self.assertEqual(pass_at_k(n=10, c=10, k=10), 1.0)

    def test_none_correct(self):
        self.assertEqual(pass_at_k(n=10, c=0, k=1), 0.0)
        self.assertEqual(pass_at_k(n=10, c=0, k=10), 0.0)

    def test_codex_examples(self):
        # n=10, c=5, k=1 -> 0.5
        self.assertAlmostEqual(pass_at_k(n=10, c=5, k=1), 0.5)
        # n=10, c=5, k=10 -> 1.0 (any draw of 10 from 10 must include a correct)
        self.assertEqual(pass_at_k(n=10, c=5, k=10), 1.0)
        # n=200, c=100, k=1 = 0.5; k=10 close to 1 but < 1
        v = pass_at_k(n=200, c=100, k=10)
        self.assertGreater(v, 0.99)
        self.assertLess(v, 1.0)

    def test_k_gt_n_raises(self):
        with self.assertRaises(ValueError):
            pass_at_k(n=3, c=1, k=4)


class TestPassCaretK(unittest.TestCase):
    def test_all_pass(self):
        # n=10, c=10, k=3 -> all C(10,3) subsets contain only successes.
        self.assertEqual(pass_caret_k(n=10, c=10, k=3), 1.0)
        self.assertEqual(pass_caret_k(n=10, c=10, k=10), 1.0)

    def test_none_pass(self):
        self.assertEqual(pass_caret_k(n=10, c=0, k=3), 0.0)
        self.assertEqual(pass_caret_k(n=10, c=2, k=3), 0.0)  # c<k

    def test_unbiased_form(self):
        # Unbiased estimator is comb(c,k)/comb(n,k), strictly different
        # from the biased plug-in (c/n)^k.
        # n=10, c=9, k=3 -> comb(9,3)/comb(10,3) = 84/120 = 0.7
        # (plug-in would give 0.9^3 = 0.729 — that bias is what the
        # latex's §4.1 argument is about; this estimator removes it.)
        self.assertAlmostEqual(pass_caret_k(n=10, c=9, k=3), 0.7, places=4)

    def test_k_eq_n(self):
        # Only one subset, so the estimator is exactly 1[c == n].
        self.assertEqual(pass_caret_k(n=10, c=9, k=10), 0.0)
        self.assertEqual(pass_caret_k(n=10, c=10, k=10), 1.0)

    def test_k_gt_n_raises(self):
        with self.assertRaises(ValueError):
            pass_caret_k(n=3, c=1, k=4)


class TestReachBarK(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(reach_bar_k([]), 0.0)
        self.assertEqual(reach_bar_k([[]]), 0.0)

    def test_all_pass(self):
        # Every stage passes in every session => reach = 1.
        self.assertEqual(reach_bar_k([[1, 1, 1], [1, 1, 1]]), 1.0)

    def test_all_fail(self):
        self.assertEqual(reach_bar_k([[0, 0, 0], [0, 0, 0]]), 0.0)

    def test_first_failure_truncates(self):
        # Pass first 2 of 4 stages, fail third => cumulative product
        # zeroes out positions 2 and 3, regardless of S[3]. Equal
        # weights => reach = 2/4.
        self.assertEqual(reach_bar_k([[1, 1, 0, 1]]), 0.5)

    def test_average_across_sessions(self):
        # Two sessions: reach(2/4) and reach(4/4) => mean 0.75.
        self.assertEqual(reach_bar_k([[1, 1, 0, 0], [1, 1, 1, 1]]), 0.75)

    def test_weights_respected(self):
        # Two stages, weights 1 and 3. Pass first only => reach = 1/4.
        self.assertEqual(reach_bar_k([[1, 0]], weights=[1.0, 3.0]), 0.25)
        # Pass both => reach = 1.0.
        self.assertEqual(reach_bar_k([[1, 1]], weights=[1.0, 3.0]), 1.0)

    def test_zero_weights(self):
        self.assertEqual(reach_bar_k([[1, 1]], weights=[0.0, 0.0]), 0.0)


class TestReachOrderStatistics(unittest.TestCase):
    """reach_at_k / reach_caret_k must collapse to pass_at_k / pass^k
    when R_j is binary (boundary case w = e_N).
    """

    def _binary_matrix(self, c: int, n: int) -> list[list[int]]:
        # Single-stage matrix with c rows of [1] and (n-c) of [0].
        # R_j = S_j directly, so per-session reach is binary.
        return [[1]] * c + [[0]] * (n - c)

    def test_collapse_to_pass_at_k(self):
        for n, c, k in [(10, 5, 3), (10, 0, 3), (10, 10, 3),
                        (8, 3, 1), (8, 3, 8), (5, 4, 2)]:
            with self.subTest(n=n, c=c, k=k):
                expected = pass_at_k(n=n, c=c, k=k)
                got = reach_at_k(self._binary_matrix(c, n), k=k)
                self.assertAlmostEqual(got, expected, places=10)

    def test_collapse_to_pass_caret_k(self):
        for n, c, k in [(10, 5, 3), (10, 9, 3), (10, 10, 3),
                        (8, 3, 1), (8, 3, 8), (5, 4, 2)]:
            with self.subTest(n=n, c=c, k=k):
                expected = pass_caret_k(n=n, c=c, k=k)
                got = reach_caret_k(self._binary_matrix(c, n), k=k)
                self.assertAlmostEqual(got, expected, places=10)

    def test_graded_max_min(self):
        # Three sessions, single stage, weights make R_j read off
        # directly: R = [1.0, 0.5, 0.0]. For k=2:
        #   reach@2 = sum_{i=k..n} comb(i-1,k-1)/comb(n,k) * R_(i)
        #           = (1/3)*0.0 + (2/3)*0.5 + (3/3)*1.0  -- wait
        # Sorted R_(1)..R_(3) = [0.0, 0.5, 1.0]; n=3, k=2; comb(3,2)=3.
        #   i=2: comb(1,1)/3 = 1/3, R_(2) = 0.5  -> 0.5/3
        #   i=3: comb(2,1)/3 = 2/3, R_(3) = 1.0  -> 2.0/3
        #   total = 2.5/3 ≈ 0.8333
        # For reach^2 (min):
        #   i=1: comb(2,1)/3 = 2/3, R_(1) = 0.0
        #   i=2: comb(1,1)/3 = 1/3, R_(2) = 0.5
        #   total = 0.5/3 ≈ 0.1667
        sm = [[1, 1], [1, 0], [0, 0]]  # R = [1, 0.5, 0] with equal weights
        self.assertAlmostEqual(reach_at_k(sm, k=2), 2.5 / 3, places=4)
        self.assertAlmostEqual(reach_caret_k(sm, k=2), 0.5 / 3, places=4)

    def test_k_eq_1_recovers_average(self):
        # reach@1 = reach^1 = average reach (every session is the
        # whole subset).
        sm = [[1, 1, 0], [1, 1, 1], [1, 0, 0], [0, 0, 0]]
        avg = reach_bar_k(sm)
        self.assertAlmostEqual(reach_at_k(sm, k=1), avg, places=10)
        self.assertAlmostEqual(reach_caret_k(sm, k=1), avg, places=10)

    def test_k_eq_n(self):
        # k=n: reach@n = max(R), reach^n = min(R).
        sm = [[1, 1, 1], [1, 1, 0], [1, 0, 0]]
        R = per_trial_reach(sm)
        self.assertAlmostEqual(reach_at_k(sm, k=3), max(R), places=10)
        self.assertAlmostEqual(reach_caret_k(sm, k=3), min(R), places=10)

    def test_empty(self):
        self.assertEqual(reach_at_k([], k=3), 0.0)
        self.assertEqual(reach_caret_k([], k=3), 0.0)

    def test_k_gt_n_raises(self):
        with self.assertRaises(ValueError):
            reach_at_k([[1], [0]], k=3)
        with self.assertRaises(ValueError):
            reach_caret_k([[1], [0]], k=3)


class TestPearsonCorr(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(pearson_corr_matrix([]), [])

    def test_perfect_positive(self):
        # y = 2x => correlation 1.0 in both off-diagonals.
        samples = [(x, 2 * x) for x in range(1, 11)]
        m = pearson_corr_matrix(samples)
        self.assertEqual(m[0][0], 1.0)
        self.assertEqual(m[1][1], 1.0)
        self.assertAlmostEqual(m[0][1], 1.0)
        self.assertAlmostEqual(m[1][0], 1.0)

    def test_perfect_negative(self):
        samples = [(x, -3 * x + 7) for x in range(1, 11)]
        m = pearson_corr_matrix(samples)
        self.assertAlmostEqual(m[0][1], -1.0)

    def test_zero_variance_returns_none(self):
        # Second column constant => undefined off-diagonal.
        samples = [(1, 5), (2, 5), (3, 5)]
        m = pearson_corr_matrix(samples)
        self.assertIsNone(m[0][1])
        self.assertIsNone(m[1][0])
        self.assertEqual(m[0][0], 1.0)
        self.assertEqual(m[1][1], 1.0)

    def test_single_sample(self):
        # n<2 => correlations undefined; diagonal still 1.0.
        m = pearson_corr_matrix([(1, 2, 3)])
        self.assertEqual(m[0][0], 1.0)
        self.assertIsNone(m[0][1])


class TestCostUsd(unittest.TestCase):
    def test_unknown_model_returns_none(self):
        self.assertIsNone(cost_usd("unknown_provider", "model_x", 1000, 1000))

    def test_haiku(self):
        # Haiku 4.5: $1 / $5 / $0.10 per Mtok
        # 1M input + 1M output => $6.00
        self.assertAlmostEqual(
            cost_usd("anthropic", "claude-haiku-4-5", 1_000_000, 1_000_000),
            6.0,
        )
        # With 1M cache reads => +$0.10 = $6.10
        self.assertAlmostEqual(
            cost_usd("anthropic", "claude-haiku-4-5",
                     1_000_000, 1_000_000, 1_000_000),
            6.10,
        )

    def test_gpt55_subscription_api_equivalent_base_rates(self):
        estimate = subscription_api_equivalent_cost(
            "gpt-5.5",
            input_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            output_tokens=1_000_000,
            initial_input_tokens=100_000,
        )
        self.assertIsNotNone(estimate)
        self.assertAlmostEqual(estimate["usd"], 35.5)
        self.assertFalse(estimate["long_context_pricing_applied"])
        self.assertEqual(estimate["actual_billing"], "subscription")

    def test_gpt55_subscription_api_equivalent_long_context_rates(self):
        estimate = subscription_api_equivalent_cost(
            "gpt-5.5",
            input_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            output_tokens=1_000_000,
            initial_input_tokens=272_001,
        )
        self.assertAlmostEqual(estimate["usd"], 56.0)
        self.assertTrue(estimate["long_context_pricing_applied"])

    def test_gpt56_sol_subscription_api_equivalent(self):
        estimate = subscription_api_equivalent_cost(
            "gpt-5.6-sol",
            input_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            output_tokens=1_000_000,
            initial_input_tokens=272_001,
        )
        self.assertAlmostEqual(estimate["usd"], 56.0)
        self.assertTrue(estimate["long_context_pricing_applied"])
        self.assertIn("gpt-5.6-sol", estimate["source"])

    def test_unknown_subscription_model_is_not_guessed(self):
        self.assertIsNone(subscription_api_equivalent_cost("future-model"))


class TestBootstrap(unittest.TestCase):
    def test_constant_array(self):
        m, lo, hi = bootstrap_ci([0.5] * 20, n_bootstrap=200, seed=42)
        self.assertAlmostEqual(m, 0.5)
        self.assertAlmostEqual(lo, 0.5)
        self.assertAlmostEqual(hi, 0.5)

    def test_mean_helper(self):
        self.assertEqual(mean([1, 2, 3, 4]), 2.5)
        self.assertEqual(mean([]), 0.0)


if __name__ == "__main__":
    unittest.main()
