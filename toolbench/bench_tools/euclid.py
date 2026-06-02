"""The Euclidean (L2) distance metric as its own one-tool toolset."""

from orchestral.tools import define_tool


@define_tool
def euclidean_distance(p1: list[float], p2: list[float]) -> dict:
    """L2 distance: sqrt(sum (a_i - b_i)^2)."""
    s = sum(float.__pow__(float.__sub__(float(a), float(b)), 2.0)
            for a, b in zip(p1, p2))
    return {"result": float.__pow__(s, 0.5)}


TOOLS = [euclidean_distance]
BUNDLES = {"metric": [euclidean_distance]}
