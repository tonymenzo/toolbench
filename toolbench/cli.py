"""
Command-line entry for the toolbench evaluation harness.

Usage:
    toolbench run \\
        --benchmark examples/geometry \\
        --model claude-haiku-4-5 \\
        --loadouts core_only,full_local \\
        --n 3 \\
        --max-cost-usd 25

For harness validation without LLM cost:
    toolbench run --benchmark examples/geometry --model stub \\
        --loadouts core_only --n 1 --max-cost-usd 0 --dry-run

`toolbench` and the short alias `tbe` are equivalent (sibling to toolbase's
`toolbase`/`tb`).
"""

import argparse
import contextlib
import datetime
import hashlib
import importlib.metadata
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple, TypedDict

import click

# REPO_ROOT is the directory containing the `toolbench/` package (the
# repo checkout in an editable install). Used for the fallback `.env`
# location and `git rev-parse`; both degrade gracefully for wheel
# installs. Adopters wire anything deployment-specific (extra provider
# factories, runtimes, ...) in adapter modules that call the
# register_* hooks at import time.
from toolbench import REPO_ROOT
from toolbench.core.budget import Budget, BudgetExceeded
from toolbench.core.failure_modes import (
    HARD_PROCESS_FAILURES, NONE, UNKNOWN, incomplete_at,
)
from toolbench.core.litellm_pricing import fetch_pricing_from_env
from toolbench.core.llm_factory import registered_providers
from toolbench.core.judge import RuleJudge
from toolbench.core.metrics import (
    reach_bar_k, bootstrap_ci, mean, pass_at_k, pass_caret_k,
    pearson_corr_matrix, reach_at_k, reach_caret_k,
)
from toolbench.core.runner import TrialRunner
from toolbench.core.runtime import check_runtime_version, registered_runtimes
from toolbench.core.store import append_jsonl, read_json, read_jsonl, write_json
from toolbench.core.harness import discover_harnesses
from toolbench.core.loadout import discover_loadouts
from toolbench.core.tool_resolver import build_agent_tools, release_sources
from toolbench.core.variant import Variant
from toolbench.core.benchmark import YamlBenchmark
from toolbench.reporting._shared import stage_matrix_from_rows
from toolbench.reporting.k_sweep import render_k_sweep
from toolbench.reporting.parallel_coords import render_parallel_coords
from toolbench.reporting.per_stage_k import render_per_stage_k
from toolbench.reporting.summary_text import render_run_summary


# Run output is written under the current working directory so `runs/`
# sits next to the benchmarks being run, not inside the installed
# package (which would put it in site-packages for a real install).
# Tests override `_OUTPUT_BASE` to redirect it to a temp dir.
_OUTPUT_BASE: Path | None = None


def _runs_root() -> Path:
    """Directory holding all run output, resolved at call time.

    Defaults to `<cwd>/runs`; `_OUTPUT_BASE`, if set, replaces the cwd.
    """
    base = _OUTPUT_BASE if _OUTPUT_BASE is not None else Path.cwd()
    return (base / "runs").resolve()


# A trial "passes" iff every stage of the rubric passes — the
# boundary case where R_j collapses to the binary all-stages indicator
# (the accompanying manuscript §4). Empty stages dict (e.g. GRADE_ERROR) is *not*
# a pass.

# Bootstrap iterations for the per-cell 3×3 correlation of the
# three-vector (reach_bar_k, pass@k, pass^k). 500 is enough to stabilize
# Pearson r at the 0.01 level for cells with n>=3.
N_BOOTSTRAP_CORR = 500
METRIC_TRIPLET_LABELS = ["reach_bar_k", "pass_at_k", "pass_caret_k"]


class StageMatrix(NamedTuple):
    """Return shape of `_stage_matrix`.

    `matrix[j][i]` is 1 iff trial `j` passed stage `i`. `weights` is
    the rubric weight vector aligned to the same column order, or
    `None` when equal weights should be used.
    """
    matrix:  list[list[int]]
    weights: list[float] | None


class MetricCorrelations(TypedDict):
    """3×3 Pearson correlation of the three-vector, bootstrap-resampled.

    `labels` orders the rows/columns of `matrix`; degenerate entries
    (zero-variance variables, n<2) are `None`.
    """
    labels:        list[str]
    matrix:        list[list[float | None]]
    n_bootstrap:   int


class CellSummary(TypedDict):
    """Per-(model × condition) aggregation row, JSON-shaped.

    Written verbatim into `summary.json` under the `cells` list. The
    `reach_*_uniform` family carries the equal-weighted twins of the
    rubric-weighted reach metrics (see the accompanying manuscript).
    """
    model:                    str
    condition:                str
    n:                        int
    k:                        int
    mean_score:               float
    score_ci95:               list[float]
    reach_bar_k:              float
    reach_bar_k_ci95:         list[float]
    reach_bar_k_uniform:      float
    reach_bar_k_uniform_ci95: list[float]
    reach_at_k_uniform:       float
    reach_caret_k_uniform:    float
    pass_at_k:                float
    pass_caret_k:              float
    pass_at_1:                float
    metric_correlations:      MetricCorrelations
    mean_cost_usd:            float | None
    mean_wall_clock_s:        float
    stages:                   dict[str, float]
    failure_modes:            dict[str, int]


class PairedDelta(TypedDict):
    """Per-(model × condition-pair) paired comparison over shared seeds.

    `*_delta` is the mean per-seed difference (condition_b − condition_a);
    CIs are paired bootstrap percentiles over the seed dimension —
    tighter than differencing two per-cell CIs because shared-seed noise
    cancels. `None` CIs mean fewer than 2 shared seeds.
    """
    model:            str
    condition_a:      str
    condition_b:      str
    n_pairs:          int
    reach_delta:      float
    reach_delta_ci95: list[float] | None
    pass_delta:       float
    pass_delta_ci95:  list[float] | None


class AggregateResult(TypedDict):
    """Top-level return shape of `aggregate()`. Matches `summary.json`."""
    cells:           list[CellSummary]
    paired_deltas:   list[PairedDelta]
    n_total_trials:  int


def _trial_passed(row: dict) -> int:
    """Boundary-case end-to-end success: every rubric stage passed."""
    stages = row.get("stages") or {}
    return 1 if stages and all(stages.values()) else 0


def _model_slug(model: str) -> str:
    """Sanitize a model name for use in a run directory.

    Strips the provider prefix (`openai/gpt-oss-120b` -> `gpt-oss-120b`)
    and replaces filesystem-unfriendly characters (`:`, spaces, etc.)
    with dashes so the dir name stays clean.
    """
    if not model:
        return "unknown"
    if "/" in model:
        model = model.rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-") or "unknown"


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class _StdoutTee:
    """Mirror stdout writes to a log file (ANSI-stripped), while passing the
    original (possibly colored) text through to the real terminal. Lets a run
    accumulate a single clean, live-tailable `console.log` at its run root."""

    def __init__(self, stream, fp):
        self._stream, self._fp = stream, fp

    def write(self, s):
        n = self._stream.write(s)
        try:
            self._fp.write(_ANSI_RE.sub("", s))
            self._fp.flush()
        except Exception:
            pass
        return n

    def flush(self):
        self._stream.flush()
        try:
            self._fp.flush()
        except Exception:
            pass

    def __getattr__(self, name):  # delegate isatty(), encoding, fileno, ...
        return getattr(self._stream, name)


@contextlib.contextmanager
def _tee_stdout(path):
    """Tee everything printed to stdout into `path` for the duration."""
    fp = open(path, "w", encoding="utf-8")
    orig = sys.stdout
    sys.stdout = _StdoutTee(orig, fp)
    try:
        yield
    finally:
        sys.stdout = orig
        fp.close()


def _load_env_file(path) -> None:
    """Minimal `.env` loader (no python-dotenv dependency).

    Parses `KEY=VALUE` lines and uses `os.environ.setdefault`, so a real
    environment variable always wins over the file. Holds both provider
    API keys and tool config (e.g. MG5_PATH) for the no-toolbase path —
    toolbase will supply per-toolset config automatically once it's used.
    """
    try:
        p = Path(path)
        if not p.is_file():
            return
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, val)
    except Exception:
        pass


def _split(s: str | None) -> list[str]:
    """Parse a comma-separated CLI list into stripped names."""
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _pkg_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except Exception:
        return "unknown"


def _print_resolution(report: dict) -> None:
    """Print a W8-style resolution preview for one (harness, loadout)."""
    ctools = report.get("core", {}).get("tools", [])
    print(f"  harness {report['harness']} / loadout {report['loadout']}:")
    print(f"    core ({len(ctools)}): "
          + (", ".join(ctools) if ctools else "(harness builtin)"))
    for s in report.get("sources", []):
        print(f"    {s['backend']} {s['config']}: {', '.join(s['tools'])}")
        prov = s.get("provenance")
        if prov:
            versions = ", ".join(
                f"{name} {info.get('version', '?')}"
                for name, info in sorted(prov.get("toolkits", {}).items())
            )
            if versions:
                print(f"      toolkit versions: {versions} "
                      f"(toolbase {prov.get('toolbase_version', '?')})")


def _load_benchmark(bench_dir: str | None):
    """Load a `YamlBenchmark` from a directory path containing `benchmark.yaml`.
    Returns None (after printing an error) when the path holds no benchmark."""
    if bench_dir and (Path(bench_dir) / "benchmark.yaml").is_file():
        return YamlBenchmark(bench_dir)
    print(f"No benchmark at {bench_dir!r}: expected a directory containing "
          "benchmark.yaml (e.g. examples/geometry).", file=sys.stderr)
    return None


def cmd_run(args: argparse.Namespace) -> int:
    benchmark = _load_benchmark(args.benchmark)
    if benchmark is None:
        return 2
    bench_name = benchmark.name
    bench_dir = benchmark.BENCHMARK_DIR

    # Harness(es) — default from benchmark.yaml's default_harness.
    all_h = discover_harnesses(bench_dir)
    h_ids = _split(args.harnesses) or (
        [benchmark.default_harness] if getattr(benchmark, "default_harness", None) else [])
    if not h_ids:
        print("No --harness given and the benchmark has no default_harness.", file=sys.stderr)
        return 2
    bad = [h for h in h_ids if h not in all_h]
    if bad:
        print(f"Unknown harness(es): {bad}. Known: {sorted(all_h)}", file=sys.stderr)
        return 2
    harnesses = [all_h[h] for h in h_ids]

    # Loadout(s) — default from benchmark.yaml's default_loadout.
    all_l = discover_loadouts(bench_dir)
    l_names = _split(args.loadouts) or (
        [benchmark.default_loadout] if getattr(benchmark, "default_loadout", None) else [])
    if not l_names:
        print("No --loadouts given and the benchmark has no default_loadout.", file=sys.stderr)
        return 2
    bad = [n for n in l_names if n not in all_l]
    if bad:
        print(f"Unknown loadout(s): {bad}. Known: {sorted(all_l)}", file=sys.stderr)
        return 2
    loadouts = [all_l[n] for n in l_names]

    # Variant(s) — the scaffolding axis (prompt + sandbox), orthogonal to
    # the loadout (tools). Default from benchmark.yaml's default_variant.
    all_v = benchmark.variants
    v_names = _split(args.variants) or (
        [benchmark.default_variant] if benchmark.default_variant else [])
    if not v_names:
        print("No --variants given and the benchmark has no default_variant.",
              file=sys.stderr)
        return 2
    bad = [n for n in v_names if n not in all_v]
    if bad:
        print(f"Unknown variant(s): {bad}. Known: {sorted(all_v)}", file=sys.stderr)
        return 2
    variants = [all_v[n] for n in v_names]

    models = _split(args.models)
    if not models:
        print("No --models given.", file=sys.stderr)
        return 2
    if args.provider:
        print("warning: --provider is ignored; the provider comes from the harness.",
              file=sys.stderr)

    known_providers = set(registered_providers())
    known_runtimes = set(registered_runtimes())
    for h in harnesses:
        if h.provider_name not in known_providers:
            print(f"harness {h.id!r} names unknown provider {h.provider_name!r}. "
                  f"Registered: {sorted(known_providers)}", file=sys.stderr)
            return 2
        # Validate the runtime too — without this, a harness claiming an
        # unimplemented runtime (claude_code, ...) would silently run on
        # whatever the runner defaults to, mislabeling the whole run.
        if h.runtime_name not in known_runtimes:
            print(f"harness {h.id!r} names unknown runtime {h.runtime_name!r}. "
                  f"Registered: {sorted(known_runtimes)}. Register additional "
                  "runtimes via toolbench.core.runtime.register_runtime().",
                  file=sys.stderr)
            return 2
        # Enforce the harness's runtime.version spec against the installed
        # runtime — a pinned-but-unenforced version would label runs with a
        # constraint that never governed them.
        version_err = check_runtime_version(h.runtime_name,
                                            h.runtime.get("version"))
        if version_err:
            print(f"harness {h.id!r}: {version_err}", file=sys.stderr)
            return 2

    timestamp = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    run_label = args.run_label or ("dryrun" if args.dry_run else "run")
    run_id = f"{timestamp}_{bench_name}_{_model_slug(models[0])}_{run_label}"
    run_dir = _runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    seeds = [args.seed_base + i for i in range(args.n)]
    stage_order = [s["id"] for s in benchmark.rubric.stages]
    stage_weights = {s["id"]: float(s.get("weight", 0.0)) for s in benchmark.rubric.stages}

    litellm_pricing = None
    if any(h.provider_name == "litellm" for h in harnesses):
        litellm_pricing = fetch_pricing_from_env()
        if litellm_pricing:
            print(f"  Pricing snapshot: {len(litellm_pricing)} models from litellm proxy.")

    manifest = {
        "run_id": run_id,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "benchmark": bench_name,
        "benchmark_dir": str(bench_dir),
        "harnesses": [{"id": h.id, "runtime": h.runtime, "provider": h.provider,
                       "core": h.core, "loop": h.loop} for h in harnesses],
        "loadouts": l_names,
        "variants": [{"name": v.name, "description": v.description,
                      "axes": v.axes} for v in variants],
        "models": [{"model": m} for m in models],
        # legacy mirrors so v0.1 reporting (keyed on task/condition) still reads:
        "task": bench_name,
        "conditions": l_names,
        "n_per_cell": args.n,
        "seeds": seeds,
        "judge": {"kind": "rule"},
        "max_cost_usd": args.max_cost_usd,
        "max_iterations": args.max_iterations,
        "max_format_retries": args.max_format_retries,
        "continue_nudges": args.continue_nudges,
        "max_rate_limit_retries": args.max_rate_limit_retries,
        "parallel": args.parallel,
        "dry_run": args.dry_run,
        "versions": {"orchestral-ai": _pkg_version("orchestral-ai"),
                     "toolbench": _pkg_version("toolbench")},
        "rubric_total_weight": round(benchmark.rubric.total_weight(), 4),
        "pass_criterion": "all_stages",
        "reach_weights": {
            "stage_order": stage_order,
            "w": [stage_weights[sid] for sid in stage_order],
        },
        "litellm_pricing": litellm_pricing,
    }
    write_json(run_dir / "manifest.json", manifest)

    budget = Budget(args.max_cost_usd)
    runner = TrialRunner(
        max_iterations=args.max_iterations,
        verbose=args.verbose,
        litellm_pricing=litellm_pricing,
        max_format_retries=args.max_format_retries,
        max_continue_nudges=args.continue_nudges,
        max_rate_limit_retries=args.max_rate_limit_retries,
    )

    # Tee all run output into a single clean run-level console.log (in
    # addition to the per-trial logs), so the whole run is live-tailable
    # from one file without an ad-hoc redirect.
    with _tee_stdout(run_dir / "console.log"):
        print(f"Run: {run_id}")
        print(f"  Benchmark: {bench_name} | Harness(es): {h_ids} | Models: {models}")
        print(f"  Loadouts: {l_names} | Variants: {v_names} | "
              f"n: {args.n} | budget: ${args.max_cost_usd}")

        # Resolution preview (W8): surface tool wiring up front, including any
        # toolbase/mcp connection error, before constructing agents. The
        # resolved reports (incl. toolbase version provenance) go into the
        # manifest so the run records exactly which toolkit versions served.
        import tempfile
        resolution_reports: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            for h in harnesses:
                for lo in loadouts:
                    try:
                        _, report = build_agent_tools(h, lo, tmp)
                        _print_resolution(report)
                        resolution_reports.append(report)
                    except Exception as e:
                        print(f"  resolution error {h.id}/{lo.name}: {type(e).__name__}: {e}")
                        resolution_reports.append({
                            "harness": h.id, "loadout": lo.name,
                            "error": f"{type(e).__name__}: {e}",
                        })
                    finally:
                        # Don't leave preview-started connections running.
                        release_sources(tmp)
        manifest["resolution"] = resolution_reports
        write_json(run_dir / "manifest.json", manifest)
        if args.dry_run:
            print("  DRY-RUN: agent.run() will be skipped.")

        if args.parallel > 1 and args.verbose:
            print(f"  note: --parallel {args.parallel} with --verbose: "
                  "per-tool-call lines from concurrent trials will interleave "
                  "on stdout (per-trial console.logs stay clean).")
        new_records, aborted_globally = _run_trial_loop(
            benchmark=benchmark, harnesses=harnesses, loadouts=loadouts,
            variants=variants, models=models, seeds=seeds, run_dir=run_dir,
            runner=runner, budget=budget, completed=set(),
            dry_run=args.dry_run, parallel=args.parallel,
        )

        _finalize_run(run_dir=run_dir, manifest=manifest, budget=budget,
                      all_trial_records=new_records, k=args.n,
                      stage_order=stage_order, stage_weights=stage_weights,
                      aborted=aborted_globally)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Resume an interrupted run: re-run only the seeds that haven't
    completed yet, append to the existing trials.jsonl, and re-aggregate.

    The manifest is the source of truth for task / model / conditions /
    seeds — we don't accept overrides except `--max-cost-usd` (which can
    be widened so a partial run isn't blocked by an exhausted budget).
    """
    run_dir = _runs_root() / args.run_id
    if not run_dir.exists():
        print(f"Unknown run: {args.run_id} (no dir at {run_dir})", file=sys.stderr)
        return 2
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"No manifest in {run_dir}", file=sys.stderr)
        return 2
    manifest = read_json(manifest_path)

    benchmark = _load_benchmark(manifest.get("benchmark_dir"))
    if benchmark is None:
        return 2
    bench_dir = benchmark.BENCHMARK_DIR

    all_h = discover_harnesses(bench_dir)
    h_ids = [h["id"] if isinstance(h, dict) else h
             for h in manifest.get("harnesses", [])]
    harnesses = [all_h[i] for i in h_ids if i in all_h]
    all_l = discover_loadouts(bench_dir)
    l_names = manifest.get("loadouts") or manifest.get("conditions", [])
    loadouts = [all_l[n] for n in l_names if n in all_l]
    all_v = benchmark.variants
    v_names = [v["name"] if isinstance(v, dict) else v
               for v in manifest.get("variants", [])]
    if not v_names and benchmark.default_variant:
        # Manifest from before variants were a first-class axis — single
        # implicit variant. Replay against the benchmark's default.
        v_names = [benchmark.default_variant]
    variants = [all_v[n] for n in v_names if n in all_v]
    models = [m["model"] if isinstance(m, dict) else m
              for m in manifest.get("models", [])]
    if not (harnesses and loadouts and variants and models):
        print("Manifest lacks harness/loadout/variant/model info; cannot resume.",
              file=sys.stderr)
        return 2

    seeds = manifest["seeds"]
    stage_order = [s["id"] for s in benchmark.rubric.stages]
    stage_weights = {s["id"]: float(s.get("weight", 0.0)) for s in benchmark.rubric.stages}

    trials_path = run_dir / "trials.jsonl"
    existing = read_jsonl(trials_path) if trials_path.exists() else []
    completed = {(r.get("harness"), r.get("loadout"),
                  r.get("variant") or (benchmark.default_variant or ""),
                  r.get("model"), r["seed"])
                 for r in existing}

    total = (len(harnesses) * len(loadouts) * len(variants)
             * len(models) * len(seeds))
    remaining = total - len(completed)
    if remaining <= 0:
        print(f"Run {args.run_id} is already complete ({total}/{total}).")
        _finalize_run(run_dir=run_dir, manifest=manifest,
                      budget=Budget(None), all_trial_records=existing,
                      k=manifest["n_per_cell"],
                      stage_order=stage_order, stage_weights=stage_weights,
                      aborted=False)
        return 0

    budget_cap = (args.max_cost_usd
                  if args.max_cost_usd is not None
                  else manifest.get("max_cost_usd"))
    budget = Budget(budget_cap)
    runner = TrialRunner(
        # These are overrides (None → defer to each harness's loop block,
        # re-read from disk on resume). Replay the original run's overrides.
        max_iterations=manifest.get("max_iterations"),
        verbose=args.verbose,
        litellm_pricing=manifest.get("litellm_pricing"),
        max_format_retries=manifest.get("max_format_retries"),
        max_continue_nudges=manifest.get("continue_nudges"),
        max_rate_limit_retries=manifest.get("max_rate_limit_retries"),
    )

    print(f"Resume: {args.run_id}")
    print(f"  {len(completed)}/{total} trials already complete; {remaining} to go.")
    print(f"  Budget cap: ${budget_cap}")

    new_records, aborted_globally = _run_trial_loop(
        benchmark=benchmark, harnesses=harnesses, loadouts=loadouts,
        variants=variants, models=models, seeds=seeds, run_dir=run_dir,
        runner=runner, budget=budget, completed=completed,
        dry_run=manifest.get("dry_run", False),
        parallel=(args.parallel if args.parallel is not None
                  else manifest.get("parallel", 1)),
    )

    _finalize_run(run_dir=run_dir, manifest=manifest, budget=budget,
                  all_trial_records=existing + new_records,
                  k=manifest["n_per_cell"],
                  stage_order=stage_order, stage_weights=stage_weights,
                  aborted=aborted_globally)
    return 0


def cmd_regrade(args: argparse.Namespace) -> int:
    """Re-run the full rubric judge against an existing run's preserved
    artifacts.

    Sandbox cleanup keeps the minimum evidence each check needs (per the
    benchmark's `artifacts:` policy; see `core/artifact_policy.py`), so
    the judge can be replayed verbatim:
    each trial's trajectory is reconstructed from `transcript.jsonl.gz`
    and graded against `artifacts/` with the *current* `benchmark.yaml`
    rubric. Rubric edits (new checks, tightened params) take full effect.
    Hard process failures (crashes) keep their failure_mode; rubric-derived
    modes are recomputed.
    """
    from toolbench.core.checks import load_benchmark_checks, merged_registry
    from toolbench.core.store import read_jsonl_gz
    from toolbench.core.trajectory import ToolCall, Trajectory

    run_dir = _runs_root() / args.run_id
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"No manifest in {run_dir}", file=sys.stderr)
        return 2
    manifest = read_json(manifest_path)

    benchmark = _load_benchmark(manifest.get("benchmark_dir"))
    if benchmark is None:
        return 2

    trials_path = run_dir / "trials.jsonl"
    rows = read_jsonl(trials_path) if trials_path.exists() else []
    if not rows:
        print(f"No trials.jsonl rows in {run_dir}", file=sys.stderr)
        return 2

    checks_path = getattr(benchmark, "checks_module_path", lambda: None)()
    judge = RuleJudge(
        registry=merged_registry(load_benchmark_checks(checks_path)),
        benchmark_dir=str(getattr(benchmark, "BENCHMARK_DIR", "")),
    )

    stage_order = [s["id"] for s in benchmark.rubric.stages]
    stage_weights = {s["id"]: float(s.get("weight", 0.0))
                     for s in benchmark.rubric.stages}

    def _load_trajectory(trial_dir: Path) -> Trajectory:
        """Rebuild a Trajectory (tool_calls only) from the transcript."""
        tpath = trial_dir / "transcript.jsonl.gz"
        tcs: list[ToolCall] = []
        final = ""
        if tpath.exists():
            for r in read_jsonl_gz(tpath):
                if r.get("type") == "tool_call":
                    tcs.append(ToolCall(
                        t=r.get("t", 0.0), name=r.get("name", ""),
                        args=r.get("args", {}), duration_s=r.get("duration_s", 0.0),
                        ok=r.get("ok", True), result_summary=r.get("result_summary", ""),
                    ))
                elif r.get("type") == "assistant":
                    final = r.get("content", "") or final
        return Trajectory(tool_calls=tcs, final_response=final)

    print(f"Regrading {len(rows)} trials in {args.run_id} (full judge replay)")
    print(f"  {'trial':<32}  {'old→new':>9}  changed stages")
    print("  " + "-" * 78)

    new_rows: list[dict] = []
    for row in rows:
        trial_dir = run_dir / "trials" / row["trial_id"]
        artifacts_dir = trial_dir / "artifacts"
        old_stages = dict(row.get("stages") or {})
        old_score = row.get("score")

        grade = judge.grade(_load_trajectory(trial_dir), benchmark.rubric,
                            str(artifacts_dir))
        new_stages = dict(grade.stages)
        new_score = grade.score

        # Hard process failures (AGENT_CRASH / MODEL_FORMAT_CRASH /
        # GRADE_ERROR) reflect a real process failure the rubric can't
        # undo — preserve them. Rubric-derived modes come from the judge.
        failure_mode = grade.failure_mode
        if row.get("failure_mode") in HARD_PROCESS_FAILURES:
            failure_mode = row["failure_mode"]

        changed = [sid for sid in stage_order
                   if bool(old_stages.get(sid)) != bool(new_stages.get(sid))]
        flips = ", ".join(
            f"{sid}:{'P→F' if old_stages.get(sid) else 'F→P'}" for sid in changed
        ) or "—"
        print(f"  {row['trial_id']:<32}  {str(old_score):>4}→{new_score:<4}  {flips}")

        new_row = dict(row)
        new_row["stages"] = new_stages
        new_row["score"] = new_score
        new_row["ok"] = new_score > 0
        new_row["failure_mode"] = failure_mode
        new_rows.append(new_row)

        # Rewrite trial.json's grade section in place (preserving the rest).
        trial_json_path = trial_dir / "trial.json"
        if trial_json_path.exists():
            tj = read_json(trial_json_path)
            tj["grade"] = grade.to_dict()
            write_json(trial_json_path, tj)

    # Rewrite trials.jsonl in place.
    with open(trials_path, "w") as f:
        for r in new_rows:
            f.write(json.dumps(r, default=str) + "\n")

    # Re-aggregate summary + plots.
    print()
    _finalize_run(
        run_dir=run_dir, manifest=manifest, budget=Budget(None),
        all_trial_records=new_rows, k=manifest.get("n_per_cell", len(new_rows)),
        stage_order=stage_order, stage_weights=stage_weights,
        aborted=False,
    )
    return 0


def _row_reach(stages: dict, stage_order: list[str],
               weights: list[float]) -> float:
    """Per-session reach R_j computed from a stages dict (with
    cumulative-product absorbing convention)."""
    total = sum(weights) or 1.0
    cum = 1.0
    out = 0.0
    for sid, w in zip(stage_order, weights):
        passed = 1 if stages.get(sid) else 0
        cum *= passed
        out += cum * w
    return out / total


def _build_work_items(*, harnesses, loadouts, variants, models, seeds,
                      completed) -> list[dict]:
    """Enumerate every (harness × loadout × variant × model × seed) trial
    not in `completed`, in *seed-major* (round-robin) order.

    Seed index is the outermost loop deliberately: when a budget abort
    cuts the run short, every cell has completed (nearly) the same number
    of trials — k degrades uniformly across conditions instead of the
    later cells of the grid being dropped wholesale, which would make
    cross-condition comparisons unbalanced exactly when budget is tight.
    """
    multi_h = len(harnesses) > 1
    multi_v = len(variants) > 1
    multi_m = len(models) > 1
    items: list[dict] = []
    for i, seed in enumerate(seeds):
        for h in harnesses:
            for lo in loadouts:
                for v in variants:
                    # Cell key for aggregation. Single-axis runs keep their
                    # old shape (lo.name); each additional swept axis is
                    # appended with `|`. Variant goes after loadout so
                    # reports group by loadout first, then variant.
                    cond_parts = ([h.id] if multi_h else []) + [lo.name]
                    if multi_v:
                        cond_parts.append(v.name)
                    condition = "|".join(cond_parts)
                    for m in models:
                        if (h.id, lo.name, v.name, m, seed) in completed:
                            continue
                        parts = ([h.id.replace("/", "-")] if multi_h else []) + [lo.name]
                        if multi_v:
                            parts.append(v.name)
                        if multi_m:
                            parts.append(_model_slug(m))
                        trial_id = "__".join(parts) + f"__n{i:03d}__seed{seed}"
                        items.append({
                            "harness": h, "loadout": lo, "variant": v,
                            "model": m, "seed": seed,
                            "trial_id": trial_id, "condition": condition,
                        })
    return items


def _run_trial_loop(*, benchmark, harnesses, loadouts, variants, models, seeds,
                    run_dir, runner, budget, completed,
                    dry_run=False, parallel=1) -> tuple[list[dict], bool]:
    """Run every trial not in `completed`, appending each finished trial
    to trials.jsonl as it lands so a later resume sees it. Returns
    (new_records, aborted).

    `parallel` is the number of trials in flight at once. Each trial is
    fully self-contained (own sandbox, agent, LLM client, console.log,
    toolbase subprocesses), and rows are appended from this thread only,
    so the only shared mutable state is the lock-protected Budget. Note
    the budget is charged when a trial *finishes*: with parallel > 1, up
    to `parallel` in-flight trials can still complete (and bill) after
    the cap is crossed before the abort takes effect.
    """
    import concurrent.futures as cf
    import threading

    items = _build_work_items(harnesses=harnesses, loadouts=loadouts,
                              variants=variants, models=models, seeds=seeds,
                              completed=completed)
    new_records: list[dict] = []
    aborted = False
    # Budget-abort signal. NB: deliberately NOT executor.shutdown(
    # cancel_futures=True) — a future cancelled while queued never gets
    # set_running_or_notify_cancel() called (its work item is discarded),
    # stays CANCELLED instead of CANCELLED_AND_NOTIFIED, and as_completed
    # blocks on it forever. Instead every future runs; launched-after-
    # abort trials return None immediately and are skipped.
    abort = threading.Event()

    def _execute(it: dict) -> dict | None:
        if abort.is_set():
            return None   # budget abort: don't start this trial
        h, lo, v = it["harness"], it["loadout"], it["variant"]
        m, seed, trial_id = it["model"], it["seed"], it["trial_id"]
        print(f"  -> trial {trial_id} "
              f"(spent: ${budget.spent:.4f}, "
              f"remaining: ${budget.remaining:.4f})")
        base_row = {"trial_id": trial_id, "model": m,
                    "harness": h.id, "loadout": lo.name,
                    "variant": v.name, "condition": it["condition"],
                    "seed": seed}
        model_cfg = {"provider": h.provider_name, "model": m,
                     "dry_run": dry_run}
        try:
            result = runner.run_trial(
                model_cfg=model_cfg, benchmark=benchmark,
                harness=h, loadout=lo, variant=v, seed=seed,
                trial_id=trial_id, run_dir=run_dir,
                budget=budget,
            )
        except BudgetExceeded:
            raise
        except Exception as e:
            # e.g. a (stubbed) toolbase source, or an import error:
            # record a failed trial and keep going.
            print(f"  trial {trial_id} could not run: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            return {**base_row, "ok": False, "score": 0.0,
                    "stages": {}, "wall_clock_s": 0.0,
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cost_usd": 0.0,
                    "tool_calls": 0,
                    "failure_mode": "resolution_error",
                    "aborted_by_budget": False}
        return {
            **base_row,
            "ok": result.ok,
            "score": result.score,
            "stages": result.grade.stages,
            "wall_clock_s": round(result.wall_clock_s, 2),
            "input_tokens": result.trajectory.tokens.get("input", 0),
            "output_tokens": result.trajectory.tokens.get("output", 0),
            "cache_read_tokens": result.trajectory.tokens.get("cache_read", 0),
            "cost_usd": round(result.cost_usd, 6) if result.cost_usd is not None else None,
            "tool_calls": len(result.trajectory.tool_calls),
            "resolved_model": result.trajectory.resolved_model,
            "failure_mode": result.grade.failure_mode,
            "attempts": result.attempts,
            "nudges": result.nudges,
            "rate_limit_retries": result.rate_limit_retries,
            "aborted_by_budget": result.aborted_by_budget,
        }

    with cf.ThreadPoolExecutor(max_workers=max(1, int(parallel))) as ex:
        futures = [ex.submit(_execute, it) for it in items]
        for fut in cf.as_completed(futures):
            try:
                row = fut.result()
            except BudgetExceeded as e:
                if not aborted:
                    print(f"  ABORT: {e}")
                aborted = True
                abort.set()
                continue
            if row is None:
                continue   # skipped: launched after the abort
            # Rows are recorded here (the submitting thread) only, so
            # trials.jsonl appends never interleave.
            new_records.append(row)
            append_jsonl(run_dir / "trials.jsonl", row)
            if row.get("aborted_by_budget") and not aborted:
                aborted = True
                abort.set()
    return new_records, aborted


def _finalize_run(*, run_dir, manifest, budget, all_trial_records, k,
                  stage_order, stage_weights, aborted) -> None:
    """Re-aggregate from the full trial set, write summary.json + summary.txt,
    and print the rendered summary."""
    summary = aggregate(all_trial_records, k=k,
                        stage_order=stage_order, stage_weights=stage_weights)
    summary["run_id"] = manifest.get("run_id", run_dir.name)
    summary["k"] = k
    summary["pass_criterion"] = "all_stages"
    summary["reach_weights"] = manifest.get("reach_weights", {})
    summary["total_spent_usd"] = round(budget.spent, 6)
    summary["aborted_by_budget"] = aborted
    write_json(run_dir / "summary.json", summary)

    # Auto-render the headline plots. Best-effort: a plotting failure
    # shouldn't blow away the rest of the run output.
    try:
        render_parallel_coords(summary, all_trial_records, manifest,
                               run_dir / "parallel_coords.png")
    except Exception as e:
        print(f"warning: failed to render parallel_coords.png: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
    try:
        render_k_sweep(summary, all_trial_records, manifest,
                       run_dir / "k_sweep.png")
    except Exception as e:
        print(f"warning: failed to render k_sweep.png: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
    try:
        render_per_stage_k(summary, all_trial_records, manifest,
                           run_dir / "per_stage_k.png")
    except Exception as e:
        print(f"warning: failed to render per_stage_k.png: "
              f"{type(e).__name__}: {e}", file=sys.stderr)

    rendered = render_run_summary(summary, manifest=manifest, run_dir=run_dir)
    (run_dir / "summary.txt").write_text(rendered + "\n")
    print()
    print(rendered)


def aggregate(trials: list[dict], k: int,
              stage_order: list[str] | None = None,
              stage_weights: dict[str, float] | None = None
              ) -> AggregateResult:
    """Aggregate trial rows into per-cell metrics.

    Args:
        trials: rows from `trials.jsonl`, each with at minimum `model`,
            `condition`, `score`, `stages`, `cost_usd`, `wall_clock_s`,
            `failure_mode`.
        k: per-cell trial count (used for the order-statistic
            estimators of `pass@k` / `pass^k`).
        stage_order: canonical rubric stage order. If `None`, inferred
            from the first non-empty `stages` dict encountered.
        stage_weights: rubric weight per stage id. If `None`, equal
            weights are used and the rubric-weighted metrics collapse
            to their equal-weighted twins.

    Returns:
        `AggregateResult` with per-(model × condition) cell summaries.
        The dict is JSON-roundtrip safe and is written verbatim to
        `summary.json`.
    """
    cells: dict[tuple[str, str], list[dict]] = {}
    for t in trials:
        cells.setdefault((t["model"], t["condition"]), []).append(t)

    cell_summaries = []
    for (model, cond), rows in cells.items():
        scores = [r["score"] for r in rows]
        passes = [_trial_passed(r) for r in rows]
        n = len(rows)
        c = sum(passes)
        costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
        wallclocks = [r["wall_clock_s"] for r in rows]
        score_mean, score_lo, score_hi = bootstrap_ci(scores)
        stage_matrix, w = _stage_matrix(rows, stage_order, stage_weights)
        reach_mean = reach_bar_k(stage_matrix, weights=w) if stage_matrix else 0.0
        corr = _bootstrap_metric_corr(rows, stage_matrix, k, weights=w)
        reach_lo, reach_hi = _reach_ci(stage_matrix, weights=w)
        # Equal-weighted reach family: same absorbing semantics, no
        # rubric weights. Lets readers interpret "depth of pipeline
        # reached" without having to know the headline-stage weight.
        # All three statistics (bar/at_k/caret_k) get a uniform-weight
        # twin so the JSON is symmetric — the summary text only renders
        # bar_k_uniform, the rest live in summary.json for downstream
        # plots/analysis.
        if stage_matrix:
            reach_bar_k_uniform   = reach_bar_k(stage_matrix, weights=None)
            reach_at_k_uniform    = reach_at_k(stage_matrix, min(k, n), weights=None)
            reach_caret_k_uniform = reach_caret_k(stage_matrix, min(k, n), weights=None)
        else:
            reach_bar_k_uniform = reach_at_k_uniform = reach_caret_k_uniform = 0.0
        reach_u_lo, reach_u_hi = _reach_ci(stage_matrix, weights=None)
        cell_summaries.append({
            "model": model,
            "condition": cond,
            "n": n,
            "mean_score": round(score_mean, 4),
            "score_ci95": [round(score_lo, 4), round(score_hi, 4)],
            # Three-vector in canonical order
            # (reach_bar_k, pass@k, pass^k) per the accompanying manuscript §6.
            "reach_bar_k": round(reach_mean, 4),
            "reach_bar_k_ci95": [round(reach_lo, 4), round(reach_hi, 4)],
            "reach_bar_k_uniform": round(reach_bar_k_uniform, 4),
            "reach_bar_k_uniform_ci95": [round(reach_u_lo, 4), round(reach_u_hi, 4)],
            "reach_at_k_uniform": round(reach_at_k_uniform, 4),
            "reach_caret_k_uniform": round(reach_caret_k_uniform, 4),
            "pass_at_k": round(pass_at_k(n, c, min(k, n)), 4) if n else 0.0,
            "pass_caret_k": round(pass_caret_k(n, c, min(k, n)), 4) if n else 0.0,
            "pass_at_1": round(c / n, 4) if n else 0.0,
            "metric_correlations": {
                "labels": METRIC_TRIPLET_LABELS,
                "matrix": _round_corr(corr),
                "n_bootstrap": N_BOOTSTRAP_CORR,
            },
            "k": k,
            "mean_cost_usd": round(mean(costs), 6) if costs else None,
            "mean_wall_clock_s": round(mean(wallclocks), 2) if wallclocks else 0.0,
            "stages": _stages_breakdown(rows),
            "failure_modes": _count_failures(rows),
        })
    return {
        "cells": cell_summaries,
        "paired_deltas": _paired_deltas(trials, stage_order, stage_weights),
        "n_total_trials": len(trials),
    }


def _paired_deltas(trials: list[dict],
                   stage_order: list[str] | None = None,
                   stage_weights: dict[str, float] | None = None
                   ) -> list[dict]:
    """Paired per-seed condition deltas, per model.

    For every pair of conditions sharing seeds under the same model,
    compute the per-seed difference of reach (and of pass) and a paired
    bootstrap CI over the seed dimension. This is the right uncertainty
    for ablation claims ("Δreach for model M with vs without the
    loadout"): conditions share seeds, so per-seed noise cancels in the
    difference — differencing two independent per-cell CIs would
    overstate the uncertainty.

    Delta direction is `condition_b − condition_a`, with conditions
    ordered by first appearance in `trials` (i.e. the CLI's order).
    Duplicate (condition, seed) rows are averaged before pairing.
    """
    if stage_order is None:
        for t in trials:
            s = t.get("stages") or {}
            if s:
                stage_order = list(s.keys())
                break
    if not stage_order:
        return []
    weights = ([stage_weights.get(sid, 0.0) for sid in stage_order]
               if stage_weights is not None else [1.0] * len(stage_order))

    # model -> condition -> seed -> list[(reach, passed)]
    by_model: dict[str, dict[str, dict[object, list[tuple[float, int]]]]] = {}
    cond_order: list[str] = []
    for t in trials:
        seed = t.get("seed")
        if seed is None:
            continue
        cond = t["condition"]
        if cond not in cond_order:
            cond_order.append(cond)
        reach = _row_reach(t.get("stages") or {}, stage_order, weights)
        by_model.setdefault(t["model"], {}).setdefault(cond, {}) \
                .setdefault(seed, []).append((reach, _trial_passed(t)))

    def _avg(vals: list[tuple[float, int]], idx: int) -> float:
        return sum(v[idx] for v in vals) / len(vals)

    out: list[dict] = []
    for model, conds in by_model.items():
        ordered = [c for c in cond_order if c in conds]
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                a, b = ordered[i], ordered[j]
                shared = sorted(set(conds[a]) & set(conds[b]), key=str)
                if not shared:
                    continue
                reach_d = [_avg(conds[b][s], 0) - _avg(conds[a][s], 0)
                           for s in shared]
                pass_d = [_avg(conds[b][s], 1) - _avg(conds[a][s], 1)
                          for s in shared]
                entry: dict = {
                    "model": model, "condition_a": a, "condition_b": b,
                    "n_pairs": len(shared),
                    "reach_delta": round(mean(reach_d), 4),
                    "pass_delta": round(mean(pass_d), 4),
                }
                if len(shared) >= 2:
                    # Bootstrapping the per-seed delta list IS the paired
                    # bootstrap: each resample draws seeds, not trials.
                    _, lo, hi = bootstrap_ci(reach_d, seed=0xFEED)
                    entry["reach_delta_ci95"] = [round(lo, 4), round(hi, 4)]
                    _, lo, hi = bootstrap_ci(pass_d, seed=0xFEED)
                    entry["pass_delta_ci95"] = [round(lo, 4), round(hi, 4)]
                else:
                    entry["reach_delta_ci95"] = None
                    entry["pass_delta_ci95"] = None
                out.append(entry)
    return out


def _stage_matrix(rows: list[dict],
                  stage_order: list[str] | None = None,
                  stage_weights: dict[str, float] | None = None
                  ) -> StageMatrix:
    """Build a 0/1 stage matrix in canonical rubric order, plus weights.

    If `stage_order` is provided (from the rubric), use it as the
    canonical column order; otherwise infer it from the first row with
    a non-empty stages dict. `weights` is `None` when no `stage_weights`
    mapping is supplied (i.e. equal weights).

    Rows whose `stages` dict is empty (e.g. `GRADE_ERROR`) are
    zero-padded to the canonical width — they reached zero stages,
    contributing 0 to reach.
    """
    canonical: list[str] | None = stage_order
    if canonical is None:
        for r in rows:
            s = r.get("stages") or {}
            if s:
                canonical = list(s.keys())
                break
    if canonical is None:
        return StageMatrix(matrix=[], weights=None)
    matrix = stage_matrix_from_rows(rows, canonical)
    w = ([stage_weights.get(sid, 0.0) for sid in canonical]
         if stage_weights is not None else None)
    return StageMatrix(matrix=matrix, weights=w)


def _bootstrap_metric_corr(rows: list[dict], stage_matrix: list[list[int]],
                           k: int, weights: list[float] | None = None
                           ) -> list[list[float | None]]:
    """Bootstrap-resample trials and compute the three-vector
    (reach_bar_k, pass@k, pass^k) correlation matrix across resamples.

    Order tracks METRIC_TRIPLET_LABELS. A degenerate cell (n<2, or no
    stages) yields an all-None off-diagonal matrix — Pearson r is
    undefined when there's no variance to detect.
    """
    n = len(rows)
    if n < 2 or not stage_matrix:
        return pearson_corr_matrix([])
    rng = random.Random(0xBEEF ^ n)
    boot_vectors: list[tuple[float, float, float]] = []
    for _ in range(N_BOOTSTRAP_CORR):
        idx = [rng.randrange(n) for _ in range(n)]
        sample_passes = [_trial_passed(rows[i]) for i in idx]
        c_b = sum(sample_passes)
        p_at_k = pass_at_k(n, c_b, min(k, n))
        p_caret_k = pass_caret_k(n, c_b, min(k, n))
        sample_stages = [stage_matrix[i] for i in idx]
        p_reach = reach_bar_k(sample_stages, weights=weights)
        boot_vectors.append((p_reach, p_at_k, p_caret_k))
    return pearson_corr_matrix(boot_vectors)


def _reach_ci(stage_matrix: list[list[int]],
              weights: list[float] | None = None,
              n_bootstrap: int = 1000, seed: int = 0xBEEF
              ) -> tuple[float, float]:
    """Bootstrap 95% CI for reach_bar_k over the row dimension."""
    if not stage_matrix:
        return (0.0, 0.0)
    n = len(stage_matrix)
    rng = random.Random(seed)
    samples = []
    for _ in range(n_bootstrap):
        idx = [rng.randrange(n) for _ in range(n)]
        samples.append(reach_bar_k([stage_matrix[i] for i in idx], weights=weights))
    samples.sort()
    lo = samples[int(n_bootstrap * 0.025)]
    hi = samples[int(n_bootstrap * 0.975) - 1]
    return (lo, hi)


def _round_corr(corr: list[list[float | None]]) -> list[list[float | None]]:
    return [
        [round(v, 4) if v is not None else None for v in row]
        for row in corr
    ]


def _stages_breakdown(rows: list[dict]) -> dict:
    if not rows or not rows[0].get("stages"):
        return {}
    keys = list(rows[0]["stages"].keys())
    return {
        sk: round(sum(1 for r in rows if r["stages"].get(sk)) / len(rows), 4)
        for sk in keys
    }


def _count_failures(rows: list[dict]) -> dict[str, int]:
    """Tally failure_mode strings across trial rows.

    Rows missing the `failure_mode` field are counted under `UNKNOWN`
    so the bucket is explicit rather than silently dropped.
    """
    out: dict[str, int] = {}
    for r in rows:
        mode = r.get("failure_mode", UNKNOWN)
        out[mode] = out.get(mode, 0) + 1
    return out


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT
        ).decode().strip()
    except Exception:
        return "unknown"


def _file_sha(path: Path) -> str:
    try:
        h = hashlib.sha256()
        h.update(Path(path).read_bytes())
        return h.hexdigest()[:12]
    except Exception:
        return "unknown"


class _SectionedGroup(click.Group):
    """A Click group that prints its commands grouped into sections, mirroring
    toolbase's `tb` help layout."""

    SECTIONS = [
        ("Running benchmarks", ["run", "resume", "regrade"]),
    ]

    def format_commands(self, ctx, formatter):
        listed: set[str] = set()
        for title, names in self.SECTIONS:
            rows = []
            for name in names:
                cmd = self.get_command(ctx, name)
                if cmd is None or cmd.hidden:
                    continue
                listed.add(name)
                rows.append((name, cmd.get_short_help_str()))
            if rows:
                with formatter.section(title):
                    formatter.write_dl(rows)
        # Any commands not placed in a section fall through to a generic list.
        rest = [(n, self.get_command(ctx, n).get_short_help_str())
                for n in self.list_commands(ctx)
                if n not in listed and not self.get_command(ctx, n).hidden]
        if rest:
            with formatter.section("Other commands"):
                formatter.write_dl(rest)


@click.group(
    cls=_SectionedGroup,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(package_name="toolbench", prog_name="toolbench")
def cli() -> None:
    """toolbench — build and run benchmarks for agentic tools and harnesses.

    The benchmarking sibling of toolbase: compose a benchmark (task + rubric)
    with a harness, a loadout (the tools the agent gets), and a variant
    (prompt + sandbox), run N trials per cell against a model, and report
    reach / pass@k / pass^k metrics.
    """
    # Load .env (provider keys + tool config paths) before anything reads
    # os.environ. cwd first (works for wheel installs, where REPO_ROOT
    # points inside site-packages), then the repo checkout; real
    # environment variables always win over either file (setdefault).
    _load_env_file(Path.cwd() / ".env")
    _load_env_file(REPO_ROOT / ".env")
    # Silence tool progress bars (tqdm) when output isn't an interactive
    # terminal — i.e. backgrounded/redirected runs — so captured logs stay
    # clean. Honored by tqdm at bar creation.
    if not sys.stdout.isatty():
        os.environ.setdefault("TQDM_DISABLE", "1")


@cli.command("run", short_help="Run a benchmark.")
@click.option("--benchmark", "--task", "benchmark", required=True,
              type=click.Path(exists=True, file_okay=False, dir_okay=True),
              help="Path to a benchmark directory (containing benchmark.yaml), "
                   "e.g. examples/geometry.")
@click.option("--harness", "--harnesses", "harnesses", default=None,
              help="Comma-separated harness id(s), e.g. orchestral/anthropic. "
                   "Default: the benchmark's default_harness.")
@click.option("--loadouts", "--conditions", "loadouts", default=None,
              help="Comma-separated loadout name(s). "
                   "Default: the benchmark's default_loadout.")
@click.option("--variant", "--variants", "variants", default=None,
              help="Comma-separated variant name(s) — the scaffolding axis "
                   "(prompt + sandbox), orthogonal to the loadout. "
                   "Default: the benchmark's default_variant.")
@click.option("--models", "--model", "models", required=True,
              help="Comma-separated model id(s). 'stub' is allowed with --dry-run.")
@click.option("--provider", default=None, hidden=True)  # deprecated: from harness
@click.option("--n", "n", type=int, default=3, show_default=True,
              help="Trials per cell.")
@click.option("--seed-base", "seed_base", type=int, default=1001, show_default=True)
@click.option("--max-cost-usd", "max_cost_usd", type=float, required=True,
              help="Hard budget cap. Run aborts when spent > cap.")
@click.option("--max-iterations", "max_iterations", type=int, default=None,
              help="Override the harness loop.max_iterations (agent.run "
                   "round-trip cap). Default: from the harness.")
@click.option("--max-format-retries", "max_format_retries", type=int, default=None,
              help="Override the harness loop.max_format_retries: on a "
                   "MODEL_FORMAT_CRASH (malformed tool-call JSON), resume the "
                   "same session this many times. Default: from the harness.")
@click.option("--max-rate-limit-retries", "max_rate_limit_retries", type=int,
              default=None,
              help="Override the harness loop.max_rate_limit_retries: on a "
                   "provider 429/529 (throttled / overloaded), back off and "
                   "resume the same session this many times before recording "
                   "the trial as RATE_LIMITED. Default: from the harness "
                   "(hard default 3).")
@click.option("--continue-nudges", "continue_nudges", type=int, default=None,
              help="Override the harness loop.continue_nudges: if the model "
                   "self-terminates with a required deliverable still absent "
                   "(presence check fails), resume it with a generic "
                   "'you haven't finished' nudge this many times. Never fires "
                   "when the deliverable exists (no oracle leakage); recorded "
                   "per trial. Default: from the harness.")
@click.option("--parallel", "parallel", type=int, default=1, show_default=True,
              help="Trials in flight at once. Each trial is self-contained "
                   "(own sandbox/agent/LLM client), so this is safe; mind "
                   "provider rate limits, and note the budget cap is checked "
                   "as trials finish, so up to N in-flight trials can complete "
                   "after the cap is crossed.")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Skip the actual LLM call; validate harness wiring only.")
@click.option("-v", "--verbose", "verbose", is_flag=True, default=False,
              help="Print a stylish line per tool call (▸ start, ✓/✗ end). "
                   "Honors NO_COLOR.")
@click.option("--run-label", "run_label", default=None)
def _run(**kw) -> int:
    """Run a benchmark across the (harness × loadout × variant × model) grid."""
    return cmd_run(SimpleNamespace(**kw))


@cli.command("resume", short_help="Resume an interrupted run.")
@click.option("--run-id", "run_id", required=True,
              help="Existing run directory under runs/, e.g. "
                   "2026-05-07T22-16-34_geometry_run.")
@click.option("--max-cost-usd", "max_cost_usd", type=float, default=None,
              help="Override the manifest's budget cap (e.g. widen it to absorb "
                   "additional trials). Defaults to the original cap from "
                   "manifest.json.")
@click.option("--parallel", "parallel", type=int, default=None,
              help="Trials in flight at once. Defaults to the original run's "
                   "--parallel from manifest.json.")
@click.option("-v", "--verbose", "verbose", is_flag=True, default=False,
              help="Print a stylish line per tool call.")
def _resume(**kw) -> int:
    """Reads the run dir's manifest + trials.jsonl, runs only the seeds that
    haven't completed yet, and re-aggregates summary.json/summary.txt."""
    return cmd_resume(SimpleNamespace(**kw))


@cli.command("regrade", short_help="Re-judge a run's preserved artifacts.")
@click.option("--run-id", "run_id", required=True,
              help="Existing run directory under runs/.")
def _regrade(**kw) -> int:
    """Re-run the rule judge against an existing run's preserved artifacts. Use
    after rubric changes (new content/numeric checks) to refresh grades +
    summary without re-executing any agent."""
    return cmd_regrade(SimpleNamespace(**kw))


def main(argv: list[str] | None = None) -> int:
    """Console-script entry (`toolbench` / `tbe`). Returns a process exit code."""
    try:
        rc = cli.main(args=argv, standalone_mode=False)
        return int(rc) if isinstance(rc, int) else 0
    except click.ClickException as e:
        e.show()
        return e.exit_code
    except click.exceptions.Abort:
        click.echo("Aborted.", err=True)
        return 1
    except SystemExit as e:  # --help / --version exit cleanly
        return int(e.code) if isinstance(e.code, int) else 0


if __name__ == "__main__":
    sys.exit(main())
