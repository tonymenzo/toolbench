"""
Judges for the eval harness.

A judge consumes a finished trajectory plus the rubric and emits a
`Grade` (per-stage pass/fail + scalar score + failure mode). The
abstract base class is `Judge`; the only concrete implementation
shipped here is `RuleJudge`, which evaluates each stage's `checks:` list (artifact /
content / numeric checks resolved from the merged registry in
`checks.py`) and records `expected_tool_calls` as a non-scoring
diagnostic.

Adding new judge kinds (LLM-as-judge, vision-judge, ...) is a matter
of subclassing `Judge` and registering at the caller — there is no
central registry in this module by design.
"""

from abc import ABC, abstractmethod
from pathlib import Path

from .checks import BUILTIN_CHECKS, run_check_full
from .metrics import per_trial_reach
from .failure_modes import NONE, UNKNOWN, incomplete_at
from .task import Grade, Rubric, StageGrade
from .trajectory import Trajectory


class Judge(ABC):
    kind: str = "abstract"

    @abstractmethod
    def grade(self, trajectory: Trajectory, rubric: Rubric,
              base_directory: str) -> Grade: ...


class RuleJudge(Judge):
    kind = "rule"

    def __init__(self, registry: dict | None = None,
                 benchmark_dir: str | Path | list[Path] | None = None):
        # `registry` merges built-in + benchmark-local checks; `benchmark_dir`
        # anchors relative `reference:` paths (one dir, or a benchmark's
        # `search_dirs` when it extends another). Both default to the
        # built-ins.
        self.registry = registry if registry is not None else BUILTIN_CHECKS
        self.benchmark_dir = benchmark_dir

    def grade(self, trajectory: Trajectory, rubric: Rubric,
              base_directory: str) -> Grade:
        base = Path(base_directory)
        # Tool name matching is case-insensitive: Orchestral lower-cases
        # registered tool names (e.g. FeynRulesToUFO -> feynrulestoufo)
        # but the rubric uses the camel-case names from the public API.
        called_tools = {tc.name.lower() for tc in trajectory.tool_calls}
        stage_grades: list[StageGrade] = []
        stages: dict[str, bool] = {}

        for stage in rubric.stages:
            sid = stage["id"]
            weight = float(stage.get("weight", 0.0))
            evidence: list[str] = []
            passed = True

            # A stage carries an ordered `checks:` list of {<check-name>: {params}}.
            # The stage passes iff every check passes; `expected_tool_calls` is
            # recorded as a diagnostic only — it never affects the score, so a
            # loadout is judged on the artifacts it produced, not the tools used.
            stage_metrics: dict = {}
            for entry in stage.get("checks") or []:
                if not isinstance(entry, dict) or len(entry) != 1:
                    passed = False
                    evidence.append(f"malformed check entry: {entry!r}")
                    continue
                (cname, cparams), = entry.items()
                ok, msg, cmetrics = run_check_full(
                    cname, base, cparams or {},
                    benchmark_dir=self.benchmark_dir, registry=self.registry,
                )
                evidence.append(f"{cname}: {msg}")
                if cmetrics:
                    # Continuous diagnostics (distance-to-reference, closeness,
                    # ...) recorded alongside the binary pass — never affect the
                    # score, which stays the prefix-product of `passed`.
                    stage_metrics[cname] = cmetrics
                if not ok:
                    passed = False
            for tname in stage.get("expected_tool_calls", []):
                used = tname.lower() in called_tools
                evidence.append(f"tool {'used' if used else 'unused'}: {tname}")

            # A `continuous: true` stage contributes a partial [0,1] credit (the
            # closeness a check recorded) instead of an all-or-nothing pass, and
            # does not gate later stages. `passed` stays binary (for pass@k).
            is_cont = bool(stage.get("continuous"))
            # `gating:` is the explicit control; `continuous: true` keeps
            # implying non-gating so existing rubrics are unchanged. Stated
            # separately because a rubric of INDEPENDENT stages wants binary
            # credit AND no gating — `continuous` alone cannot say that.
            gates = bool(stage.get("gating", not is_cont))
            if is_cont:
                credit = next(
                    (float(m["closeness"]) for m in stage_metrics.values()
                     if isinstance(m, dict) and m.get("closeness") is not None),
                    1.0 if passed else 0.0)
            else:
                credit = 1.0 if passed else 0.0
            stage_grades.append(StageGrade(
                id=sid, passed=passed, weight=weight,
                description=stage.get("description", ""),
                evidence="; ".join(evidence),
                metrics=stage_metrics,
                continuous=is_cont, credit=credit, gates=gates,
            ))
            stages[sid] = passed

        # Score = the per-trial reach R_j (normalized to [0,1]), computed via
        # the shared `per_trial_reach`. A gating stage absorbs (a fail zeroes
        # later contributions); a non-gating stage contributes its credit and
        # lets the rest stand. `continuous` controls the credit (partial vs
        # binary), `gating:` controls the absorption — they are independent.
        # A rubric that sets neither reduces to the original prefix-product.
        weights = [s.weight for s in stage_grades]
        credit_row = [s.credit for s in stage_grades]
        gating = [s.gates for s in stage_grades]
        reach = per_trial_reach([credit_row], weights, gating)
        score = round(reach[0], 4) if reach else 0.0

        if all(s.passed for s in stage_grades):
            failure_mode = NONE
        else:
            first_failed = next((s for s in stage_grades if not s.passed), None)
            failure_mode = incomplete_at(first_failed.id) if first_failed else UNKNOWN

        return Grade(
            score=score,
            stages=stages,
            stage_grades=stage_grades,
            failure_mode=failure_mode,
            judge_kind=self.kind,
        )


# ----------------------------------------------------------------------
# LLM judge
# ----------------------------------------------------------------------

_JUDGE_SYSTEM = """\
You are grading one stage of a scientific benchmark, strictly and \
independently.

You will be given: the stage's criterion, the answer artifacts the agent \
produced, and (when the benchmark defines one) the reference answer. Decide \
whether the stage's criterion is met.

Rules:
- Judge the ANSWER against the CRITERION and the REFERENCE. An answer that is \
mathematically equivalent to the reference but written differently is CORRECT \
(different but equivalent algebraic arrangement, a different-but-valid way of \
writing the same number or expression).
- Do not reward confident presentation, length, or plausible-looking work. An \
answer that is absent, hedged into meaninglessness, or not actually derived \
does not pass.
- You are not told which model produced this, and must not speculate.

Reply with ONLY a JSON object, no prose and no code fence:
{"passed": true|false, "reason": "<one sentence>", "confidence": <0.0-1.0>}
"""

_ARTIFACT_CHARS = 8000


class LLMJudge(Judge):
    """Grade a rubric with a model instead of deterministic checks.

    Emits the SAME `Grade` shape as `RuleJudge` — binary `passed` per
    stage plus the identical reach computation — so every downstream
    metric (reach, pass@k, pass^k, the stage funnel, condition deltas)
    works unchanged and rule-graded and LLM-graded runs stay structurally
    comparable. A judge that returned some holistic score instead would
    fork every one of those.

    Context per stage is gathered generically from the stage's own
    `checks:` params: `file:` paths are read from the trial sandbox and
    `reference:` paths from the benchmark dir. That means no
    benchmark-specific judging code — a rubric that already tells the
    rule judge where to look tells this one too.
    """

    kind = "llm"

    def __init__(self, *, llm, model: str | None = None,
                 harness_id: str | None = None,
                 benchmark_dir: str | Path | list | None = None,
                 max_tokens: int = 1024, artifact_chars: int = _ARTIFACT_CHARS):
        self.llm = llm
        self.model = model
        self.harness_id = harness_id
        self.benchmark_dir = benchmark_dir
        self.max_tokens = max_tokens
        self.artifact_chars = artifact_chars

    @property
    def kind_label(self) -> str:
        return f"llm:{self.harness_id or '?'}:{self.model or '?'}"

    # -- context gathering -------------------------------------------------

    def _bench_dirs(self) -> list[Path]:
        bd = self.benchmark_dir
        if bd is None:
            return []
        if isinstance(bd, (str, Path)):
            return [Path(bd)]
        return [Path(d) for d in bd]

    def _read(self, path: Path) -> str | None:
        try:
            if not path.is_file():
                return None
            text = path.read_text(errors="replace")
        except Exception:
            return None
        if len(text) > self.artifact_chars:
            text = text[:self.artifact_chars] + "\n...[truncated]"
        return text

    def _gather(self, stage: dict, base: Path) -> str:
        """Answer + reference material named by the stage's own checks."""
        seen: set[str] = set()
        blocks: list[str] = []
        for entry in stage.get("checks") or []:
            if not isinstance(entry, dict) or len(entry) != 1:
                continue
            (_, params), = entry.items()
            params = params or {}
            rel = params.get("file")
            if rel and rel not in seen:
                seen.add(rel)
                body = self._read(base / rel)
                blocks.append(f"--- agent artifact: {rel} ---\n"
                              + (body if body is not None else "<missing>"))
            ref = params.get("reference")
            if ref and ref not in seen:
                seen.add(ref)
                for d in self._bench_dirs():
                    body = self._read(d / ref)
                    if body is not None:
                        blocks.append(f"--- reference answer: {ref} ---\n{body}")
                        break
            for key in ("field", "ref_field"):
                if params.get(key):
                    blocks.append(f"(the graded field is {params[key]!r})")
                    break
        return "\n\n".join(blocks) if blocks else "<no artifacts referenced>"

    # -- one stage ---------------------------------------------------------

    def _ask(self, stage: dict, context_text: str) -> tuple[bool, str, float]:
        from orchestral.context.context import Context
        from orchestral.context.message import Message

        criterion = (stage.get("description")
                     or stage.get("judge_criteria")
                     or stage.get("id", ""))
        schema = ('{"passed": true|false, "reason": "<one sentence>", '
                  '"confidence": <0.0-1.0>}')
        prompt = (f"CRITERION\n{criterion}\n\n"
                  f"MATERIAL\n{context_text}\n\n"
                  "Does the answer meet the criterion? Reply with ONLY this "
                  f"JSON object and nothing else:\n{schema}")
        # orchestral Message takes `text`, not `content`.
        msgs = [Message(role="user", text=prompt)]
        # Weaker judges reliably reason correctly and then answer in prose
        # anyway. One corrective retry costs far less than discarding the
        # verdict. We never infer a verdict FROM the prose: a mis-read "No."
        # scored as a pass would silently corrupt the very rule-vs-LLM
        # comparison this judge exists to produce, so an unparseable second
        # reply is recorded as a judge error instead.
        for attempt in range(2):
            ctx = Context(messages=list(msgs), system_prompt=_JUDGE_SYSTEM)
            text = _response_text(
                self.llm.get_response(ctx, max_tokens=self.max_tokens))
            try:
                return _parse_verdict(text)
            except ValueError:
                if attempt:
                    raise
                msgs += [
                    Message(role="assistant", text=text),
                    Message(role="user", text=(
                        "That was prose. Reply with ONLY the JSON object — no "
                        f"prose, no code fence, no explanation:\n{schema}")),
                ]
        raise AssertionError("unreachable")

    # -- Judge API ---------------------------------------------------------

    def grade(self, trajectory: Trajectory, rubric: Rubric,
              base_directory: str) -> Grade:
        base = Path(base_directory)
        stage_grades: list[StageGrade] = []
        stages: dict[str, bool] = {}
        notes: list[str] = []

        for stage in rubric.stages:
            sid = stage["id"]
            weight = float(stage.get("weight", 0.0))
            is_cont = bool(stage.get("continuous"))
            gates = bool(stage.get("gating", not is_cont))
            try:
                passed, reason, conf = self._ask(stage, self._gather(stage, base))
                evidence = f"llm: {reason} (confidence {conf:.2f})"
            except Exception as e:
                # A judge failure must not be scored as an agent failure;
                # record it and leave the stage unpassed with a clear note.
                passed, evidence = False, f"llm judge error: {type(e).__name__}: {e}"
                notes.append(f"{sid}: {evidence}")
            stage_grades.append(StageGrade(
                id=sid, passed=passed, weight=weight,
                description=stage.get("description", ""),
                evidence=evidence, metrics={},
                continuous=is_cont, credit=1.0 if passed else 0.0, gates=gates,
            ))
            stages[sid] = passed

        weights = [s.weight for s in stage_grades]
        credit_row = [s.credit for s in stage_grades]
        gating = [s.gates for s in stage_grades]
        reach = per_trial_reach([credit_row], weights, gating)
        score = round(reach[0], 4) if reach else 0.0

        if all(s.passed for s in stage_grades):
            failure_mode = NONE
        else:
            first = next((s for s in stage_grades if not s.passed), None)
            failure_mode = incomplete_at(first.id) if first else UNKNOWN

        return Grade(
            score=score, stages=stages, stage_grades=stage_grades,
            failure_mode=failure_mode, judge_kind=self.kind_label,
            judge_notes="; ".join(notes) or None,
        )


def _response_text(resp) -> str:
    """Pull assistant text out of an orchestral Response, tolerantly.

    orchestral `Message` carries `.text`; older/other shapes use
    `.content`, possibly as a list of blocks. Never fall back to
    `str(resp)` — that yields the Response repr, which parses as prose
    and produces a confusing "judge did not return JSON".
    """
    msg = getattr(resp, "message", None)
    if msg is None:
        choices = getattr(resp, "message_choices", None) or []
        msg = choices[0] if choices else None
    if msg is None:
        raise ValueError("judge response carried no message")
    for attr in ("text", "content"):
        val = getattr(msg, attr, None)
        if isinstance(val, str) and val.strip():
            return val
        if isinstance(val, list):          # block-style content
            parts = [getattr(b, "text", None)
                     or (b.get("text") if isinstance(b, dict) else None)
                     for b in val]
            joined = "\n".join(p for p in parts if p)
            if joined.strip():
                return joined
    raise ValueError("judge response had no text content")


def _parse_verdict(text: str) -> tuple[bool, str, float]:
    """Extract {passed, reason, confidence} from a judge reply.

    Models wrap JSON in fences or prose often enough that a bare
    json.loads is not worth the flake; take the first balanced object.
    """
    import json as _json
    import re as _re
    blob = text.strip()
    if blob.startswith("```"):
        blob = _re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", blob).strip()
    if not blob.startswith("{"):
        m = _re.search(r"\{.*\}", blob, _re.S)
        blob = m.group(0) if m else blob
    try:
        obj = _json.loads(blob)
    except Exception as e:
        raise ValueError(f"judge did not return JSON ({e}); got: {text[:200]!r}")
    if "passed" not in obj:
        raise ValueError(f"judge verdict missing 'passed': {obj!r}")
    return (bool(obj["passed"]),
            str(obj.get("reason", "")).strip() or "(no reason given)",
            float(obj.get("confidence", 0.0)))


class DualJudge(Judge):
    """Run several judges; the FIRST is authoritative.

    `grade()` returns the primary judge's Grade with the others attached
    in `alt_grades`, so `score` and every metric derived from it stay
    deterministic while the extra opinions ride along for comparison.
    """

    def __init__(self, judges: list[Judge]):
        if not judges:
            raise ValueError("DualJudge needs at least one judge")
        self.judges = judges

    @property
    def kind(self) -> str:                       # type: ignore[override]
        return " + ".join(getattr(j, "kind_label", j.kind) for j in self.judges)

    def grade(self, trajectory: Trajectory, rubric: Rubric,
              base_directory: str) -> Grade:
        primary = self.judges[0].grade(trajectory, rubric, base_directory)
        for j in self.judges[1:]:
            try:
                primary.alt_grades.append(
                    j.grade(trajectory, rubric, base_directory).to_dict())
            except Exception as e:
                primary.alt_grades.append({
                    "judge_kind": getattr(j, "kind_label", j.kind),
                    "error": f"{type(e).__name__}: {e}",
                })
        return primary
