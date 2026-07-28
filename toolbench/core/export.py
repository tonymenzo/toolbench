"""Portable, schema-versioned export of a completed run.

A run directory is a working artifact: tens of gigabytes of transcripts and
intermediate data, with absolute paths from whichever machine produced it. It is
not a thing you can hand to a collaborator, attach to a paper, or render a
results page from.

This module produces the thing you can. Two layers, because they have different
audiences and differ in size by two orders of magnitude:

  trials.jsonl   one flat, denormalized, schema-versioned row per trial --
                 every number needed to reproduce the reported metrics.
                 Kilobytes. This is what a results page consumes and what a
                 reviewer checks.
  bundle/        the graded evidence behind those rows: per-trial answer files,
                 audit logs, run summaries, manifest. Megabytes.

Transcripts stay out by default: they dominate the size and are the most likely
place for machine-specific or sensitive strings to hide. `--include-transcripts`
opts in.

The flat schema is deliberately denormalized -- cell coordinates repeat on every
row -- so a consumer needs no join logic and no knowledge of toolbench's
internal layout. `schema_version` is the compatibility contract: additive
changes bump the minor, anything that moves or retypes an existing field bumps
the major.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = "1.0"

# Per-trial evidence copied into the bundle. Globs are relative to a trial dir.
TRIAL_EVIDENCE = ["trial.json", "audit.txt", "ux_feedback.md",
                  "artifacts/results/*"]
RUN_EVIDENCE = ["summary.json", "summary.txt", "trials.jsonl", "manifest.json"]


def _scrubber(run_dir: Path):
    """Replace machine-specific absolute paths with stable placeholders.

    An allowlist would be safer than a denylist, but paths are the one class we
    can rewrite losslessly rather than redact -- the *structure* of a path is
    often what makes an audit log readable. Home directories and the run root
    become placeholders; everything else is left intact and visible, so a human
    reviewing the export can still see what leaked rather than trusting that
    nothing did.
    """
    subs = []
    home = str(Path.home())
    subs.append((re.compile(re.escape(str(run_dir.resolve()))), "${RUN}"))
    for var in ("TOOLBENCH_REPO", "HEPBENCH_HOME"):
        v = os.environ.get(var)
        if v:
            subs.append((re.compile(re.escape(str(Path(v).resolve()))), f"${{{var}}}"))
    subs.append((re.compile(re.escape(home)), "${HOME}"))

    def scrub(text: str) -> str:
        for pat, rep in subs:
            text = pat.sub(rep, text)
        return text

    return scrub


def _flatten_trial(row: dict, detail: dict | None, run_meta: dict) -> dict:
    """One trials.jsonl row -> one flat export record."""
    grade = (detail or {}).get("grade", {}) or {}
    stage_grades = grade.get("stage_grades") or []

    weights, metrics, evidence = {}, {}, {}
    for sg in stage_grades:
        sid = sg.get("id")
        if not sid:
            continue
        weights[sid] = sg.get("weight")
        ev = sg.get("evidence")
        if ev:
            evidence[sid] = ev
        # `metrics` is keyed by CHECK name, each holding that check's metric
        # dict: {check: {metric: value}}. Flatten one level and keep the
        # interpretable scalars; bulky per-item dictionaries (per_band_dex,
        # per_peak_dex, ...) stay in the bundle for anyone who needs them.
        keep = {}
        for check, mm in (sg.get("metrics") or {}).items():
            if not isinstance(mm, dict):
                continue
            for k, v in _iter_scalar_metrics(mm):
                keep[k if len(sg.get("metrics")) == 1 else f"{check}.{k}"] = v
        if keep:
            metrics[sid] = keep

    return {
        "schema_version": SCHEMA_VERSION,
        # ---- identity / cell coordinates (denormalized on purpose) ----
        "run_id": run_meta.get("run_id"),
        "benchmark": run_meta.get("benchmark"),
        "trial_id": row.get("trial_id"),
        "harness": row.get("harness"),
        "loadout": row.get("loadout"),
        "variant": row.get("variant"),
        "model": row.get("model"),
        "resolved_model": row.get("resolved_model"),
        "seed": row.get("seed"),
        # ---- outcome ----
        "score": row.get("score"),
        "pass_threshold": run_meta.get("pass_threshold"),
        "passed": (None if row.get("score") is None
                   or run_meta.get("pass_threshold") is None
                   else float(row["score"]) >= float(run_meta["pass_threshold"])),
        "failure_mode": row.get("failure_mode"),
        "ok": row.get("ok"),
        # ---- per-stage ----
        "stages": row.get("stages") or {},
        "stage_credits": row.get("stage_credits") or {},
        "stage_weights": weights,
        "stage_metrics": metrics,
        "stage_evidence": evidence,
        # ---- telemetry ----
        "wall_clock_s": row.get("wall_clock_s"),
        "cost_usd": row.get("cost_usd"),
        "tokens": {
            "input": row.get("input_tokens"),
            "output": row.get("output_tokens"),
            "cache_read": row.get("cache_read_tokens"),
            "cache_creation": row.get("cache_creation_tokens"),
            "initial_input": row.get("initial_input_tokens"),
        },
        "tool_calls": row.get("tool_calls"),
        "tool_errors": row.get("tool_errors"),
        "attempts": row.get("attempts"),
        "nudges": row.get("nudges"),
        # ---- provenance ----
        "provenance": run_meta.get("provenance", {}),
    }


def _iter_scalar_metrics(m: dict) -> Iterable[tuple]:
    for k, v in (m or {}).items():
        if isinstance(v, (int, float, str, bool)) or v is None:
            yield k, v


def _run_meta(run_dir: Path) -> dict:
    manifest = {}
    mp = run_dir / "manifest.json"
    if mp.is_file():
        manifest = json.loads(mp.read_text())
    summary = {}
    sp = run_dir / "summary.json"
    if sp.is_file():
        summary = json.loads(sp.read_text())

    # The pass bar lives in the summary under several equivalent spellings
    # depending on toolbench version; take the first that resolves rather than
    # assuming one layout.
    pass_threshold = None
    for getter in (
        lambda: summary.get("reach_weights", {}).get("pass_threshold"),
        lambda: summary.get("benchmark_config", {}).get("rubric", {}).get("pass_threshold"),
        lambda: (summary.get("cells") or [{}])[0].get("pass_threshold"),
        lambda: summary.get("pass_threshold"),
        lambda: manifest.get("pass_threshold"),
    ):
        try:
            v = getter()
        except (AttributeError, TypeError, IndexError):
            v = None
        if v is not None:
            pass_threshold = float(v)
            break

    return {
        "run_id": manifest.get("run_id") or run_dir.name,
        "benchmark": manifest.get("task") or manifest.get("benchmark"),
        "pass_threshold": pass_threshold,
        "provenance": {
            "git_sha": manifest.get("git_sha") or manifest.get("provenance", {}).get("git_sha")
            if isinstance(manifest.get("provenance"), dict) else manifest.get("git_sha"),
            "versions": manifest.get("versions"),
            "resolution": manifest.get("resolution"),
            "harness": manifest.get("harness"),
        },
    }


def export_run(run_dir: Path, out_dir: Path, *, scrub: bool = True,
               include_transcripts: bool = False,
               archive: bool = False) -> dict:
    """Write a portable export of `run_dir` into `out_dir`. Returns a report."""
    run_dir = Path(run_dir)
    out_dir = Path(out_dir)
    if not (run_dir / "trials.jsonl").is_file():
        raise FileNotFoundError(f"{run_dir} has no trials.jsonl -- not a completed run")

    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = out_dir / "bundle"
    bundle.mkdir(exist_ok=True)
    do_scrub = _scrubber(run_dir) if scrub else (lambda t: t)

    meta = _run_meta(run_dir)

    # ---- layer 1: the flat table -------------------------------------
    rows, n_trials = [], 0
    for line in (run_dir / "trials.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        tid = row.get("trial_id")
        detail = None
        dp = run_dir / "trials" / str(tid) / "trial.json"
        if dp.is_file():
            detail = json.loads(dp.read_text())
        rows.append(_flatten_trial(row, detail, meta))
        n_trials += 1

    flat = out_dir / "trials.jsonl"
    with flat.open("w") as fh:
        for r in rows:
            fh.write(do_scrub(json.dumps(r, sort_keys=True)) + "\n")

    (out_dir / "run.json").write_text(do_scrub(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "run_id": meta["run_id"],
        "benchmark": meta["benchmark"],
        "pass_threshold": meta["pass_threshold"],
        "n_trials": n_trials,
        "provenance": meta["provenance"],
        "bundle_includes_transcripts": include_transcripts,
        "scrubbed": scrub,
    }, indent=2)) + "\n")

    # ---- layer 2: the evidence bundle --------------------------------
    copied = 0
    for name in RUN_EVIDENCE:
        src = run_dir / name
        if src.is_file():
            _copy(src, bundle / name, do_scrub)
            copied += 1
    tdir = run_dir / "trials"
    if tdir.is_dir():
        for t in sorted(tdir.iterdir()):
            if not t.is_dir():
                continue
            for pattern in TRIAL_EVIDENCE:
                for src in sorted(t.glob(pattern)):
                    if src.is_file():
                        _copy(src, bundle / "trials" / t.name / src.relative_to(t),
                              do_scrub)
                        copied += 1
            if include_transcripts:
                src = t / "transcript.jsonl.gz"
                if src.is_file():
                    # Binary: copied verbatim, so it is NOT scrubbed. This is
                    # why transcripts are opt-in.
                    dst = bundle / "trials" / t.name / src.name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    copied += 1

    report = {"run_id": meta["run_id"], "n_trials": n_trials,
              "files_bundled": copied, "out_dir": str(out_dir),
              "scrubbed": scrub, "transcripts": include_transcripts}

    if archive:
        tar_path = out_dir.parent / f"{out_dir.name}.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tf:
            tf.add(out_dir, arcname=out_dir.name)
        report["archive"] = str(tar_path)
        report["archive_bytes"] = tar_path.stat().st_size
    return report


def _copy(src: Path, dst: Path, scrub) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = src.read_text()
    except (UnicodeDecodeError, ValueError):
        shutil.copy2(src, dst)
        return
    dst.write_text(scrub(text))
