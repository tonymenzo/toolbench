"""prompts.json + sandbox_init.json: the trial's starting conditions."""

import json
import os

import pytest

from toolbench.core.trial_start import (PROMPTS_FILE, SANDBOX_INIT_FILE,
                                        record_trial_start, snapshot_tree)
from tests.helpers import GEOMETRY_DIR


def test_snapshot_lists_files_and_dirs_without_following_links(tmp_path):
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "run.cmnd").write_text("abc")
    (tmp_path / "top.txt").write_text("x")
    os.symlink(tmp_path / "cards", tmp_path / "link")
    snap = snapshot_tree(tmp_path)
    assert snap["dirs"] == ["cards"]
    assert snap["files"]["cards/run.cmnd"][0] == 3
    assert set(snap["files"]) == {"cards/run.cmnd", "top.txt", "link"}
    assert snap["truncated"] is False


def test_record_writes_both_files(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "seed.txt").write_text("s")
    record_trial_start(tmp_path, sandbox, system_prompt="SYS", user_prompt="USER")
    assert json.loads((tmp_path / PROMPTS_FILE).read_text()) == {"system": "SYS", "user": "USER"}
    assert "seed.txt" in json.loads((tmp_path / SANDBOX_INIT_FILE).read_text())["files"]


def test_record_never_raises(tmp_path):
    record_trial_start(tmp_path / "missing", tmp_path, system_prompt="", user_prompt="")


def test_dry_run_records_trial_start(tmp_path):
    pytest.importorskip("orchestral")
    import toolbench.cli as cli
    orig = cli._OUTPUT_BASE
    try:
        cli._OUTPUT_BASE = tmp_path
        assert cli.main(["run", "--benchmark", str(GEOMETRY_DIR), "--loadouts", "full_local",
                         "--harness", "orchestral/anthropic", "--model", "stub",
                         "--n", "1", "--max-cost-usd", "0", "--dry-run"]) == 0
    finally:
        cli._OUTPUT_BASE = orig
    (trial,) = (tmp_path / "runs").glob("*/trials/*")
    prompts = json.loads((trial / PROMPTS_FILE).read_text())
    assert prompts["system"] and prompts["user"]
    assert (trial / SANDBOX_INIT_FILE).is_file()
