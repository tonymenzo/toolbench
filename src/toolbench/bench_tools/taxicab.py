"""The Manhattan (L1) distance metric as its own one-tool toolset."""

from orchestral.tools import define_tool


@define_tool
def manhattan_distance(p1: list[float], p2: list[float]) -> dict:
    """L1 distance: sum |a_i - b_i|."""
    return {"result": sum(abs(float.__sub__(float(a), float(b)))
                          for a, b in zip(p1, p2))}


TOOLS = [manhattan_distance]
BUNDLES = {"metric": [manhattan_distance]}
