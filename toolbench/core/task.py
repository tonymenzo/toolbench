"""
Task / Rubric / Grade dataclasses.

A `Task` is a benchmark family: it carries the family-level invariants
(name, rubric, benchmark dir) that every variant shares. Per-variant
assets (prompts, sandbox template, axis labels) live on `Variant`
instances discovered under the benchmark's `variants/` directory; the
runner pairs a `Task` with a `Variant` per trial.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class StageGrade:
    id: str
    passed: bool
    weight: float
    evidence: str = ""
    description: str = ""
    # Continuous diagnostics a check chose to record. Convention (used
    # generically by the harness for display/persistence, and safe to omit):
    #   - "distance": float | None  -- the raw distance-to-reference that the
    #     continuous credit is derived from (e.g. mean dex in log10 g, a
    #     relative error). None when undefined (no comparable output).
    #   - "distance_label": str     -- human label + unit for that distance
    #     (e.g. "dex (log10 g)", "rel err"), shown next to the value.
    # Any other keys are check-specific and passed through untouched. Binary
    # stages typically record nothing here.
    metrics: dict = field(default_factory=dict)
    # `continuous` stages contribute their `credit` (∈[0,1]) to the reach score
    # and do NOT gate later stages; binary stages have credit = 1.0/0.0 and gate
    # via the absorbing convention. `passed` is still the binary outcome used for
    # pass@k. Default: binary (credit tracks passed).
    continuous: bool = False
    credit: float = 0.0
    # Whether this stage gates the ones after it (the absorbing convention).
    # Two independent properties got conflated under `continuous`: partial
    # credit, and gating. A rubric whose stages are INDEPENDENT rather than a
    # pipeline (e.g. three separate quantities in one task) wants binary credit
    # but no gating, which `continuous` could only express by also claiming a
    # partial credit it never computes. Set `gating: false` on a stage for that.
    # Default follows `continuous` so every pre-existing rubric is unchanged.
    gates: bool = True


@dataclass
class Grade:
    # `score` ∈ [0,1]: rubric-weighted reach under the absorbing
    # convention (normalized prefix product) — see RuleJudge.grade.
    score: float
    stages: dict[str, bool]
    stage_grades: list[StageGrade]
    failure_mode: str
    judge_kind: str
    judge_notes: str | None = None
    # Additional grades of the SAME trial by other judges (e.g. an LLM
    # judge alongside the rule judge). Purely additive: `score` and every
    # metric derived from it always come from the PRIMARY judge, so a run
    # stays deterministic and regradeable no matter what else graded it.
    # Each entry is a full Grade dict, told apart by its `judge_kind`.
    alt_grades: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "stages": self.stages,
            "stage_grades": [
                {"id": s.id, "passed": s.passed, "weight": s.weight,
                 "description": s.description, "evidence": s.evidence,
                 "metrics": s.metrics, "continuous": s.continuous,
                 "credit": s.credit, "gates": s.gates}
                for s in self.stage_grades
            ],
            "failure_mode": self.failure_mode,
            "judge_kind": self.judge_kind,
            "judge_notes": self.judge_notes,
            "alt_grades": self.alt_grades,
        }


@dataclass
class Rubric:
    # Stages are kept as raw dicts (consumed directly by the judge and
    # by cli aggregation). Each stage has the shape
    # `{id, description?, weight, checks: [{<check-name>: {params}}], expected_tool_calls?}`.
    stages: list[dict] = field(default_factory=list)
    rubric_type: str = "stagewise"
    # pass@k / pass^k definition. None -> a trial "passes" iff every stage passes
    # (the binary all-stages criterion; correct for binary-only rubrics). A float
    # in [0,1] -> a trial passes iff its per-trial reach R_j >= this threshold,
    # which is the meaningful definition once the rubric has continuous stages
    # (all-stages is then almost never met). Tunable; runs can be regraded.
    pass_threshold: float | None = None

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Rubric":
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls.from_block(data)

    @classmethod
    def from_block(cls, data: dict | None) -> "Rubric":
        """Build a Rubric from an inline `rubric:` mapping (e.g. the
        `rubric:` block of a benchmark.yaml) or a loaded rubric.yaml.
        """
        data = data or {}
        pt = data.get("pass_threshold")
        return cls(
            stages=data.get("stages", []) or [],
            rubric_type=data.get("type", "stagewise"),
            pass_threshold=(float(pt) if pt is not None else None),
        )

    def total_weight(self) -> float:
        return sum(s.get("weight", 0.0) for s in self.stages)

    def validate(self) -> None:
        """Raise ValueError on a malformed rubric. Called at load time."""
        if self.rubric_type != "stagewise":
            # `final_output` is documented but not implemented in v0.1.
            raise ValueError(
                f"unsupported rubric type {self.rubric_type!r}; "
                "only 'stagewise' is implemented"
            )
        if not self.stages:
            raise ValueError("rubric has no stages")
        for i, s in enumerate(self.stages):
            sid = s.get("id")
            if not sid:
                raise ValueError(f"rubric stage {i} is missing 'id'")
            if not isinstance(s.get("weight"), (int, float)):
                raise ValueError(f"rubric stage {sid!r} needs a numeric 'weight'")
            if not s.get("checks"):
                raise ValueError(
                    f"rubric stage {sid!r} has no checks (need a `checks:` list)"
                )


class Task:
    """A benchmark family — carries only the invariants shared by every
    variant. Per-variant prompts and sandbox come from `Variant`."""
    name: str = ""
    rubric: Rubric
    BENCHMARK_DIR: Path
