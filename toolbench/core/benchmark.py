"""
Declarative (YAML) benchmark.

A benchmark directory ships a family-level `benchmark.yaml` (plus shared
`harnesses/`, `loadouts/`, `ground_truth/`, optional `checks/`) and one
or more self-contained variants under `variants/<name>/`, each with its
own `variant.yaml` + `prompts/` + (optional) `sandbox/template/`.

`YamlBenchmark` reads the family yaml and discovers variants; per-trial
prompts and sandbox come from the chosen `Variant`. Rubric, ground truth,
and benchmark-local checks are family-level invariants (constant across
variants) so cross-variant reach deltas remain comparable.
"""

from pathlib import Path

import yaml

from toolbench.core.artifact_policy import ArtifactPolicy
from toolbench.core.task import Rubric, Task
from toolbench.core.variant import Variant, discover_variants


class YamlBenchmark(Task):
    """A `Task` family materialized from a `benchmark.yaml`."""

    def __init__(self, benchmark_dir: str | Path):
        self.BENCHMARK_DIR = Path(benchmark_dir).resolve()
        with open(self.BENCHMARK_DIR / "benchmark.yaml") as f:
            self.cfg = yaml.safe_load(f) or {}

        self.name = self.cfg.get("name") or self.BENCHMARK_DIR.name
        self.version = self.cfg.get("version", "")
        self.description = self.cfg.get("description", "")
        self.default_harness = self.cfg.get("default_harness")
        self.default_loadout = self.cfg.get("default_loadout")

        self.rubric = Rubric.from_block(self.cfg.get("rubric"))
        self.rubric.validate()

        # What sandbox cleanup preserves for regrade. Defaults cover the
        # common artifact types; a benchmark whose deliverables fall
        # outside them must declare an `artifacts:` block (see
        # toolbench/core/artifact_policy.py).
        try:
            self.artifact_policy = ArtifactPolicy.from_block(
                self.cfg.get("artifacts"))
        except ValueError as e:
            raise ValueError(f"benchmark {self.name!r}: {e}") from e

        self._variants: dict[str, Variant] = discover_variants(self.BENCHMARK_DIR)
        if not self._variants:
            raise ValueError(
                f"benchmark {self.name!r}: no variants discovered under "
                f"{self.BENCHMARK_DIR / 'variants'}. Each benchmark needs at "
                "least one `variants/<name>/variant.yaml` (single-variant "
                "benchmarks usually call this `default`)."
            )
        self.default_variant = self.cfg.get("default_variant")
        if self.default_variant is None and len(self._variants) == 1:
            self.default_variant = next(iter(self._variants))
        if self.default_variant and self.default_variant not in self._variants:
            raise ValueError(
                f"benchmark {self.name!r}: default_variant "
                f"{self.default_variant!r} is not among discovered variants "
                f"{sorted(self._variants)}."
            )

    # --- path accessors -------------------------------------------------
    def _resolve(self, rel: str | None) -> Path | None:
        return (self.BENCHMARK_DIR / rel).resolve() if rel else None

    @property
    def ground_truth_dir(self) -> Path | None:
        return self._resolve((self.cfg.get("ground_truth") or {}).get("dir"))

    def checks_module_path(self) -> Path | None:
        """Filesystem path to the benchmark-local `checks/checks.py`, if any."""
        return self._resolve(self.cfg.get("checks"))

    # --- variants -------------------------------------------------------
    @property
    def variants(self) -> dict[str, Variant]:
        return dict(self._variants)

    def get_variant(self, name: str | None = None) -> Variant:
        """Return the named variant, or the default if `name` is None."""
        chosen = name or self.default_variant
        if chosen is None:
            raise ValueError(
                f"benchmark {self.name!r}: no variant name supplied and no "
                "default_variant set. Available: "
                f"{sorted(self._variants)}."
            )
        if chosen not in self._variants:
            raise ValueError(
                f"benchmark {self.name!r}: unknown variant {chosen!r}. "
                f"Available: {sorted(self._variants)}."
            )
        return self._variants[chosen]
