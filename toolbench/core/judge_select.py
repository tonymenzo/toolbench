"""
Judge selection: which judge(s) grade a run, and how they are addressed.

Separation of concerns
----------------------
*What* is assessed is benchmark content — the rubric, in `benchmark.yaml`.
*Who* assesses it is a property of a measurement run, so it is selectable
per run and never edits the benchmark.

Resolution precedence (first wins):

    1. CLI            --judge / --judge-harness / --judge-model
    2. harness config the harness's optional `judge:` block
    3. default        rule

A judge is addressed exactly like the agent under test — as an
`(harness, model)` pair resolved through the same runtime/provider
registry — so a subscription runtime (`claude-code/default`,
`codex/default`) and an API provider (`orchestral/anthropic`) are both
valid judges with no special-casing. That is what makes the
subscription-grades-API and API-grades-subscription cross-product work.

Dual grading
------------
`--judge rule+llm` runs both. The RULE grade is always primary: `score`
and every metric derived from it come from the deterministic judge, so a
run stays reproducible and `regrade`-able forever. The LLM grade is
attached additively in `Grade.alt_grades`. `--judge llm` (LLM only)
exists for ablations; it should not produce a headline number, because
the score would then drift with the judge model's version.

Cost
----
A subscription-routed judge follows the same convention as the
subscription agent harnesses: `provider: subscription` is credential-free
and never priced, so no cost is attributed to it and `--max-cost-usd`
does not bind on it. Bound subscription judges by call count instead.
"""

from dataclasses import dataclass, field

# The kinds a `--judge` value may name.
KNOWN_KINDS = ("rule", "llm")
DEFAULT_KIND = "rule"


@dataclass(frozen=True)
class JudgeSpec:
    """Which judges to run, in order. `kinds[0]` is primary."""
    kinds: tuple[str, ...] = (DEFAULT_KIND,)
    harness: str | None = None      # e.g. "orchestral/anthropic"
    model: str | None = None        # e.g. "claude-opus-4-8"
    # Free-form extras from a harness `judge:` block (max_tokens, ...).
    params: dict = field(default_factory=dict)

    @property
    def primary(self) -> str:
        return self.kinds[0]

    @property
    def wants_llm(self) -> bool:
        return "llm" in self.kinds

    def label(self) -> str:
        """Human-readable judge identity for the run summary/manifest."""
        base = " + ".join(self.kinds)
        if not self.wants_llm:
            return base
        who = self.model or "<model?>"
        via = self.harness or "<harness?>"
        return f"{base} ({who} via {via})"


def parse_kinds(value: str | None) -> tuple[str, ...]:
    """`"rule+llm"` -> `("rule", "llm")`. Order is significant."""
    if not value:
        return (DEFAULT_KIND,)
    kinds = tuple(k.strip().lower() for k in value.replace(",", "+").split("+")
                  if k.strip())
    if not kinds:
        return (DEFAULT_KIND,)
    unknown = [k for k in kinds if k not in KNOWN_KINDS]
    if unknown:
        raise ValueError(
            f"unknown judge kind(s) {unknown}; known: {list(KNOWN_KINDS)}")
    if len(set(kinds)) != len(kinds):
        raise ValueError(f"duplicate judge kinds in {value!r}")
    return kinds


def resolve(harness_judge: dict | None = None, *,
            cli_judge: str | None = None,
            cli_harness: str | None = None,
            cli_model: str | None = None) -> JudgeSpec:
    """Merge the harness `judge:` block with CLI overrides.

    Each field falls back independently, so a harness can pin the judge
    model while the CLI flips between `rule` and `rule+llm`.
    """
    hj = dict(harness_judge or {})
    kinds = parse_kinds(cli_judge if cli_judge is not None else hj.get("kind"))
    params = {k: v for k, v in hj.items()
              if k not in ("kind", "harness", "model")}
    spec = JudgeSpec(
        kinds=kinds,
        harness=cli_harness or hj.get("harness"),
        model=cli_model or hj.get("model"),
        params=params,
    )
    spec.validate()
    return spec


def _validate(self: JudgeSpec) -> None:
    if self.wants_llm and not self.harness:
        raise ValueError(
            "an LLM judge needs a harness: pass --judge-harness "
            "(e.g. orchestral/anthropic, claude-code/default) or set "
            "`judge: {harness: ...}` in the harness config")


JudgeSpec.validate = _validate


# ----------------------------------------------------------------------
# Construction
# ----------------------------------------------------------------------

class _CliJudgeLLM:
    """Adapter making a subscription CLI agent usable as a one-shot judge.

    `claude -p` / `codex exec` own the model call, exactly as they do when
    driving a trial. Following the subscription-harness convention, no cost
    is attributed (`provider: subscription` is credential-free and never
    priced), so `--max-cost-usd` does not bind on a subscription judge —
    bound it by call count instead.

    Exposes the same `get_response(context, **kw)` surface as an orchestral
    LLM so `LLMJudge` does not care which route it got.
    """

    def __init__(self, runtime: str, model: str | None, timeout_s: int = 300):
        self.runtime, self.model, self.timeout_s = runtime, model, timeout_s

    def _argv(self, prompt: str) -> list[str]:
        if self.runtime == "claude_code":
            argv = ["claude", "-p", prompt, "--output-format", "text"]
            if self.model:
                argv += ["--model", self.model]
            return argv
        if self.runtime == "codex":
            argv = ["codex", "exec", prompt]
            if self.model:
                argv += ["--model", self.model]
            return argv
        raise ValueError(f"no subscription judge adapter for runtime {self.runtime!r}")

    def get_response(self, context, **_kw):
        import os
        import subprocess
        from types import SimpleNamespace
        sys_p = getattr(context, "system_prompt", None) or ""
        msgs = context.get_messages() if hasattr(context, "get_messages") else []
        user = "\n\n".join(
            str(getattr(m, "text", None) or getattr(m, "content", "") or "")
            for m in msgs)
        prompt = f"{sys_p}\n\n---\n\n{user}" if sys_p else user
        # Subscription auth via the logged-in CLI: never inject an API key.
        # toolbench loads .env into os.environ, so ANTHROPIC_API_KEY is
        # normally present and would push the CLI onto the API path — the
        # same reason the claude_code RUNTIME strips it (runtime.py:371).
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        out = subprocess.run(self._argv(prompt), capture_output=True, text=True,
                             timeout=self.timeout_s, env=env)
        if out.returncode != 0:
            raise RuntimeError(
                f"{self.runtime} judge exited {out.returncode}: "
                f"{(out.stderr or '').strip()[:300]}")
        return SimpleNamespace(message=SimpleNamespace(content=out.stdout),
                               usage=None)


def build_judge(spec: JudgeSpec, *, registry=None, benchmark_dir=None,
                harnesses: dict | None = None):
    """Materialize the judge(s) named by `spec`.

    `harnesses` is the benchmark's harness map (from `discover_harnesses`),
    used to resolve the JUDGE's route — which may be a different harness
    from the one running the agent. That indirection is the whole point:
    it is what lets a subscription model judge an API model's run and
    vice versa.
    """
    from .judge import DualJudge, LLMJudge, RuleJudge

    built = []
    for kind in spec.kinds:
        if kind == "rule":
            built.append(RuleJudge(registry=registry, benchmark_dir=benchmark_dir))
        elif kind == "llm":
            built.append(_build_llm_judge(spec, benchmark_dir, harnesses))
        else:                                    # pragma: no cover - parse guards
            raise ValueError(f"unknown judge kind {kind!r}")
    return built[0] if len(built) == 1 else DualJudge(built)


def build_llm_judge(spec: JudgeSpec, *, benchmark_dir=None, harnesses=None):
    """The LLM judge alone, or None when `spec` names no LLM judge.

    The post-grade companion to the primary rule judge in the runner: the
    rule grade is computed first and stays authoritative, then this judge
    (when configured) runs serially against the finished sandbox and its
    result is attached additively. Returning None keeps the common
    rule-only path free of any judge-construction cost.
    """
    if not spec.wants_llm:
        return None
    return _build_llm_judge(spec, benchmark_dir, harnesses)


def _build_llm_judge(spec: JudgeSpec, benchmark_dir, harnesses):
    from .judge import LLMJudge

    h = (harnesses or {}).get(spec.harness)
    if h is None:
        known = sorted(harnesses or {})
        raise ValueError(
            f"judge harness {spec.harness!r} not found for this benchmark; "
            f"available: {known}")
    runtime = h.runtime_name
    model = spec.model or h.provider.get("model")

    if runtime == "orchestral":
        from .llm_factory import build_llm
        llm = build_llm(provider=h.provider_name, model=model)
    else:
        llm = _CliJudgeLLM(runtime, model,
                           timeout_s=int(h.runtime.get("call_timeout_s", 300)))
    return LLMJudge(
        llm=llm, model=model, harness_id=spec.harness,
        benchmark_dir=benchmark_dir,
        max_tokens=int(spec.params.get("max_tokens", 1024)),
    )
