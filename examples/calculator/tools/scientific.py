"""Scientific tools for the calculator example toolkit."""

import json
import math

from orchestral import define_tool


@define_tool
def power(base: float, exp: float) -> str:
    """Raise base to the given exponent (use exp=0.5 for a square root)."""
    return json.dumps({"result": float(base) ** float(exp)})


@define_tool
def sqrt(x: float) -> str:
    """Square root of a number."""
    return json.dumps({"result": math.sqrt(float(x))})
