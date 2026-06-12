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

A benchmark may declare `extends: <path-to-sibling-benchmark-dir>` to
inherit another benchmark's assets. The child is a *distinct* benchmark
(own name, own version, own run cells) that overrides whole top-level
keys — typically the rubric and prompts — while inheriting everything it
doesn't declare: ground truth, checks, harnesses, loadouts, variants,
defaults. This is how a family of sibling benchmarks shares one set of
assets without duplicating directories (e.g. a shape-only or yield-only
grading of the same underlying task). Inheritance is depth-1 by design:
a parent must be self-contained, so overlay chains can't accumulate.
Identity keys (`name`, `version`, `description`) are never inherited.
"""

import copy
from pathlib import Path

import yaml

from toolbench.core.artifact_policy import ArtifactPolicy
from toolbench.core.task import Rubric, Task
from toolbench.core.variant import Variant, discover_variants

# Keys that identify a benchmark rather than configure it — a child
# overlay never inherits these from its parent.
_IDENTITY_KEYS = ("name", "version", "description", "extends")


def _load_layer(bench_dir: Path) -> dict:
    """Load one benchmark.yaml and anchor its declared paths.

    `ground_truth.dir` and `checks` are resolved to absolute paths here,
    against the directory that declared them, so that after an `extends`
    merge each path still points where its own yaml said — an inherited
    ground truth must not silently re-anchor at the child.
    """
    with open(bench_dir / "benchmark.yaml") as f:
        cfg = yaml.safe_load(f) or {}
    gt = cfg.get("ground_truth")
    if isinstance(gt, dict) and gt.get("dir"):
        cfg["ground_truth"] = {**gt, "dir": str((bench_dir / gt["dir"]).resolve())}
    if cfg.get("checks"):
        cfg["checks"] = str((bench_dir / cfg["checks"]).resolve())
    return cfg


class YamlBenchmark(Task):
    """A `Task` family materialized from a `benchmark.yaml`."""

    def __init__(self, benchmark_dir: str | Path):
        self.BENCHMARK_DIR = Path(benchmark_dir).resolve()
        child_cfg = _load_layer(self.BENCHMARK_DIR)

        self.extends_dir: Path | None = None
        if child_cfg.get("extends"):
            self.extends_dir = self._resolve_parent(child_cfg["extends"])
            parent_cfg = _load_layer(self.extends_dir)
            if parent_cfg.get("extends"):
                raise ValueError(
                    f"benchmark at {self.BENCHMARK_DIR} extends "
                    f"{self.extends_dir}, which itself extends "
                    f"{parent_cfg['extends']!r}. Inheritance is depth-1: "
                    "a parent benchmark must be self-contained."
                )
            inherited = {k: v for k, v in parent_cfg.items()
                         if k not in _IDENTITY_KEYS}
            self.cfg = {**inherited, **child_cfg}
        else:
            self.cfg = child_cfg

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

        # Variants: union of parent and child, child shadowing by name —
        # an overlay usually ships its own prompts (the ask is what
        # changed) but may inherit rungs it doesn't restate.
        self._variants: dict[str, Variant] = {}
        if self.extends_dir is not None:
            self._variants.update(discover_variants(self.extends_dir))
        self._variants.update(discover_variants(self.BENCHMARK_DIR))
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

    def _resolve_parent(self, extends: str) -> Path:
        """Validate and resolve the `extends:` target directory."""
        parent = (self.BENCHMARK_DIR / extends).resolve()
        if parent == self.BENCHMARK_DIR:
            raise ValueError(
                f"benchmark at {self.BENCHMARK_DIR}: `extends` points at "
                "itself."
            )
        if not (parent / "benchmark.yaml").is_file():
            raise FileNotFoundError(
                f"benchmark at {self.BENCHMARK_DIR}: `extends: {extends}` "
                f"resolves to {parent}, which holds no benchmark.yaml."
            )
        return parent

    # --- path accessors -------------------------------------------------
    @property
    def search_dirs(self) -> list[Path]:
        """Directories searched for discovery-based assets (harnesses,
        loadouts, rubric `reference:` paths): the benchmark's own dir
        first, then the extended parent's, so the child shadows."""
        dirs = [self.BENCHMARK_DIR]
        if self.extends_dir is not None:
            dirs.append(self.extends_dir)
        return dirs

    def resolved_config(self) -> dict:
        """The post-merge config, with `ground_truth.dir` and `checks`
        absolute — the manifest embeds this so a run of an overlay
        benchmark records what the parent said at run time, not just a
        pointer that may drift."""
        out = copy.deepcopy(self.cfg)
        out["extends"] = str(self.extends_dir) if self.extends_dir else None
        return out

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
