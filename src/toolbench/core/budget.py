"""
Cost budget tracker for the eval harness.

A `Budget` is checked between trials by the CLI. `add()` raises
`BudgetExceeded` when the running total crosses `max_usd`, allowing the
CLI to abort a multi-trial run cleanly.
"""

import threading


class BudgetExceeded(Exception):
    """Raised when a Budget's spent total exceeds its max_usd cap."""


class Budget:
    def __init__(self, max_usd: float | None):
        self.max_usd = max_usd
        self._spent = 0.0
        self._lock = threading.Lock()

    def add(self, usd: float | None) -> None:
        if usd is None:
            return
        with self._lock:
            self._spent += float(usd)
            if self.max_usd is not None and self._spent > self.max_usd:
                raise BudgetExceeded(
                    f"Budget exceeded: spent ${self._spent:.4f} > cap ${self.max_usd:.4f}"
                )

    @property
    def spent(self) -> float:
        with self._lock:
            return self._spent

    @property
    def remaining(self) -> float:
        if self.max_usd is None:
            return float("inf")
        with self._lock:
            return self.max_usd - self._spent
