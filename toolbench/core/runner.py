"""
TrialRunner — runs one (benchmark, harness, loadout, model, seed) trial
end-to-end.

Builds the agent's tool list (harness core ∪ loadout toolkit) via
`tool_resolver.build_agent_tools`, constructs an Orchestral `Agent`, runs
it, captures the trajectory via `TrajectoryHook`, extracts cost and token
usage from `agent.context`, grades the result with a benchmark-aware
`RuleJudge`, and persists a per-trial record under
`runs/<run_id>/trials/<trial_id>/`.
"""

import datetime
import shutil
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from orchestral.tools.hooks import TruncateOutputHook

from .artifact_policy import DEFAULT_POLICY, ArtifactPolicy
from .budget import Budget, BudgetExceeded
from .crash_classifier import classify_crash
from .failure_modes import (
    AGENT_CRASH, GRADE_ERROR, MODEL_FORMAT_CRASH, MODEL_STOPPED_EARLY, NONE,
    RATE_LIMITED, TRANSIENT_API_ERROR,
)
from .checks import (
    load_benchmark_checks, load_benchmark_roles, merged_registry, merged_roles,
    missing_presence,
)
from .harness import Harness
from .judge import Judge, RuleJudge
from .litellm_pricing import cost_from_proxy
from .llm_factory import StubLLM, build_llm
from .loadout import Loadout
from .metrics import cost_usd, per_trial_reach
from .runtime import build_agent
from .skills import prepare_skills, skill_names
from .store import write_json, write_jsonl_gz
from .task import Grade, Task
from .tool_resolver import build_agent_tools, release_sources
from .trajectory import Trajectory, TrajectoryHook, make_agent_display_hook
from .variant import Variant
from toolbench.reporting.transcript import render_footer, render_header


DEFAULT_SYSTEM_PROMPT = (
    "You are an expert assistant. Solve the task carefully and verify your work."
)


# Harness `provider:` keys that configure toolbench itself rather than
# the model request. Everything else in the provider block is forwarded
# as a request parameter on every model call (e.g. max_tokens).
_PROVIDER_CONTROL_KEYS = ("name", "cache_bust")

# Tool-call argument keys that carry agent-authored source code. The
# code-executing core tools (RunPythonTool and friends) run an
# *ephemeral temp file outside the sandbox*, so the scripts the agent
# actually wrote during the task never land in the sandbox and would be
# lost at cleanup. We lift them out of the trajectory into
# artifacts/scripts/.
CODE_ARG_KEYS = ("code", "script", "source", "program")


def _warn(prefix: str, exc: BaseException) -> None:
    """Emit a one-line stderr breadcrumb for a non-fatal failure.

    Used in best-effort code paths (artifact preservation, token-usage
    extraction) where we swallow the exception so the trial completes,
    but want to leave a queryable record. Keep the line shape uniform
    so adopters can grep:

        warning: <prefix>: <ExcType>: <msg>
    """
    print(f"warning: {prefix}: {type(exc).__name__}: {exc}",
          file=sys.stderr)


@dataclass
class TrialResult:
    trial_id: str
    ok: bool
    score: float
    grade: Grade
    trajectory: Trajectory
    wall_clock_s: float
    cost_usd: float | None
    aborted_by_budget: bool
    error: str | None
    attempts: int = 1            # 1 + number of MODEL_FORMAT_CRASH retries
    nudges: int = 0              # presence-gated continue-nudges issued
    rate_limit_retries: int = 0  # RATE_LIMITED backoff resumes used
    transient_retries: int = 0   # TRANSIENT_API_ERROR backoff resumes used


# Hard fallbacks for the orchestral loop knobs, used only when a harness's
# `loop:` block omits them (and no CLI override is given).
_LOOP_DEFAULTS = {"max_iterations": 150, "max_format_retries": 3,
                  "continue_nudges": 0, "max_rate_limit_retries": 3,
                  "max_transient_retries": 4}

# Backoff schedule for RATE_LIMITED resumes (seconds per retry; the last
# entry repeats if a harness allows more retries than entries). Generous
# on purpose: a 429/529 means the provider wants us to back off, and a
# wasted minute is far cheaper than a wasted trial.
_RATE_LIMIT_BACKOFF_S = (10, 30, 60)

# Backoff schedule for TRANSIENT_API_ERROR resumes. An unreachable or
# 5xx-ing endpoint often stays down for tens of seconds; back off harder
# and for longer than the throttle schedule so a brief outage doesn't
# wipe out a campaign (the failure mode that zeroed five colliderbench
# tasks on 2026-06-13 when the endpoint went unreachable mid-run).
_TRANSIENT_BACKOFF_S = (15, 45, 90, 120)

# Indirection so tests can patch the sleep without touching the global
# time module (parallel trials sleep in their own worker threads).
_sleep = time.sleep


class TrialRunner:
    def __init__(self, judge: Judge | None = None,
                 max_iterations: int | None = None,
                 verbose: bool = False,
                 litellm_pricing: dict | None = None,
                 max_format_retries: int | None = None,
                 max_continue_nudges: int | None = None,
                 max_rate_limit_retries: int | None = None,
                 max_transient_retries: int | None = None,
                 llm_judge: Judge | None = None):
        self.judge = judge
        # Optional LLM judge run SERIALLY AFTER the authoritative rule grade,
        # against the finished sandbox (mirrors the post-completion UX turn:
        # opt-in, its result attached additively in grade.alt_grades, its
        # failures swallowed so a judge hiccup never sinks a graded trial).
        # Built once per run because a run is a single benchmark, so its
        # benchmark_dir and judge model are constant across trials. None on the
        # common rule-only path.
        self.llm_judge = llm_judge
        self.verbose = verbose
        # Loop knobs are OVERRIDES: None means "defer to the harness's `loop:`
        # block" (the source of truth, runtime-specific to orchestral); a
        # concrete value here (a CLI flag, or an explicit test value) wins.
        # Resolved per-trial against `harness.loop` in `run_trial`:
        #   - max_iterations: agent.run round-trip cap.
        #   - max_format_retries: MODEL_FORMAT_CRASH resumes (nondeterministic
        #     serialization error; safe to retry).
        #   - continue_nudges: presence-gated resumes when the model self-
        #     terminates with a required deliverable still absent. Default 0
        #     (strict autonomy); never fires when the deliverable exists, so
        #     no oracle leakage.
        #   - max_rate_limit_retries: RATE_LIMITED resumes (provider 429/529;
        #     operational, retried with backoff so throttling doesn't get
        #     recorded as a model failure).
        #   - max_transient_retries: TRANSIENT_API_ERROR resumes (connect/
        #     read timeout, dropped connection, HTTP 5xx; operational, so a
        #     brief endpoint outage doesn't get recorded as a model failure
        #     or contaminate a campaign).
        self.max_iterations = max_iterations
        self.max_format_retries = max_format_retries
        self.max_continue_nudges = max_continue_nudges
        self.max_rate_limit_retries = max_rate_limit_retries
        self.max_transient_retries = max_transient_retries
        # Optional snapshot of {model_name: {input, cache_read, output}}
        # captured from the litellm proxy at run start. Used as a cost
        # fallback when the proxy doesn't populate `usage.cost`.
        self.litellm_pricing = litellm_pricing

    def _resolve_loop(self, harness: Harness) -> dict:
        """Effective loop config = CLI/explicit override, else harness.loop,
        else hard default.

        Any `loop:` key the runner does not consume gets a loud stderr
        warning — a knob that's written in the harness but governs
        nothing would otherwise mislabel the run's conditions silently.
        """
        loop = getattr(harness, "loop", None) or {}
        unconsumed = sorted(set(loop) - set(_LOOP_DEFAULTS))
        if unconsumed:
            print(f"warning: harness {harness.id!r}: loop key(s) {unconsumed} "
                  f"are not consumed by the runner and have NO effect. "
                  f"Consumed keys: {sorted(_LOOP_DEFAULTS)}.",
                  file=sys.stderr)
        out = {}
        for key, override in (
            ("max_iterations", self.max_iterations),
            ("max_format_retries", self.max_format_retries),
            ("continue_nudges", self.max_continue_nudges),
            ("max_rate_limit_retries", self.max_rate_limit_retries),
            ("max_transient_retries", self.max_transient_retries),
        ):
            if override is not None:
                out[key] = int(override)
            else:
                out[key] = int(loop.get(key, _LOOP_DEFAULTS[key]))
        return out

    def run_trial(self, model_cfg: dict, benchmark: Task, harness: Harness,
                  loadout: Loadout, variant: Variant, seed: int, trial_id: str,
                  run_dir: Path, budget: Budget) -> TrialResult:
        run_dir = Path(run_dir)
        trial_dir = run_dir / "trials" / trial_id
        sandbox_dir = trial_dir / "sandbox"
        trial_dir.mkdir(parents=True, exist_ok=True)
        sandbox_dir.mkdir(exist_ok=True)

        # Loop config: harness `loop:` block is the source of truth, with any
        # CLI/explicit override winning (resolved per-trial so a multi-harness
        # run honors each harness's own loop settings).
        loop_cfg = self._resolve_loop(harness)
        max_iterations = loop_cfg["max_iterations"]
        max_format_retries = loop_cfg["max_format_retries"]
        max_continue_nudges = loop_cfg["continue_nudges"]
        max_rate_limit_retries = loop_cfg["max_rate_limit_retries"]
        max_transient_retries = loop_cfg["max_transient_retries"]

        # Per-variant scaffolding: the variant owns the prompts and the
        # sandbox seed (the things that change between difficulty rungs);
        # the benchmark family supplies the invariant rubric and checks.
        variant.setup_workspace(str(sandbox_dir))

        llm = build_llm(
            provider=model_cfg["provider"],
            model=model_cfg.get("model"),
            dry_run=model_cfg.get("dry_run", False),
        )
        # Harness-declared request params (max_tokens, temperature, ...):
        # everything in the provider block except toolbench's own control
        # keys. Forwarded on every agent.run() call — orchestral passes
        # them through to the provider API per request.
        llm_kwargs = {k: v for k, v in (harness.provider or {}).items()
                      if k not in _PROVIDER_CONTROL_KEYS}

        # Tools = harness core ∪ loadout toolkit.
        tools, tool_report = build_agent_tools(harness, loadout, str(sandbox_dir))

        # Benchmark-aware judge: built-in checks + the benchmark's local
        # checks module, with `reference:` paths anchored at the benchmark's
        # search dirs (its own dir, plus the parent's when it extends one).
        checks_path = getattr(benchmark, "checks_module_path", lambda: None)()
        registry = merged_registry(load_benchmark_checks(checks_path))
        roles = merged_roles(load_benchmark_roles(checks_path))
        benchmark_dir = (getattr(benchmark, "search_dirs", None)
                         or str(getattr(benchmark, "BENCHMARK_DIR", "")))
        judge = RuleJudge(registry=registry, benchmark_dir=benchmark_dir)
        prompt_base = variant.read_user_prompt()
        system_prompt = variant.read_system_prompt() or DEFAULT_SYSTEM_PROMPT
        # Loadout skills: tool-use guidance that travels with the loadout.
        # on_demand skills land in <sandbox>/skills/ with a prompt pointer;
        # inline skills are embedded. Strict: a declared-but-missing skill
        # raises here rather than silently running a thinner arm.
        skills_addendum = prepare_skills(loadout.skills, sandbox_dir,
                                         loadout_name=loadout.name)
        if skills_addendum:
            system_prompt = f"{system_prompt}\n\n{skills_addendum}"

        trajectory = Trajectory()
        traj_hook = TrajectoryHook(
            trajectory, verbose=self.verbose,
            log_path=trial_dir / "console.log",
        )
        hooks = [TruncateOutputHook(max_length=10000), traj_hook]

        # Styled trial header to console.log + stdout. `condition` here is the
        # human-facing per-trial label (loadout + variant); the cell-key string
        # used by aggregate() is built in the CLI.
        header_condition = f"{loadout.name} / {variant.name}"
        header = render_header(
            trial_id=trial_id, model=model_cfg.get("model", "?"),
            provider=model_cfg.get("provider", "?"), task=benchmark.name,
            seed=seed, condition=header_condition,
            start_dt=datetime.datetime.now(),
        )
        if self.verbose:
            print(header, flush=True)
        traj_hook.write_to_log(header)

        # Cache-busting nonce, opt-in via `provider: {cache_bust: true}`.
        # Needed ONLY for routes with *response-level* caching (e.g. a
        # LiteLLM proxy with caching enabled), where identical requests
        # can return the identical completion and the k trials of a cell
        # stop being independent samples. Provider prompt/KV caches
        # (Anthropic, OpenAI) only reuse prefix computation — sampling
        # stays stochastic — so they don't need this, and by default the
        # model sees the prompt verbatim with no trial metadata appended.
        if (harness.provider or {}).get("cache_bust"):
            prompt = f"{prompt_base}\n\n<!-- trial: {trial_id} seed: {seed} -->"
        else:
            prompt = prompt_base

        t0 = time.monotonic()
        error: str | None = None
        aborted = False
        agent = None
        last_response = None         # Final Message returned by agent.run().
        crash_exc: BaseException | None = None
        format_retries = 0       # MODEL_FORMAT_CRASH resumes used (serialization)
        transient_retries = 0    # TRANSIENT_API_ERROR backoff resumes used
        nudges = 0               # presence-gated continue-nudges issued
        rate_limit_retries = 0   # RATE_LIMITED backoff resumes used
        try:
            if isinstance(llm, StubLLM):
                # Dry-run: skip the LLM call entirely. Persist a minimal
                # record so the rest of the harness (grading, cleanup,
                # summary) can be validated end-to-end with zero cost.
                trajectory.final_response = "[dry-run: agent.run skipped]"
            else:
                display_hook = make_agent_display_hook(traj_hook) if self.verbose else None
                # Construct the agent via the runtime registry — the
                # harness's `runtime.name` picks the implementation
                # (validated against the registry by the CLI up front).
                agent = build_agent(
                    harness.runtime_name or "orchestral",
                    llm=llm, tools=tools, tool_hooks=hooks,
                    system_prompt=system_prompt,
                    display_hook=display_hook,
                    # Additive context: runtimes driving an external agent
                    # process (claude_code) scope a config-file MCP server
                    # to the sandbox. Orchestral ignores these via **_.
                    sandbox_dir=str(sandbox_dir),
                    harness=harness, loadout=loadout,
                )
                # One resume loop over the SAME agent / sandbox / context.
                # After each agent.run we either:
                #   (a) recover a MODEL_FORMAT_CRASH (nondeterministic
                #       serialization error) by feeding the parse error back, or
                #   (b) on a *deliberate* stop with a required deliverable still
                #       ABSENT, issue a presence-gated continue-nudge.
                # Both are bounded; neither consults a correctness check, so a
                # finished trial (even a wrong one) is never disturbed and the
                # grading oracle never leaks. Other crashes / genuine
                # completions end the loop.
                message = prompt
                while True:
                    crash_exc = None
                    error = None
                    try:
                        response = agent.run(message, max_iterations=max_iterations,
                                             **llm_kwargs)
                        last_response = response
                        trajectory.final_response = getattr(response, "text", "") or str(response)
                    except Exception as e:
                        crash_exc = e
                        error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                        traj_hook.write_to_log("\n--- agent crash ---\n" + error)
                        crash_kind = classify_crash(e, error)[0]
                        if (crash_kind == MODEL_FORMAT_CRASH
                                and format_retries < max_format_retries):
                            format_retries += 1
                            note = (f"\n--- retry {format_retries}/{max_format_retries} "
                                    f"after MODEL_FORMAT_CRASH (resuming same session) ---")
                            if self.verbose:
                                print(note, flush=True)
                            traj_hook.write_to_log(note)
                            # Hand the concrete parser error back so the model
                            # re-issues the offending call, partial work intact.
                            detail = str(e).strip() or "arguments were not valid JSON"
                            message = (
                                "Your previous tool call could not be executed: its "
                                f"arguments were not valid JSON ({detail}). Re-issue "
                                "that tool call now as a single well-formed JSON object, "
                                "following these rules:\n"
                                "1. The arguments must be LITERAL JSON, not Python. Do "
                                "not use expressions like \"x\"*1024 or string "
                                "concatenation; write out the final literal value.\n"
                                "2. When a value contains code, text, or LaTeX (e.g. a "
                                "`data` argument), JSON-escape every special character: "
                                "backslash as \\\\, newline as \\n, tab as \\t, and "
                                "double-quote as \\\". Avoid stray non-ASCII whitespace.\n"
                                "Then continue the task.")
                            continue
                        if (crash_kind == RATE_LIMITED
                                and rate_limit_retries < max_rate_limit_retries):
                            # Provider throttling is operational, not a model
                            # failure — back off and resume the same session so
                            # a 429/529 burst doesn't contaminate the results.
                            delay = _RATE_LIMIT_BACKOFF_S[
                                min(rate_limit_retries,
                                    len(_RATE_LIMIT_BACKOFF_S) - 1)]
                            rate_limit_retries += 1
                            note = (f"\n--- retry {rate_limit_retries}/"
                                    f"{max_rate_limit_retries} after RATE_LIMITED "
                                    f"(backing off {delay}s, resuming same session) ---")
                            if self.verbose:
                                print(note, flush=True)
                            traj_hook.write_to_log(note)
                            _sleep(delay)
                            message = (
                                "The previous request was interrupted by a temporary "
                                "provider error (rate limit). Continue the task from "
                                "where you left off.")
                            continue
                        if (crash_kind == TRANSIENT_API_ERROR
                                and transient_retries < max_transient_retries):
                            # Transport/server blip (connect or read timeout,
                            # dropped connection, HTTP 5xx). The request never
                            # got a well-formed answer, so the agent's context
                            # is unchanged — back off and resume the same
                            # session. Without this, one unreachable-endpoint
                            # window zeroes out every trial it spans.
                            delay = _TRANSIENT_BACKOFF_S[
                                min(transient_retries,
                                    len(_TRANSIENT_BACKOFF_S) - 1)]
                            transient_retries += 1
                            note = (f"\n--- retry {transient_retries}/"
                                    f"{max_transient_retries} after "
                                    f"TRANSIENT_API_ERROR (backing off {delay}s, "
                                    f"resuming same session) ---")
                            if self.verbose:
                                print(note, flush=True)
                            traj_hook.write_to_log(note)
                            _sleep(delay)
                            message = (
                                "The previous request was interrupted by a temporary "
                                "connection error reaching the model. Continue the "
                                "task from where you left off.")
                            continue
                        break  # unrecoverable crash, or retries exhausted

                    # Deliberate stop (no exception). Presence-gated nudge: resume
                    # ONLY if a required deliverable is absent — never on a
                    # correctness failure — so a finished trial is left alone.
                    if nudges < max_continue_nudges:
                        missing = missing_presence(
                            benchmark.rubric, sandbox_dir, registry=registry,
                            roles=roles, benchmark_dir=benchmark_dir)
                        if missing:
                            nudges += 1
                            note = (f"\n--- nudge {nudges}/{max_continue_nudges}: "
                                    "required deliverable not yet present, resuming ---")
                            if self.verbose:
                                print(note, flush=True)
                            traj_hook.write_to_log(note)
                            message = (
                                "You ended your turn, but the task's required output "
                                f"is not present yet ({missing}). Keep working with "
                                "the tools until the deliverable exists; do not stop "
                                "until you have produced it.")
                            continue
                    break  # complete, finished-but-wrong, or no nudge warranted/left
        finally:
            if agent is not None:
                # Extract usage even on crash: prior Responses still have
                # token data we don't want to throw away.
                self._extract_usage(agent, trajectory,
                                    configured_model=model_cfg.get("model"))
            # Tear down live source connections (toolbase subprocesses, MCP
            # sessions) started for this sandbox — a no-op when the loadout
            # used neither backend. Grading only reads sandbox files, so the
            # tools aren't needed past here.
            release_sources(str(sandbox_dir))
        # NB: traj_hook stays open through grading + footer emission; it is
        # closed at the end of run_trial.

        attempts = 1 + format_retries   # 1 try + format-crash resumes
        wall_clock = time.monotonic() - t0

        # Charge the budget. Resumed format-crash retries reuse the same
        # agent/context, so all attempts' tokens already accumulate in this
        # one trajectory. If this throws, we still grade and persist the
        # partial trial — the CLI will then stop launching new ones.
        try:
            budget.add(trajectory.cost_usd)
        except BudgetExceeded as e:
            aborted = True
            error = error or str(e)

        # Always grade against the artifacts on disk — partial work
        # done before a crash still earns credit on completed stages.
        try:
            grade = judge.grade(trajectory, benchmark.rubric, str(sandbox_dir))
        except Exception as e:
            grade = Grade(
                score=0.0, stages={}, stage_grades=[],
                failure_mode=GRADE_ERROR,
                judge_kind=judge.kind,
                judge_notes=str(e),
            )

        # If the agent crashed, override the natural failure_mode so
        # the taxonomy reports the crash, but the score still reflects
        # what got completed. Classify the crash so common patterns
        # (e.g. gpt-oss tool-call JSON corruption) get their own bucket
        # instead of being lumped under generic AGENT_CRASH.
        if error and "BudgetExceeded" not in error and crash_exc is not None:
            failure_mode, reason = classify_crash(crash_exc, error)
            grade.failure_mode = failure_mode
            grade.judge_notes = (
                (grade.judge_notes + " | " if grade.judge_notes else "")
                + reason
            )
        elif (last_response is not None
              and not getattr(last_response, "tool_calls", None)
              and grade.failure_mode != NONE):
            # The agent loop exited because the model returned a
            # Response with no tool calls — i.e. the model thought it
            # was done — but the rubric is incomplete. Distinct from
            # AGENT_CRASH (no exception) and INCOMPLETE_AT_X (which we
            # reserve for max-iter / unfinished work where the model
            # was still issuing tool calls).
            grade.failure_mode = MODEL_STOPPED_EARLY

        # Post-grade LLM-judge phase (opt-in, SERIAL, non-authoritative).
        # The rule grade above is final and untouched; this runs a separate
        # judge against the same finished sandbox and attaches its grade to
        # grade.alt_grades. It reads only sandbox files (like the rule judge),
        # so it is safe after tool teardown. A judge failure is recorded on
        # the alt grade and never disturbs the trial's score or failure mode —
        # the same discipline as the UX turn. Skipped after an unrecoverable
        # crash, when there is no delivered work to judge.
        if self.llm_judge is not None and crash_exc is None:
            note = f"\n--- LLM-judge phase ({self.llm_judge.kind}) ---"
            if self.verbose:
                print(note, flush=True)
            traj_hook.write_to_log(note)
            try:
                alt = self.llm_judge.grade(trajectory, benchmark.rubric,
                                           str(sandbox_dir))
                grade.alt_grades.append(alt.to_dict())
                traj_hook.write_to_log(
                    f"\n--- LLM judge: score {alt.score} "
                    f"({alt.judge_kind}) ---")
            except Exception as e:
                grade.alt_grades.append({
                    "judge_kind": getattr(self.llm_judge, "kind", "llm"),
                    "error": f"{type(e).__name__}: {e}"})
                traj_hook.write_to_log(
                    "\n--- LLM-judge phase failed (ignored) ---\n"
                    f"{type(e).__name__}: {e}")

        # Emit the styled END / RESULT / COST block.
        stage_order = [s["id"] for s in benchmark.rubric.stages]
        stage_w = [float(s.get("weight", 0.0)) for s in benchmark.rubric.stages]
        # grade.score IS the per-trial reach R_j (continuous when the rubric has
        # `continuous` stages, else the binary prefix-product) — use it directly
        # so the footer matches the recorded score.
        trial_reach = grade.score
        passed = bool(grade.stages) and all(grade.stages.values())
        failure_reason = ""
        # For AGENT_CRASH / GRADE_ERROR / MODEL_STOPPED_EARLY, the
        # actual cause sits in judge_notes (set by the runner / judge).
        # For INCOMPLETE_AT_X, the first failed stage's evidence is the
        # informative line.
        if grade.failure_mode in {AGENT_CRASH, GRADE_ERROR, MODEL_STOPPED_EARLY}:
            failure_reason = grade.judge_notes or ""
        else:
            for sg in grade.stage_grades:
                if not sg.passed:
                    failure_reason = sg.evidence
                    break
            if not failure_reason and grade.judge_notes:
                failure_reason = grade.judge_notes
        cost_note = ""
        if (trajectory.cost_usd is not None) and trajectory.cost_usd == 0.0:
            cost_note = "(local)"
        footer = render_footer(
            end_t=wall_clock,
            reach=trial_reach,
            passed=passed,
            failure_mode=grade.failure_mode,
            failure_reason=failure_reason,
            cost_usd=trajectory.cost_usd,
            tokens=trajectory.tokens,
            wall_s=wall_clock,
            cost_note=cost_note,
        )
        if self.verbose:
            print(footer, flush=True)
        traj_hook.write_to_log(footer)
        traj_hook.close()

        full_trial = {
            "trial_id": trial_id,
            "config": {
                "model": model_cfg,
                "harness": {
                    "id": harness.id, "runtime": harness.runtime,
                    "provider": harness.provider, "core": harness.core,
                    "loop": harness.loop,
                },
                "loadout": loadout.name,
                "skills": skill_names(loadout.skills),
                "variant": {
                    "name": variant.name,
                    "description": variant.description,
                    "axes": variant.axes,
                },
                "tools": tool_report,
                "seed": seed,
                "benchmark": benchmark.name,
            },
            "trajectory": trajectory.to_metadata_dict(),
            "grade": grade.to_dict(),
            "wall_clock_s": round(wall_clock, 2),
            "cost_usd": trajectory.cost_usd,
            "attempts": attempts,
            "nudges": nudges,
            "rate_limit_retries": rate_limit_retries,
            "transient_retries": transient_retries,
            "aborted_by_budget": aborted,
            "error": error,
            "artifacts": {
                "transcript":  "transcript.jsonl.gz",
                "console_log": "console.log",
                "sandbox":     "artifacts/",
            },
        }
        write_json(trial_dir / "trial.json", full_trial)

        # Full tool-call list lives here; trial.json carries only the
        # metadata summary.
        transcript_records = [
            {"type": "tool_call", **tc.to_dict()}
            for tc in trajectory.tool_calls
        ]
        if trajectory.final_response:
            transcript_records.append({
                "type": "assistant",
                "content": trajectory.final_response,
            })
        write_jsonl_gz(trial_dir / "transcript.jsonl.gz", transcript_records)

        self._cleanup_sandbox(sandbox_dir, trial_dir,
                              tool_calls=trajectory.tool_calls,
                              policy=getattr(benchmark, "artifact_policy", None))

        # Regrade-safety audit: the artifacts dir is all `regrade` will
        # ever see, so re-run the judge against it and warn loudly if any
        # stage that just passed would flip — that means the benchmark's
        # artifact policy fails to preserve a file its own rubric reads.
        try:
            replay = judge.grade(trajectory, benchmark.rubric,
                                 str(trial_dir / "artifacts"))
            flips = [sid for sid, ok in grade.stages.items()
                     if ok and not replay.stages.get(sid)]
            if flips:
                print(
                    f"warning: trial {trial_id}: stage(s) {flips} passed in the "
                    "sandbox but FAIL against the preserved artifacts — the "
                    "benchmark's `artifacts:` policy is missing a file its "
                    "rubric reads; `toolbench regrade` would flip these.",
                    file=sys.stderr)
        except Exception as exc:
            _warn("artifact regrade audit", exc)

        return TrialResult(
            trial_id=trial_id,
            ok=(error is None and grade.score > 0),
            score=grade.score,
            grade=grade,
            trajectory=trajectory,
            wall_clock_s=wall_clock,
            cost_usd=trajectory.cost_usd,
            aborted_by_budget=aborted,
            error=error,
            attempts=attempts,
            nudges=nudges,
            rate_limit_retries=rate_limit_retries,
            transient_retries=transient_retries,
        )

    def _extract_usage(self, agent, trajectory: Trajectory,
                       *, configured_model: str | None = None) -> None:
        """Pull token + cost totals from agent.context.

        Orchestral normalizes per-provider usage into a common schema:
        `prompt_tokens` / `completion_tokens` / `total_tokens`, plus
        provider-specific extras (e.g. Anthropic adds
        `cache_creation_input_tokens` and `cache_read_input_tokens`).
        We aggregate across all Responses on the context.

        Cost-source priority:
          1. `usage.cost` from the Response (preferred — provider sets it).
          2. The litellm proxy pricing snapshot captured at run start
             (when `--provider litellm`), keyed on `configured_model`.
          3. The static `metrics.PRICING_TABLE` (last-resort fallback).
        """
        # CLI runtimes (claude_code, codex) drive an external agent process and
        # accumulate their own usage from its output; use it directly rather
        # than scanning for orchestral Response objects (which they never make).
        direct = getattr(agent, "token_usage", None)
        if direct is not None:
            trajectory.tokens.update({
                "initial_input": int(direct.get("initial_input", 0) or 0),
                "input": int(direct.get("input", 0) or 0),
                "output": int(direct.get("output", 0) or 0),
                "cache_read": int(direct.get("cache_read", 0) or 0),
                "cache_creation": int(direct.get("cache_creation", 0) or 0),
            })
            if direct.get("model"):
                trajectory.resolved_model = direct["model"]
            if direct.get("cost") is not None:
                trajectory.cost_usd = round(float(direct["cost"]), 6)
            return
        try:
            from orchestral.llm.base.response import Response
            tot_in = tot_out = tot_cache_read = tot_cache_creation = 0
            tot_cost = 0.0
            had_cost = False
            model_name = None
            for msg in agent.context.messages:
                if not isinstance(msg, Response) or msg.usage is None:
                    continue
                model_name = msg.usage.model_name or model_name
                tk = msg.usage.tokens or {}
                tot_in += int(tk.get("prompt_tokens", 0) or 0)
                tot_out += int(tk.get("completion_tokens", 0) or 0)
                tot_cache_read += int(
                    tk.get("cache_read_input_tokens", tk.get("cache_read", 0)) or 0
                )
                tot_cache_creation += int(
                    tk.get("cache_creation_input_tokens", tk.get("cache_creation", 0)) or 0
                )
                if msg.usage.cost is not None:
                    tot_cost += float(msg.usage.cost)
                    had_cost = True
            trajectory.tokens.update({
                "input": tot_in,
                "output": tot_out,
                "cache_read": tot_cache_read,
                "cache_creation": tot_cache_creation,
            })
            # The snapshot the provider actually served (may be a dated
            # version of the configured alias) — kept as reproducibility
            # evidence on the trial record.
            trajectory.resolved_model = model_name
            # A provider-reported cost of exactly $0 alongside nonzero
            # token usage is indistinguishable from a proxy that has no
            # pricing configured for the model (litellm reports cost=0.0
            # for unpriced routes). Trust positive costs; for zero/absent
            # cost, try the fallbacks first and only keep the reported $0
            # when no fallback knows the model (genuinely free routes).
            if had_cost and tot_cost > 0:
                trajectory.cost_usd = round(tot_cost, 6)
            else:
                # 2nd choice: the litellm proxy pricing snapshot.
                proxy_cost = cost_from_proxy(
                    self.litellm_pricing,
                    configured_model or model_name or "",
                    tot_in, tot_out, tot_cache_read,
                )
                if proxy_cost is not None and proxy_cost > 0:
                    trajectory.cost_usd = round(proxy_cost, 6)
                else:
                    # 3rd choice: the static fallback table. Proxy routes
                    # prefix the vendor ("azure/claude-haiku-4-5"); the
                    # table keys on the bare model name.
                    names = []
                    for nm in (configured_model, model_name):
                        if nm:
                            names += [nm, nm.split("/")[-1]]
                    fallback = None
                    for nm in names:
                        provider_guess = (
                            "anthropic" if "claude" in nm else
                            "openai" if ("gpt" in nm or "o1" in nm) else
                            None
                        )
                        if provider_guess:
                            fallback = cost_usd(provider_guess, nm,
                                                tot_in, tot_out, tot_cache_read)
                        if fallback is not None:
                            break
                    if fallback is not None:
                        trajectory.cost_usd = round(fallback, 6)
                    elif had_cost:
                        trajectory.cost_usd = round(tot_cost, 6)
        except Exception as exc:
            # Token extraction is best-effort. A missing Usage shouldn't
            # tank the trial — but leave a breadcrumb so a missing
            # cost number isn't mysterious downstream.
            _warn("token/cost extraction failed", exc)

    @staticmethod
    def _dump_agent_scripts(tool_calls, artifacts_dir: Path) -> None:
        """Materialize agent-authored source code into artifacts/scripts/.

        Code-executing tools (RunPythonTool, ...) run a temp file outside
        the sandbox, so the scripts the agent wrote during the task would
        otherwise survive only inside the gzipped transcript. We dump each
        such call's code to a numbered, ordered file so a trial's actual
        work — selection, reconstruction, plotting — is auditable as
        plain files without parsing transcripts.
        """
        scripts_dir = artifacts_dir / "scripts"
        kept = 0
        for i, tc in enumerate(tool_calls):
            args = tc.args or {}
            code = next(
                (args[k] for k in CODE_ARG_KEYS
                 if isinstance(args.get(k), str) and args[k].strip()),
                None,
            )
            if code is None:
                continue
            kept += 1
            name = tc.name.lower()
            ext = ".py" if "python" in name else (
                ".sh" if any(s in name for s in ("command", "bash", "shell"))
                else ".txt")
            fname = f"{kept:03d}_{tc.name.lower()}{ext}"
            header = (f"# trajectory tool-call #{i + 1}: {tc.name} "
                      f"(ok={tc.ok}, {tc.duration_s:.1f}s)\n")
            try:
                scripts_dir.mkdir(parents=True, exist_ok=True)
                (scripts_dir / fname).write_text(header + code + "\n")
            except Exception as exc:
                _warn(f"script preserve {fname}", exc)

    @staticmethod
    def _cleanup_sandbox(sandbox_dir: Path, trial_dir: Path,
                         tool_calls=(), policy: ArtifactPolicy | None = None) -> None:
        """Copy minimum-regrade evidence to trial_dir/artifacts/, then
        nuke the sandbox.

        What survives is governed by `policy` (the benchmark's
        `artifacts:` block, or `artifact_policy.DEFAULT_POLICY`):

          0. scripts/           — agent-authored code lifted from the
             trajectory (RunPythonTool runs temp files outside the
             sandbox, so this is the only place they're preserved).
          1. policy.keep_full   — verbatim copy.
          2. policy.truncate    — copy first N records (sufficient for
             rubric content_check schema + min_records gates without
             keeping the full multi-MB record dumps).
          3. policy.keep_root   — bare-name files at sandbox root.

        Everything else gets nuked with the sandbox.
        """
        policy = policy or DEFAULT_POLICY
        artifacts_dir = trial_dir / "artifacts"
        artifacts_dir.mkdir(exist_ok=True)

        # Class 0: the agent's own scripts (independent of the sandbox).
        TrialRunner._dump_agent_scripts(tool_calls, artifacts_dir)

        # Skip macOS junk, Python cache, and third-party tool machinery.
        def _skip(p: Path) -> bool:
            name = p.name
            if name == ".DS_Store":
                return True
            if "__pycache__" in p.parts:
                return True
            posix = p.as_posix()
            if any(seg in posix for seg in policy.exclude_segments):
                return True
            return False

        # Class 1: full copy. Use rglob so the agent's chosen layout
        # is preserved (e.g. data/run01/ stays at data/run01/).
        seen_full: set[Path] = set()
        for ext in policy.keep_full:
            for src in sandbox_dir.rglob(f"*{ext}"):
                if not src.is_file() or src in seen_full or _skip(src):
                    continue
                seen_full.add(src)
                try:
                    rel = src.relative_to(sandbox_dir)
                    dst = artifacts_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                except Exception as exc:
                    _warn(f"artifact preserve (full) {src.name}", exc)

        # Class 2: truncated copies of bulk record-oriented files.
        for ext, max_records in policy.truncate:
            for src in sandbox_dir.rglob(f"*{ext}"):
                if not src.is_file() or _skip(src):
                    continue
                try:
                    rel = src.relative_to(sandbox_dir)
                    dst = artifacts_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    _copy_truncated_jsonl(src, dst, max_records)
                except Exception as exc:
                    _warn(f"artifact preserve (truncated) {src.name}", exc)

        # Class 3: bare-name root files.
        for name in policy.keep_root:
            src = sandbox_dir / name
            if src.is_file():
                try:
                    shutil.copy2(src, artifacts_dir / name)
                except Exception as exc:
                    _warn(f"artifact preserve (root) {name}", exc)

        shutil.rmtree(sandbox_dir, ignore_errors=True)


def _copy_truncated_jsonl(src: Path, dst: Path, max_records: int) -> None:
    """Copy the first `max_records` non-empty lines of a .jsonl. The
    full file may be many MB (e.g. Pythia events.jsonl with 5000 events
    × 5 KB/event); we only need ~200 records to satisfy the rubric's
    schema + min_records gates.
    """
    written = 0
    with open(src, "r", encoding="utf-8", errors="replace") as r, \
         open(dst, "w", encoding="utf-8") as w:
        for line in r:
            if not line.strip():
                continue
            w.write(line if line.endswith("\n") else line + "\n")
            written += 1
            if written >= max_records:
                break
