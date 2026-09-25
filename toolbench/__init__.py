"""toolbench: a platform and CLI for building benchmarks for agentic tools and harnesses."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

# DERIVED, never hardcoded. This constant sat at "0.4.0" through four minor
# releases: the CLI and the run manifest read
# `importlib.metadata.version("toolbench")` instead, so nothing exercised it and
# nothing caught the drift. Anything reading the exported `__version__` -- a
# downstream consumer, or a future manifest field -- was told the wrong release.
# Reading the same source the CLI does makes the two incapable of disagreeing.
try:
    __version__ = _pkg_version("toolbench")
except PackageNotFoundError:      # a source tree that was never installed
    __version__ = "0.0.0+unknown"

# Repository root (the directory that contains this `toolbench/` package in an
# editable install). The CLI reads `REPO_ROOT/.env` for provider keys + tool
# config and uses it as the cwd for `git rev-parse`. When toolbench is installed
# as a wheel there is no repo checkout; the path simply won't contain a
# `.env`/`.git`, and those lookups degrade gracefully (no .env loaded, git sha =
# "unknown").
REPO_ROOT = Path(__file__).resolve().parents[1]

__all__ = ["__version__", "REPO_ROOT"]
