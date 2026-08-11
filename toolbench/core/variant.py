"""
Variant schema + discovery.

A *variant* is a toolbench scaffolding axis, orthogonal to the loadout (tools):
it bundles the things that change between difficulty rungs — the prompts
the agent sees and the sandbox it starts from — while the family-level
rubric, ground truth, and checks stay invariant so cross-rung deltas
remain comparable.

Each variant lives in its own self-contained directory under a benchmark's
`variants/`, with a `variant.yaml` plus its own `prompts/` and (optional)
`sandbox/template/`. A variant carries `axes:` labels describing which
modular components it includes (e.g. `{spec: under, env: full}`) — the
reporting layer uses these to attribute reach deltas to a single capability
without anyone hardcoding which rungs to subtract.
"""

import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Variant:
    """A scaffolding-ablation arm of a benchmark.

    Holds the per-variant assets (prompts, sandbox template) and the
    component-axis labels that let the reporting layer attribute reach
    deltas to a single capability. Resolved paths are absolute; the
    `axes` mapping is a documentation contract used by reporting.
    """
    name: str
    variant_dir: Path
    description: str = ""
    axes: dict[str, str] = field(default_factory=dict)
    user_prompt_file: Path | None = None
    system_prompt_file: Path | None = None
    template_dir: Path | None = None
    # sha256 of each seeded file, captured by setup_workspace so the end of
    # the trial can tell whether the agent rewrote its own contract.
    _template_manifest: dict[str, str] = field(default_factory=dict,
                                               repr=False)

    @classmethod
    def from_dict(cls, data: dict | None, *, name: str,
                  variant_dir: str | Path) -> "Variant":
        """Build a Variant from a `variant.yaml` mapping.

        Path fields are resolved relative to `variant_dir` so a variant
        directory can be moved or copied without rewriting its yaml.
        """
        data = data or {}
        vdir = Path(variant_dir).resolve()

        def _resolve(rel: str | None) -> Path | None:
            return (vdir / rel).resolve() if rel else None

        sandbox = data.get("sandbox") or {}
        return cls(
            name=data.get("name", name),
            variant_dir=vdir,
            description=data.get("description", "") or "",
            axes=dict(data.get("axes") or {}),
            user_prompt_file=_resolve(data.get("user_prompt_file")),
            system_prompt_file=_resolve(data.get("system_prompt_file")),
            template_dir=_resolve(sandbox.get("template_dir")),
        )

    def read_user_prompt(self) -> str:
        return (self.user_prompt_file.read_text()
                if self.user_prompt_file and self.user_prompt_file.is_file()
                else "")

    def read_system_prompt(self) -> str:
        return (self.system_prompt_file.read_text()
                if self.system_prompt_file and self.system_prompt_file.is_file()
                else "")

    def setup_workspace(self, base_directory: str | Path) -> None:
        """Materialize this variant's sandbox seed into `base_directory`.

        If no `template_dir` is configured, just create the (empty) sandbox.
        The hardest rungs ship no scaffolding by design — that is the point
        of the env-bare arm, not an error.
        """
        base = Path(base_directory)
        base.mkdir(parents=True, exist_ok=True)
        self._template_manifest = self.template_manifest()
        if self.template_dir is None:
            return
        if not self.template_dir.exists():
            raise FileNotFoundError(
                f"variant {self.name!r}: sandbox.template_dir does not exist: "
                f"{self.template_dir}. Either point it at an existing dir or "
                "omit the `sandbox:` block to start from an empty sandbox."
            )
        ignore = shutil.ignore_patterns(".DS_Store")
        for item in self.template_dir.iterdir():
            if item.name == ".DS_Store":
                continue
            target = base / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True, ignore=ignore)
            else:
                shutil.copy2(item, target)

    def template_manifest(self) -> dict[str, str]:
        """Map sandbox-relative path -> sha256 for every seeded file.

        Taken at setup so the same files can be re-checked when the trial
        ends; see `verify_workspace`.
        """
        if self.template_dir is None or not self.template_dir.exists():
            return {}
        out: dict[str, str] = {}
        for src in sorted(self.template_dir.rglob("*")):
            if not src.is_file() or src.name == ".DS_Store":
                continue
            rel = src.relative_to(self.template_dir).as_posix()
            out[rel] = hashlib.sha256(src.read_bytes()).hexdigest()
        return out

    def verify_workspace(self, base_directory: str | Path) -> dict[str, str]:
        """Report seeded files the agent deleted or rewrote.

        The sandbox seed is a CONTRACT, not a suggestion: `bare` ships
        exactly one file, results/answer_schema.json, and the whole rubric
        is written against the field names in it. Nothing stops an agent
        from clobbering it -- codex's apply_patch `*** Add File:` silently
        overwrites an existing path (verified against codex-cli 0.146.0,
        which still reports `kind: "add"` for the overwrite). An agent that
        does so can then validate its answer against its OWN substitute and
        get a clean pass locally while missing every graded key.

        That is exactly what happened to gpt-5.6-sol / bare / tools_defined
        / seed 1001 on 2026-08-10: it listed the sandbox with
        `rg --files -g '!results/**'`, never saw the schema it had been
        given, wrote its own over the top, self-validated against that, and
        scored 0.0 on a trial whose physics was otherwise worth ~0.81. The
        trial record said only MODEL_STOPPED_EARLY and the agent's own
        write-up asserted the file "was absent", so the cause took a full
        forensic pass to recover.

        This does NOT prevent the overwrite -- protecting the file would
        change the affordances of the sandbox mid-study and make new trials
        non-comparable with old ones. It records it, so a zero of this kind
        is legible immediately instead of looking like a physics failure.
        """
        base = Path(base_directory)
        drift: dict[str, str] = {}
        for rel, digest in (self._template_manifest or {}).items():
            live = base / rel
            if not live.is_file():
                drift[rel] = "deleted"
            elif hashlib.sha256(live.read_bytes()).hexdigest() != digest:
                drift[rel] = "overwritten"
        return drift


def variants_dir(benchmark_dir: str | Path) -> Path:
    return Path(benchmark_dir) / "variants"


def discover_variants(benchmark_dir: str | Path) -> dict[str, Variant]:
    """Map variant-name -> Variant for every `variants/<name>/variant.yaml`.

    The variant's name is its directory name; the `name:` field inside
    variant.yaml is allowed for documentation but must match the dir.
    """
    root = variants_dir(benchmark_dir)
    out: dict[str, Variant] = {}
    if not root.is_dir():
        return out
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        yaml_path = sub / "variant.yaml"
        if not yaml_path.is_file():
            continue
        out[sub.name] = _load_file(yaml_path, sub.name)
    return out


def load_variant(benchmark_dir: str | Path, name: str) -> Variant:
    path = variants_dir(benchmark_dir) / name / "variant.yaml"
    if not path.is_file():
        avail = sorted(discover_variants(benchmark_dir))
        raise FileNotFoundError(
            f"no variant {name!r} (looked for {path}). Available: {avail}"
        )
    return _load_file(path, name)


def _load_file(path: Path, name: str) -> Variant:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    v = Variant.from_dict(data, name=name, variant_dir=path.parent)
    declared = data.get("name")
    if declared and declared != name:
        raise ValueError(
            f"variant {name!r} (dir-derived) has mismatched `name: {declared!r}` "
            f"in {path}. Either remove the `name:` field or make it match the dir."
        )
    return v
