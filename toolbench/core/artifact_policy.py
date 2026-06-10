"""
Artifact-preservation policy: what survives sandbox cleanup.

After a trial is graded, the sandbox is deleted; only the files matched
by this policy are copied into `trials/<id>/artifacts/` first. Those
artifacts are the *only* evidence `toolbench regrade` can replay the
rubric against, so the policy must keep (at least) every file the
rubric's checks read. A deliverable that cleanup deletes will grade
correctly on the original run and then silently flip to FAIL on
regrade — the policy is correctness-critical, not a disk-space nicety.

The defaults below preserve the framework's historical behavior (tuned
for HEP benchmarks: UFO `.py` modules, `.lhe` event files, `.npy`
arrays, plots, JSONL record dumps). A benchmark whose deliverables fall
outside them MUST declare its own policy in `benchmark.yaml`:

    artifacts:
      keep_full: [".pdf", ".png", ".csv", ".json"]   # copy verbatim
      truncate:                                       # keep first N records
        - { ext: ".jsonl", max_records: 200 }
      keep_root: ["todos.md"]                         # bare names at sandbox root
      exclude_segments: ["bin/internal"]              # third-party machinery to prune

Each key is optional and *replaces* its default when given (no merging,
so a benchmark can also shrink the keep-set).
"""

from dataclasses import dataclass


# Historical defaults. FULL: small files where truncation would break a
# rubric check or the headline deliverable itself. TRUNCATE: bulk
# record-oriented files kept only up to the schema/min-record gates.
DEFAULT_KEEP_FULL = (
    ".pdf", ".png",         # headline plots / agent-side figures
    ".npy",                 # numeric result arrays
    ".py",                  # agent-written modules (e.g. UFO model dirs)
    ".lhe", ".lhe.gz",      # event files (gzipped is small)
    ".json",                # structured answers (e.g. output/answer.json)
)
DEFAULT_TRUNCATE = ((".jsonl", 200),)
DEFAULT_KEEP_ROOT = ("todos.md",)
# Path segments owned by third-party tools (not the agent and not a
# graded deliverable), e.g. MadGraph's interpreter dump under
# `<output>/bin/internal/` — ~40 .py files that would otherwise be
# swept up by a `.py` keep_full rule.
DEFAULT_EXCLUDE_SEGMENTS = ("bin/internal",)


@dataclass(frozen=True)
class ArtifactPolicy:
    """What sandbox cleanup preserves into `trials/<id>/artifacts/`."""
    keep_full: tuple[str, ...] = DEFAULT_KEEP_FULL
    truncate: tuple[tuple[str, int], ...] = DEFAULT_TRUNCATE
    keep_root: tuple[str, ...] = DEFAULT_KEEP_ROOT
    exclude_segments: tuple[str, ...] = DEFAULT_EXCLUDE_SEGMENTS

    @classmethod
    def from_block(cls, data: dict | None) -> "ArtifactPolicy":
        """Build a policy from a benchmark.yaml `artifacts:` mapping.

        Absent block / absent keys keep their defaults. Raises ValueError
        on a malformed block so a typo'd policy fails at load time, not
        silently at cleanup time.
        """
        if not data:
            return cls()
        if not isinstance(data, dict):
            raise ValueError(
                f"`artifacts:` must be a mapping, got {type(data).__name__}"
            )
        unknown = set(data) - {"keep_full", "truncate", "keep_root",
                               "exclude_segments"}
        if unknown:
            raise ValueError(
                f"`artifacts:` has unknown key(s) {sorted(unknown)}; expected "
                "keep_full / truncate / keep_root / exclude_segments"
            )
        kwargs = {}
        for key in ("keep_full", "keep_root", "exclude_segments"):
            if key in data:
                vals = data[key]
                if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
                    raise ValueError(f"artifacts.{key} must be a list of strings")
                if key == "keep_full":
                    # Normalize: extensions must start with a dot so the
                    # rglob pattern matches whole extensions, not stems.
                    vals = [v if v.startswith(".") else "." + v for v in vals]
                kwargs[key] = tuple(vals)
        if "truncate" in data:
            entries = data["truncate"]
            if not isinstance(entries, list):
                raise ValueError("artifacts.truncate must be a list of "
                                 "{ext, max_records} mappings")
            out = []
            for e in entries:
                if not (isinstance(e, dict) and "ext" in e and "max_records" in e):
                    raise ValueError(
                        f"artifacts.truncate entry must be {{ext, max_records}}, "
                        f"got {e!r}"
                    )
                ext = str(e["ext"])
                ext = ext if ext.startswith(".") else "." + ext
                out.append((ext, int(e["max_records"])))
            kwargs["truncate"] = tuple(out)
        return cls(**kwargs)


DEFAULT_POLICY = ArtifactPolicy()
