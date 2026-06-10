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


def discover_loadouts(benchmark_dir: str | Path) -> dict[str, Loadout]:
    root = loadouts_dir(benchmark_dir)
    out: dict[str, Loadout] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.yaml")):
        out[path.stem] = _load_file(path, path.stem)
    return out


def load_loadout(benchmark_dir: str | Path, name: str) -> Loadout:
    path = loadouts_dir(benchmark_dir) / (name + ".yaml")
    if not path.is_file():
        avail = sorted(discover_loadouts(benchmark_dir))
        raise FileNotFoundError(
            f"no loadout {name!r} (looked for {path}). Available: {avail}"
        )
    return _load_file(path, name)


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
    return loadout
