"""--campaign: a run lands in runs/campaigns/NAME/, keeps working with the
nested run id, and the dashboard can open the whole campaign."""

import json
from pathlib import Path

import pytest

import toolbench.cli as cli
import toolbench.dashboard as dashboard
from tests.helpers import GEOMETRY_DIR

pytest.importorskip("orchestral")


@pytest.fixture
def runs_base(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_OUTPUT_BASE", tmp_path)
    return tmp_path / "runs"


def _dry_run(*extra):
    return cli.main(["run", "--benchmark", str(GEOMETRY_DIR), "--loadouts", "full_local",
                     "--harness", "orchestral/anthropic", "--model", "stub", "--n", "1",
                     "--max-cost-usd", "0", "--dry-run", *extra])


def test_run_lands_in_the_campaign_and_records_it(runs_base, capsys):
    assert _dry_run("--campaign", "symbolic_2026-10") == 0
    (run_dir,) = (runs_base / "campaigns" / "symbolic_2026-10").iterdir()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["campaign"] == "symbolic_2026-10"
    assert f"--run-id campaigns/symbolic_2026-10/{run_dir.name}" in capsys.readouterr().out
    # The nested id works for resume (complete run: re-finalizes, exits 0).
    rid = f"campaigns/symbolic_2026-10/{run_dir.name}"
    assert cli.main(["resume", "--run-id", rid]) == 0


def test_run_without_campaign_is_unchanged(runs_base):
    assert _dry_run() == 0
    assert not (runs_base / "campaigns").exists()
    (run_dir,) = runs_base.iterdir()
    assert json.loads((run_dir / "manifest.json").read_text())["campaign"] is None


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", ".hidden", "sp ace"])
def test_campaign_name_must_be_one_safe_component(runs_base, bad):
    assert _dry_run("--campaign", bad) == 2
    assert not runs_base.exists() or not any(runs_base.rglob("manifest.json"))


def test_dashboard_campaign_opens_the_campaign_directory(runs_base, monkeypatch):
    served = []
    monkeypatch.setattr(dashboard, "serve", lambda root, **kw: served.append(Path(root)))
    (runs_base / "campaigns" / "c1").mkdir(parents=True)
    assert cli.main(["dashboard", "--campaign", "c1"]) == 0
    assert served == [runs_base.resolve() / "campaigns" / "c1"]
    assert cli.main(["dashboard", "--campaign", "missing"]) == 1
    assert cli.main(["dashboard", "x", "--campaign", "c1"]) != 0
