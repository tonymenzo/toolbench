"""toolbench: a platform and CLI for building benchmarks for agentic tools and harnesses."""

from pathlib import Path

__version__ = "0.2.0"

# Repository root (the directory that contains this `toolbench/` package in an
# editable install). The CLI reads `REPO_ROOT/.env` for provider keys + tool
# config and uses it as the cwd for `git rev-parse`. When toolbench is installed
# as a wheel there is no repo checkout; the path simply won't contain a
# `.env`/`.git`, and those lookups degrade gracefully (no .env loaded, git sha =
# "unknown").
REPO_ROOT = Path(__file__).resolve().parents[1]

__all__ = ["__version__", "REPO_ROOT"]
