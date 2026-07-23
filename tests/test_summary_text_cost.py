from toolbench.reporting.summary_text import _render_cost, _render_run_header


def test_zero_spend_is_not_inferred_to_be_local_litellm():
    lines = _render_run_header(
        {"total_spent_usd": 0.0, "cells": []},
        {"run_id": "r", "task": "t", "n_per_cell": 1},
    )
    rendered = "\n".join(lines)
    assert "$0.00 spent" in rendered
    assert "local via litellm" not in rendered


def test_subscription_estimate_is_explicitly_not_actual_spend():
    lines = _render_run_header(
        {
            "total_spent_usd": 0.0,
            "estimated_api_equivalent_cost_usd": 15.515566,
            "cells": [],
        },
        {"run_id": "r", "task": "t", "n_per_cell": 1, "max_cost_usd": 1.0},
    )
    rendered = "\n".join(lines)
    assert "$0.00 spent" in rendered
    assert "$15.52 API-equivalent (estimated, subscription)" in rendered


def test_cell_renders_subscription_estimate():
    lines = _render_cost({
        "n": 2,
        "mean_cost_usd": None,
        "mean_estimated_api_equivalent_cost_usd": 3.25,
        "mean_wall_clock_s": 1,
    })
    assert "$6.50 total" in "\n".join(lines)
    assert "(estimated, subscription)" in "\n".join(lines)
