"""
JSONL/JSON persistence helpers for the eval harness.

Transcripts are gzipped JSONL (one record per tool call plus a final
assistant record) so multi-thousand-call trials stay small on disk.
"""

import gzip
import json
from pathlib import Path
from typing import Iterable, Any


def write_json(path: str | Path, obj: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(obj, f, indent=2, default=str)


def read_json(path: str | Path) -> Any:
    with open(path) as f:
        return json.load(f)


def append_jsonl(path: str | Path, record: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def read_jsonl(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with open(p) as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl_gz(path: str | Path, records: Iterable[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt") as f:
        for record in records:
            f.write(json.dumps(record, default=str) + "\n")


def read_jsonl_gz(path: str | Path) -> list[dict]:
    p = Path(path)
    with gzip.open(p, "rt") as f:
        return [json.loads(line) for line in f if line.strip()]
