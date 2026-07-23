"""
Unified check registry.

A *check* is a function `(sandbox: Path, params: dict) -> (passed: bool,
evidence: str)`. That contract is shared by the filesystem checks in
`content_checks.py` and the value checks in `numeric_checks.py`; this
module merges both into one registry keyed by name, adds the built-ins
the declarative rubric uses (`json_with_keys`, `close_to`), and lets a
benchmark contribute its own checks from a local `checks/checks.py`.

In a rubric stage a check is written key-as-discriminator —
`{ <check-name>: { ...params } }` — so `run_check(name, sandbox, params)`
dispatches by the name; everything else in the mapping is `params`. A
relative `reference:` param is resolved against the benchmark directory.
"""

import importlib.util
import json
import os
from pathlib import Path
from typing import Callable

from .content_checks import CONTENT_CHECKS
from .numeric_checks import NUMERIC_CHECKS


def json_with_keys(sandbox: Path, params: dict) -> tuple[bool, str]:
    """`file` exists, parses as a JSON object, and has every `required_keys`."""
    path = sandbox / params["file"]
    if not path.exists():
        return False, f"{params['file']} missing"
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return False, f"invalid JSON: {e}"
    missing = [k for k in (params.get("required_keys") or []) if k not in data]
    return (not missing), ("ok" if not missing else f"missing keys {missing}")


def close_to(sandbox: Path, params: dict) -> tuple[bool, str]:
    """`output[field]` within `tolerance_frac` of the reference value.

    Handles a number or a list (component-wise). Reference is read from the
    JSON file named by `reference` (resolved against the benchmark dir by
    `run_check`), under the same `field`.
    """
    got = json.loads((sandbox / params["file"]).read_text())[params["field"]]
    ref = json.loads(Path(params["reference"]).read_text())[params["field"]]
    tol = float(params.get("tolerance_frac", 0.0))
    if isinstance(ref, list):
        if not isinstance(got, list) or len(got) != len(ref):
            return False, f"shape mismatch: {got!r} vs {ref!r}"
        pairs = list(zip(got, ref))
    else:
        pairs = [(got, ref)]
    bad = [(g, r) for g, r in pairs if abs(g - r) > tol * max(abs(r), 1e-9)]
    ok = not bad
    return ok, f"got={got} ref={ref} tol={tol}" + ("" if ok else f" off={bad}")


# Built-in registry: filesystem + numeric + declarative-rubric checks.
BUILTIN_CHECKS: dict[str, Callable[[Path, dict], tuple[bool, str]]] = {
    **CONTENT_CHECKS,
    **NUMERIC_CHECKS,
    "json_with_keys": json_with_keys,
    "close_to": close_to,
}

# Each check has a `role`: `presence` checks verify the agent *produced* a
# required deliverable (artifact exists / right shape); `correctness` checks
# verify it's *right* (values / physics). The distinction drives (a) the
# operational-vs-capability failure taxonomy and (b) the continue-nudge gate
# — a self-terminated trial is only nudged when a *presence* check fails (a
# deliverable is missing), never on a correctness failure (which would leak
# the grading oracle). Content checks are presence by nature, numeric checks
# correctness by nature; the two newer checks are tagged explicitly. An
# untagged check defaults to `correctness` (the safe default: it never gates
# a nudge).
PRESENCE, CORRECTNESS = "presence", "correctness"
BUILTIN_CHECK_ROLES: dict[str, str] = {
    **{name: PRESENCE for name in CONTENT_CHECKS},
    **{name: CORRECTNESS for name in NUMERIC_CHECKS},
    "json_with_keys": PRESENCE,
    "close_to": CORRECTNESS,
}


def _load_benchmark_module(module_path: str | Path | None):
    """Import a benchmark-local checks module, or return None."""
    if not module_path:
        return None
    path = Path(module_path)
    if not path.is_file():
        raise FileNotFoundError(f"benchmark checks module not found: {path}")
    spec = importlib.util.spec_from_file_location(f"_bench_checks_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_benchmark_checks(module_path: str | Path | None) -> dict:
    """Import a benchmark-local checks module; return its `CHECKS` dict.

    `module_path` is a filesystem path to a .py file exposing a top-level
    `CHECKS` mapping (and optionally a `ROLES` mapping; see
    `load_benchmark_roles`). Returns {} when there is no module.
    """
    mod = _load_benchmark_module(module_path)
    return dict(getattr(mod, "CHECKS", {}) or {}) if mod else {}


def load_benchmark_roles(module_path: str | Path | None) -> dict:
    """Return a benchmark-local checks module's `ROLES` mapping (check-name ->
    'presence'|'correctness'), or {} when absent. Lets a benchmark declare the
    role of its own checks so the nudge gate + failure taxonomy generalize."""
    mod = _load_benchmark_module(module_path)
    return dict(getattr(mod, "ROLES", {}) or {}) if mod else {}


def merged_registry(benchmark_checks: dict | None) -> dict:
    """Built-ins + benchmark-local checks; a name present in both is an error."""
    benchmark_checks = benchmark_checks or {}
    clash = set(BUILTIN_CHECKS) & set(benchmark_checks)
    if clash:
        raise ValueError(
            f"benchmark check(s) {sorted(clash)} collide with built-in checks; "
            "rename the benchmark-local check(s)"
        )
    return {**BUILTIN_CHECKS, **benchmark_checks}


def merged_roles(benchmark_roles: dict | None) -> dict:
    """Built-in check roles overlaid with a benchmark's own `ROLES`."""
    return {**BUILTIN_CHECK_ROLES, **(benchmark_roles or {})}


def missing_presence(rubric, sandbox: str | Path, *, registry: dict | None = None,
                     roles: dict | None = None,
                     benchmark_dir: str | Path | list[Path] | None = None) -> str:
    """Describe required deliverables that are ABSENT — i.e. the rubric's
    `presence`-role checks that currently fail — or '' if every deliverable is
    present.

    This deliberately runs ONLY presence checks: it answers "did the agent
    produce the outputs it was asked for?" (task-compliance, info the agent
    already has) and never consults a `correctness` check (which would leak
    the grading oracle). It is the gate for the continue-nudge: a self-
    terminated trial is nudged iff this returns a non-empty description.
    """
    registry = registry if registry is not None else BUILTIN_CHECKS
    roles = roles if roles is not None else BUILTIN_CHECK_ROLES
    failed: list[str] = []
    for stage in getattr(rubric, "stages", []):
        for entry in stage.get("checks") or []:
            if not (isinstance(entry, dict) and len(entry) == 1):
                continue
            (cname, cparams), = entry.items()
            if roles.get(cname, CORRECTNESS) != PRESENCE:
                continue
            ok, msg = run_check(cname, sandbox, cparams or {},
                                benchmark_dir=benchmark_dir, registry=registry)
            if not ok:
                failed.append(f"{cname}: {msg}")
    return "; ".join(failed[:3])


def run_check_full(name: str, sandbox: str | Path, params: dict | None, *,
                   benchmark_dir: str | Path | list[Path] | None = None,
                   registry: dict | None = None) -> tuple[bool, str, dict]:
    """Dispatch a single check by name. `params` is the check's config
    (everything under its key in the rubric stage). A relative
    `reference:` param is resolved against `benchmark_dir` — one dir, or
    a search path of dirs (a benchmark's `search_dirs`, child first when
    it extends another): the first dir where the reference exists wins,
    so an inherited rubric finds the parent's ground truth and a child
    override shadows it. With no hit, the first dir is used so the
    check's missing-file message names the benchmark's own path.
    """
    registry = registry if registry is not None else BUILTIN_CHECKS
    fn = registry.get(name)
    if fn is None:
        return False, f"unknown check {name!r}. Known: {sorted(registry)}", {}
    p = dict(params or {})
    ref = p.get("reference")
    if ref and benchmark_dir and not os.path.isabs(str(ref)):
        dirs = ([Path(benchmark_dir)]
                if isinstance(benchmark_dir, (str, Path))
                else [Path(d) for d in benchmark_dir])
        chosen = next((d for d in dirs if (d / str(ref)).exists()), dirs[0])
        p["reference"] = str(chosen / str(ref))
    try:
        result = fn(Path(sandbox), p)
    except Exception as e:  # never let a check crash the judge
        return False, f"{name} raised {type(e).__name__}: {e}", {}
    # Checks return (ok, msg) or, when they want to record continuous
    # diagnostics (e.g. a distance-to-reference), (ok, msg, metrics).
    if isinstance(result, tuple) and len(result) == 3:
        ok, msg, metrics = result
        return bool(ok), str(msg), dict(metrics or {})
    ok, msg = result
    return bool(ok), str(msg), {}


def run_check(name: str, sandbox: str | Path, params: dict | None, *,
              benchmark_dir: str | Path | list[Path] | None = None,
              registry: dict | None = None) -> tuple[bool, str]:
    """Backward-compatible 2-tuple entry point (the metrics dict is dropped).
    Callers that want the continuous diagnostics use `run_check_full`."""
    ok, msg, _ = run_check_full(name, sandbox, params,
                                benchmark_dir=benchmark_dir, registry=registry)
    return ok, msg
