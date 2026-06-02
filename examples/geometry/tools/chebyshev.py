"""The Chebyshev (L-infinity) distance metric as its own one-tool toolset."""

from orchestral.tools import define_tool


@define_tool
def chebyshev_distance(p1: list[float], p2: list[float]) -> dict:
    """L-infinity distance: max |a_i - b_i|."""
    return {"result": max(abs(float.__sub__(float(a), float(b)))
                          for a, b in zip(p1, p2))}


TOOLS = [chebyshev_distance]
BUNDLES = {"metric": [chebyshev_distance]}
