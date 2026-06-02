"""A one-off, benchmark-local tool (W9): no toolset to publish, just a module
a loadout can name via a `python:` source."""

from orchestral.tools import define_tool


@define_tool
def midpoint(p1: list[float], p2: list[float]) -> dict:
    """Component-wise midpoint of two points."""
    return {"result": [float.__truediv__(float.__add__(a, b), 2.0)
                       for a, b in zip(p1, p2)]}


TOOLS = [midpoint]
