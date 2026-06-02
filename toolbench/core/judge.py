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

from .checks import BUILTIN_CHECKS, run_check
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
                 benchmark_dir: str | None = None):
        # `registry` merges built-in + benchmark-local checks; `benchmark_dir`
        # anchors relative `reference:` paths. Both default to the built-ins.
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
            for entry in stage.get("checks") or []:
                if not isinstance(entry, dict) or len(entry) != 1:
                    passed = False
                    evidence.append(f"malformed check entry: {entry!r}")
                    continue
                (cname, cparams), = entry.items()
                ok, msg = run_check(
                    cname, base, cparams or {},
                    benchmark_dir=self.benchmark_dir, registry=self.registry,
                )
                evidence.append(f"{cname}: {msg}")
                if not ok:
                    passed = False
            for tname in stage.get("expected_tool_calls", []):
                used = tname.lower() in called_tools
                evidence.append(f"tool {'used' if used else 'unused'}: {tname}")

            stage_grades.append(StageGrade(
                id=sid, passed=passed, weight=weight,
                description=stage.get("description", ""),
                evidence="; ".join(evidence),
            ))
            stages[sid] = passed

        score = round(sum(s.weight for s in stage_grades if s.passed), 4)

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
