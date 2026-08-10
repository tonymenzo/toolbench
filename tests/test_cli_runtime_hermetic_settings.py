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
