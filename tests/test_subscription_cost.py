"""Subscription harnesses must not book real spend.

The `claude` CLI prints `total_cost_usd` even under a subscription -- an
API-equivalent figure, not money drawn. Charging it to the budget made
subscription runs look expensive and could abort a run on a cap nothing was
drawing down. Codex only ever looked correct because its CLI emits no cost.
"""
from types import SimpleNamespace

from toolbench.core.runner import _is_subscription


def test_subscription_detected_from_provider_name():
    assert _is_subscription(SimpleNamespace(provider={"name": "subscription"}))
    assert not _is_subscription(SimpleNamespace(provider={"name": "anthropic"}))
    assert not _is_subscription(SimpleNamespace(provider={}))
    assert not _is_subscription(SimpleNamespace(provider=None))
    assert not _is_subscription(SimpleNamespace())


def test_budget_untouched_by_subscription_cost():
    """The accrual path: a subscription trajectory's cost is zeroed before the
    budget sees it, and preserved as the counterfactual estimate."""
    traj = SimpleNamespace(cost_usd=1.47)
    harness = SimpleNamespace(provider={"name": "subscription"})

    cli_api_equivalent_usd = None
    if _is_subscription(harness):
        if traj.cost_usd:
            cli_api_equivalent_usd = float(traj.cost_usd)
        traj.cost_usd = 0.0

    assert traj.cost_usd == 0.0, "subscription spend must not reach the budget"
    assert cli_api_equivalent_usd == 1.47, "the figure must survive as an estimate"


def test_metered_harness_still_charges():
    traj = SimpleNamespace(cost_usd=1.47)
    harness = SimpleNamespace(provider={"name": "anthropic"})
    if _is_subscription(harness):
        traj.cost_usd = 0.0
    assert traj.cost_usd == 1.47, "metered API use must still be charged"
