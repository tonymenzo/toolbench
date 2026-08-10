"""A graded trial must not inherit the operator's ambient Claude Code settings.

The claude_code runtime drives the real `claude` CLI. Left to its defaults the
CLI loads every setting source including the USER level, so anything sitting in
~/.claude/skills/ is surfaced to the model — in every arm of every run. A guide
a past `tb connect` left behind, or one the operator hand-wrote, would then join
the measured configuration unrecorded: a `core_only` arm would receive domain
guidance it is defined not to have, and the tools-vs-no-tools delta it is there
to measure would be contaminated.

This was observed, not hypothesised: a headless run on a developer machine
listed three tb-surfaced `aster__*` guides plus a hand-written one.

`--setting-sources project` scopes the CLI to the trial's own sandbox (its cwd),
which is where the runner materialises the loadout's skills, and drops the
machine's ambient state. It is hardcoded rather than harness-configurable
because a harness that could switch it off could silently invalidate a campaign.
"""
import json
import stat
import textwrap
from pathlib import Path

import toolbench.core.runtime as runtime_mod
from toolbench.core.runtime import ClaudeCodeAgent


def _recording_cli(tmp_path: Path) -> tuple[str, Path]:
    """A fake `claude` that dumps its own argv, then emits a valid result."""
    argv_dump = tmp_path / "argv.json"
    events = [
        {"type": "system", "session_id": "s1"},
        {"type": "result", "result": "ok", "session_id": "s1",
         "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    body = textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, sys
        json.dump(sys.argv, open({str(argv_dump)!r}, "w"))
        for line in {[json.dumps(e) for e in events]!r}:
            print(line)
    """)
    path = tmp_path / "claude"
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path), argv_dump


def _run_and_capture(tmp_path, monkeypatch) -> list[str]:
    fake, argv_dump = _recording_cli(tmp_path)
    monkeypatch.setattr(runtime_mod.shutil, "which", lambda _n: fake)
    monkeypatch.setattr(runtime_mod, "_CLAUDE_CODE_TIMEOUT_S", 30)
    agent = ClaudeCodeAgent(system_prompt="s", sandbox_dir=str(tmp_path))
    agent.run("go")
    return json.loads(argv_dump.read_text())


def test_setting_sources_scoped_to_project(tmp_path, monkeypatch):
    argv = _run_and_capture(tmp_path, monkeypatch)
    assert "--setting-sources" in argv, (
        "the CLI would load user-level settings, surfacing ~/.claude/skills "
        "into every arm")
    assert argv[argv.index("--setting-sources") + 1] == "project"


def test_user_scope_is_never_requested(tmp_path, monkeypatch):
    """Belt and braces: no code path may widen the scope back to `user`."""
    argv = _run_and_capture(tmp_path, monkeypatch)
    sources = argv[argv.index("--setting-sources") + 1].split(",")
    assert "user" not in sources, sources
    assert sources == ["project"], sources


# ── run provenance: the ambient stack the no-tools arm depends on ─────────

def test_environment_record_captures_the_scientific_stack():
    """A no-tools arm's capability rests entirely on the ambient environment.

    The tools arm's toolkit version was already recorded; the control arm's
    stack was not, which made the control uncontrolled. A 2026-08 campaign ran
    its two arms against different Pythia builds with nothing in the record to
    show it.
    """
    from toolbench.cli import _environment_record
    rec = _environment_record()
    for key in ("python", "python_executable", "platform", "packages",
                "conda_prefix"):
        assert key in rec, key
    assert rec["python"].count(".") >= 1
    assert isinstance(rec["packages"], dict)
    # every reported version must be a real string, never None
    assert all(isinstance(v, str) and v for v in rec["packages"].values())


def test_environment_record_never_fails_a_run(monkeypatch):
    """Provenance is a record, not a gate: probing must never raise.

    Covers both ways it can go wrong -- a package that simply is not installed
    (omitted), and a metadata backend that blows up (recorded as "unknown"
    rather than propagating out and killing the run before any trial starts).
    """
    import importlib.metadata as md

    import toolbench.cli as cli

    monkeypatch.setattr(cli, "_ENV_PACKAGES", ("numpy", "definitely_absent_pkg"))
    rec = cli._environment_record()
    assert "definitely_absent_pkg" not in rec["packages"]   # absent, not fatal
    assert "numpy" in rec["packages"]

    def boom(_name):
        raise RuntimeError("metadata backend exploded")

    monkeypatch.setattr(md, "version", boom)
    monkeypatch.setattr(cli, "_ENV_PACKAGES", ("numpy",))
    rec = cli._environment_record()
    assert rec["packages"].get("numpy") == "unknown", rec["packages"]


def test_git_sha_accepts_a_directory_and_degrades(tmp_path):
    """The benchmark lives in a different repo from the framework."""
    from toolbench.cli import _git_sha
    assert _git_sha(tmp_path) == "unknown"      # not a repo -> no crash
    assert isinstance(_git_sha(), str)
