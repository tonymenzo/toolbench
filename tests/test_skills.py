"""Loadout skills: delivery modes, strictness, path resolution, and the
end-to-end wiring (skills actually reach the sandbox + system prompt —
the gap where they were previously parsed and silently dropped)."""

import tempfile
import unittest
from pathlib import Path

from tests.helpers import load_geometry
from toolbench.core.loadout import discover_loadouts
from toolbench.core.skills import prepare_skills, skill_names

try:
    import orchestral  # noqa: F401
    from orchestral.context.message import Message
    from orchestral.llm.base.response import Response
    HAVE = True
except Exception:
    HAVE = False


class TestPrepareSkills(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.sandbox = root / "sandbox"
        self.sandbox.mkdir()
        self.skill_file = root / "recipe.md"
        self.skill_file.write_text("# Recipe\nUse add() then power().")

    def tearDown(self):
        self._tmp.cleanup()

    def _entry(self, **over):
        e = {"name": "recipe", "file": str(self.skill_file)}
        e.update(over)
        return e

    def test_no_skills_no_addendum(self):
        self.assertEqual(prepare_skills([], self.sandbox), "")
        self.assertEqual(prepare_skills(None, self.sandbox), "")

    def test_on_demand_copies_and_points(self):
        addendum = prepare_skills([self._entry()], self.sandbox)
        copied = self.sandbox / "skills" / "recipe.md"
        self.assertTrue(copied.is_file())
        self.assertIn("Use add()", copied.read_text())
        self.assertIn("skills/recipe.md", addendum)
        self.assertNotIn("Use add()", addendum)   # pointer, not content

    def test_inline_embeds_content(self):
        addendum = prepare_skills([self._entry(mode="inline")], self.sandbox)
        self.assertIn("Use add() then power()", addendum)
        self.assertFalse((self.sandbox / "skills").exists())

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            prepare_skills([self._entry(file="/nope/skill.md")], self.sandbox)

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            prepare_skills([self._entry(mode="psychic")], self.sandbox)

    def test_missing_name_or_file_raises(self):
        with self.assertRaises(ValueError):
            prepare_skills([{"file": str(self.skill_file)}], self.sandbox)

    def test_skill_names(self):
        self.assertEqual(skill_names([self._entry(), {"name": "b", "file": "x"}]),
                         ["recipe", "b"])


class TestLoadoutSkillResolution(unittest.TestCase):
    def test_guided_loadout_skill_path_resolved_and_exists(self):
        bench = load_geometry()
        guided = discover_loadouts(bench.BENCHMARK_DIR)["guided"]
        self.assertEqual(len(guided.skills), 1)
        f = Path(guided.skills[0]["file"])
        self.assertTrue(f.is_absolute())
        self.assertTrue(f.is_file())          # ./skills/distance_recipe.md


@unittest.skipUnless(HAVE, "orchestral not importable")
class TestSkillsReachTheAgent(unittest.TestCase):
    """End-to-end: the guided loadout's skill lands in the sandbox and
    the agent's system prompt — the exact wiring that used to be absent."""

    class _LLM:
        def __init__(self):
            self.system_texts = []

        def set_tools(self, tools):
            self.tools = tools

        def get_response(self, context, **kw):
            for m in context.get_messages():
                if getattr(m, "role", None) == "system":
                    self.system_texts.append(
                        getattr(m, "text", "") or getattr(m, "content", ""))
            return Response(model="s",
                            message=Message(role="assistant", text="done",
                                            tool_calls=None))

    def test_guided_skill_in_system_prompt(self):
        from toolbench.core.budget import Budget
        from toolbench.core.harness import discover_harnesses
        from toolbench.core.llm_factory import register_provider
        from toolbench.core.runner import TrialRunner
        from toolbench.core.store import read_json

        bench = load_geometry()
        bd = bench.BENCHMARK_DIR
        llm = self._LLM()
        register_provider("skilltest", lambda model=None, **kw: llm)
        harness = discover_harnesses(bd)["orchestral/anthropic"]
        harness.provider = {"name": "skilltest"}
        guided = discover_loadouts(bd)["guided"]

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runner = TrialRunner(max_iterations=2, verbose=False)
        runner.run_trial(
            model_cfg={"provider": "skilltest", "model": "x"},
            benchmark=bench, harness=harness, loadout=guided,
            variant=bench.get_variant(), seed=1, trial_id="t0",
            run_dir=Path(tmp.name), budget=Budget(None),
        )
        self.assertTrue(llm.system_texts)
        self.assertIn("skills/distance_recipe.md", llm.system_texts[0])
        # The trial record carries the skill names.
        tj = read_json(Path(tmp.name) / "trials" / "t0" / "trial.json")
        self.assertEqual(tj["config"]["skills"], ["distance_recipe"])


if __name__ == "__main__":
    unittest.main()
