"""Benchmark registry for the eval harness.

A benchmark lives in its own subdirectory under `eval/benchmarks/`.
Preferred form: a declarative `benchmark.yaml` (+ prompts/, harnesses/,
loadouts/, sandbox/template/, ground_truth/, checks/) — no Python needed;
it is loaded as a `YamlBenchmark`. A legacy benchmark may instead ship a
`task.py` with a `Task` subclass.

`BENCHMARKS` maps name -> a zero-arg factory returning a fresh `Task`.
"""

from pathlib import Path

import yaml

from toolbench.core.benchmark import YamlBenchmark

_BENCH_DIR = Path(__file__).resolve().parent


def _yaml_name(sub: Path) -> str:
    try:
        data = yaml.safe_load((sub / "benchmark.yaml").read_text()) or {}
    except Exception:
        data = {}
    return data.get("name") or sub.name


def _discover() -> dict:
    out: dict = {}
    for sub in sorted(p for p in _BENCH_DIR.iterdir() if p.is_dir()):
        if (sub / "benchmark.yaml").is_file():
            out[_yaml_name(sub)] = (lambda d=sub: YamlBenchmark(d))
    return out


BENCHMARKS = _discover()
