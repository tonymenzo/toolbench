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

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "stages": self.stages,
            "stage_grades": [
                {"id": s.id, "passed": s.passed, "weight": s.weight,
                 "description": s.description, "evidence": s.evidence}
                for s in self.stage_grades
            ],
            "failure_mode": self.failure_mode,
            "judge_kind": self.judge_kind,
            "judge_notes": self.judge_notes,
        }


@dataclass
class Rubric:
    # Stages are kept as raw dicts (consumed directly by the judge and
    # by cli aggregation). Each stage has the shape
    # `{id, description?, weight, checks: [{<check-name>: {params}}], expected_tool_calls?}`.
    stages: list[dict] = field(default_factory=list)
    rubric_type: str = "stagewise"

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
        return cls(
            stages=data.get("stages", []) or [],
            rubric_type=data.get("type", "stagewise"),
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
