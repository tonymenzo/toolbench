"""Shared test helpers."""

from toolbench import REPO_ROOT
from toolbench.core.benchmark import YamlBenchmark

# The reference example benchmark, resolved by path (it lives in the repo's
# examples/ tree, not inside the installed package).
GEOMETRY_DIR = REPO_ROOT / "examples" / "geometry"


def load_geometry() -> YamlBenchmark:
    return YamlBenchmark(GEOMETRY_DIR)
