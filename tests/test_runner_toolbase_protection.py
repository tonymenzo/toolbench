from pathlib import Path

from toolbench.core.runner import _toolbase_protected_paths


def test_toolbase_and_editable_sources_are_protected(tmp_path, monkeypatch):
    toolbase_home = tmp_path / ".toolbase"
    slot = toolbase_home / "cache" / "physics-kit" / "editable"
    slot.mkdir(parents=True)
    source = tmp_path / "physics-kit-source"
    source.mkdir()
    runtime_data = tmp_path / "physics-kit-runtime"
    runtime_data.mkdir()
    (slot / ".install_meta.yaml").write_text(
        "editable: true\n"
        f"source_path: {source}\n"
    )
    monkeypatch.setenv("TOOLBASE_HOME", str(toolbase_home))
    config_dir = toolbase_home / "config"
    config_dir.mkdir()
    (config_dir / "physics-kit.yaml").write_text(
        f"base_directory: {runtime_data}\n"
    )

    paths = _toolbase_protected_paths()

    assert str(toolbase_home.resolve()) in paths
    assert str(source.resolve()) in paths
    assert str(runtime_data.resolve()) in paths
