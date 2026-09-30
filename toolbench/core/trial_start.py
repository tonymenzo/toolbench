"""
A trial's starting conditions, recorded for live inspection.

Written into the trial directory immediately before the agent's first turn:

  prompts.json        the exact system and user prompts the agent was given
                      (the system prompt as composed from the variant, the
                      loadout addendum and any skills pointer).
  sandbox_init.json   the sandbox as it stood at that moment: every file with
                      its size and mtime, and every directory. Anything absent
                      from it was produced by the agent.

Taken after the runtime's own setup (MCP config, skills, AGENTS.md), so
harness scaffolding counts as initial state. Sizes and mtimes rather than
hashes keep this cheap for seeds carrying large data files; a seeded file
counts as modified when either changes.

Like `status.json`, these are observational: nothing that runs or grades a
trial reads them, `export` does not include them, and a failure to write
them never fails the trial.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

PROMPTS_FILE = "prompts.json"
SANDBOX_INIT_FILE = "sandbox_init.json"

# Upper bound on recorded entries, so a seed carrying e.g. a whole virtualenv
# cannot stall trial startup. The snapshot says when it was truncated.
MAX_ENTRIES = 50_000


def snapshot_tree(root: str | Path) -> dict:
    """List every file (with [size, mtime_ns]) and directory under `root`,
    keyed by POSIX path relative to it. Symlinks are recorded, not followed."""
    root = Path(root)
    files: dict[str, list[int]] = {}
    dirs: list[str] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        for name in dirnames + filenames:
            if len(files) + len(dirs) >= MAX_ENTRIES:
                truncated = True
                break
            path = Path(dirpath) / name
            rel = (rel_dir / name).as_posix()
            try:
                st = path.lstat()
            except OSError:
                continue
            if name in dirnames and not path.is_symlink():
                dirs.append(rel)
            else:
                files[rel] = [st.st_size, st.st_mtime_ns]
        if truncated:
            break
    return {"files": files, "dirs": sorted(dirs), "truncated": truncated}


def record_trial_start(trial_dir: str | Path, sandbox_dir: str | Path, *,
                       system_prompt: str, user_prompt: str) -> None:
    """Write `prompts.json` and `sandbox_init.json` into `trial_dir`."""
    trial_dir = Path(trial_dir)
    try:
        (trial_dir / PROMPTS_FILE).write_text(json.dumps(
            {"system": system_prompt, "user": user_prompt}, indent=2))
        (trial_dir / SANDBOX_INIT_FILE).write_text(json.dumps(snapshot_tree(sandbox_dir)))
    except OSError:
        pass  # observational only: never fail a trial over it
