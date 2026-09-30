"""Rate lookup, effective input tokens, and the cost basis.

THREE THINGS THIS PINS.

1. RATES COME FROM ORCHESTRAL, not a table in this repo. toolbench's own tables
   had drifted to six models (`gpt-4o`, `gemini-2.0-flash`) and carried no
   cache-WRITE rate at all, which is the field that made a hand computation
   11% low. orchestral ships a `PricingModel` per provider, keyed by exact
   model id, with cache read and write rates and dated snapshots covered.

2. AN UNKNOWN MODEL MUST BE DISTINGUISHABLE FROM A FREE ONE. orchestral's
   `get_cost` returns 0.0 for a model it has never heard of, which is the same
   value a genuinely free model returns, so membership is checked directly
   rather than inferred from a zero.

3. EFFECTIVE INPUT AND COST SHARE ONE BASIS. Anthropic's table prices cache
   writes at the 5m tier (1.25x base); the claude CLI bills 1h writes at 2x,
   so a rate-derived figure understates a claude-code run. Where the runtime
   reports an authoritative cost, effective input is solved from THAT, so the
   two lines can never disagree with each other -- a reader who divides one by
   the other gets the rate actually charged.
"""

import pytest

from toolbench.core.token_economics import (
    effective_input_tokens, lookup_rates, no_cache_upper_bound,
    rate_derived_cost,
)


CLAUDE = "claude-haiku-4-5-20251001"
GPT = "gpt-5.5"
OSS = "openai/gpt-oss-120b"

# A real claude-code trial (tools_eda arm, decay_V_to_FFbar).
TRIAL = {"input": 105, "cache_read": 773_808, "cache_creation": 48_614,
         "output": 33_283}


# ---------------------------------------------------------------------------
# Rate lookup
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model", [CLAUDE, "claude-haiku-4-5", GPT, OSS])
def test_known_models_resolve(model):
    r = lookup_rates(model)
    assert r is not None, f"no rates for {model}"
    assert r["input"] > 0 and r["output"] > 0


def test_dated_snapshot_and_bare_alias_agree():
    """A campaign pins the dated id; both must price the same."""
    assert lookup_rates(CLAUDE)["input"] == lookup_rates("claude-haiku-4-5")["input"]


def test_unknown_model_is_none_not_zero():
    """orchestral's get_cost returns 0.0 here, which is indistinguishable from
    a free model. The lookup must say 'unknown' instead."""
    assert lookup_rates("no-such-model-xyz") is None


def test_rates_are_normalized_across_providers():
    """Anthropic uses cache_creation/cache_read_input_tokens; OpenAI and groq
    use cached_prompt_tokens and have no write rate at all. Callers must not
    have to know which."""
    for model in (CLAUDE, GPT, OSS):
        r = lookup_rates(model)
        assert set(r) >= {"input", "output", "cache_read", "cache_write"}


def test_providers_without_cache_writes_report_zero_not_missing():
    """OpenAI does not charge for cache writes. That is a rate of 0, a fact --
    not an absent field to be guessed at."""
    assert lookup_rates(GPT)["cache_write"] == 0.0
    assert lookup_rates(OSS)["cache_write"] == 0.0
    assert lookup_rates(CLAUDE)["cache_write"] > 0.0


# ---------------------------------------------------------------------------
# Effective input tokens
# ---------------------------------------------------------------------------

def test_effective_input_is_rate_weighted_in_uncached_units():
    r = lookup_rates(CLAUDE)          # 1.00 base, 0.1 read, 1.25 write
    eff = effective_input_tokens(TRIAL, r)
    expected = 105 + 0.10 * 773_808 + 1.25 * 48_614
    assert eff == pytest.approx(expected, rel=1e-9)


def test_effective_input_is_far_below_raw_when_cached():
    r = lookup_rates(CLAUDE)
    raw = TRIAL["input"] + TRIAL["cache_read"] + TRIAL["cache_creation"]
    assert effective_input_tokens(TRIAL, r) < 0.25 * raw


def test_effective_input_from_authoritative_cost_is_solved_not_assumed():
    """The claude CLI bills 1h cache writes at 2x base; orchestral's table
    prices the 5m tier at 1.25x. Given the reported cost, effective input is
    solved from it so the two summary lines share one basis."""
    r = lookup_rates(CLAUDE)
    reported = 0.3411                                  # the CLI's own figure
    eff = effective_input_tokens(TRIAL, r, authoritative_cost_usd=reported)
    # cost = (eff * r_in + output * r_out) / 1e6, solved for eff
    expected = (reported - TRIAL["output"] * r["output"] / 1e6) * 1e6 / r["input"]
    assert eff == pytest.approx(expected, rel=1e-6)
    # and it exceeds the 5m-tier estimate, because writes really cost 2x
    assert eff > effective_input_tokens(TRIAL, r)


def test_effective_input_needs_rates():
    assert effective_input_tokens(TRIAL, None) is None


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------

def test_rate_derived_cost_matches_a_hand_calculation():
    r = lookup_rates(CLAUDE)
    cost = rate_derived_cost(TRIAL, r)
    expected = (105 * 1.00 + 773_808 * 0.10
                + 48_614 * 1.25 + 33_283 * 5.00) / 1e6
    assert cost == pytest.approx(expected, rel=1e-9)


def test_no_cache_upper_bound_ignores_the_cache_fields():
    """Needs only base rates -- the most likely thing to be available -- and
    brackets the answer from above."""
    r = lookup_rates(CLAUDE)
    raw_in = TRIAL["input"] + TRIAL["cache_read"] + TRIAL["cache_creation"]
    expected = (raw_in * r["input"] + TRIAL["output"] * r["output"]) / 1e6
    assert no_cache_upper_bound(TRIAL, r) == pytest.approx(expected, rel=1e-9)


def test_upper_bound_is_above_the_actual_cost():
    r = lookup_rates(CLAUDE)
    assert no_cache_upper_bound(TRIAL, r) > rate_derived_cost(TRIAL, r)


def test_cost_helpers_need_rates():
    assert rate_derived_cost(TRIAL, None) is None
    assert no_cache_upper_bound(TRIAL, None) is None


def test_a_free_model_prices_at_zero_without_being_confused_for_unknown():
    """If a provider ever lists a 0-rate model, that is a price, and the rate
    record still exists -- unlike an unknown model, which has none."""
    free = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    assert rate_derived_cost(TRIAL, free) == 0.0
    assert lookup_rates("no-such-model-xyz") is None


# ---------------------------------------------------------------------------
# Cost provenance: a runtime's own figure must reach the summary, and must not
# be overwritten by a table estimate.
#
# The authoritative cost lived in trial.json but was None in trials.jsonl, so
# the cell aggregate -- which reads rows -- got nothing, and every subscription
# run reported no API-equivalent at all. Two parallel paths: the runner wrote
# the CLI's figure to the trial record, while the row was filled only by the
# rate-table loop, which returns None for any model the table lacks (every
# Claude model). The row must carry the runtime's value when there is one.
# ---------------------------------------------------------------------------

from toolbench.cli import _fill_api_equivalent_estimate


def test_a_runtimes_own_cost_is_not_overwritten_by_a_table_estimate():
    """The claude CLI reports Anthropic's own figure, including the 1h cache
    tier a rate table does not model. It wins."""
    row = {"model": "gpt-5.5", "estimated_api_equivalent_cost_usd": 0.3411,
           "input_tokens": 100, "output_tokens": 10,
           "cache_read_tokens": 0, "initial_input_tokens": 100}
    _fill_api_equivalent_estimate(row)
    assert row["estimated_api_equivalent_cost_usd"] == 0.3411


def test_a_table_estimate_fills_an_absent_value():
    """codex reports no cost field at all, so the table is the only source."""
    row = {"model": "gpt-5.5", "estimated_api_equivalent_cost_usd": None,
           "input_tokens": 1_000_000, "output_tokens": 0,
           "cache_read_tokens": 0, "initial_input_tokens": 1000}
    _fill_api_equivalent_estimate(row)
    assert row["estimated_api_equivalent_cost_usd"] == pytest.approx(5.00, rel=1e-6)


def test_an_unknown_model_leaves_the_value_absent():
    row = {"model": "no-such-model-xyz", "estimated_api_equivalent_cost_usd": None,
           "input_tokens": 1000, "output_tokens": 10,
           "cache_read_tokens": 0, "initial_input_tokens": 1000}
    _fill_api_equivalent_estimate(row)
    assert row["estimated_api_equivalent_cost_usd"] is None


# ---------------------------------------------------------------------------
# `opening request` must be omitted when it cannot be distinguished from the
# total.
#
# The figure is only meaningful if the runtime reports usage PER TURN, so the
# first event is genuinely the first request. codex emits one cumulative
# payload for the whole run, so its "first event" IS the total -- a codex trial
# rendered `108,097 opening request` beside `108,097 raw in`. Printing a total
# under that label is the same class of error as the claude-code aliasing it
# replaced, so the line is dropped rather than mislabelled.
# ---------------------------------------------------------------------------

from toolbench.reporting.summary_text import opening_request_is_meaningful


def test_opening_request_is_meaningful_when_below_the_total():
    tok = {"input": 100, "cache_read": 700_000, "cache_creation": 50_000,
           "initial_input": 76_240}
    assert opening_request_is_meaningful(tok)


def test_opening_request_is_dropped_when_it_equals_the_total():
    """The codex case: one cumulative usage payload."""
    tok = {"input": 17_345, "cache_read": 90_752, "cache_creation": 0,
           "initial_input": 108_097}
    assert not opening_request_is_meaningful(tok)


def test_opening_request_is_dropped_when_absent():
    assert not opening_request_is_meaningful(
        {"input": 10, "cache_read": 0, "cache_creation": 0, "initial_input": 0})


def test_opening_request_is_dropped_if_it_exceeds_the_total():
    """Cannot happen from a consistent report; if it does, the field is not
    what we think it is, so do not present it."""
    tok = {"input": 10, "cache_read": 10, "cache_creation": 0,
           "initial_input": 999}
    assert not opening_request_is_meaningful(tok)


# ---------------------------------------------------------------------------
# A zero charge must be stated as a fact, not left looking like missing data.
#
# A subscription cell rendered `cost $0.00 total  $0.00 / trial` -- two bare
# zeros indistinguishable from an unpopulated field, directly above a
# non-zero API-equivalent figure. The run header says SUBSCRIPTION, but a
# reader looking at one cell had no way to tell "nothing was charged" from
# "we do not know what this cost".
# ---------------------------------------------------------------------------

from toolbench.reporting.summary_text import _render_cost


def _cost_text(cell):
    return "\n".join(_render_cost(cell))


def test_a_zero_charge_says_nothing_was_charged():
    text = _cost_text({"n": 1, "mean_cost_usd": 0.0,
                       "mean_estimated_api_equivalent_cost_usd": 0.2675,
                       "mean_wall_clock_s": 99.0, "model": "gpt-5.5",
                       "mean_tokens": {"input": 17_345, "cache_read": 90_752,
                                       "cache_creation": 0, "output": 4_512,
                                       "initial_input": 108_097}})
    assert "charged" in text
    assert "no metered spend" in text


def test_a_metered_run_still_reports_its_real_charge():
    text = _cost_text({"n": 2, "mean_cost_usd": 0.51,
                       "mean_estimated_api_equivalent_cost_usd": None,
                       "mean_wall_clock_s": 10.0, "model": "gpt-5.5",
                       "mean_tokens": {"input": 100, "cache_read": 0,
                                       "cache_creation": 0, "output": 10,
                                       "initial_input": 50}})
    assert "$1.02 total" in text and "$0.51 / trial" in text
    assert "no metered spend" not in text


def test_unknown_cost_is_still_n_a():
    """No charge recorded AND no estimate: genuinely unknown, say so."""
    text = _cost_text({"n": 1, "mean_cost_usd": None,
                       "mean_estimated_api_equivalent_cost_usd": None,
                       "mean_wall_clock_s": 1.0, "model": "x",
                       "mean_tokens": {}})
    assert "n/a" in text
