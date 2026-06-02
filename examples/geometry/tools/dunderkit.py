"""Arithmetic primitives, each tool's body a single Python dunder call."""

from orchestral.tools import define_tool


@define_tool
def add(a: float, b: float) -> dict:
    """a + b, via float.__add__."""
    return {"result": float.__add__(float(a), float(b))}


@define_tool
def subtract(a: float, b: float) -> dict:
    """a - b, via float.__sub__."""
    return {"result": float.__sub__(float(a), float(b))}


@define_tool
def multiply(a: float, b: float) -> dict:
    """a * b, via float.__mul__."""
    return {"result": float.__mul__(float(a), float(b))}


@define_tool
def divide(a: float, b: float) -> dict:
    """a / b, via float.__truediv__."""
    return {"result": float.__truediv__(float(a), float(b))}


@define_tool
def power(base: float, exp: float) -> dict:
    """base ** exp, via float.__pow__. (sqrt = power(x, 0.5))"""
    return {"result": float.__pow__(float(base), float(exp))}


TOOLS = [add, subtract, multiply, divide, power]   # discovery entrypoint (all tools)

# BUNDLES — author-declared groupings within this toolset (a toolbase "bundle").
BUNDLES = {
    "additive":       [add, subtract],
    "multiplicative": [multiply, divide],
    "powers":         [power],
}
