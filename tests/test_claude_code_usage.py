"""Token accounting for the claude-code runtime.

`usage.iterations` is NOT one entry per message: the CLI emits a single entry
there whatever the turn count, while the top-level `usage` block carries the
aggregate. Summing the iterations therefore undercounts, and worse as the
session grows. Measured against claude-code 2.1.284:

    turns=5   true in= 69,719  out=  834    iter-sum in=23,718  out= 90
    turns=6   true in= 93,411  out=1,168    iter-sum in=23,975  out=136

input low by 2.9-3.9x, output by 8.6-9.3x. Because the error scales with turn
count it cannot be corrected after the fact, and it differs BETWEEN ARMS of an
ablation: an arm reaching its answer in fewer turns takes a different
systematic discount from the arm it is compared against, which is how a
token-efficiency claim gets silently inverted.

It went unnoticed because the old code fell back to `[usage]` when
`iterations` was absent — on a CLI build that omitted the key it read the
aggregate and was right, so one field meant the true total on some runs and a
fraction of it on others, with nothing recorded to tell them apart.
"""

from pathlib import Path

from toolbench.core.runtime import ClaudeCodeAgent


def _agent(tmp_path: Path) -> ClaudeCodeAgent:
    return ClaudeCodeAgent(system_prompt="system", sandbox_dir=str(tmp_path),
                           model="claude-test")


# A real 5-turn result event, trimmed to the usage fields. Note `iterations`
# holds ONE entry that accounts for a fraction of the aggregate.
FIVE_TURN_RESULT = {
    "usage": {
        "input_tokens": 26,
        "cache_read_input_tokens": 59_788,
        "cache_creation_input_tokens": 9_954,
        "output_tokens": 751,
        "iterations": [
            {"input_tokens": 8,
             "cache_read_input_tokens": 23_470,
             "cache_creation_input_tokens": 173,
             "output_tokens": 117,
             "type": "message"},
        ],
    },
    "total_cost_usd": 0.25,
}


def test_cumulative_counts_come_from_the_aggregate_not_the_iterations(tmp_path):
    a = _agent(tmp_path)
    a._accumulate_usage(FIVE_TURN_RESULT)
    tk = a.token_usage
    assert tk["input"] == 26
    assert tk["cache_read"] == 59_788
    assert tk["cache_creation"] == 9_954
    assert tk["output"] == 751
    # The bug: these were 8 / 23,470 / 173 / 117.
    assert tk["output"] != 117, "summed the single iteration instead of the aggregate"


def test_total_input_is_the_sum_of_the_three_input_kinds(tmp_path):
    a = _agent(tmp_path)
    a._accumulate_usage(FIVE_TURN_RESULT)
    tk = a.token_usage
    assert tk["input"] + tk["cache_read"] + tk["cache_creation"] == 69_768


def test_initial_input_is_the_opening_request_not_the_total(tmp_path):
    """The first iteration IS the opening request (system prompt + tool
    schemas + task) — confirmed empirically by its stability across session
    lengths, 23,718 at five turns and 23,975 at six, where a last-message
    reading would have grown. It must NOT be the cumulative total."""
    a = _agent(tmp_path)
    a._accumulate_usage(FIVE_TURN_RESULT)
    tk = a.token_usage
    assert tk["initial_input"] == 8 + 23_470 + 173
    total_in = tk["input"] + tk["cache_read"] + tk["cache_creation"]
    assert tk["initial_input"] < total_in


def test_multiple_invocations_accumulate(tmp_path):
    """One trial can span several `claude -p` calls (the resume loop), and each
    contributes a result event."""
    a = _agent(tmp_path)
    a._accumulate_usage(FIVE_TURN_RESULT)
    a._accumulate_usage(FIVE_TURN_RESULT)
    tk = a.token_usage
    assert tk["output"] == 2 * 751
    assert tk["cache_read"] == 2 * 59_788
    # initial_input is the FIRST request only, so a second call must not move it.
    assert tk["initial_input"] == 8 + 23_470 + 173
    assert tk["cost"] == 0.5


def test_usage_without_an_iterations_key_still_reads_the_aggregate(tmp_path):
    """Older CLI builds omitted `iterations`. That path must keep working, and
    initial_input then falls back to the aggregate — the best available
    reading when no per-message breakdown is offered."""
    a = _agent(tmp_path)
    a._accumulate_usage({"usage": {"input_tokens": 100,
                                   "cache_read_input_tokens": 900,
                                   "cache_creation_input_tokens": 50,
                                   "output_tokens": 40}})
    tk = a.token_usage
    assert (tk["input"], tk["cache_read"], tk["cache_creation"], tk["output"]) \
        == (100, 900, 50, 40)
    assert tk["initial_input"] == 1050


def test_empty_usage_is_a_no_op(tmp_path):
    a = _agent(tmp_path)
    a._accumulate_usage({})
    a._accumulate_usage({"usage": {}})
    assert a.token_usage["input"] == 0
    assert a.token_usage["output"] == 0
    assert a.token_usage["initial_input"] == 0
