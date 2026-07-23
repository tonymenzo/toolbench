"""
Content-based artifact predicates for the rule judge.

Filename globs are brittle: an agent that writes
`analysis/mass_overlay.pdf` instead of `lq_mass_comparison.pdf`
silently fails a glob like `lq_mass*.pdf` even though the deliverable
is present. Each predicate here looks at file *contents* — UFO module
set, LHE event blocks, JSONL record schema, NumPy array shape, PDF
magic bytes — so the test passes regardless of the agent's naming
choice.

A predicate takes (sandbox_dir, params_dict) and returns
(passed: bool, evidence: str). They are exported via `CONTENT_CHECKS`,
merged into the unified registry in `checks.py`, and invoked by name
from a rubric stage's `checks:` list:

    checks:
      - jsonl_with_keys:
          required_keys: [n, particles]
          min_records: 100
"""

import gzip
import json
from pathlib import Path
from typing import Callable


# Canonical UFO module names (FeynRules → UFO output). A directory
# containing all five is unambiguously a UFO model directory; the
# remaining files (object_library, function_library, …) sometimes
# differ across UFO versions, so the check stops at this minimum set.
UFO_REQUIRED = {
    "parameters.py", "particles.py", "vertices.py",
    "couplings.py", "lorentz.py",
}


def _scope(sandbox: Path, params: dict) -> Path:
    """Return the subtree under which the predicate should search.

    `under_subpath` is a relative path inside the sandbox. `..` segments
    are stripped so a malformed rubric can't escape the sandbox; if the
    cleaned path doesn't exist on disk, fall back to the sandbox root.
    """
    sub = params.get("under_subpath")
    if not sub:
        return sandbox
    parts = [p for p in Path(sub).parts if p not in ("", "..", "/")]
    if not parts:
        return sandbox
    target = sandbox.joinpath(*parts)
    return target if target.exists() else sandbox


def check_ufo_dir(sandbox: Path, params: dict) -> tuple[bool, str]:
    """A directory containing the canonical UFO module set."""
    root = _scope(sandbox, params)
    for parameters_py in root.rglob("parameters.py"):
        d = parameters_py.parent
        try:
            present = {f.name for f in d.iterdir() if f.is_file()}
        except OSError:
            continue
        missing = UFO_REQUIRED - present
        if not missing:
            return True, f"UFO dir at {d.relative_to(sandbox)}"
    return False, f"no UFO directory with {sorted(UFO_REQUIRED)} found under {root.relative_to(sandbox) if root != sandbox else '.'}"


def check_lhe_with_events(sandbox: Path, params: dict) -> tuple[bool, str]:
    """Any .lhe / .lhe.gz file with >= min_events <event> blocks."""
    min_events = int(params.get("min_events", 10))
    candidates = list(sandbox.rglob("*.lhe")) + list(sandbox.rglob("*.lhe.gz"))
    for p in candidates:
        opener = gzip.open if p.suffix == ".gz" else open
        try:
            with opener(p, "rt", errors="ignore") as f:
                count = 0
                for line in f:
                    if "<event>" in line:
                        count += 1
                        if count >= min_events:
                            return True, f"{p.relative_to(sandbox)}: ≥{min_events} events"
        except Exception:
            continue
    return False, f"no .lhe* with ≥{min_events} <event> blocks (scanned {len(candidates)} files)"


def check_jsonl_with_keys(sandbox: Path, params: dict) -> tuple[bool, str]:
    """Any .jsonl whose first record contains every key in `required_keys`,
    with ≥ min_records data lines.
    """
    required = set(params.get("required_keys") or [])
    min_records = int(params.get("min_records", 1))
    if not required:
        return False, "content_check.jsonl_with_keys: required_keys must be non-empty"
    candidates = list(sandbox.rglob("*.jsonl"))
    for p in candidates:
        try:
            with open(p) as f:
                lines = [ln for ln in f if ln.strip()]
            if len(lines) < min_records:
                continue
            first = json.loads(lines[0])
        except Exception:
            continue
        if isinstance(first, dict) and required <= set(first.keys()):
            return True, (
                f"{p.relative_to(sandbox)}: {len(lines)} records, "
                f"keys ⊇ {sorted(required)}"
            )
    return False, (
        f"no .jsonl with keys ⊇ {sorted(required)} and ≥{min_records} records "
        f"(scanned {len(candidates)} files)"
    )


def check_npy_array(sandbox: Path, params: dict) -> tuple[bool, str]:
    """Any .npy file with a NumPy array satisfying shape constraints.

    Params:
      ndim: required ndim (omit to skip the check).
      min_len: minimum total array size.
      dtype_kind: one of "f"/"i"/"u" if a specific dtype family is needed.
    """
    import numpy as np
    ndim = params.get("ndim")
    min_len = int(params.get("min_len", 1))
    dtype_kind = params.get("dtype_kind")
    candidates = list(sandbox.rglob("*.npy"))
    for p in candidates:
        try:
            arr = np.load(p, allow_pickle=False)
        except Exception:
            continue
        if ndim is not None and arr.ndim != int(ndim):
            continue
        if dtype_kind and arr.dtype.kind != dtype_kind:
            continue
        if arr.size < min_len:
            continue
        return True, (
            f"{p.relative_to(sandbox)}: shape={tuple(arr.shape)}, dtype={arr.dtype}"
        )
    return False, (
        f"no .npy with ndim={ndim}, size≥{min_len}, dtype_kind={dtype_kind!r} "
        f"(scanned {len(candidates)} files)"
    )


# --------------------------------------------------------------------------
# Format-agnostic helpers.
#
# The strict checks above key on a specific tool's output structure (a .npy
# array, a .jsonl with named keys). By-hand agents (core_only / recipes) that
# drive the software themselves save the *same physics* in whatever format
# they reach for — one value per line, a CSV column, a JSON list, a HepMC
# stream. The helpers below discover the deliverable by content across the
# common numeric/record formats so a stage is credited on the physics, not
# on matching the tool's schema.
# --------------------------------------------------------------------------

# Extensions we treat as plain-text tabular numeric dumps.
_TABULAR_EXTS = (".csv", ".tsv", ".dat", ".txt")
# Per-event record-stream extensions (a HepMC event line starts with "E ").
_HEPMC_EXTS = (".hepmc", ".hepmc2", ".hepmc3")


def _json_numeric_arrays(obj, np) -> list:
    """Pull 1-D numeric arrays out of a decoded JSON object: a bare list of
    numbers, each numeric field of a list-of-records, each numeric column of
    a list-of-rows, or each numeric list value of a dict."""
    out = []
    if isinstance(obj, list) and obj:
        if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in obj):
            out.append(np.asarray(obj, dtype=float))
        elif all(isinstance(x, dict) for x in obj):
            keys = set().union(*[d.keys() for d in obj])
            for k in keys:
                vals = [d.get(k) for d in obj]
                if all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       for v in vals):
                    out.append(np.asarray(vals, dtype=float))
        elif all(isinstance(x, (list, tuple)) for x in obj):
            try:
                arr = np.asarray(obj, dtype=float)
                if arr.ndim == 2:
                    out.extend(arr[:, c] for c in range(arr.shape[1]))
            except Exception:
                pass
    elif isinstance(obj, dict):
        for v in obj.values():
            if (isinstance(v, list) and v
                    and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                            for x in v)):
                out.append(np.asarray(v, dtype=float))
    return out


def _extract_float_arrays(path: Path, np) -> list:
    """Return candidate 1-D finite-float arrays from a numeric file of any
    supported format (.npy / .csv / .tsv / .dat / .txt / .json). For tabular
    or multi-field data every column/field is returned as its own candidate,
    so a downstream peak/shape test can pick whichever one is the physics.
    Returns [] for files that don't parse as numeric."""
    suf = path.suffix.lower()
    raw: list = []
    try:
        if suf == ".npy":
            arr = np.load(path, allow_pickle=False)
            if arr.dtype.kind in "fiu":
                raw.append(arr)
        elif suf in _TABULAR_EXTS:
            delim = "," if suf == ".csv" else ("\t" if suf == ".tsv" else None)
            data = np.genfromtxt(path, delimiter=delim, comments="#", dtype=float)
            data = np.asarray(data, dtype=float)
            if data.ndim == 1:
                raw.append(data)
            elif data.ndim == 2 and data.size:
                raw.extend(data[:, c] for c in range(data.shape[1]))
        elif suf == ".json":
            raw = _json_numeric_arrays(json.loads(path.read_text()), np)
    except Exception:
        return []

    cleaned = []
    for a in raw:
        a = np.asarray(a, dtype=float).ravel()
        a = a[np.isfinite(a)]
        if a.size:
            cleaned.append(a)
    return cleaned


def check_numeric_array(sandbox: Path, params: dict) -> tuple[bool, str]:
    """Format-agnostic numeric-array presence check: any file (of any
    supported format) yielding a 1-D numeric array with ≥ min_len finite
    values. This is the format-free counterpart of `npy_array` — it credits
    "the agent produced a mass/observable array" regardless of whether it
    was saved as .npy, a CSV column, a text dump, or a JSON list.

    Params:
        min_len: minimum number of finite values. Default 1.
        dtype_kind: retained for rubric back-compat; ignored (all formats
            are coerced to float).
        under_subpath: restrict the search to this subtree.
    """
    import numpy as np
    min_len = int(params.get("min_len", 1))
    root = _scope(sandbox, params)
    scanned = 0
    for ext in (".npy",) + _TABULAR_EXTS + (".json",):
        for p in root.rglob(f"*{ext}"):
            scanned += 1
            for arr in _extract_float_arrays(p, np):
                if arr.size >= min_len:
                    return True, (f"{p.relative_to(sandbox)}: "
                                  f"{arr.size} finite values")
    return False, (f"no numeric array with ≥{min_len} finite values in any "
                   f"format (.npy/.csv/.tsv/.dat/.txt/.json; scanned {scanned})")


def check_record_stream(sandbox: Path, params: dict) -> tuple[bool, str]:
    """Format-agnostic per-event record-stream check: evidence that a
    per-event dataset of ≥ min_records records was produced, in any common
    form — a .jsonl (any schema), a HepMC event stream, or a tabular/array
    dump with ≥ min_records rows. Format-free counterpart of
    `jsonl_with_keys`: it credits the showering / clustering stage on the
    existence of the per-event data, not on the exact keys a tool emits.

    Params:
        min_records: minimum record count. Default 100.
        under_subpath: restrict the search to this subtree.
    """
    min_records = int(params.get("min_records", 100))
    root = _scope(sandbox, params)

    for p in root.rglob("*.jsonl"):
        try:
            n = sum(1 for ln in open(p, errors="ignore") if ln.strip())
        except OSError:
            continue
        if n >= min_records:
            return True, f"{p.relative_to(sandbox)}: {n} JSONL records"

    for ext in _HEPMC_EXTS:
        for p in root.rglob(f"*{ext}"):
            try:
                with open(p, errors="ignore") as f:
                    n = sum(1 for ln in f if ln[:2] in ("E ", "E\t"))
            except OSError:
                continue
            if n >= min_records:
                return True, f"{p.relative_to(sandbox)}: {n} HepMC events"

    import numpy as np
    for ext in _TABULAR_EXTS + (".npy", ".json"):
        for p in root.rglob(f"*{ext}"):
            arrs = _extract_float_arrays(p, np)
            if arrs and max(a.size for a in arrs) >= min_records:
                return True, (f"{p.relative_to(sandbox)}: "
                              f"≥{min_records} per-event rows")

    return False, (f"no per-event record stream with ≥{min_records} records "
                   f"(.jsonl / HepMC / tabular)")


def check_pdf_nonempty(sandbox: Path, params: dict) -> tuple[bool, str]:
    """Any .pdf of at least min_bytes that starts with the %PDF magic."""
    return check_plot_nonempty(sandbox, dict(params, extensions=["pdf"]))


# Magic-byte signatures per file extension — used to reject files
# renamed with the wrong extension (e.g. an empty PDF that's actually
# zero bytes, or a stub renamed to .png).
_MAGIC_BYTES = {
    "pdf": b"%PDF-",
    "png": b"\x89PNG\r\n\x1a\n",
}


def check_plot_nonempty(sandbox: Path, params: dict) -> tuple[bool, str]:
    """Any plot file (.pdf or .png by default) of at least min_bytes
    with the right magic header.

    This is a *presence* check — it rejects zero-byte placeholders and
    files renamed with the wrong extension, but it does NOT validate
    plot content (a blank matplotlib figure clears even a 5KB threshold).
    Pair with a numeric_check for content validation.

    Use this for content_checks where the agent's headline deliverable
    is a plot — both formats are equally valid task outputs.

    Params:
        extensions: list[str], file extensions to accept (no leading
            dot). Default ["pdf", "png"].
        min_bytes: int. Default 1000 — a presence floor, not a quality
            floor.
        under_subpath: str, restrict search to this subtree. Default
            sandbox root.
        exclude_subpaths: list[str], directory-name segments to skip.
            A candidate is ignored if any listed segment appears in its
            path. Use this to reject plots auto-generated by upstream
            tools (e.g. MadGraph drops diagnostic `card.png`/`matrix11.png`
            under `HTML/` and `SubProcesses/`) so the check only credits
            the agent's own deliverable, wherever it chose to save it.
    """
    extensions = [e.lower().lstrip(".") for e in
                  (params.get("extensions") or ["pdf", "png"])]
    min_bytes = int(params.get("min_bytes", 1000))
    exclude = set(params.get("exclude_subpaths") or [])
    root = _scope(sandbox, params)

    candidates: list[Path] = []
    for ext in extensions:
        candidates.extend(root.rglob(f"*.{ext}"))

    for p in candidates:
        if exclude and exclude.intersection(p.parts):
            continue
        try:
            size = p.stat().st_size
            if size < min_bytes:
                continue
            ext = p.suffix.lower().lstrip(".")
            magic = _MAGIC_BYTES.get(ext)
            if magic is None:
                continue
            with open(p, "rb") as f:
                head = f.read(len(magic))
        except OSError:
            continue
        if head == magic:
            return True, (f"{p.relative_to(sandbox)}: {size} bytes, "
                          f"{ext.upper()} header")

    pretty_ext = "/".join(f".{e}" for e in extensions)
    return False, (
        f"no {pretty_ext} ≥{min_bytes} bytes with magic header "
        f"(scanned {len(candidates)} files under "
        f"{root.relative_to(sandbox) if root != sandbox else '.'})"
    )


CONTENT_CHECKS: dict[str, Callable[[Path, dict], tuple[bool, str]]] = {
    "ufo_dir":          check_ufo_dir,
    "lhe_with_events":  check_lhe_with_events,
    "jsonl_with_keys":  check_jsonl_with_keys,   # strict: named keys, .jsonl only
    "record_stream":    check_record_stream,     # format-free per-event stream
    "npy_array":        check_npy_array,          # strict: .npy only
    "numeric_array":    check_numeric_array,      # format-free numeric array
    "pdf_nonempty":     check_pdf_nonempty,    # back-compat alias
    "plot_nonempty":    check_plot_nonempty,   # accepts .pdf or .png
}
