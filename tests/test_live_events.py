"""TrajectoryHook streams a trial to events.jsonl as it happens, for the
dashboard's live log: tool calls (a start, then the finished call under the
same id and with the transcript's fields), agent messages and recovery
turns. A write failure must never reach the trial."""

import json

import pytest

pytest.importorskip("orchestral")

from toolbench.core.trajectory import Trajectory, TrajectoryHook  # noqa: E402


def _events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_hook_streams_calls_messages_and_interventions(tmp_path):
    traj = Trajectory()
    hook = TrajectoryHook(traj, log_path=tmp_path / "console.log",
                          events_path=tmp_path / "events.jsonl")
    hook.before_call("read_file", {"path": "a.txt"})
    assert _events(tmp_path / "events.jsonl")[0]["type"] == "tool_start"  # flushed live
    hook.agent_message("Reading the task.")
    hook.after_call("read_file", "contents")
    hook.intervention({"type": "rate_limit_retry", "index": 0, "after_tool_call": 1,
                       "reason": "RATE_LIMITED", "injected_message": "Continue."})
    hook.agent_message("   ")  # blank text is not a message
    hook.close()

    start, agent, call, retry = _events(tmp_path / "events.jsonl")
    assert start == {"type": "tool_start", "id": 1, "t": start["t"],
                     "name": "read_file", "args": {"path": "a.txt"}}
    assert agent["type"] == "agent" and agent["text"] == "Reading the task."
    assert call == {"type": "tool_call", "id": 1, **traj.tool_calls[0].to_dict()}
    assert retry["type"] == "intervention" and retry["kind"] == "rate_limit_retry"
    assert start["t"] <= agent["t"] <= call["t"] <= retry["t"]
    assert "[agent]  Reading the task." in (tmp_path / "console.log").read_text()


def test_unwritable_events_never_fail_the_trial(tmp_path):
    traj = Trajectory()
    (tmp_path / "events.jsonl").mkdir()  # cannot be opened as a file
    hook = TrajectoryHook(traj, events_path=tmp_path / "events.jsonl")
    hook.before_call("t", {})
    hook.after_call("t", "ok")
    hook.close()
    assert len(traj.tool_calls) == 1


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_cli_runtimes_stream_agent_messages(tmp_path, monkeypatch, runtime):
    import toolbench.core.runtime as runtime_mod
    from tests.test_cli_runtime_stderr_drain import _fake_cli, _shrink_killer
    events = {
        "claude": [
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Listing the inputs."},
                {"type": "tool_use", "id": "u1", "name": "mcp__tb__list_dir", "input": {"path": "."}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "u1", "content": "a.txt"}]}},
            {"type": "result", "result": "done", "usage": {}},
        ],
        "codex": [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Listing the inputs."}},
            {"type": "turn.completed", "usage": {}},
        ],
    }[runtime]
    fake = _fake_cli(tmp_path, runtime, events)
    monkeypatch.setattr(runtime_mod.shutil, "which", lambda _name: fake)
    _shrink_killer(monkeypatch)
    hook = TrajectoryHook(Trajectory(), events_path=tmp_path / "events.jsonl")
    cls = runtime_mod.ClaudeCodeAgent if runtime == "claude" else runtime_mod.CodexAgent
    cls(system_prompt="s", sandbox_dir=str(tmp_path), traj_hook=hook).run("go")
    hook.close()
    got = _events(tmp_path / "events.jsonl")
    assert got[0] == {"type": "agent", "t": got[0]["t"], "text": "Listing the inputs."}
    if runtime == "claude":
        assert [e["type"] for e in got[1:]] == ["tool_start", "tool_call"]
