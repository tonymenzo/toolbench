"""MCP preflight (#42): it must fail when there is nothing to verify against,
and it must start the server where trials serve from (the run directory)."""

from types import SimpleNamespace

import pytest

import toolbench.core.runtime as runtime
from toolbench.cli import _mcp_preflight


def _harness(runtime_name="claude_code"):
    return SimpleNamespace(id="claude-code/default", runtime_name=runtime_name)


def _loadout(tb_loadout="symbolic-eda", project_root=None, name="tools_eda"):
    cfg = {"loadout": tb_loadout}
    if project_root:
        cfg["project_root"] = project_root
    return SimpleNamespace(name=name,
                           sources=[SimpleNamespace(backend="toolbase", config=cfg)])


def _report(tools=("eda__a", "eda__b"), error=None):
    if error:
        return {"harness": "claude-code/default", "loadout": "tools_eda", "error": error}
    return {"harness": "claude-code/default", "loadout": "tools_eda",
            "sources": [{"backend": "toolbase", "tools": list(tools)}]}


@pytest.fixture
def served(monkeypatch):
    """Stub `toolbase serve`: records the cwd it was started from."""
    calls = []

    def fake_verify(loadout, *, cwd, **_):
        calls.append(cwd)
        if isinstance(fake_verify.result, Exception):
            raise fake_verify.result
        return fake_verify.result
    fake_verify.result = ["eda__a", "eda__b"]
    monkeypatch.setattr(runtime, "verify_toolbase_mcp", fake_verify)
    return SimpleNamespace(set=lambda r: setattr(fake_verify, "result", r), calls=calls)


def test_failed_resolution_fails_even_if_a_server_would_answer(tmp_path, served):
    # The issue's repro: resolution recorded an error (so no tools), yet a
    # server started elsewhere serves 2 tools. Previously this printed OK.
    failures, records = _mcp_preflight(
        [_harness()], [_loadout()],
        [_report(error="ServeConfigError: No loadout named 'symbolic-eda'.")], tmp_path)
    assert len(failures) == 1 and "resolution failed" in failures[0]
    assert records[0]["ok"] is False
    assert served.calls == []          # nothing to check against: never "passes"


def test_empty_expectation_is_a_failure_not_a_pass(tmp_path, served):
    failures, _ = _mcp_preflight([_harness()], [_loadout()], [_report(tools=())], tmp_path)
    assert len(failures) == 1 and "no toolbase tools" in failures[0]


def test_ok_serves_from_the_run_directory_and_records_it(tmp_path, served):
    failures, records = _mcp_preflight([_harness()], [_loadout()], [_report()], tmp_path)
    assert failures == []
    assert served.calls == [str(tmp_path)]
    assert records == [{"harness": "claude-code/default", "loadout": "tools_eda",
                        "toolbase_loadout": "symbolic-eda", "cwd": str(tmp_path),
                        "ok": True, "served": 2, "error": None}]


def test_missing_tools_fail(tmp_path, served):
    served.set(["eda__a"])
    failures, records = _mcp_preflight([_harness()], [_loadout()], [_report()], tmp_path)
    assert "missing ['eda__b']" in failures[0]
    assert records[0]["served"] == 1


def test_server_error_fails(tmp_path, served):
    served.set(RuntimeError("serve exited"))
    failures, _ = _mcp_preflight([_harness()], [_loadout()], [_report()], tmp_path)
    assert "RuntimeError: serve exited" in failures[0]


def test_project_root_is_not_where_trials_serve_from(tmp_path, served):
    elsewhere = tmp_path / "elsewhere"
    served.set(RuntimeError("no loadout"))
    failures, _ = _mcp_preflight([_harness()], [_loadout(project_root=str(elsewhere))],
                                 [_report()], tmp_path / "run")
    assert served.calls == [str(tmp_path / "run")]
    assert "project_root" in failures[0] and "not used by MCP runtimes" in failures[0]


def test_in_process_runtimes_and_toolless_loadouts_are_skipped(tmp_path, served):
    core = SimpleNamespace(name="core_only", sources=[])
    failures, records = _mcp_preflight(
        [_harness("orchestral"), _harness()], [core], [], tmp_path)
    assert failures == [] and records == [] and served.calls == []
