"""Basic arithmetic tools for the calculator example toolkit."""

import json

from orchestral import define_tool


@define_tool
def add(a: float, b: float) -> str:
    """Add two numbers."""
    return json.dumps({"result": float(a) + float(b)})


@define_tool
def subtract(a: float, b: float) -> str:
    """Subtract b from a."""
    return json.dumps({"result": float(a) - float(b)})


@define_tool
def multiply(a: float, b: float) -> str:
    """Multiply two numbers."""
    return json.dumps({"result": float(a) * float(b)})


@define_tool
def divide(a: float, b: float) -> str:
    """Divide a by b."""
    return json.dumps({"result": float(a) / float(b)})
