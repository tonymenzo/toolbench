"""
Variant discovery + axis/prompt/sandbox behavior for the reference
`geometry` benchmark.
"""

import sys
import pathlib
import tempfile
import unittest
from pathlib import Path

# Ensure repo root is importable when tests are run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.helpers import load_geometry
from toolbench.core.variant import (  # noqa: E402,F401
    Variant,
    discover_variants,
)


class TestGeometryVariants(unittest.TestCase):
    """The reference math benchmark exercises the variant (scaffolding) axis."""

    def setUp(self):
        self.bench = load_geometry()
        self.variants = self.bench.variants

    def test_three_variants_discovered(self):
        self.assertEqual(set(self.variants), {"direct", "derived", "polar"})

    def test_default_variant_is_direct(self):
        self.assertEqual(self.bench.default_variant, "direct")

    def test_derived_has_no_sandbox_template(self):
        # The `derived` variant ships no sandbox; points live in the prompt.
        self.assertIsNone(self.variants["derived"].template_dir)


class TestSandboxSeedIntegrity(unittest.TestCase):
    """An agent can rewrite the contract it was handed; we must notice.

    Motivated by a real trial (gpt-5.6-sol / llp_forward bare /
    tools_defined / seed 1001, 2026-08-10): the agent listed its sandbox
    with `rg --files -g '!results/**'`, never saw the answer schema it had
    been given, wrote its own over the top with codex's apply_patch
    `*** Add File:` -- which overwrites silently -- validated its answer
    against that substitute, and scored 0.0 on a trial whose physics was
    otherwise worth ~0.81. Nothing in the trial record said so.
    """

    def _variant(self, tmp):
        tpl = pathlib.Path(tmp) / "template"
        (tpl / "results").mkdir(parents=True)
        (tpl / "results" / "answer_schema.json").write_text('{"required": ["x"]}')
        (tpl / "README.md").write_text("read me")
        return Variant(name="bare", variant_dir=pathlib.Path(tmp),
                       template_dir=tpl)

    def test_clean_sandbox_reports_no_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = self._variant(tmp)
            sandbox = pathlib.Path(tmp) / "sandbox"
            v.setup_workspace(sandbox)
            self.assertEqual(v.verify_workspace(sandbox), {})

    def test_overwritten_contract_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = self._variant(tmp)
            sandbox = pathlib.Path(tmp) / "sandbox"
            v.setup_workspace(sandbox)
            # what apply_patch `*** Add File:` does to an existing path
            (sandbox / "results" / "answer_schema.json").write_text('{"mine": 1}')
            self.assertEqual(v.verify_workspace(sandbox),
                             {"results/answer_schema.json": "overwritten"})

    def test_deleted_seed_file_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = self._variant(tmp)
            sandbox = pathlib.Path(tmp) / "sandbox"
            v.setup_workspace(sandbox)
            (sandbox / "README.md").unlink()
            self.assertEqual(v.verify_workspace(sandbox),
                             {"README.md": "deleted"})

    def test_agent_created_files_are_not_drift(self):
        # The agent writes results/answer.json, work/, scripts -- all fine.
        with tempfile.TemporaryDirectory() as tmp:
            v = self._variant(tmp)
            sandbox = pathlib.Path(tmp) / "sandbox"
            v.setup_workspace(sandbox)
            (sandbox / "results" / "answer.json").write_text("{}")
            (sandbox / "work").mkdir()
            self.assertEqual(v.verify_workspace(sandbox), {})

    def test_variant_with_no_template_never_drifts(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = Variant(name="derived", variant_dir=pathlib.Path(tmp))
            sandbox = pathlib.Path(tmp) / "sandbox"
            v.setup_workspace(sandbox)
            self.assertEqual(v.verify_workspace(sandbox), {})


if __name__ == "__main__":
    unittest.main()
