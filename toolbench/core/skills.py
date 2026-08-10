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


def _frontmatter_description(src: Path) -> str:
    """The skill's `description:` from its YAML frontmatter, if it has one.

    Runtimes with a native skill concept surface this themselves. Runtimes
    without one (codex) get only the prompt pointer, and a pointer that reads
    "- skills/pythia_forward_run_cards.md: pythia_forward_run_cards" tells the
    agent nothing about whether the file is worth opening -- it is the slug
    twice. Carrying the description across makes the decision informed rather
    than a coin flip, which is the difference between measuring whether
    guidance helps and measuring whether an agent gambles on unlabelled files.

    Parsed without a YAML dependency: the block is a leading `---` fence and
    the field is a single line. Anything unexpected yields "" rather than
    raising -- a pointer is a convenience, never a reason to fail a trial.
    """
    try:
        text = src.read_text()
    except Exception:
        return ""
    if not text.lstrip().startswith("---"):
        return ""
    body = text.lstrip()[3:]
    end = body.find("\n---")
    if end == -1:
        return ""
    for line in body[:end].splitlines():
        if line.strip().lower().startswith("description:"):
            desc = line.split(":", 1)[1].strip().strip('"\'')
            return " ".join(desc.split())
    return ""


def _write_native_skill(root: Path, name: str, src: Path,
                        loadout_name: str) -> None:
    """Materialize one skill as a PROJECT-scoped Claude Code skill.

    Writes `<root>/<name>/SKILL.md`, which the CLI discovers under `project`
    setting scope because the sandbox is the trial's cwd. The model then sees
    the skill's `description` without opening anything — the whole reason to
    prefer this over a filename pointer.

    The directory name is what the CLI lists the skill as; a `name:` in the
    frontmatter does not override it. Frontmatter is otherwise passed through
    untouched (extra keys such as a toolkit's `bundle:` are harmless), and
    synthesized when the source has none, since the CLI requires it.
    """
    dst_dir = root / name
    dst_dir.mkdir(parents=True, exist_ok=True)
    text = src.read_text()
    if not text.lstrip().startswith("---"):
        where = f" (from the {loadout_name} loadout)" if loadout_name else ""
        text = (f"---\nname: {name}\n"
                f"description: Guidance bundled with this toolset{where}.\n"
                f"---\n\n{text}")
    (dst_dir / "SKILL.md").write_text(text)


def prepare_skills(skills: list, sandbox_dir: str | Path, *,
                   loadout_name: str = "", native_dir: str | Path | None = None) -> str:
    """Materialize a loadout's skills for one trial.

    Returns the system-prompt addendum ('' when there is nothing to add).
    Raises on a missing file or malformed entry — a skill the loadout declares
    but the agent never receives would corrupt the measurement silently.

    `inline` skills are always embedded in the system prompt. `on_demand`
    skills are delivered one of two ways:

    - `native_dir` set (the runner passes `<sandbox>/.claude/skills` for
      runtimes that drive the Claude Code CLI): written as real project-scoped
      skills, so the CLI surfaces each one's name AND description to the model
      and the agent can invoke it. No prompt addendum is needed or emitted —
      the harness's own skill machinery does the advertising.
    - otherwise: copied to `<sandbox>/skills/<name><ext>` with a one-line
      pointer appended to the system prompt. This is the portable fallback for
      runtimes with no skill concept; it relies on the agent choosing to read
      a file it can only identify by name, so prefer the native path where the
      runtime supports it.

    Both paths keep skills PER-TRIAL and PER-ARM, which is the property that
    matters: a skill must reach exactly the arm whose loadout declares it.
    """
    if not skills:
        return ""
    sandbox = Path(sandbox_dir)
    native_root = Path(native_dir) if native_dir is not None else None
    pointers: list[str] = []
    inline_blocks: list[str] = []
    agents_lines: list[str] = []

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
        elif native_root is not None:
            _write_native_skill(native_root, name, src, loadout_name)
        else:  # on_demand, portable fallback
            dst = sandbox / SKILLS_SUBDIR / f"{name}{src.suffix or '.md'}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            desc = _frontmatter_description(src)
            pointers.append(f"- {dst.relative_to(sandbox)}: {name}"
                            + (f" — {desc}" if desc else ""))
            agents_lines.append(
                f"- `{dst.relative_to(sandbox)}` — {name}"
                + (f": {desc}" if desc else ""))

    if agents_lines:
        _write_agents_pointer(sandbox, agents_lines)

    parts: list[str] = []
    if pointers:
        parts.append(
            "Skill guides are available in your working directory — read "
            "them when relevant:\n" + "\n".join(pointers)
        )
    if inline_blocks:
        parts.append("\n\n".join(inline_blocks))
    return "\n\n".join(parts)


def _write_agents_pointer(sandbox: Path, lines: list[str]) -> None:
    """Advertise the guides in `<sandbox>/AGENTS.md` as well as the prompt.

    AGENTS.md is codex's only MODEL-FACING instruction channel: it is
    auto-injected into every session (verified against codex-cli 0.146.0),
    whereas `~/.codex/prompts/` entries are user-typed `/slash` commands and
    so are unreachable in a headless `codex exec` benchmark run.

    A POINTER, not the body. That keeps the semantics equivalent to a native
    claude_code skill -- the description is always visible, the body is read
    on demand -- rather than turning the guide into `inline`, which would be
    always-in-context and a different measurement. `mode: inline` remains the
    way to ask for guaranteed delivery.

    Appends to an existing AGENTS.md rather than clobbering it: a benchmark's
    sandbox template may legitimately ship one, and silently replacing it
    would remove task material.
    """
    doc = sandbox / "AGENTS.md"
    block = ("\n## Reference guides available in this workspace\n\n"
             "Read these when they are relevant to the task; they are "
             "reference material, not instructions to follow blindly.\n\n"
             + "\n".join(lines) + "\n")
    prior = doc.read_text() if doc.is_file() else ""
    doc.write_text(prior + block if prior else block.lstrip("\n"))


def skill_names(skills: list) -> list[str]:
    """The declared skill names, for the trial record (best-effort: no
    validation here; `prepare_skills` is the strict gate)."""
    return [str(e.get("name", "?")) for e in skills or [] if isinstance(e, dict)]
