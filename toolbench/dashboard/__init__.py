"""
Live, read-only dashboard for a campaign of toolbench runs.

`toolbench dashboard <dir>` serves a local web page showing every run under
`<dir>`: which trials are queued, running, finished or interrupted, how each
cell is scoring so far, spend against budget, and each finished run's
`summary.txt`. See `state` for how the snapshot is derived from run
directories and `server` for the HTTP surface.
"""

from toolbench.dashboard.server import serve

__all__ = ["serve"]
