"""
Loadout schema + discovery.

A *loadout* is a toolbench ablation arm: a toolkit (the tools the agent is
equipped with, beyond the harness's core) plus optional skills. The
toolkit is an ordered list of `sources`, each a single-key mapping whose
key is the backend (`python`, `toolbase`, or `mcp`) and whose value is
that backend's config — the same key-as-discriminator shape used by
rubric checks.

Loadouts live one file per loadout under a benchmark's `loadouts/`
directory; the loadout's name is the filename stem.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_BACKENDS = ("python", "toolbase", "mcp")


@dataclass
class Source:
    backend: str            # "python" | "toolbase"
    config: object          # python: a module/path str; toolbase: a dict
    options: dict = field(default_factory=dict)   # sibling keys, e.g. {"select": [...]}

    @classmethod
    def from_entry(cls, entry: dict, *, loadout: str) -> "Source":
        # A source entry is a mapping with exactly one *backend* key
        # (`python` / `toolbase` / `mcp`) plus any option siblings
        # (e.g. `select`):
        #   - { python: tools/dunderkit.py, select: [additive] }
        #   - { toolbase: { profile: my-profile } }
        #   - { mcp: { command: ["npx", "@some/mcp-server"] } }
        #   - { mcp: { url: "https://host/mcp" }, select: [search] }
        if not isinstance(entry, dict):
            raise ValueError(
                f"loadout {loadout!r}: each `tools.sources` entry must be a "
                f"mapping with one backend key; got {entry!r}"
            )
        backends = [k for k in entry if k in VALID_BACKENDS]
        if len(backends) != 1:
            raise ValueError(
                f"loadout {loadout!r}: each source must name exactly one backend "
                f"({' / '.join(VALID_BACKENDS)}); got keys {sorted(entry)}"
            )
        backend = backends[0]
        options = {k: v for k, v in entry.items() if k != backend}
        return cls(backend=backend, config=entry[backend], options=options)

    @property
    def select(self):
        return self.options.get("select")


@dataclass
class Loadout:
    name: str
    sources: list[Source] = field(default_factory=list)
    skills: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict | None, *, name: str = "") -> "Loadout":
        data = data or {}
        name = data.get("name", name)
        raw_sources = ((data.get("tools") or {}).get("sources")) or []
        sources = [Source.from_entry(e, loadout=name) for e in raw_sources]
        return cls(name=name, sources=sources, skills=data.get("skills") or [])


def loadouts_dir(benchmark_dir: str | Path) -> Path:
    return Path(benchmark_dir) / "loadouts"


def _search_dirs(benchmark_dir) -> list[Path]:
    """Normalize a single benchmark dir or a search path (a benchmark's
    `search_dirs`, child first when it extends another) to a dir list."""
    if isinstance(benchmark_dir, (str, Path)):
        return [Path(benchmark_dir)]
    return [Path(d) for d in benchmark_dir]


def discover_loadouts(benchmark_dir) -> dict[str, Loadout]:
    """Map name -> Loadout for every `loadouts/<name>.yaml`.

    `benchmark_dir` is one dir or a search path of dirs; a name found in
    an earlier dir shadows the same name in a later one (extends
    semantics). Each file's relative paths anchor at its own benchmark
    dir, so an inherited loadout keeps pointing at the parent's tools.
    """
    out: dict[str, Loadout] = {}
    for d in _search_dirs(benchmark_dir):
        root = loadouts_dir(d)
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.yaml")):
            if path.stem not in out:
                out[path.stem] = _load_file(path, path.stem)
    return out


def load_loadout(benchmark_dir, name: str) -> Loadout:
    looked = []
    for d in _search_dirs(benchmark_dir):
        path = loadouts_dir(d) / (name + ".yaml")
        if path.is_file():
            return _load_file(path, name)
        looked.append(str(path))
    avail = sorted(discover_loadouts(benchmark_dir))
    raise FileNotFoundError(
        f"no loadout {name!r} (looked for {', '.join(looked)}). "
        f"Available: {avail}"
    )


def _load_file(path: Path, name: str) -> Loadout:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    loadout = Loadout.from_dict(data, name=name)
    # A `python:` source may name a benchmark-local module by relative path
    # (e.g. `tools/euclid.py`). Resolve it against the benchmark dir — the
    # parent of this `loadouts/` directory — so it works regardless of cwd.
    bench_dir = path.parent.parent
    for src in loadout.sources:
        if src.backend == "python" and isinstance(src.config, str):
            candidate = bench_dir / src.config
            if candidate.exists():
                src.config = str(candidate.resolve())
    # Skill `file:` paths are benchmark-relative too (e.g.
    # `./skills/distance_recipe.md`). Resolve unconditionally — a skill
    # that doesn't exist must fail loudly at trial setup, not be left as
    # a cwd-dependent relative path that may or may not resolve later.
    for skill in loadout.skills:
        if isinstance(skill, dict) and isinstance(skill.get("file"), str):
            p = Path(skill["file"])
            if not p.is_absolute():
                skill["file"] = str((bench_dir / p).resolve())
    return loadout
