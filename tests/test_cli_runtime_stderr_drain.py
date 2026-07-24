"""The CLI runtimes (claude_code, codex) must drain the child's stderr
while streaming stdout: with both on PIPE and only stdout read, a child
that logs more than the OS pipe buffer (~64 KB) to stderr blocks on the
write, the stdout stream stalls, and the trial hangs until the wall-clock
killer fires — occupying a --parallel worker slot the whole time.

Each fake CLI floods stderr BEFORE writing any stdout, so an undrained
stderr pipe deadlocks the run deterministically instead of passing by
luck. The wall-clock killer is shrunk so a regression fails the test in
seconds rather than hanging it for the real 3-hour ceiling.
"""

import json
import stat
import textwrap
from pathlib import Path

import pytest

import toolbench.core.runtime as runtime_mod
from toolbench.core.runtime import ClaudeCodeAgent, CodexAgent

# Well past any OS pipe buffer (64 KB on macOS/Linux).
_STDERR_BYTES = 1 << 20


def _fake_cli(tmp_path: Path, name: str, stdout_events: list[dict]) -> str:
    """An executable that floods stderr, then emits `stdout_events` as
    NDJSON on stdout and exits 0."""
    lines = [json.dumps(ev) for ev in stdout_events]
    body = textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys
        sys.stderr.write("x" * {_STDERR_BYTES})
        sys.stderr.flush()
        for line in {lines!r}:
            print(line)
    """)
    path = tmp_path / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _shrink_killer(monkeypatch):
    monkeypatch.setattr(runtime_mod, "_CLAUDE_CODE_TIMEOUT_S", 30)


def test_claude_code_survives_stderr_flood(tmp_path, monkeypatch):
    fake = _fake_cli(tmp_path, "claude", [
        {"type": "system", "session_id": "sess-1"},
        {"type": "result", "result": "done", "session_id": "sess-1",
         "usage": {"input_tokens": 5, "output_tokens": 2}},
    ])
    monkeypatch.setattr(runtime_mod.shutil, "which", lambda _name: fake)
    _shrink_killer(monkeypatch)
    agent = ClaudeCodeAgent(system_prompt="s", sandbox_dir=str(tmp_path))
    resp = agent.run("go")
    assert resp.text == "done"
    assert agent.session_id == "sess-1"
    assert agent.token_usage["input"] == 5


def test_codex_survives_stderr_flood(tmp_path, monkeypatch):
    fake = _fake_cli(tmp_path, "codex", [
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "item.completed",
         "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed",
         "usage": {"input_tokens": 3, "output_tokens": 1}},
    ])
    monkeypatch.setattr(runtime_mod.shutil, "which", lambda _name: fake)
    _shrink_killer(monkeypatch)
    agent = CodexAgent(system_prompt="s", sandbox_dir=str(tmp_path))
    resp = agent.run("go")
    assert resp.text == "done"
    assert agent.thread_id == "t-1"


def test_claude_code_error_reports_drained_stderr(tmp_path, monkeypatch):
    # No result event: run() must raise, and the message must carry the
    # stderr the drain thread collected (not an empty post-mortem read).
    fake = _fake_cli(tmp_path, "claude", [])
    monkeypatch.setattr(runtime_mod.shutil, "which", lambda _name: fake)
    _shrink_killer(monkeypatch)
    agent = ClaudeCodeAgent(system_prompt="s", sandbox_dir=str(tmp_path))
    with pytest.raises(RuntimeError) as excinfo:
        agent.run("go")
    assert "xxxx" in str(excinfo.value)
