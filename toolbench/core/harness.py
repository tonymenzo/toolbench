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
    # Optional default judge for runs on this harness:
    #   judge: {kind: rule+llm, harness: orchestral/anthropic, model: claude-opus-4-8}
    # A benchmark family that always wants a second opinion can pin it here
    # instead of repeating CLI flags. `--judge*` overrides field by field, so
    # a harness can fix the judge model while the CLI flips rule <-> rule+llm.
    # Note this names a judge, NOT the agent under test: `harness` here is the
    # route the JUDGE is called through, and may differ from this harness.
    judge: dict = field(default_factory=dict)

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
            judge=data.get("judge") or {},
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
        if self.judge:
            if not isinstance(self.judge, dict):
                raise ValueError(
                    f"harness {self.id!r}: `judge` must be a mapping "
                    "{kind, harness, model}")
            unknown = set(self.judge) - {"kind", "harness", "model",
                                         "max_tokens", "temperature"}
            if unknown:
                raise ValueError(
                    f"harness {self.id!r}: unknown judge key(s) "
                    f"{sorted(unknown)}; known: kind, harness, model, "
                    "max_tokens, temperature")
            # Fail at load time rather than after a paid run.
            from .judge_select import resolve
            resolve(self.judge)


def harnesses_dir(benchmark_dir: str | Path) -> Path:
    return Path(benchmark_dir) / "harnesses"


def _search_dirs(benchmark_dir) -> list[Path]:
    """Normalize a single benchmark dir or a search path (a benchmark's
    `search_dirs`, child first when it extends another) to a dir list."""
    if isinstance(benchmark_dir, (str, Path)):
        return [Path(benchmark_dir)]
    return [Path(d) for d in benchmark_dir]


def discover_harnesses(benchmark_dir) -> dict[str, Harness]:
    """Map harness-id -> Harness for every yaml under `harnesses/`.

    The id is the path relative to `harnesses/` with `.yaml` removed,
    forward-slashed: `harnesses/orchestral/anthropic.yaml` ->
    `orchestral/anthropic`; `harnesses/claude_code.yaml` -> `claude_code`.

    `benchmark_dir` is one dir or a search path of dirs; an id found in
    an earlier dir shadows the same id in a later one (extends semantics).
    """
    out: dict[str, Harness] = {}
    for d in _search_dirs(benchmark_dir):
        root = harnesses_dir(d)
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.yaml")):
            hid = path.relative_to(root).with_suffix("").as_posix()
            if hid not in out:
                out[hid] = _load_file(path, hid)
    return out


def load_harness(benchmark_dir, harness_id: str) -> Harness:
    looked = []
    for d in _search_dirs(benchmark_dir):
        path = harnesses_dir(d) / (harness_id + ".yaml")
        if path.is_file():
            return _load_file(path, harness_id)
        looked.append(str(path))
    avail = sorted(discover_harnesses(benchmark_dir))
    raise FileNotFoundError(
        f"no harness {harness_id!r} (looked for {', '.join(looked)}). "
        f"Available: {avail}"
    )


def _load_file(path: Path, hid: str) -> Harness:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    h = Harness.from_dict(data, id=hid)
    h.validate()
    return h
