"""Regression coverage for the subscription-backed Codex CLI runtime."""

from pathlib import Path

from toolbench.core.harness import Harness
from toolbench.core.llm_factory import SubscriptionLLM
from toolbench.core.runtime import CodexAgent, _codex_factory


def _agent(tmp_path: Path, **kw) -> CodexAgent:
    return CodexAgent(system_prompt="system", sandbox_dir=str(tmp_path),
                      model="gpt-test", **kw)


def test_first_turn_is_isolated_and_explicit(tmp_path):
    a = _agent(tmp_path, reasoning_effort="high")
    cmd = a._build_command("/bin/codex", "prompt")
    assert cmd[:2] == ["/bin/codex", "exec"]
    assert "--ignore-user-config" in cmd
    assert cmd[cmd.index("-m") + 1] == "gpt-test"
    assert cmd[cmd.index("-s") + 1] == "workspace-write"
    assert cmd[cmd.index("-C") + 1] == str(tmp_path.resolve())
    assert 'model_reasoning_effort="high"' in cmd


def test_toolbase_mcp_is_auto_approved_for_noninteractive_exec(tmp_path):
    a = _agent(tmp_path, profile="hep-symb")
    args = a._mcp_config_args()
    assert any("default_tools_approval_mode=\"approve\"" in x for x in args)


def test_resume_uses_only_resume_supported_flags(tmp_path):
    a = _agent(tmp_path)
    a.thread_id = "thread-123"
    cmd = a._build_command("/bin/codex", "continue")
    assert cmd[:4] == ["/bin/codex", "exec", "resume", "thread-123"]
    assert "--json" in cmd and "--ignore-user-config" in cmd
    assert "-s" not in cmd
    assert "-C" not in cmd


def test_run_matrix_model_overrides_harness_default(tmp_path):
    h = Harness.from_dict({
        "runtime": {"name": "codex", "reasoning_effort": "medium"},
        "provider": {"name": "subscription", "model": "old-default"},
        "core": {"builtin": True},
    }, id="codex/default")
    agent = _codex_factory(system_prompt="s", sandbox_dir=str(tmp_path),
                           harness=h, loadout=None, tool_hooks=[],
                           llm=SubscriptionLLM(model="requested-model"))
    assert agent.model == "requested-model"
    assert agent.reasoning_effort == "medium"


def test_codex_usage_accumulates_and_tracks_cache(tmp_path):
    a = _agent(tmp_path)
    a._accumulate_usage({"input_tokens": 100, "cached_input_tokens": 40,
                         "cache_write_input_tokens": 7, "output_tokens": 12})
    a._accumulate_usage({"input_tokens": 150, "cached_input_tokens": 100,
                         "output_tokens": 8})
    assert a.token_usage == {
        "initial_input": 100, "input": 110, "output": 20,
        "cache_read": 140, "cache_creation": 7,
        "cost": None, "model": "gpt-test",
    }


def test_mcp_tool_view_keeps_qualified_tool_name():
    view = CodexAgent._tool_view({
        "type": "mcp_tool_call", "server": "toolbase",
        "tool": "heptapod__NaturalUnitsConverter",
        "arguments": {"conversion_request": "1 GeV^-1 to m"},
        "result": {"content": [{"type": "text", "text": "ok"}]},
        "error": None, "status": "completed",
    })
    assert view[0] == "heptapod__NaturalUnitsConverter"
    assert view[1] == {"conversion_request": "1 GeV^-1 to m"}
    assert view[3] is False


def test_mcp_tool_view_detects_application_error_payload():
    view = CodexAgent._tool_view({
        "type": "mcp_tool_call", "server": "toolbase",
        "tool": "heptapod__PythiaFromRunCard",
        "arguments": {"cmnd_path": "card.cmnd"},
        "result": {"content": [{"type": "text",
                               "text": "Input validation error: required"}]},
        "error": None, "status": "completed",
    })
    assert view[3] is True


def test_file_change_failure_is_an_error():
    view = CodexAgent._tool_view({
        "type": "file_change", "status": "failed",
        "output": "sandbox-exec: sandbox_apply: Operation not permitted",
    })
    assert view[0] == "file_change"
    assert isinstance(view[1], dict)
    assert view[3] is True
    assert "Operation not permitted" in view[2]


def test_no_protected_paths_preserves_native_sandbox(tmp_path):
    a = _agent(tmp_path)
    cmd = a._build_command("codex", "hello")
    assert a._protect_ground_truth_reads(cmd) == cmd
    assert cmd[cmd.index("-s") + 1] == "workspace-write"


def test_protected_paths_use_single_external_sandbox(tmp_path, monkeypatch):
    protected = tmp_path / "ground_truth"
    protected.mkdir()
    a = _agent(tmp_path)
    a.protected_paths = [str(protected)]
    cmd = a._build_command("codex", "hello")
    assert cmd[:2] == ["codex", "exec"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert "-s" not in cmd
    assert 'default_permissions="toolbench_benchmark"' in cmd
    assert 'permissions.toolbench_benchmark.extends=":workspace"' in cmd
    assert (f'permissions.toolbench_benchmark.filesystem={{'
            f'"{protected}"="deny"}}') in cmd
    assert a._protect_ground_truth_reads(cmd) == cmd


def test_protected_danger_full_access_does_not_add_write_denial(
        tmp_path, monkeypatch):
    a = _agent(tmp_path)
    a.protected_paths = [str(tmp_path / "ground_truth")]
    a.sandbox_mode = "danger-full-access"
    cmd = a._build_command("codex", "hello")
    assert ('permissions.toolbench_benchmark.filesystem={'
            '"/"="write",') in " ".join(cmd)
    assert "permissions.toolbench_benchmark.network.enabled=true" in cmd
    assert 'permissions.toolbench_benchmark.extends=' not in " ".join(cmd)
