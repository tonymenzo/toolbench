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

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


VALID_MODES = ("on_demand", "inline")

# Sandbox subdirectory on_demand skills are materialized into.
SKILLS_SUBDIR = "skills"


@dataclass
class ResolvedSkill:
    """One skill, located and ready to materialize.

    ``root`` is the unit to COPY and ``is_dir`` says how: a directory-form
    skill keeps its ``references/`` and ``scripts/`` beside its ``SKILL.md``
    and they have to come along, while a file-form skill IS its markdown.
    Taking only ``doc`` for the first kind delivers an index of dangling
    links -- which for a guide whose substance lives in ``references/`` is
    most of the guide.
    """

    name: str
    doc: Path
    root: Path
    is_dir: bool
    mode: str
    #: Where it came from, for the trial record: "benchmark" or
    #: "toolbase:<toolkit>@<slot>".
    source: str
    toolkit: Optional[str] = None
    slug: Optional[str] = None
    version: Optional[str] = None

    def record(self) -> dict:
        """Provenance for the trial record.

        Names what was RESOLVED, not what was declared. A run that recorded
        only "feynrules" could not say which toolkit slot it came from, and
        the slots differ: the same skill in two installed versions of one
        toolkit had 11 pitfall entries in one and 6 in the other.
        """
        return {
            "name": self.name, "source": self.source, "mode": self.mode,
            "toolkit": self.toolkit, "slug": self.slug,
            "version": self.version, "path": str(self.root),
            "doc": str(self.doc), "is_dir": self.is_dir,
        }


def _parse_entry(entry: dict, *, loadout_name: str) -> dict:
    """Validate one `skills:` entry into a spec dict.

    Two forms:

    - ``{name, file, mode?}``    a skill that ships with the benchmark.
    - ``{toolbase: "<toolkit>__<slug>", mode?}``  one served by toolbase with
      the toolkit, located at trial setup against the SERVING slot.

    The second form exists because the first cannot name a skill that lives
    in someone else's checkout at a version toolbase chooses. Writing a path
    to it by hand pins the wrong thing: the path is stable, the slot the
    loadout actually serves is not.
    """
    if not isinstance(entry, dict):
        raise ValueError(
            f"loadout {loadout_name!r}: each `skills:` entry must be a "
            f"mapping with `name`/`file` or `toolbase`, got {entry!r}"
        )
    mode = entry.get("mode", "on_demand")
    if mode not in VALID_MODES:
        raise ValueError(
            f"loadout {loadout_name!r}: skill has unknown mode "
            f"{mode!r}; expected one of {list(VALID_MODES)}"
        )

    ref = entry.get("toolbase")
    if ref:
        if entry.get("file"):
            raise ValueError(
                f"loadout {loadout_name!r}: skill {ref!r} sets both "
                f"`toolbase:` and `file:`; use one -- `toolbase:` locates the "
                f"skill itself and a `file:` beside it would silently win or "
                f"disagree"
            )
        ref = str(ref)
        if "__" not in ref:
            raise ValueError(
                f"loadout {loadout_name!r}: `toolbase:` skill must be "
                f"'<toolkit>__<slug>' (the name `tb activate` uses); "
                f"got {ref!r}"
            )
        toolkit, slug = ref.split("__", 1)
        return {"kind": "toolbase", "toolkit": toolkit, "slug": slug,
                "name": entry.get("name") or slug, "mode": mode}

    name, file = entry.get("name"), entry.get("file")
    if not name or not file:
        raise ValueError(
            f"loadout {loadout_name!r}: a skill needs both `name:` and "
            f"`file:` (or a `toolbase:` reference); got {entry!r}"
        )
    return {"kind": "file", "name": str(name), "file": Path(str(file)),
            "mode": mode}


def _toolbase_skill_rows(toolbase_loadout: Optional[str]) -> dict:
    """``{(toolkit, slug): row}`` from ``tb list --json``.

    Shells out rather than importing toolbase's internals. The decision is not
    just "which skills exist": it folds in two-layer config resolution, bundle
    availability, the install scope of the slot, the loadout's allow/blocklists
    and slug normalisation. Re-deriving that here is precisely the drift the
    single-decision refactor in toolbase removed, and most of the machinery is
    private. `tb list --json` is the public interface that already composes it.
    One subprocess per trial against a trial measured in tens of minutes.

    ONLY THE SERVING SLOT. `tb list --json` emits one entry per installed
    slot, so a toolkit with five versions installed yields five rows for the
    same skill, pointing at five different copies. Filtering by name alone
    picks an arbitrary one -- in practice the highest version rather than the
    pinned one, which is how a loadout pinned to `editable` can be handed the
    skill from an older release. ``serving`` is the flag that means "the slot
    toolbase would spawn"; ``active`` means the loadout names the toolkit.
    Both are required.

    Args:
        toolbase_loadout: The toolbase loadout name to resolve against
            (from the arm's ``tools.sources``), or None for the active one.

    Returns:
        Rows keyed by (toolkit, slug), each carrying the slot's version.

    Raises:
        RuntimeError: toolbase is not installed, or the query failed.
    """
    cmd = ["tb", "list", "--json"]
    if toolbase_loadout:
        cmd += ["--loadout", str(toolbase_loadout)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(
            f"could not run `{' '.join(cmd)}` to locate toolbase skills: {e}"
        ) from e
    if proc.returncode != 0:
        raise RuntimeError(
            f"`{' '.join(cmd)}` failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"`{' '.join(cmd)}` did not return JSON: {e}") from e

    rows: dict = {}
    for entry in payload:
        if not (entry.get("serving") and entry.get("active")):
            continue
        tk = entry.get("name")
        for sk in (entry.get("skills") or []):
            row = dict(sk)
            row["version"] = entry.get("version")
            rows[(tk, sk.get("slug"))] = row
    return rows


def _resolve_spec(spec: dict, *, loadout_name: str,
                  toolbase_rows: Optional[dict] = None) -> ResolvedSkill:
    """Locate one parsed spec on disk.

    Strict on purpose, matching how a missing `file:` is treated: a skill the
    loadout declares but the agent never receives is a thinner arm than the
    measurement claims, and it fails silently in the results rather than
    loudly at setup.
    """
    if spec["kind"] == "file":
        src = spec["file"]
        if not src.is_file():
            raise FileNotFoundError(
                f"loadout {loadout_name!r}: skill {spec['name']!r} file not "
                f"found: {src}"
            )
        return ResolvedSkill(
            name=spec["name"], doc=src, root=src, is_dir=False,
            mode=spec["mode"], source="benchmark",
        )

    tk, slug = spec["toolkit"], spec["slug"]
    rows = toolbase_rows if toolbase_rows is not None else {}
    row = rows.get((tk, slug))
    if row is None:
        known = sorted(f"{a}__{b}" for a, b in rows)
        raise ValueError(
            f"loadout {loadout_name!r}: skill {tk}__{slug} is not served by "
            f"the toolkit toolbase would spawn. Served skills: "
            f"{known or '(none)'}. A toolkit that is installed but not named "
            f"by the loadout surfaces nothing, whatever its skills say."
        )
    state = row.get("state")
    if state != "on":
        raise ValueError(
            f"loadout {loadout_name!r}: skill {tk}__{slug} resolves to "
            f"{state!r}, not 'on', so it would not reach the agent. "
            + {
                "not-enabled": "The toolbase loadout declares skills.enabled "
                               "and this is not in it.",
                "off": "It was deactivated (`tb activate "
                       f"{tk}__{slug}` to undo).",
                "gated": "Its bundle's config requirements are unmet.",
            }.get(str(state), "")
        )
    doc, root = row.get("doc"), row.get("root")
    if not doc or not root:
        raise ValueError(
            f"loadout {loadout_name!r}: toolbase reported no path for "
            f"{tk}__{slug}"
        )
    return ResolvedSkill(
        name=spec["name"], doc=Path(doc), root=Path(root),
        is_dir=bool(row.get("is_dir")), mode=spec["mode"],
        source=f"toolbase:{tk}@{row.get('version')}",
        toolkit=tk, slug=slug, version=row.get("version"),
    )


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
                        loadout_name: str,
                        skill: Optional["ResolvedSkill"] = None) -> None:
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

    # A directory-form skill brings its whole tree. `references/` and
    # `scripts/` sit beside SKILL.md and the guide links to them; copying the
    # markdown alone delivers an index of dangling links, which for a guide
    # whose substance is in references/ is most of the guide. Copied first so
    # the SKILL.md written below (possibly with synthesized frontmatter) wins.
    if skill is not None and skill.is_dir:
        for item in skill.root.iterdir():
            if item.name == skill.doc.name:
                continue
            dest = dst_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

    text = src.read_text()
    if not text.lstrip().startswith("---"):
        where = f" (from the {loadout_name} loadout)" if loadout_name else ""
        text = (f"---\nname: {name}\n"
                f"description: Guidance bundled with this toolset{where}.\n"
                f"---\n\n{text}")
    (dst_dir / "SKILL.md").write_text(text)


def resolve_skills(skills: list, *, loadout_name: str = "",
                   toolbase_loadout: str | None = None) -> "list[ResolvedSkill]":
    """Locate a loadout's skills without writing anything.

    Split from materialization so it can run where the TOOLS resolve, in
    ``build_agent_tools``. That call happens twice for a reason worth
    preserving: once in the CLI's resolution preview, against a temp
    directory, which is what puts the arm in ``manifest["resolution"]`` and
    fails a mis-wired arm before any trial burns time; and once per trial,
    against the trial's own sandbox. Skills resolving anywhere else would be
    absent from the first and so from the manifest, and a typo in a skill
    name would surface twenty minutes into a run instead of at the preview.

    Args:
        skills: Raw ``skills:`` entries from the loadout.
        loadout_name: For error messages.
        toolbase_loadout: The toolbase loadout the arm serves from.

    Returns:
        One ResolvedSkill per entry, in declaration order.

    Raises:
        ValueError / FileNotFoundError: any entry that would not reach the
            agent, which is a thinner arm than the measurement claims.
    """
    if not skills:
        return []
    specs = [_parse_entry(e, loadout_name=loadout_name) for e in skills]
    # One query for the whole arm, and only when something needs it -- a
    # benchmark-local skill must not make a trial depend on toolbase.
    rows = (_toolbase_skill_rows(toolbase_loadout)
            if any(sp["kind"] == "toolbase" for sp in specs) else {})
    return [_resolve_spec(sp, loadout_name=loadout_name, toolbase_rows=rows)
            for sp in specs]


def _from_record(rec: dict) -> "ResolvedSkill":
    """Rebuild a ResolvedSkill from the record resolution already produced.

    Lets a trial materialize from the answer the resolver reached rather than
    asking again -- one query per resolution, not one per consumer of it.
    """
    return ResolvedSkill(
        name=rec["name"], doc=Path(rec["doc"]), root=Path(rec["path"]),
        is_dir=bool(rec.get("is_dir")), mode=rec.get("mode", "on_demand"),
        source=rec.get("source", "benchmark"), toolkit=rec.get("toolkit"),
        slug=rec.get("slug"), version=rec.get("version"),
    )


def prepare_skills(skills: list, sandbox_dir: str | Path, *,
                   loadout_name: str = "", native_dir: str | Path | None = None,
                   toolbase_loadout: str | None = None,
                   records: "list[dict] | None" = None) -> tuple:
    """Materialize a loadout's skills for one trial.

    Returns ``(system_prompt_addendum, records)`` -- the addendum is '' when
    there is nothing to add, and ``records`` are provenance dicts naming what
    was RESOLVED for the trial record.
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
        return "", []
    sandbox = Path(sandbox_dir)
    native_root = Path(native_dir) if native_dir is not None else None
    pointers: list[str] = []
    inline_blocks: list[str] = []
    agents_blocks: list[tuple[str, str, str, str]] = []

    resolved = (
        [_from_record(r) for r in records] if records is not None
        else resolve_skills(skills, loadout_name=loadout_name,
                            toolbase_loadout=toolbase_loadout)
    )

    for skill in resolved:
        name, src, mode = skill.name, skill.doc, skill.mode
        if mode == "inline":
            inline_blocks.append(
                f"### Skill: {name}\n{src.read_text().strip()}"
            )
        elif native_root is not None:
            _write_native_skill(native_root, name, src, loadout_name,
                                skill=skill)
        else:  # on_demand, portable fallback
            dst = sandbox / SKILLS_SUBDIR / f"{name}{src.suffix or '.md'}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            if skill.is_dir:
                # Same reason as the native path: the guide links sideways.
                for item in skill.root.iterdir():
                    if item.name == skill.doc.name:
                        continue
                    target = dst.parent / item.name
                    if item.is_dir():
                        shutil.copytree(item, target, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item, target)
            desc = _frontmatter_description(src)
            pointers.append(f"- {dst.relative_to(sandbox)}: {name}"
                            + (f" — {desc}" if desc else ""))
            agents_blocks.append((name, desc, str(dst.relative_to(sandbox)),
                                  _strip_frontmatter(src.read_text())))

    if agents_blocks:
        _write_agents_doc(sandbox, agents_blocks)

    parts: list[str] = []
    if pointers:
        parts.append(
            "Skill guides are available in your working directory — read "
            "them when relevant:\n" + "\n".join(pointers)
        )
    if inline_blocks:
        parts.append("\n\n".join(inline_blocks))
    return "\n\n".join(parts), [sk.record() for sk in resolved]


def _strip_frontmatter(text: str) -> str:
    """The document body, without its YAML frontmatter block."""
    if not text.lstrip().startswith("---"):
        return text.strip()
    body = text.lstrip()[3:]
    end = body.find("\n---")
    return (body[end + 4:] if end != -1 else body).strip()


def _write_agents_doc(sandbox: Path, blocks) -> None:
    """Deliver the guides IN FULL via `<sandbox>/AGENTS.md`.

    AGENTS.md is codex's only MODEL-FACING instruction channel: it is
    auto-injected into every session (verified against codex-cli 0.146.0),
    whereas `~/.codex/prompts/` entries are user-typed `/slash` commands and
    so are unreachable in a headless `codex exec` benchmark run.

    THE BODY, not a pointer. A pointer would leave "did the agent choose to
    open the file" inside the measurement, and on the 2026-08-10 gpt-5.6 run
    that nuisance variable dominated: the guide was never opened at all. What
    the ablation is meant to isolate is whether the bundle's KNOWLEDGE helps,
    and the no-tools arm receives nothing either way, so injecting the body
    makes the within-model delta measure exactly that.

    It does mean a runtime WITHOUT a native skill mechanism receives the
    guidance unconditionally, while one WITH it (claude_code) still loads the
    body on invocation. That asymmetry is real and is a property of the
    harnesses, not of this code: each delivers the bundle through the
    strongest channel it has. It must be stated when deltas from two runtimes
    are compared -- and `mode: inline` on both is the way to make them
    symmetric if that comparison is load-bearing.

    Appends to an existing AGENTS.md rather than clobbering it: a benchmark's
    sandbox template may legitimately ship one, and silently replacing it
    would remove task material.
    """
    doc = sandbox / "AGENTS.md"
    parts = ["## Reference guides for this workspace\n",
             "The following material is provided with your toolset. It is "
             "reference documentation, not instructions to follow blindly; "
             "apply the parts that bear on the task.\n"]
    for name, desc, rel, body in blocks:
        parts.append(f"\n### {name}\n")
        if desc:
            parts.append(f"_{desc}_\n")
        parts.append(f"\n(Also on disk at `{rel}`.)\n\n{body}\n")
    block = "\n".join(parts)
    prior = doc.read_text() if doc.is_file() else ""
    doc.write_text((prior.rstrip() + "\n\n" + block) if prior else block)


def skill_names(skills: list) -> list[str]:
    """The DECLARED skill names (best-effort; `prepare_skills` is the strict
    gate, and its records are what the trial recordings use).

    Handles both entry forms: a `toolbase:` reference has no `name:` of its
    own unless one is given, and falls back to its slug.
    """
    out = []
    for e in skills or []:
        if not isinstance(e, dict):
            continue
        if e.get("name"):
            out.append(str(e["name"]))
        elif e.get("toolbase"):
            ref = str(e["toolbase"])
            out.append(ref.split("__", 1)[1] if "__" in ref else ref)
        else:
            out.append("?")
    return out
