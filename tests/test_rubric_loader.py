"""
Rubric loader: shape + weights for the reference `geometry` benchmark.
"""

import unittest

import toolbench.core.benchmark  # noqa: F401
from tests.helpers import load_geometry


class TestGeometryRubric(unittest.TestCase):
    def setUp(self):
        self.bench = load_geometry()
        self.order = [s["id"] for s in self.bench.rubric.stages]
        self.weights = [float(s["weight"]) for s in self.bench.rubric.stages]

    def test_loads(self):
        self.assertTrue(self.order)

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(self.weights), 1.0)

    def test_each_stage_has_id_and_weight(self):
        self.assertEqual(len(self.order), len(self.weights))

    def test_headline_stage_weighted_most(self):
        # The final (headline) stage carries the most weight.
        self.assertEqual(self.weights[-1], max(self.weights))


if __name__ == "__main__":
    unittest.main()
