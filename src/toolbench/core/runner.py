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

from orchestral import Agent
from orchestral.tools.hooks import TruncateOutputHook

from .budget import Budget, BudgetExceeded
from .crash_classifier import classify_crash
from .failure_modes import (
    AGENT_CRASH, GRADE_ERROR, MODEL_FORMAT_CRASH, MODEL_STOPPED_EARLY, NONE,
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
from .store import write_json, write_jsonl_gz
from .task import Grade, Task
from .tool_resolver import build_agent_tools, release_toolbase
from .trajectory import Trajectory, TrajectoryHook, make_agent_display_hook
from .variant import Variant
from toolbench.reporting.transcript import render_footer, render_header


DEFAULT_SYSTEM_PROMPT = (
    "You are an expert physicist. Solve the task carefully and verify your work."
)


# Sandbox cleanup keeps the *minimum evidence* each rubric check
# needs to be re-run later via `toolbench.cli regrade`. Path-preserving
# copy into trials/<id>/artifacts/, then the entire sandbox is
# deleted.
#
# Three classes:
#   FULL: copy verbatim. Small files where any truncation would
#         break the corresponding rubric check or the headline
#         deliverable itself.
#   TRUNCATED: bulk record-oriented files we keep only the first N
#              records of, enough to clear the schema + min-record
#              gates without keeping multi-MB-per-file dumps.
#   (everything else): deleted with the sandbox.
KEEP_EXTENSIONS_FULL = (
    ".pdf", ".png",         # headline plots / agent-side figures
    ".npy",                 # reconstructed mass arrays
    ".py",                  # UFO module files + agent's plotting scripts
    ".lhe", ".lhe.gz",      # MadGraph parton-level events (gzipped is small)
    ".json",                # structured answers (e.g. output/answer.json)
)

# Files truncated to N records and copied to artifacts/.
# (extension, max_records).
TRUNCATED_EXTENSIONS = (
    (".jsonl", 200),        # events.jsonl + jets.jsonl headers; >100 needed
)

# Bare-name files at the sandbox root we always preserve.
KEEP_ROOT_FILES = ("todos.md",)

# Tool-call argument keys that carry agent-authored source code. The
# code-executing core tools (RunPythonTool and friends) run an
# *ephemeral temp file outside the sandbox*, so the scripts the agent
# actually wrote during the task — its reconstruction, selection and
# plotting code — never land in the sandbox and would be lost at
# cleanup. We lift them out of the trajectory into artifacts/scripts/.
CODE_ARG_KEYS = ("code", "script", "source", "program")

# Path segments owned by third-party tools (not the agent and not a
# graded deliverable): MadGraph dumps its entire interpreter under
# `<output>/bin/internal/`, ~40 .py files that would otherwise be
# swept up by the `.py` FULL rule and bury the agent's own output.
# The top-level UFO dir still satisfies the `ufo_dir` check, so
# pruning these does not affect regrade.
MACHINERY_PATH_SEGMENTS = ("bin/internal",)


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
    attempts: int = 1   # 1 + number of MODEL_FORMAT_CRASH retries
    nudges: int = 0     # presence-gated continue-nudges issued


# Hard fallbacks for the orchestral loop knobs, used only when a harness's
# `loop:` block omits them (and no CLI override is given).
_LOOP_DEFAULTS = {"max_iterations": 150, "max_format_retries": 2,
                  "continue_nudges": 0}


class TrialRunner:
    def __init__(self, judge: Judge | None = None,
                 max_iterations: int | None = None,
                 verbose: bool = False,
                 litellm_pricing: dict | None = None,
                 max_format_retries: int | None = None,
                 max_continue_nudges: int | None = None):
        self.judge = judge
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
        self.max_iterations = max_iterations
        self.max_format_retries = max_format_retries
        self.max_continue_nudges = max_continue_nudges
        # Optional snapshot of {model_name: {input, cache_read, output}}
        # captured from the litellm proxy at run start. Used as a cost
        # fallback when the proxy doesn't populate `usage.cost`.
        self.litellm_pricing = litellm_pricing

    def _resolve_loop(self, harness: Harness) -> dict:
        """Effective loop config = CLI/explicit override, else harness.loop,
        else hard default."""
        loop = getattr(harness, "loop", None) or {}
        out = {}
        for key, override in (
            ("max_iterations", self.max_iterations),
            ("max_format_retries", self.max_format_retries),
            ("continue_nudges", self.max_continue_nudges),
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

        # Per-variant scaffolding: the variant owns the prompts and the
        # sandbox seed (the things that change between difficulty rungs);
        # the benchmark family supplies the invariant rubric and checks.
        variant.setup_workspace(str(sandbox_dir))

        llm = build_llm(
            provider=model_cfg["provider"],
            model=model_cfg.get("model"),
            dry_run=model_cfg.get("dry_run", False),
        )

        # Tools = harness core ∪ loadout toolkit.
        tools, tool_report = build_agent_tools(harness, loadout, str(sandbox_dir))

        # Benchmark-aware judge: built-in checks + the benchmark's local
        # checks module, with `reference:` paths anchored at the benchmark dir.
        checks_path = getattr(benchmark, "checks_module_path", lambda: None)()
        registry = merged_registry(load_benchmark_checks(checks_path))
        roles = merged_roles(load_benchmark_roles(checks_path))
        benchmark_dir = str(getattr(benchmark, "BENCHMARK_DIR", ""))
        judge = RuleJudge(registry=registry, benchmark_dir=benchmark_dir)
        prompt_base = variant.read_user_prompt()
        system_prompt = variant.read_system_prompt() or DEFAULT_SYSTEM_PROMPT

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

        # Per-trial nonce so identical sandboxes don't all cache-hit on the
        # upstream LiteLLM proxy (defeating independent sampling).
        prompt = f"{prompt_base}\n\n<!-- trial: {trial_id} seed: {seed} -->"

        t0 = time.monotonic()
        error: str | None = None
        aborted = False
        agent = None
        last_response = None         # Final Message returned by agent.run().
        crash_exc: BaseException | None = None
        format_retries = 0   # MODEL_FORMAT_CRASH resumes used (serialization)
        nudges = 0           # presence-gated continue-nudges issued
        try:
            if isinstance(llm, StubLLM):
                # Dry-run: skip the LLM call entirely. Persist a minimal
                # record so the rest of the harness (grading, cleanup,
                # summary) can be validated end-to-end with zero cost.
                trajectory.final_response = "[dry-run: agent.run skipped]"
            else:
                display_hook = make_agent_display_hook(traj_hook) if self.verbose else None
                agent = Agent(
                    llm=llm, tools=tools, tool_hooks=hooks,
                    system_prompt=system_prompt, debug=False,
                    display_hook=display_hook,
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
                        response = agent.run(message, max_iterations=max_iterations)
                        last_response = response
                        trajectory.final_response = getattr(response, "text", "") or str(response)
                    except Exception as e:
                        crash_exc = e
                        error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                        traj_hook.write_to_log("\n--- agent crash ---\n" + error)
                        if (classify_crash(e, error)[0] == MODEL_FORMAT_CRASH
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
                                "that tool call now as a single well-formed JSON object "
                                "(escape newlines as \\n and quotes as \\\"), then "
                                "continue.")
                            continue
                        break  # unrecoverable crash, or format-retries exhausted

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
            # Tear down any toolbase subprocesses started for this sandbox
            # (no-op when the loadout used no `toolbase:` source). Grading
            # only reads sandbox files, so the tools aren't needed past here.
            release_toolbase(str(sandbox_dir))
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

        # Emit the styled END / RESULT / COST block.
        stage_order = [s["id"] for s in benchmark.rubric.stages]
        stage_w = [float(s.get("weight", 0.0)) for s in benchmark.rubric.stages]
        row = [1 if grade.stages.get(sid) else 0 for sid in stage_order]
        reach_list = per_trial_reach([row], stage_w) if stage_order else []
        trial_reach = reach_list[0] if reach_list else 0.0
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
                              tool_calls=trajectory.tool_calls)

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
        )

    def _extract_usage(self, agent: Agent, trajectory: Trajectory,
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
            if had_cost:
                trajectory.cost_usd = round(tot_cost, 6)
            else:
                # 2nd choice: the litellm proxy pricing snapshot.
                proxy_cost = cost_from_proxy(
                    self.litellm_pricing,
                    configured_model or model_name or "",
                    tot_in, tot_out, tot_cache_read,
                )
                if proxy_cost is not None:
                    trajectory.cost_usd = round(proxy_cost, 6)
                else:
                    # 3rd choice: the static fallback table.
                    provider_guess = (
                        "anthropic" if model_name and "claude" in model_name else
                        "openai" if model_name and ("gpt" in model_name or "o1" in model_name) else
                        None
                    )
                    if provider_guess and model_name:
                        fallback = cost_usd(provider_guess, model_name,
                                            tot_in, tot_out, tot_cache_read)
                        if fallback is not None:
                            trajectory.cost_usd = round(fallback, 6)
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
                         tool_calls=()) -> None:
        """Copy minimum-regrade evidence to trial_dir/artifacts/, then
        nuke the sandbox.

        Keep-classes:
          0. scripts/             — agent-authored code lifted from the
             trajectory (RunPythonTool runs temp files outside the
             sandbox, so this is the only place they're preserved).
          1. KEEP_EXTENSIONS_FULL — verbatim copy.
          2. TRUNCATED_EXTENSIONS — copy first N records (sufficient for
             rubric content_check schema + min_records gates without
             keeping the full multi-MB record dumps).
          3. KEEP_ROOT_FILES      — bare-name files at sandbox root.

        Everything else gets nuked with the sandbox.
        """
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
            if any(seg in posix for seg in MACHINERY_PATH_SEGMENTS):
                return True
            return False

        # Class 1: full copy. Use rglob so the agent's chosen layout
        # is preserved (e.g. data/run01/ stays at data/run01/).
        seen_full: set[Path] = set()
        for ext in KEEP_EXTENSIONS_FULL:
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
        for ext, max_records in TRUNCATED_EXTENSIONS:
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
        for name in KEEP_ROOT_FILES:
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
