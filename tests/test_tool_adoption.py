"""Domain-tool adoption is decided by what the arm SERVED, not by a naming
artefact.

THE BUG. Adoption was detected as `"__" in raw_name` -- the MCP namespace
prefix. The resolver serves `heptapod__ComputeDecayRate`, but the claude-code
runtime records the call unqualified as `ComputeDecayRate`, so the test failed
and the whole TOOLS section was suppressed: a run whose agent called
`ComputeDecayRate` twice reported `adoption 0/1 trials used domain tools`. That
is the single number a tools-vs-core campaign exists to report.

The authoritative set is already in `manifest["resolution"]` -- the tools each
loadout actually served -- so membership replaces the heuristic. It is also
per-loadout, which the string test could never be: `core_only` serves none, so
any call it makes is by definition not a domain tool.

The script-adoption hint list has the same shape of fault: it was hardcoded to
one benchmark's tools (`MesonDecay`, `HarvestForwardFlux`, `PythiaFromRunCard`),
so it could never detect a symbolic run importing the eda toolkit. Derived from
the served set, it follows whatever the arm serves.
"""

import pytest

from toolbench.cli import _served_domain_tools, _is_domain_call


MANIFEST = {
    "resolution": [
        {"loadout": "core_only", "sources": []},
        {"loadout": "tools_eda", "sources": [
            {"tools": ["heptapod__ComputeDecayRate",
                       "heptapod__ComputeCrossSection"]}]},
    ]
}


def test_served_tools_are_read_per_loadout():
    served = _served_domain_tools(MANIFEST)
    assert served["tools_eda"] == {"computedecayrate", "computecrosssection"}
    assert served["core_only"] == set()


def test_namespace_is_stripped_so_either_spelling_matches():
    """The resolver qualifies; claude-code does not. Both must count."""
    served = _served_domain_tools(MANIFEST)["tools_eda"]
    assert _is_domain_call("heptapod__ComputeDecayRate", served)
    assert _is_domain_call("ComputeDecayRate", served)


def test_core_tools_are_never_domain_calls():
    served = _served_domain_tools(MANIFEST)["tools_eda"]
    for name in ("Bash", "Write", "Read", "Edit", "Glob", "TodoWrite"):
        assert not _is_domain_call(name, served), name


def test_an_arm_serving_nothing_has_no_domain_calls():
    """core_only cannot adopt domain tools -- it has none. The old string test
    would have counted any namespaced name here."""
    served = _served_domain_tools(MANIFEST)["core_only"]
    assert not _is_domain_call("heptapod__ComputeDecayRate", served)
    assert not _is_domain_call("Bash", served)


def test_matching_is_case_insensitive():
    served = _served_domain_tools(MANIFEST)["tools_eda"]
    assert _is_domain_call("computedecayrate", served)


def test_a_manifest_without_resolution_yields_no_served_sets():
    """Best-effort: an old or partial manifest must not raise."""
    assert _served_domain_tools({}) == {}
    assert _served_domain_tools({"resolution": []}) == {}


def test_unknown_tool_is_not_a_domain_call():
    served = _served_domain_tools(MANIFEST)["tools_eda"]
    assert not _is_domain_call("SomeOtherTool", served)
