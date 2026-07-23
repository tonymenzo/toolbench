"""Judge selection: parsing, precedence, and what may not be silently wrong.

The invariant that matters most here is that the RULE grade stays
primary under dual grading. If an LLM grade could displace it, a run
would stop being reproducible — its score would drift with the judge
model's version — which is exactly what a benchmark must not do.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from toolbench.core.judge import DualJudge, Judge  # noqa: E402
from toolbench.core.judge_select import (  # noqa: E402
    JudgeSpec, build_judge, parse_kinds, resolve,
)
from toolbench.core.task import Grade, Rubric  # noqa: E402
from toolbench.core.trajectory import Trajectory  # noqa: E402


class TestParseKinds(unittest.TestCase):
    def test_default_is_rule(self):
        self.assertEqual(parse_kinds(None), ("rule",))
        self.assertEqual(parse_kinds(""), ("rule",))

    def test_dual_order_is_significant(self):
        self.assertEqual(parse_kinds("rule+llm"), ("rule", "llm"))
        self.assertEqual(parse_kinds("llm+rule"), ("llm", "rule"))

    def test_comma_accepted(self):
        self.assertEqual(parse_kinds("rule,llm"), ("rule", "llm"))

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            parse_kinds("rule+vibes")

    def test_duplicate_rejected(self):
        with self.assertRaises(ValueError):
            parse_kinds("llm+llm")


class TestPrecedence(unittest.TestCase):
    HARNESS_JUDGE = {"kind": "rule+llm", "harness": "orchestral/anthropic",
                     "model": "claude-opus-4-8"}

    def test_harness_block_used_when_no_cli(self):
        spec = resolve(self.HARNESS_JUDGE)
        self.assertEqual(spec.kinds, ("rule", "llm"))
        self.assertEqual(spec.model, "claude-opus-4-8")

    def test_cli_overrides_field_by_field(self):
        # Flip the kind while keeping the harness-pinned judge model.
        spec = resolve(self.HARNESS_JUDGE, cli_judge="rule")
        self.assertEqual(spec.kinds, ("rule",))
        self.assertEqual(spec.model, "claude-opus-4-8")

        spec = resolve(self.HARNESS_JUDGE, cli_model="claude-haiku-4-5")
        self.assertEqual(spec.model, "claude-haiku-4-5")
        self.assertEqual(spec.harness, "orchestral/anthropic")

    def test_default_without_harness_block(self):
        spec = resolve(None)
        self.assertEqual(spec.kinds, ("rule",))
        self.assertFalse(spec.wants_llm)

    def test_llm_without_harness_is_rejected_at_resolve(self):
        # Must fail before a paid run, not after.
        with self.assertRaises(ValueError):
            resolve({"kind": "llm"})

    def test_extra_params_pass_through(self):
        spec = resolve({**self.HARNESS_JUDGE, "max_tokens": 4096})
        self.assertEqual(spec.params.get("max_tokens"), 4096)

    def test_label_names_the_judge(self):
        label = resolve(self.HARNESS_JUDGE).label()
        self.assertIn("claude-opus-4-8", label)
        self.assertIn("orchestral/anthropic", label)
        self.assertEqual(resolve(None).label(), "rule")


class _StubJudge(Judge):
    def __init__(self, kind, score):
        self._kind, self._score = kind, score

    @property
    def kind(self):
        return self._kind

    def grade(self, trajectory, rubric, base_directory):
        return Grade(score=self._score, stages={}, stage_grades=[],
                     failure_mode="NONE", judge_kind=self._kind)


class _ExplodingJudge(Judge):
    kind = "llm"

    def grade(self, *a, **kw):
        raise RuntimeError("provider is down")


class TestDualJudge(unittest.TestCase):
    def _grade(self, judges):
        return DualJudge(judges).grade(Trajectory(), Rubric(stages=[]), ".")

    def test_primary_score_wins(self):
        g = self._grade([_StubJudge("rule", 0.42), _StubJudge("llm", 0.99)])
        self.assertEqual(g.score, 0.42)
        self.assertEqual(g.judge_kind, "rule")
        self.assertEqual(len(g.alt_grades), 1)
        self.assertEqual(g.alt_grades[0]["score"], 0.99)
        self.assertEqual(g.alt_grades[0]["judge_kind"], "llm")

    def test_judge_failure_does_not_corrupt_primary(self):
        # A dead judge provider must cost the alt opinion, never the score.
        g = self._grade([_StubJudge("rule", 0.42), _ExplodingJudge()])
        self.assertEqual(g.score, 0.42)
        self.assertIn("error", g.alt_grades[0])
        self.assertIn("provider is down", g.alt_grades[0]["error"])

    def test_single_judge_is_not_wrapped(self):
        j = build_judge(JudgeSpec(kinds=("rule",)))
        self.assertNotIsInstance(j, DualJudge)
        self.assertEqual(j.kind, "rule")

    def test_unknown_judge_harness_names_the_alternatives(self):
        spec = JudgeSpec(kinds=("llm",), harness="orchestral/nope")
        with self.assertRaises(ValueError) as ctx:
            build_judge(spec, harnesses={"orchestral/anthropic": object()})
        self.assertIn("orchestral/anthropic", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
