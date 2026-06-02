"""
Harness schema + discovery.

A *harness* configures the agent RUNTIME (orchestral, claude_code, ...),
the PROVIDER it drives (anthropic / openai / ...), the core/primitive
tools it supplies, and its loop policy. The MODEL is deliberately NOT
part of a harness — it is a run-time flag, so one harness serves many
models.

Harness files live under a benchmark's `harnesses/` directory. A
provider-agnostic runtime is a directory of provider files
(`harnesses/orchestral/anthropic.yaml`); a provider-locked runtime is a
flat file (`harnesses/claude_code.yaml`). A harness's *id* is its path
under `harnesses/` minus the `.yaml` suffix (`orchestral/anthropic`,
`claude_code`).

The `core:` block is one of:
  - `core: { tools: [RunCommandTool, ...] }` — we supply the named
    orchestral primitives (resolved via tool_policy._build_core_tool).
  - `core: { builtin: true }` — the runtime ships its own core tools
    (e.g. claude_code / codex); we supply none.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Harness:
    name: str
    runtime: dict                       # {name, version}
    provider: dict                      # {name, ...default request params}
    core: dict                          # {tools: [...]} XOR {builtin: true}
    loop: dict = field(default_factory=dict)
    id: str = ""                        # path-stem id, e.g. "orchestral/anthropic"

    @property
    def runtime_name(self) -> str:
        return self.runtime.get("name", "")

    @property
    def provider_name(self) -> str:
        return self.provider.get("name", "")

    @classmethod
    def from_dict(cls, data: dict | None, *, id: str = "") -> "Harness":
        data = data or {}
        return cls(
            name=data.get("name", id),
            runtime=data.get("runtime") or {},
            provider=data.get("provider") or {},
            core=data.get("core") or {},
            loop=data.get("loop") or {},
            id=id,
        )

    def validate(self) -> None:
        if not self.runtime.get("name"):
            raise ValueError(f"harness {self.id!r}: runtime.name is required")
        if not self.provider.get("name"):
            raise ValueError(f"harness {self.id!r}: provider.name is required")
        has_tools = "tools" in self.core
        has_builtin = bool(self.core.get("builtin"))
        if has_tools == has_builtin:
            raise ValueError(
                f"harness {self.id!r}: `core` must have exactly one of "
                "`tools: [...]` (supply orchestral primitives) or "
                "`builtin: true` (the runtime ships its own)."
            )


def harnesses_dir(benchmark_dir: str | Path) -> Path:
    return Path(benchmark_dir) / "harnesses"


def discover_harnesses(benchmark_dir: str | Path) -> dict[str, Harness]:
    """Map harness-id -> Harness for every yaml under `harnesses/`.

    The id is the path relative to `harnesses/` with `.yaml` removed,
    forward-slashed: `harnesses/orchestral/anthropic.yaml` ->
    `orchestral/anthropic`; `harnesses/claude_code.yaml` -> `claude_code`.
    """
    root = harnesses_dir(benchmark_dir)
    out: dict[str, Harness] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*.yaml")):
        hid = path.relative_to(root).with_suffix("").as_posix()
        out[hid] = _load_file(path, hid)
    return out


def load_harness(benchmark_dir: str | Path, harness_id: str) -> Harness:
    root = harnesses_dir(benchmark_dir)
    path = root / (harness_id + ".yaml")
    if not path.is_file():
        avail = sorted(discover_harnesses(benchmark_dir))
        raise FileNotFoundError(
            f"no harness {harness_id!r} (looked for {path}). Available: {avail}"
        )
    return _load_file(path, harness_id)


def _load_file(path: Path, hid: str) -> Harness:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    h = Harness.from_dict(data, id=hid)
    h.validate()
    return h
