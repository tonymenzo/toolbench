"""
Variant discovery + axis/prompt/sandbox behavior for the reference
`geometry` benchmark.
"""

import sys
import unittest
from pathlib import Path

# Ensure repo root is importable when tests are run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from toolbench.benchmarks import BENCHMARKS  # noqa: E402
from toolbench.core.variant import (  # noqa: E402,F401
    Variant,
    discover_variants,
)


class TestGeometryVariants(unittest.TestCase):
    """The reference math benchmark exercises the variant (scaffolding) axis."""

    def setUp(self):
        self.bench = BENCHMARKS["geometry"]()
        self.variants = self.bench.variants

    def test_three_variants_discovered(self):
        self.assertEqual(set(self.variants), {"direct", "derived", "polar"})

    def test_default_variant_is_direct(self):
        self.assertEqual(self.bench.default_variant, "direct")

    def test_derived_has_no_sandbox_template(self):
        # The `derived` variant ships no sandbox; points live in the prompt.
        self.assertIsNone(self.variants["derived"].template_dir)


if __name__ == "__main__":
    unittest.main()
