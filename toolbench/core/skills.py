"""
Loadout skills: recipe/guide documents delivered to the agent.

A skill is tool-use guidance that travels with a loadout — the third leg
of a domain harness (tools + skills + prompts). Each entry in a
loadout's `skills:` list is `{name, file, mode?}`:

    skills:
      - name: distance_recipe
        file: ./skills/distance_recipe.md   # relative to the benchmark dir
        mode: on_demand                     # or: inline

Two delivery modes:

  - `on_demand` (default): the file is copied into the trial sandbox at
    `skills/<name>.md` and the system prompt gains a one-line pointer.
    The agent reads it when it judges it relevant (progressive
    disclosure — the Claude Code skills model). Costs ~no context until
    consulted; requires the agent to have a way to read files (a
    ReadFile/RunCommand core tool).
  - `inline`: the full content is embedded in the system prompt.
    Always visible, costs context every turn; right for short recipes
    or harnesses whose core ships no file tools.

Skills are part of the *measured configuration*, so resolution is
strict: a missing file or unknown mode raises at trial setup rather
than silently running a thinner arm than the loadout declares.
"""

import shutil
from pathlib import Path


VALID_MODES = ("on_demand", "inline")

# Sandbox subdirectory on_demand skills are materialized into.
SKILLS_SUBDIR = "skills"


def _parse_entry(entry: dict, *, loadout_name: str) -> tuple[str, Path, str]:
    """Validate one `skills:` entry into (name, file path, mode)."""
    if not isinstance(entry, dict):
        raise ValueError(
            f"loadout {loadout_name!r}: each `skills:` entry must be a "
            f"mapping with `name`/`file`, got {entry!r}"
        )
    name = entry.get("name")
    file = entry.get("file")
    if not name or not file:
        raise ValueError(
            f"loadout {loadout_name!r}: a skill needs both `name:` and "
            f"`file:`; got {entry!r}"
        )
    mode = entry.get("mode", "on_demand")
    if mode not in VALID_MODES:
        raise ValueError(
            f"loadout {loadout_name!r}: skill {name!r} has unknown mode "
            f"{mode!r}; expected one of {list(VALID_MODES)}"
        )
    return str(name), Path(str(file)), mode


def prepare_skills(skills: list, sandbox_dir: str | Path, *,
                   loadout_name: str = "") -> str:
    """Materialize a loadout's skills for one trial.

    Copies `on_demand` skills into `<sandbox>/skills/<name><ext>` and
    returns the system-prompt addendum covering both modes ('' when the
    loadout has no skills). Raises on a missing file or malformed entry
    — a skill the loadout declares but the agent never receives would
    corrupt the measurement silently.
    """
    if not skills:
        return ""
    sandbox = Path(sandbox_dir)
    pointers: list[str] = []
    inline_blocks: list[str] = []

    for entry in skills:
        name, src, mode = _parse_entry(entry, loadout_name=loadout_name)
        if not src.is_file():
            raise FileNotFoundError(
                f"loadout {loadout_name!r}: skill {name!r} file not found: "
                f"{src}"
            )
        if mode == "inline":
            inline_blocks.append(
                f"### Skill: {name}\n{src.read_text().strip()}"
            )
        else:  # on_demand
            dst = sandbox / SKILLS_SUBDIR / f"{name}{src.suffix or '.md'}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            pointers.append(f"- {dst.relative_to(sandbox)}: {name}")

    parts: list[str] = []
    if pointers:
        parts.append(
            "Skill guides are available in your working directory — read "
            "them when relevant:\n" + "\n".join(pointers)
        )
    if inline_blocks:
        parts.append("\n\n".join(inline_blocks))
    return "\n\n".join(parts)


def skill_names(skills: list) -> list[str]:
    """The declared skill names, for the trial record (best-effort: no
    validation here; `prepare_skills` is the strict gate)."""
    return [str(e.get("name", "?")) for e in skills or [] if isinstance(e, dict)]
