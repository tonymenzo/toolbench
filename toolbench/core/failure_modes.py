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
  - `SESSION_LIMIT`       — a subscription coding-agent CLI (claude-code /
                            codex) refused the request because the logged-in
                            account hit its plan's session / usage quota
                            (e.g. "You've hit your session limit · resets
                            4:20am"). This is a property of the ACCOUNT's
                            plan state at that moment, not of the system
                            under test, so it is excluded from the scored
                            population entirely (see `EXCLUDED_FROM_METRICS`)
                            rather than recorded as a score-0 measurement.
                            The runner aborts the remaining queue on the
                            first one (all trials would fail identically
                            until the quota resets); `resume` re-runs them.
  - `TRANSIENT_API_ERROR` — a transient transport/server fault reaching
                            the provider (connect/read timeout, dropped
                            connection, HTTP 5xx) that survived the
                            runner's bounded backoff retries. Operational,
                            not a capability failure — a single endpoint
                            blip must not contaminate a whole campaign.
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
TRANSIENT_API_ERROR     = "TRANSIENT_API_ERROR"
SESSION_LIMIT           = "SESSION_LIMIT"
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
    TRANSIENT_API_ERROR, SESSION_LIMIT, GRADE_ERROR,
})


# Failure modes that represent "no measurement was taken" rather than a
# capability outcome, and are therefore dropped from the scored population
# (reach / pass@k / pass^k / stage funnel / paired deltas) instead of being
# folded in as a score-0 trial. A subscription session/usage-quota
# termination is a property of the account's plan state, not of the system
# under test — counting it as a zero would misreport the system's capability.
# The trials are still RECORDED on disk (for provenance and so `resume` can
# re-run them) and surfaced in the summary as an explicit excluded count; they
# just never enter the metrics. Deliberately scoped to `SESSION_LIMIT` for now
# — the other operational modes (RATE_LIMITED / TRANSIENT_API_ERROR /
# CONTEXT_LENGTH_EXCEEDED) retain their existing in-population behaviour; add
# them here only as a considered, separately-reviewed change.
EXCLUDED_FROM_METRICS: frozenset[str] = frozenset({
    SESSION_LIMIT,
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
