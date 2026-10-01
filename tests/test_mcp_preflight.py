"""MCP preflight (#42): it must fail when there is nothing to verify against,
and it must start the server where trials serve from (the run directory)."""

from types import SimpleNamespace

import pytest

import toolbench.cli as cli
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
    monkeypatch.setattr(cli, "_toolbase_project", lambda start: "/proj")
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
                        "toolbase_loadout": "symbolic-eda", "phase": "run",
                        "cwd": str(tmp_path), "toolbase_project": "/proj",
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


def test_project_root_is_refused_for_mcp_runtimes(tmp_path, served):
    # Resolution honours project_root but `toolbase serve` cannot, so the
    # trials would serve a different project: refuse rather than diverge.
    failures, records = _mcp_preflight(
        [_harness()], [_loadout(project_root=str(tmp_path / "elsewhere"))],
        [_report()], tmp_path / "run")
    assert served.calls == []
    assert "not supported by the claude_code runtime" in failures[0]
    assert records[0]["ok"] is False


def test_project_is_announced_once(tmp_path, served, capsys):
    two = [_loadout(name="tools_eda"), _loadout(name="tools_eda")]
    _mcp_preflight([_harness()], two, [_report()], tmp_path)
    assert capsys.readouterr().out.count("toolbase project: /proj") == 1


def test_in_process_runtimes_and_toolless_loadouts_are_skipped(tmp_path, served):
    core = SimpleNamespace(name="core_only", sources=[])
    failures, records = _mcp_preflight(
        [_harness("orchestral"), _harness()], [core], [], tmp_path)
    assert failures == [] and records == [] and served.calls == []


def test_toolbase_project_skips_the_home_config_dir(tmp_path):
    # toolbase's own lookup: a run under $HOME must not resolve to ~/.toolbase.
    pytest.importorskip("toolbase.envs")
    proj = tmp_path / "proj"
    (proj / ".toolbase").mkdir(parents=True)
    run = proj / "runs" / "r1"
    run.mkdir(parents=True)
    assert cli._toolbase_project(run) == str(proj.resolve())


# ── resume runs the same preflight ──────────────────────────────────

def test_resume_aborts_on_preflight_failure_and_leaves_the_run_untouched(tmp_path, monkeypatch):
    from tests.test_resume import _resume_args, _row, _seed_run
    monkeypatch.setattr(cli, "_OUTPUT_BASE", tmp_path)
    run_id, run_dir = _seed_run(tmp_path, rows=[_row(1001, failure_mode="resolution_error")],
                                max_cost_usd=1.0)
    before = (run_dir / "trials.jsonl").read_text()
    seen = {}

    def fake(harnesses, loadouts, reports, rdir, *, phase):
        seen.update(phase=phase, rdir=rdir)
        return ["h/tools (toolbase loadout x): resolution failed"], [{"phase": phase, "ok": False}]
    monkeypatch.setattr(cli, "_mcp_preflight", fake)
    assert cli.cmd_resume(_resume_args(run_id)) == 2
    assert seen == {"phase": "resume", "rdir": run_dir}
    # Not even the retryable row was dropped: nothing on disk changed but the record.
    assert (run_dir / "trials.jsonl").read_text() == before
    manifest = __import__("json").loads((run_dir / "manifest.json").read_text())
    assert manifest["mcp_preflight"] == [{"phase": "resume", "ok": False}]
