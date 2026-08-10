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

    # ── native delivery (runtimes with a real skill concept) ──────────────

    def _native(self):
        return self.sandbox / ".claude" / "skills"

    def test_native_writes_a_project_skill_and_no_pointer(self):
        """The CLI advertises name+description itself, so no addendum."""
        addendum = prepare_skills([self._entry()], self.sandbox,
                                  native_dir=self._native())
        doc = self._native() / "recipe" / "SKILL.md"
        self.assertTrue(doc.is_file())
        self.assertIn("Use add()", doc.read_text())
        self.assertEqual(addendum, "")
        # and NOT the pointer-style copy
        self.assertFalse((self.sandbox / "skills").exists())

    def test_native_synthesizes_frontmatter_when_absent(self):
        """The CLI requires frontmatter; the source here has none."""
        prepare_skills([self._entry()], self.sandbox,
                       loadout_name="tools_x", native_dir=self._native())
        text = (self._native() / "recipe" / "SKILL.md").read_text()
        self.assertTrue(text.lstrip().startswith("---"))
        self.assertIn("name: recipe", text)
        self.assertIn("description:", text)

    def test_native_passes_existing_frontmatter_through(self):
        self.skill_file.write_text(
            "---\nname: Long Human Name\ndescription: What it is for.\n"
            "bundle: llp\n---\n\nBody here.\n")
        prepare_skills([self._entry()], self.sandbox,
                       native_dir=self._native())
        text = (self._native() / "recipe" / "SKILL.md").read_text()
        # untouched, extra keys included — the directory name is what the CLI
        # lists the skill as, so a spaced `name:` is harmless
        self.assertIn("name: Long Human Name", text)
        self.assertIn("bundle: llp", text)
        self.assertIn("Body here.", text)

    def test_native_is_per_trial_not_global(self):
        """The skill must land under the sandbox and nowhere else."""
        prepare_skills([self._entry()], self.sandbox,
                       native_dir=self._native())
        doc = (self._native() / "recipe" / "SKILL.md").resolve()
        self.assertTrue(str(doc).startswith(str(self.sandbox.resolve())))

    def test_inline_still_inlines_even_with_native_dir(self):
        addendum = prepare_skills([self._entry(mode="inline")], self.sandbox,
                                  native_dir=self._native())
        self.assertIn("Use add() then power()", addendum)
        self.assertFalse(self._native().exists())

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


class TestSandboxProjectRootIsolation(unittest.TestCase):
    """A trial sandbox must not inherit the enclosing repo's project skills.

    Sandboxes live inside the benchmark repo (`runs/<id>/trials/<t>/sandbox`),
    and the Claude Code CLI resolves project scope from the enclosing project
    root — so the repo's own `.claude/skills/` reached every arm of every run.
    `--setting-sources project` does not help: from the sandbox's point of view
    those ARE project scope. A `.git` in the sandbox stops the walk-up.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = Path(self._tmp.name) / "sandbox"
        self.sandbox.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_a_project_root_marker(self):
        from toolbench.core.runner import _isolate_project_root
        _isolate_project_root(self.sandbox)
        self.assertTrue((self.sandbox / ".git").exists())

    def test_is_idempotent_and_leaves_an_existing_repo_alone(self):
        from toolbench.core.runner import _isolate_project_root
        git = self.sandbox / ".git"
        git.mkdir()
        (git / "SENTINEL").write_text("preexisting")
        _isolate_project_root(self.sandbox)
        self.assertEqual((git / "SENTINEL").read_text(), "preexisting")

    def test_falls_back_to_a_bare_marker_without_git(self):
        """git absent must not fail the trial — the marker alone suffices."""
        import toolbench.core.runner as runner_mod
        from toolbench.core.runner import _isolate_project_root
        real = runner_mod.subprocess.run

        def boom(*a, **k):
            raise FileNotFoundError("git")

        runner_mod.subprocess.run = boom
        try:
            _isolate_project_root(self.sandbox)
        finally:
            runner_mod.subprocess.run = real
        self.assertTrue((self.sandbox / ".git").is_dir())


class TestPointerCarriesDescription(unittest.TestCase):
    """Runtimes without a native skill concept must still get a useful pointer.

    codex has no model-facing skill mechanism, so the prompt pointer is the
    ONLY signal it gets. Emitting just the slug ("- skills/x.md: x") tells the
    agent nothing about whether to open the file, which turns "does the guide
    help" into "does the agent gamble on an unlabelled filename".
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sandbox = self.root / "sandbox"
        self.sandbox.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _skill(self, text):
        p = self.root / "guide.md"
        p.write_text(text)
        return [{"name": "guide", "file": str(p)}]

    def test_description_is_carried_into_the_pointer(self):
        add = prepare_skills(self._skill(
            "---\nname: A Guide\ndescription: Process selection and the traps"
            " that rescale a result.\n---\n\nBody.\n"), self.sandbox)
        self.assertIn("skills/guide.md", add)
        self.assertIn("Process selection and the traps", add)

    def test_missing_or_malformed_frontmatter_degrades_quietly(self):
        for text in ("no frontmatter at all\n",
                     "---\nname: only a name\n---\nbody\n",
                     "---\nunterminated: block\n"):
            add = prepare_skills(self._skill(text), self.sandbox)
            self.assertIn("skills/guide.md", add)       # pointer still emitted
            self.assertNotIn("None", add)

    def test_native_delivery_needs_no_pointer(self):
        """Native runtimes surface the description themselves."""
        add = prepare_skills(self._skill(
            "---\nname: A\ndescription: D.\n---\nbody\n"), self.sandbox,
            native_dir=self.sandbox / ".claude" / "skills")
        self.assertEqual(add, "")


class TestAgentsPointerForCodex(unittest.TestCase):
    """codex's only model-facing channel is an auto-injected AGENTS.md.

    Its `~/.codex/prompts/` entries are user-typed slash commands, unreachable
    in a headless `codex exec` run, so without this a codex trial's only
    signal was a line buried in the first-turn prompt.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sandbox = self.root / "sandbox"
        self.sandbox.mkdir()
        self.src = self.root / "guide.md"
        self.src.write_text("---\nname: G\ndescription: How to do the thing.\n"
                            "---\n\nBODY-TEXT\n")
        self.entry = [{"name": "guide", "file": str(self.src)}]

    def tearDown(self):
        self._tmp.cleanup()

    def test_guide_is_delivered_in_full(self):
        """The BODY, not a pointer.

        A pointer leaves "did the agent open the file" inside the
        measurement; on the 2026-08-10 gpt-5.6 run the guide was never opened
        at all. The no-tools arm receives nothing either way, so injecting the
        body makes the within-model delta isolate the knowledge itself.
        """
        prepare_skills(self.entry, self.sandbox)
        doc = (self.sandbox / "AGENTS.md").read_text()
        self.assertIn("BODY-TEXT", doc)            # the guide itself
        self.assertIn("How to do the thing", doc)  # its description
        self.assertIn("skills/guide.md", doc)      # still discoverable on disk

    def test_frontmatter_is_not_pasted_into_agents_md(self):
        """Raw YAML would render as prose and read as instructions."""
        doc = (self.sandbox / "AGENTS.md")
        prepare_skills(self.entry, self.sandbox)
        text = doc.read_text()
        self.assertNotIn("name: G", text)
        self.assertNotIn("---", text.split("BODY-TEXT")[0][-40:])

    def test_existing_agents_md_is_appended_not_clobbered(self):
        (self.sandbox / "AGENTS.md").write_text("# Task rules\nKeep these.\n")
        prepare_skills(self.entry, self.sandbox)
        doc = (self.sandbox / "AGENTS.md").read_text()
        self.assertIn("Keep these.", doc)      # task material survives
        self.assertIn("skills/guide.md", doc)

    def test_native_runtime_writes_no_agents_md(self):
        """claude_code surfaces skills itself; a second channel would double."""
        prepare_skills(self.entry, self.sandbox,
                       native_dir=self.sandbox / ".claude" / "skills")
        self.assertFalse((self.sandbox / "AGENTS.md").exists())

    def test_inline_mode_writes_no_agents_md(self):
        prepare_skills([{**self.entry[0], "mode": "inline"}], self.sandbox)
        self.assertFalse((self.sandbox / "AGENTS.md").exists())
