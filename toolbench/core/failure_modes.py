"""
Canonical failure-mode labels for graded trials.

Each completed trial carries exactly one `failure_mode` string. The
labels here are the closed vocabulary the rest of the harness expects;
downstream code (regrade, summary aggregation, plot legends, crash
classifier) keys off these constants so a rename here propagates
without ad-hoc string searches.

Categories
----------

  - `NONE`                — all rubric stages passed.
  - `UNKNOWN`             — failure_mode was missing or unrecognized.
  - `INCOMPLETE_AT_<ID>`  — every stage up to but not including the
                            named one passed; the named one failed.
                            Generated via `incomplete_at(stage_id)`.

Hard process failures (the trial never produced a gradeable
trajectory):

  - `AGENT_CRASH`         — agent.run() raised an exception we did
                            not classify more specifically.
  - `MODEL_FORMAT_CRASH`  — provider returned malformed tool-call
                            JSON (see crash_classifier).
  - `CONTEXT_LENGTH_EXCEEDED` — the conversation grew past the model's
                            context window; the provider rejected the
                            request (HTTP 400 / context_length_exceeded).
                            Operational, not a capability failure.
  - `RATE_LIMITED`        — the provider throttled or shed the request
                            (HTTP 429 / overloaded) and the runner's
                            bounded backoff retries were exhausted.
                            Operational, not a capability failure.
  - `MODEL_STOPPED_EARLY` — model produced an assistant message
                            instead of issuing the next expected tool
                            call (no exception, but no progress).
  - `GRADE_ERROR`         — judge raised while scoring an otherwise
                            completed trajectory.

`HARD_PROCESS_FAILURES` is the subset that `regrade` leaves untouched:
the process really did fail and re-running the rubric can't change
that. `MODEL_STOPPED_EARLY` is *not* in this set because regrade can
re-derive whether the trajectory's artifacts now satisfy the rubric.
"""


# All-stages-passed.
NONE = "NONE"

# Sentinel for missing / unrecognized labels.
UNKNOWN = "UNKNOWN"

# Hard process failures.
AGENT_CRASH             = "AGENT_CRASH"
MODEL_FORMAT_CRASH      = "MODEL_FORMAT_CRASH"
CONTEXT_LENGTH_EXCEEDED = "CONTEXT_LENGTH_EXCEEDED"
RATE_LIMITED            = "RATE_LIMITED"
MODEL_STOPPED_EARLY     = "MODEL_STOPPED_EARLY"
GRADE_ERROR             = "GRADE_ERROR"

# Prefix for the dynamic `INCOMPLETE_AT_<STAGE_ID>` family.
INCOMPLETE_AT_PREFIX = "INCOMPLETE_AT_"

# Process failures that `regrade` should leave untouched — re-running
# the rubric can't undo a crash that prevented the agent from ever
# producing gradeable artifacts. `MODEL_STOPPED_EARLY` is intentionally
# *not* here: stopping early can still leave gradeable artifacts and
# regrade may legitimately upgrade it to NONE / INCOMPLETE_AT_<id>.
HARD_PROCESS_FAILURES: frozenset[str] = frozenset({
    AGENT_CRASH, MODEL_FORMAT_CRASH, CONTEXT_LENGTH_EXCEEDED, RATE_LIMITED,
    GRADE_ERROR,
})


def incomplete_at(stage_id: str) -> str:
    """Build the `INCOMPLETE_AT_<STAGE_ID>` label for the named stage.

    The stage id is upper-cased; downstream comparisons are case-
    sensitive, so callers must use this helper rather than constructing
    the string by hand.
    """
    return f"{INCOMPLETE_AT_PREFIX}{stage_id.upper()}"


def is_incomplete_at(failure_mode: str) -> bool:
    """True iff `failure_mode` is an `INCOMPLETE_AT_<...>` label."""
    return failure_mode.startswith(INCOMPLETE_AT_PREFIX)
