from toolbench.cli import _runtime_version_label


def test_codex_version_keeps_numeric_component():
    assert _runtime_version_label("codex", "codex-cli 0.145.0") == "0.145.0"


def test_claude_version_keeps_leading_numeric_component():
    assert _runtime_version_label(
        "claude_code", "2.1.218 (Claude Code)"
    ) == "2.1.218"
