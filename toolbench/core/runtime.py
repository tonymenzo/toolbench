"""
Agent-runtime registry.

A harness names its runtime (`runtime: {name: orchestral}`); this module
maps that name to a factory that constructs the agent driving the trial.
Mirrors the provider registry in `llm_factory.py`: the framework ships
the `orchestral` runtime, and adopters register additional runtimes
(claude_code, codex, a domain harness, ...) at import time via
`register_runtime` — the CLI validates every harness's runtime name
against this registry before any trial runs, so a harness claiming an
unregistered runtime fails fast instead of silently running orchestral.

Factory contract
----------------

    factory(*, llm, tools, tool_hooks, system_prompt, display_hook,
            sandbox_dir=None, harness=None, loadout=None) -> agent

`sandbox_dir` / `harness` / `loadout` are ADDITIVE, defaulted-optional
context the runner passes through: the orchestral factory ignores them
(`**_`), while runtimes that drive an external agent process (e.g.
`claude_code`) need them to scope a config-file MCP server to the
trial's sandbox. Older factories that don't accept them keep working
because `build_agent` only forwards them when present.

The returned agent must provide:

  - `run(message: str, max_iterations: int = ..., **llm_kwargs)` —
    execute the agent loop, returning a final response object (ideally
    with `.text` and `.tool_calls` attributes; both are read defensively).
    Must be *resumable*: a subsequent `run(new_message)` continues the
    same session/context (the runner uses this for format-crash retries
    and continue-nudges). `**llm_kwargs` carries the harness's provider
    request params (e.g. `max_tokens`) through to the model call.
  - `context.messages` — iterable of messages for token/cost extraction.
    Runtimes whose context is not orchestral-shaped still work; usage
    extraction degrades to a stderr warning and a missing cost.

Runtimes whose core tools are built in (`core: {builtin: true}`) receive
`tools=[]`; `tool_hooks` / `display_hook` may be ignored by runtimes
that have their own transcript capture, but then tool-call trajectories
will be empty in the trial record unless the adapter bridges them.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable


RuntimeFactory = Callable[..., Any]

_RUNTIMES: dict[str, RuntimeFactory] = {}
# runtime name -> installed distribution that implements it, used by
# `check_runtime_version` to enforce a harness's `runtime.version` spec.
_RUNTIME_DISTS: dict[str, str] = {}


def register_runtime(name: str, factory: RuntimeFactory,
                     dist: str | None = None) -> None:
    """Register a runtime factory under `name` (case-insensitive).

    `dist` names the installed distribution implementing the runtime
    (e.g. "orchestral-ai") so a harness's `runtime.version` spec can be
    enforced against it; omit it and version specs for this runtime are
    skipped with a warning. Re-registering the same name silently
    replaces the prior factory — useful for test fakes and overrides.
    """
    _RUNTIMES[name.lower()] = factory
    if dist:
        _RUNTIME_DISTS[name.lower()] = dist


def registered_runtimes() -> list[str]:
    """Sorted list of currently-registered runtime names."""
    return sorted(_RUNTIMES)


def build_agent(runtime_name: str, *, llm, tools, tool_hooks,
                system_prompt: str, display_hook=None,
                sandbox_dir: str | None = None, harness=None,
                loadout=None, protected_paths=None) -> Any:
    """Construct the agent for `runtime_name` via its registered factory.

    `sandbox_dir` / `harness` / `loadout` are additive context for
    runtimes that drive an external agent process; every shipped factory
    accepts and (for orchestral) ignores them via `**_`, so passing them
    is always safe.

    Raises ValueError when the runtime isn't registered (the CLI also
    pre-validates, so reaching that error here means a programmatic
    caller skipped validation).
    """
    factory = _RUNTIMES.get(runtime_name.lower())
    if factory is None:
        raise ValueError(
            f"Unknown runtime: {runtime_name!r}. "
            f"Registered: {registered_runtimes()}. "
            f"Add one with `toolbench.core.runtime.register_runtime(name, factory)`."
        )
    return factory(llm=llm, tools=tools, tool_hooks=tool_hooks,
                   system_prompt=system_prompt, display_hook=display_hook,
                   sandbox_dir=sandbox_dir, harness=harness, loadout=loadout,
                   protected_paths=protected_paths)


def check_runtime_version(runtime_name: str, spec: str | None, *,
                          installed: str | None = None) -> str | None:
    """Enforce a harness's `runtime.version` spec (PEP 440, e.g. ">=1.3").

    Returns an error message when the installed runtime distribution
    does not satisfy `spec`, None when it does — or when the check can't
    be performed (no spec; runtime registered without a `dist`; the
    `packaging` library unavailable), in which case a stderr warning
    notes the skip so an unenforced pin is at least visible.

    `installed` overrides the metadata lookup (tests).
    """
    if not spec:
        return None
    name = runtime_name.lower()
    if installed is None:
        dist = _RUNTIME_DISTS.get(name)
        if dist is None:
            print(f"warning: runtime {runtime_name!r} was registered without "
                  f"a `dist`; cannot enforce runtime.version {spec!r}.",
                  file=sys.stderr)
            return None
        try:
            import importlib.metadata
            installed = importlib.metadata.version(dist)
        except Exception:
            return (f"runtime {runtime_name!r}: cannot read the installed "
                    f"version of {dist!r} to enforce runtime.version {spec!r}.")
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version
    except ImportError:
        print(f"warning: `packaging` is unavailable; runtime.version {spec!r} "
              f"for {runtime_name!r} was NOT enforced.", file=sys.stderr)
        return None
    try:
        ok = SpecifierSet(str(spec)).contains(Version(installed),
                                              prereleases=True)
    except Exception as e:
        return (f"runtime {runtime_name!r}: invalid runtime.version spec "
                f"{spec!r} ({type(e).__name__}: {e}).")
    if not ok:
        return (f"runtime {runtime_name!r}: installed version {installed} "
                f"does not satisfy the harness's runtime.version {spec!r}.")
    return None


def _orchestral_factory(*, llm, tools, tool_hooks, system_prompt,
                        display_hook=None, **_):
    # `**_` absorbs additive runner context (sandbox_dir/harness/loadout)
    # that orchestral doesn't need, keeping this factory backward-compatible.
    from orchestral import Agent
    return Agent(
        llm=llm, tools=tools, tool_hooks=tool_hooks,
        system_prompt=system_prompt, debug=False,
        display_hook=display_hook,
    )


register_runtime("orchestral", _orchestral_factory, dist="orchestral-ai")


# ── claude_code runtime ─────────────────────────────────────────────
#
# Drives a benchmark trial with the Claude Code CLI (`claude -p`) instead
# of the in-process orchestral loop. The heptapod toolkit is served to it
# over MCP (`toolbase serve`), scoped to the trial sandbox because the
# toolkit's `base_directory` schema default is `${CWD}` and we spawn
# `claude` (which in turn spawns `toolbase serve`) with cwd = sandbox.
#
# Auth is the user's logged-in subscription — we never pass an API key, so
# this incurs no per-token API cost. The CLI runs fully autonomously via
# `--permission-mode acceptEdits` plus an explicit `--allowedTools` list
# (no `--dangerously-skip-permissions`, which a classifier blocks).

# The MCP server name `tb connect claude-code` writes into .mcp.json.
_TOOLBASE_MCP_SERVER = "toolbase"


def _toolbase_command() -> str:
    """Resolve the Toolbase executable for child MCP processes.

    Invoking Toolbench with an absolute virtual-environment Python does not
    activate that environment or prepend its ``bin`` directory to ``PATH``.
    Prefer PATH when available, then the executable beside the running Python.

    Raises ``RuntimeError`` when Toolbase cannot be located, instead of
    returning a bare ``"toolbase"``. A bare command is what silently broke a
    campaign: a child MCP process launched as ``toolbase serve ...`` failed to
    start (``command not found`` on the child's PATH), the agent merely saw
    "No such tool available", and the trial still graded as if it had tools.
    Fail loudly here — the run preflight (`verify_toolbase_mcp`) turns this into
    an abort before any trial runs.
    """
    found = shutil.which("toolbase")
    if found:
        return found
    sibling = Path(sys.executable).resolve().with_name("toolbase")
    if sibling.is_file():
        return str(sibling)
    raise RuntimeError(
        "toolbase executable not found: neither on PATH nor beside the running "
        f"Python ({Path(sys.executable).resolve().with_name('toolbase')}). "
        "Install toolbase into this environment (`pip install toolbase`); a CLI "
        "runtime cannot serve the loadout's MCP tools without it."
    )
# No hardcoded toolkit: the toolbase loadout a CLI runtime serves is derived
# from the benchmark's loadout (its `toolbase: {loadout: ...}` source) via
# `_toolbase_loadout_for`. None => serve no MCP server (the `core` baseline
# runs with only the builtin tools below).
_CLAUDE_CODE_LOADOUT = None
# Builtin Claude Code tools the agent needs in addition to the MCP tools.
_CLAUDE_CODE_BUILTIN_TOOLS = [
    "Bash", "Write", "Edit", "Read", "Glob", "Grep", "TodoWrite",
]
# Generous per-call ceiling — recast trials shower events and run Delphes.
_CLAUDE_CODE_TIMEOUT_S = 3 * 60 * 60
# Default per-MCP-tool-call timeout (seconds) handed to `toolbase serve
# --call-timeout`, overridable per harness via `runtime.call_timeout_s`.
# toolbase's own default is 60s, which kills a multi-thousand-event Pythia
# shower / Delphes run mid-call; 900s (15 min) fits inside the 2h trial wall.
_CLAUDE_CODE_CALL_TIMEOUT_S = 3600


def _drain_stream(stream, chunks: list) -> None:
    """Read `stream` to EOF from a daemon thread, collecting text into
    `chunks`.

    The CLI runtimes spawn their child with both stdout and stderr on
    PIPE but stream only stdout. Without a concurrent stderr reader, a
    child that logs more than the OS pipe buffer (~64 KB) to stderr
    blocks on the write, the stdout stream stalls, and the trial hangs
    until the wall-clock killer fires — occupying a --parallel worker
    slot the whole time.
    """
    try:
        for line in stream:
            chunks.append(line)
    except Exception:
        pass


def _toolbase_loadout_for(loadout) -> tuple[str | None, str | None]:
    """The toolbase ``(loadout, project_root)`` a CLI runtime should serve over
    MCP, taken from the benchmark's loadout — its first ``toolbase: {loadout,
    project_root}`` source. Returns ``(None, None)`` when the loadout has no
    toolbase source (e.g. the ``core`` baseline), in which case the CLI runs
    with only its builtin tools. This is what keeps the CLI runtimes generic:
    the served toolkit follows the benchmark loadout, never a hardcoded name."""
    for src in (getattr(loadout, "sources", None) or []):
        if getattr(src, "backend", None) == "toolbase":
            cfg = src.config if isinstance(src.config, dict) else {}
            return cfg.get("loadout"), cfg.get("project_root")
    return None, None


# Runtimes that serve the benchmark loadout's toolbase loadout to their agent
# over a stdio MCP server (`toolbase serve --loadout ...`) rather than resolving tools
# in-process. For these the MCP connection is verified in the run preflight: a
# toolbase loadout that resolves but serves zero tools (a mis-wired toolbase command,
# env churn) otherwise runs the whole "tools" arm silently tool-less. A new CLI
# runtime that serves toolbase over MCP registers its name here.
_MCP_SERVING_RUNTIMES = {"claude_code", "codex"}


def runtime_serves_toolbase_mcp(runtime_name: str) -> bool:
    """True when a harness's runtime drives its agent's tools through a
    `toolbase serve` MCP child (claude-code, codex), so the run preflight must
    verify the server actually serves its tools. In-process runtimes
    (orchestral) resolve tools directly and need no MCP check."""
    return (runtime_name or "").lower() in _MCP_SERVING_RUNTIMES


def verify_toolbase_mcp(loadout: str, *, cwd: str, call_timeout_s: int = 60,
                        timeout: float = 45.0) -> list[str]:
    """Start `toolbase serve --loadout <loadout>` exactly as a CLI runtime does
    and complete an MCP initialize + tools/list handshake, returning the served
    tool names.

    Raises ``RuntimeError`` if the server cannot launch, the handshake times
    out, or it serves zero tools. This is the preflight guard that a `tools`
    loadout actually reaches its tools *before* any trial runs (a served-but-
    empty toolset previously graded as a valid tools arm)."""
    import queue
    import threading
    import time as _time

    cmd = [_toolbase_command(), "serve", "--loadout", loadout,
           "--call-timeout", str(call_timeout_s)]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd=cwd, bufsize=1)
    except Exception as e:
        raise RuntimeError(f"could not launch `{' '.join(cmd)}`: {e}") from e

    err_chunks: list[str] = []
    threading.Thread(target=_drain_stream, args=(proc.stderr, err_chunks),
                     daemon=True).start()
    out_q: "queue.Queue" = queue.Queue()

    def _read_out():
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                out_q.put(line)
        except Exception:
            pass
        out_q.put(None)

    threading.Thread(target=_read_out, daemon=True).start()

    def _send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    def _fail(msg):
        tail = "".join(err_chunks)[-400:].strip()
        raise RuntimeError(f"{msg} (server exit={proc.poll()}"
                           + (f"; stderr: …{tail}" if tail else "") + ")")
    try:
        _send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                          "clientInfo": {"name": "toolbench-preflight",
                                         "version": "0"}}})
        _send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            try:
                line = out_q.get(timeout=1.0)
            except queue.Empty:
                if proc.poll() is not None:
                    _fail(f"MCP server for toolbase loadout {loadout!r} exited before "
                          "returning tools/list")
                continue
            if line is None:
                _fail(f"MCP server for toolbase loadout {loadout!r} closed its output "
                      "before returning tools/list")
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == 2:
                tools = [t.get("name")
                         for t in (msg.get("result") or {}).get("tools", [])]
                if not tools:
                    _fail(f"MCP server served 0 tools for toolbase loadout {loadout!r}")
                return tools
        _fail(f"MCP handshake for toolbase loadout {loadout!r} timed out after "
              f"{timeout:.0f}s")
    finally:
        try:
            proc.terminate(); proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


class _ClaudeCodeResponse:
    """Minimal response object matching the runner's defensive reads:
    `.text`, `.tool_calls`, and a `.context.messages` iterable."""

    class _Context:
        def __init__(self, messages):
            self.messages = messages

    def __init__(self, text: str, *, messages=None):
        self.text = text or ""
        self.tool_calls: list = []
        # Empty by default — usage extraction degrades gracefully per the
        # factory contract (it only counts orchestral Response objects).
        self.context = self._Context(messages or [])


class ClaudeCodeAgent:
    """Agent adapter that shells out to `claude -p`.

    Resumable: the first `run()` starts a session and remembers its
    `session_id`; subsequent `run()`s pass `--resume <id>` so the runner's
    format-crash / nudge resume loop continues the same Claude session in
    the same sandbox.
    """

    def __init__(self, *, system_prompt: str, sandbox_dir: str,
                 model: str | None = None,
                 loadout: str | None = _CLAUDE_CODE_LOADOUT,
                 project_root: str | None = None,
                 call_timeout_s: int = _CLAUDE_CODE_CALL_TIMEOUT_S,
                 traj_hook=None, env=None, cli_opts=None, protected_paths=None):
        self.system_prompt = system_prompt or ""
        self.sandbox_dir = Path(sandbox_dir).resolve()
        # Absolute paths the Bash sandbox must deny-read (e.g. the benchmark's
        # ground-truth tree). Only used when cli_opts["sandbox"] is on.
        self.protected_paths = [str(Path(os.path.expanduser(
            os.path.expandvars(p))).resolve()) for p in (protected_paths or [])]
        self.model = model or "claude-haiku-4-5"
        self.loadout = loadout
        self.project_root = project_root
        self.call_timeout_s = int(call_timeout_s)
        self.harness_env = dict(env or {})
        # Curated pass-through of harness `runtime.*` keys to optional `claude`
        # CLI flags (effort, fallback_model, max_budget_usd, add_dir,
        # disallowed_tools). Correctness-critical flags (output-format,
        # permission-mode, allowedTools, mcp-config) stay hardcoded in run().
        self.cli_opts = dict(cli_opts or {})
        # The runner's TrajectoryHook: firing before_call/after_call on it per
        # streamed tool call records the call onto the trajectory (-> transcript
        # + artifact dump) AND emits the orchestral-format line to console.log,
        # live. None in non-runner contexts (degrades to no streaming).
        self.traj_hook = traj_hook
        self.session_id: str | None = None
        self._mcp_config_path: Path | None = None
        # Token usage accumulated from `claude`'s stream-json result events
        # across all turns of this session (task loop + any nudges/UX turn).
        # `initial_input` is the first request's total input; the rest are
        # cumulative. Read by the runner's `_extract_usage`.
        self.token_usage = {"initial_input": 0, "input": 0, "output": 0,
                            "cache_read": 0, "cache_creation": 0,
                            "cost": None, "model": self.model}
        self._usage_seen = False

    # -- MCP wiring -----------------------------------------------------
    def _ensure_mcp_config(self) -> Path | None:
        """Write `<sandbox>/.mcp.json` wiring the benchmark loadout's toolbase loadout
        as a stdio MCP server, or return None when the loadout serves no
        toolbase loadout (the agent then runs with only its builtin tools).
        `toolbase serve` resolves its toolkits' `base_directory` (default
        `${CWD}`) from the cwd we launch `claude` in — the sandbox — so the
        served tools operate inside the sandbox."""
        if not self.loadout:
            return None
        if self._mcp_config_path is not None:
            return self._mcp_config_path
        # --call-timeout raises toolbase's per-tool wall above its 60s default
        # (the wall that fired "PythiaFromRunCard failed after 60.0s"): a real
        # Pythia shower / Delphes run of a few thousand events legitimately
        # takes minutes. The value comes from the harness yaml
        # (`runtime.call_timeout_s`); see _CLAUDE_CODE_CALL_TIMEOUT_S.
        # `toolbase serve` has no project-root flag; it resolves config by
        # walking up from its cwd (the sandbox) to the benchmark's .toolbase.
        args = ["serve", "--loadout", self.loadout,
                "--call-timeout", str(self.call_timeout_s)]
        path = self.sandbox_dir / ".mcp.json"
        config = {
            "mcpServers": {
                _TOOLBASE_MCP_SERVER: {
                    "type": "stdio",
                    "command": _toolbase_command(),
                    "args": args,
                }
            }
        }
        path.write_text(json.dumps(config, indent=2))
        self._mcp_config_path = path
        return path

    # -- run ------------------------------------------------------------
    def run(self, message: str, max_iterations: int | None = None, **llm_kwargs):
        claude = shutil.which("claude")
        if claude is None:
            raise RuntimeError(
                "claude_code runtime: the `claude` CLI is not on PATH. "
                "Install Claude Code and log in (subscription auth)."
            )
        mcp_config = self._ensure_mcp_config()
        # When the loadout serves a toolbase loadout, allow its MCP tools;
        # otherwise (the `core` baseline) the agent gets only its builtin tools.
        allowed_tools = list(_CLAUDE_CODE_BUILTIN_TOOLS)
        if mcp_config is not None:
            allowed_tools.insert(0, f"mcp__{_TOOLBASE_MCP_SERVER}__*")
        allowed = ",".join(allowed_tools)
        cmd = [
            claude, "-p", message,
            "--model", self.model,
            "--append-system-prompt", self.system_prompt,
        ]
        if mcp_config is not None:
            cmd += ["--mcp-config", str(mcp_config), "--strict-mcp-config"]
        cmd += [
            "--allowedTools", allowed,
            "--permission-mode", "acceptEdits",
            # HERMETIC SETTINGS — correctness-critical, hence hardcoded here
            # rather than exposed as a harness knob. Without it the CLI loads
            # its DEFAULT setting sources, user level included, so every guide
            # in ~/.claude/skills/ is surfaced to the model in EVERY arm of
            # every run. Anything a past `tb connect` left behind, or that the
            # operator hand-wrote, silently joins the measured configuration:
            # a core_only arm receives domain guidance it is defined not to
            # have, and nothing in the manifest records it. `project` keeps the
            # CLI's built-in skills and whatever the runner materialises INTO
            # the sandbox — the sandbox is the trial's cwd, so it is project
            # scope — while dropping the machine's ambient state.
            "--setting-sources", "project",
            # stream-json (NDJSON) lets us record each tool call onto the
            # trajectory AS IT HAPPENS, so the trial's console.log + transcript
            # show the live tool-call timeline (same format as orchestral) and
            # are spot-checkable for fabrication. Requires --verbose in -p mode.
            "--output-format", "stream-json", "--verbose",
        ]
        # Curated, harness-configurable flags (from runtime.*). Optional; each
        # is appended only when set. The correctness-critical flags above are
        # never overridable here.
        opts = self.cli_opts
        if opts.get("disallowed_tools"):
            cmd += ["--disallowedTools",
                    ",".join(str(t) for t in opts["disallowed_tools"])]
        if opts.get("effort"):
            cmd += ["--effort", str(opts["effort"])]
        if opts.get("fallback_model"):
            cmd += ["--fallback-model", str(opts["fallback_model"])]
        if opts.get("max_budget_usd") is not None:
            cmd += ["--max-budget-usd", str(opts["max_budget_usd"])]
        if opts.get("add_dir"):
            cmd += ["--add-dir", *[str(d) for d in opts["add_dir"]]]
        # Bash filesystem sandbox (macOS Seatbelt). Confines the Bash tool's
        # WRITES to the sandbox and DENIES READS of the benchmark's ground truth
        # (self.protected_paths) plus any harness-declared extra paths. Applies
        # only to Bash + children; MCP tools run outside it and are unaffected.
        if opts.get("sandbox"):
            deny = list(dict.fromkeys(
                self.protected_paths
                + [str(Path(os.path.expanduser(os.path.expandvars(p))).resolve())
                   for p in (opts.get("sandbox_deny") or [])]))
            settings = {
                "sandbox": {
                    "enabled": True,
                    "filesystem": {
                        # The sandbox + the system temp dirs (so OpenMP/numpy
                        # temp files that default to /tmp don't fail). None of
                        # these hold anything integrity-sensitive; the answer
                        # key is blocked via denyRead below.
                        "allowWrite": [str(self.sandbox_dir), "/tmp",
                                       "/private/tmp"],
                        "denyRead": deny,
                    },
                    "autoAllowBashIfSandboxed": True,
                    "failIfUnavailable": True,
                    "allowUnsandboxedCommands": False,
                }
            }
            cmd += ["--settings", json.dumps(settings)]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        elif self.traj_hook is not None:
            # First turn of the session: record what we appended via
            # --append-system-prompt, so every run's log proves the harness's
            # system prompt actually reached the agent (length + first line).
            sp = self.system_prompt or ""
            first = sp.splitlines()[0] if sp else "(empty)"
            self.traj_hook.write_to_log(
                f"\n--- append-system-prompt: {len(sp)} chars | "
                f"first line: {first[:100]} ---")

        # Subscription auth via the logged-in CLI: never inject an API key.
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        # Harness-declared env (runtime.env), e.g. ENABLE_TOOL_SEARCH=false to
        # load all MCP tools eagerly rather than behind tool-search deferral.
        _apply_harness_env(env, self.harness_env)
        # Client-side MCP timeouts as a backstop; the SERVER-side wall that
        # actually fires is `toolbase serve --call-timeout` (set in the
        # .mcp.json above). Keep them consistent so neither side cuts a long
        # but legitimate Pythia/Delphes call short.
        env.setdefault("MCP_TOOL_TIMEOUT", str(self.call_timeout_s * 1000))
        env.setdefault("MCP_TIMEOUT", str(60 * 1000))
        # Under the Bash sandbox, writes are confined to the sandbox; point
        # tools that cache to $HOME (matplotlib fontlist, etc.) at a writable
        # spot inside it so plotting doesn't fail on a denied cache write.
        if opts.get("sandbox"):
            # matplotlib caches its fontlist to $HOME (write-denied under the
            # sandbox); point it at the sandbox so plotting works. NOTE: this
            # env is inherited by the MCP toolbase child too, but MPLCONFIGDIR
            # is inert for the tools. We deliberately do NOT redirect TMPDIR
            # here: that would change where the heavy MCP tools (MG5/Delphes)
            # write temp and thus influence the actual run. Bash's own temp
            # needs are covered by allowing the system temp dirs to WRITE (see
            # the sandbox settings), which leaves the tools' env untouched.
            env.setdefault("MPLCONFIGDIR", str(self.sandbox_dir / ".mplconfig"))
            # fontconfig / other libs cache into $HOME/.cache (write-denied under
            # the sandbox), which surfaced as "Fontconfig error: no writable
            # cache directories" during plotting. Redirect the XDG cache home
            # into the sandbox so those caches have somewhere to go. Like
            # MPLCONFIGDIR this is a cache redirect and inert for the MCP tools.
            _cache = self.sandbox_dir / ".cache"
            try:
                _cache.mkdir(exist_ok=True)
            except Exception:
                pass
            env.setdefault("XDG_CACHE_HOME", str(_cache))

        proc = subprocess.Popen(
            cmd, cwd=str(self.sandbox_dir), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        # Hard wall even if the stream goes silent (the overall trial ceiling).
        killer = threading.Timer(_CLAUDE_CODE_TIMEOUT_S, proc.kill)
        killer.start()
        # Drain stderr concurrently so a chatty child can't fill the pipe
        # and deadlock the stdout stream (see _drain_stream).
        stderr_chunks: list[str] = []
        stderr_reader = threading.Thread(
            target=_drain_stream, args=(proc.stderr, stderr_chunks),
            daemon=True)
        stderr_reader.start()

        hook = self.traj_hook
        id2name: dict = {}
        result_data = None
        try:
            for raw in proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type")
                if etype == "assistant":
                    for b in (ev.get("message") or {}).get("content") or []:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            short = (b.get("name") or "tool").split("__")[-1]
                            id2name[b.get("id")] = short
                            if hook is not None:
                                try:
                                    hook.before_call(short, b.get("input") or {})
                                except Exception:
                                    pass
                elif etype == "user":
                    for b in (ev.get("message") or {}).get("content") or []:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            short = id2name.get(b.get("tool_use_id"), "tool")
                            c = b.get("content", "")
                            if isinstance(c, list):
                                c = " ".join(str(x.get("text", "")) for x in c
                                             if isinstance(x, dict))
                            res = str(c)
                            if b.get("is_error"):
                                # Orchestral error-string convention so the
                                # console renders the `|--` error continuation
                                # and the call is counted as a tool error.
                                res = "Error: Execution Error\n- Reason: " + res
                            if hook is not None:
                                try:
                                    hook.after_call(short, res)
                                except Exception:
                                    pass
                elif etype == "result":
                    result_data = ev
                    if ev.get("session_id"):
                        self.session_id = ev["session_id"]
                elif etype == "system" and ev.get("session_id"):
                    self.session_id = ev["session_id"]
            proc.wait(timeout=60)
        finally:
            killer.cancel()
            stderr_reader.join(timeout=10)
            stderr = "".join(stderr_chunks)

        if result_data is None:
            raise RuntimeError(
                "claude_code runtime: stream ended with no result message "
                f"(exit {proc.returncode}). stderr:\n{stderr[:2000]}"
            )
        if result_data.get("is_error"):
            subtype = result_data.get("subtype") or "error"
            raise RuntimeError(
                f"claude_code runtime: claude reported is_error ({subtype}): "
                f"{result_data.get('result') or result_data.get('api_error_status')}"
            )
        self._accumulate_usage(result_data)
        return _ClaudeCodeResponse(result_data.get("result") or "")

    def _accumulate_usage(self, result_data: dict) -> None:
        """Fold this turn's token usage from `claude`'s result event into
        `self.token_usage`. The `usage.iterations` list carries per-message
        counts (fresh `input_tokens` plus cached reads/writes); summing them
        gives the tokens actually processed. The first iteration's total input
        is recorded as `initial_input` (the starting context size)."""
        u = (result_data or {}).get("usage") or {}
        its = u.get("iterations") or ([u] if u else [])
        for it in its:
            i_in = int(it.get("input_tokens", 0) or 0)
            i_cr = int(it.get("cache_read_input_tokens", 0) or 0)
            i_cc = int(it.get("cache_creation_input_tokens", 0) or 0)
            self.token_usage["input"] += i_in
            self.token_usage["cache_read"] += i_cr
            self.token_usage["cache_creation"] += i_cc
            self.token_usage["output"] += int(it.get("output_tokens", 0) or 0)
            if not self._usage_seen:
                self.token_usage["initial_input"] = i_in + i_cr + i_cc
                self._usage_seen = True
        cost = result_data.get("total_cost_usd")
        if cost is not None:
            self.token_usage["cost"] = (self.token_usage["cost"] or 0.0) + float(cost)


def _cli_runtime_common(harness, loadout, tool_hooks):
    """Shared factory plumbing for the CLI-driven runtimes (claude_code,
    codex): the model + per-call timeout from the harness, the served toolbase
    loadout from the benchmark loadout, and the runner's TrajectoryHook (records tool
    calls onto the trajectory + emits the styled console line; other hooks like
    TruncateOutputHook are ignored — they only matter for an in-process model).
    Also reads `runtime.env` (a mapping of environment variables to set on the
    CLI subprocess, e.g. `ENABLE_TOOL_SEARCH: "false"` to load all MCP tools
    eagerly instead of behind Claude Code's tool-search deferral).
    Returns (model, call_timeout_s, tb_loadout, project_root, traj_hook, env)."""
    model = None
    call_timeout_s = _CLAUDE_CODE_CALL_TIMEOUT_S
    env_overrides: dict = {}
    if harness is not None:
        provider = getattr(harness, "provider", None) or {}
        model = provider.get("model")
        runtime_cfg = getattr(harness, "runtime", None) or {}
        if runtime_cfg.get("call_timeout_s") is not None:
            call_timeout_s = int(runtime_cfg["call_timeout_s"])
        env_overrides = dict(runtime_cfg.get("env") or {})
    # `loadout` is this trial's benchmark condition; the name it yields is
    # the toolbase loadout to serve. Distinct local only because the
    # parameter already holds the other one.
    tb_loadout, project_root = _toolbase_loadout_for(loadout)
    traj_hook = None
    for h in (tool_hooks or []):
        if (hasattr(h, "before_call") and hasattr(h, "after_call")
                and hasattr(h, "trajectory")):
            traj_hook = h
            break
    return model, call_timeout_s, tb_loadout, project_root, traj_hook, env_overrides


def _apply_harness_env(env: dict, overrides) -> None:
    """Merge harness-declared `runtime.env` vars into the subprocess env,
    authoritatively (the committed harness config defines the run environment,
    for reproducibility). YAML booleans are lowercased so that
    `ENABLE_TOOL_SEARCH: false` becomes the string 'false', not 'False'."""
    for k, v in (overrides or {}).items():
        env[str(k)] = "true" if v is True else "false" if v is False else str(v)


def _claude_code_factory(*, system_prompt, sandbox_dir=None, harness=None,
                         loadout=None, tool_hooks=None, llm=None,
                         protected_paths=None, **_):
    # `**_` absorbs the orchestral-shaped kwargs (tools, display_hook) this
    # runtime doesn't use. We DO use tool_hooks (the runner's TrajectoryHook,
    # for console.log + transcript parity with orchestral) and llm (it carries
    # the run matrix's --models value; see below).
    if not sandbox_dir:
        raise ValueError(
            "claude_code runtime requires sandbox_dir (the runner passes it)."
        )
    model, call_timeout_s, tb_loadout, project_root, traj_hook, env_overrides = \
        _cli_runtime_common(harness, loadout, tool_hooks)
    # The run matrix's --models value is carried by SubscriptionLLM and MUST
    # override the harness's provider.model default; otherwise every cell runs
    # the harness default model regardless of its --models label (mirrors the
    # codex factory).
    requested_model = getattr(llm, "model", None)
    if requested_model:
        model = requested_model
    return ClaudeCodeAgent(
        system_prompt=system_prompt, sandbox_dir=sandbox_dir, model=model,
        loadout=tb_loadout, project_root=project_root,
        call_timeout_s=call_timeout_s, traj_hook=traj_hook, env=env_overrides,
        cli_opts=_claude_code_cli_opts(harness), protected_paths=protected_paths,
    )


def _claude_code_cli_opts(harness) -> dict:
    """Curated map of harness `runtime.*` keys to optional `claude` CLI flags.
    Only these keys are honored (unknown runtime keys are silently ignored, as
    ever); correctness-critical flags stay hardcoded in ClaudeCodeAgent.run().
      runtime.disallowed_tools (list) -> --disallowedTools
      runtime.effort (str)            -> --effort  (low|medium|high|xhigh|max)
      runtime.fallback_model (str)    -> --fallback-model
      runtime.max_budget_usd (number) -> --max-budget-usd
      runtime.add_dir (list)          -> --add-dir
    """
    rc = (getattr(harness, "runtime", None) or {}) if harness is not None else {}
    opts: dict = {}
    if rc.get("disallowed_tools"):
        opts["disallowed_tools"] = list(rc["disallowed_tools"])
    for key in ("effort", "fallback_model"):
        if rc.get(key):
            opts[key] = rc[key]
    if rc.get("max_budget_usd") is not None:
        opts["max_budget_usd"] = rc["max_budget_usd"]
    if rc.get("add_dir"):
        opts["add_dir"] = list(rc["add_dir"])
    # Bash filesystem sandbox (macOS Seatbelt): confine the Bash tool's WRITES
    # to the sandbox and deny READS of the benchmark's ground truth + any extra
    # declared paths. Applies only to Bash + its children; MCP tools are
    # unaffected. Off unless the harness opts in.
    if rc.get("sandbox"):
        opts["sandbox"] = True
        opts["sandbox_deny"] = [str(p) for p in (rc.get("sandbox_deny") or [])]
    return opts


register_runtime("claude_code", _claude_code_factory)


# ── codex runtime ───────────────────────────────────────────────────
#
# Drives a benchmark trial with the OpenAI Codex CLI (`codex exec --json`)
# under the user's logged-in ChatGPT subscription (never an API key, so no
# per-token API cost). Mirrors the claude_code runtime: the loadout's
# toolbase loadout is served over MCP (wired via `-c mcp_servers.*` TOML
# overrides), scoped to the trial sandbox because we launch `codex` with
# cwd = sandbox (so the toolbase MCP server it spawns inherits it). Tool
# calls are streamed as JSONL events and bridged onto the runner's
# trajectory, so the transcript matches the orchestral/claude_code format.
#
# `codex exec` only restricts the AGENT's own shell commands via its
# `-s/--sandbox` policy; the MCP tools run in the (separate, unsandboxed)
# toolbase server, so heavy MadGraph/Pythia/Delphes calls are unaffected.
# Default `workspace-write` lets the agent inspect/write inside the sandbox
# without the EXTREMELY-DANGEROUS bypass flag; override via runtime.sandbox.
_CODEX_DEFAULT_SANDBOX = "workspace-write"


class CodexAgent:
    """Agent adapter that shells out to `codex exec --json`.

    Resumable: the first `run()` starts a thread and remembers its
    `thread_id`; subsequent `run()`s use `codex exec resume <id>` so the
    runner's format-crash / nudge loop continues the same Codex session in
    the same sandbox."""

    def __init__(self, *, system_prompt: str, sandbox_dir: str,
                 model: str | None = None, loadout: str | None = None,
                 project_root: str | None = None,
                 call_timeout_s: int = _CLAUDE_CODE_CALL_TIMEOUT_S,
                 sandbox_mode: str = _CODEX_DEFAULT_SANDBOX, traj_hook=None,
                 env=None, reasoning_effort: str | None = None,
                 protected_paths=None):
        self.system_prompt = system_prompt or ""
        self.sandbox_dir = Path(sandbox_dir).resolve()
        self.model = model            # None => codex uses its configured default
        self.loadout = loadout
        self.project_root = project_root
        self.call_timeout_s = int(call_timeout_s)
        self.sandbox_mode = sandbox_mode or _CODEX_DEFAULT_SANDBOX
        self.harness_env = dict(env or {})
        self.reasoning_effort = reasoning_effort
        self.protected_paths = [str(Path(p).resolve())
                                for p in (protected_paths or []) if p]
        self.traj_hook = traj_hook
        self.thread_id: str | None = None
        self.token_usage = {"initial_input": 0, "input": 0, "output": 0,
                            "cache_read": 0, "cache_creation": 0,
                            "cost": None, "model": self.model}
        self._usage_seen = False

    # -- MCP wiring -----------------------------------------------------
    def _mcp_config_args(self) -> list[str]:
        """`-c mcp_servers.toolbase.*` overrides serving the loadout's toolbase
        toolbase loadout, or [] when the benchmark loadout serves none (the agent runs
        with only Codex's builtin shell). Values are TOML (JSON is valid TOML
        for strings/arrays). `toolbase serve` resolves config from its cwd
        (the sandbox), which the codex process — and thus its MCP child —
        runs in."""
        if not self.loadout:
            return []
        serve_args = ["serve", "--loadout", self.loadout,
                      "--call-timeout", str(self.call_timeout_s)]
        return [
            "-c", f"mcp_servers.{_TOOLBASE_MCP_SERVER}.command="
                  + json.dumps(_toolbase_command()),
            "-c", f"mcp_servers.{_TOOLBASE_MCP_SERVER}.args="
                  + json.dumps(serve_args),
            # `codex exec` is noninteractive. Without a server-level default,
            # discovered MCP calls end as "user cancelled MCP tool call" even
            # though the tool was registered correctly.
            "-c", f"mcp_servers.{_TOOLBASE_MCP_SERVER}."
                  "default_tools_approval_mode=" + json.dumps("approve"),
        ]

    def _build_command(self, codex: str, prompt: str) -> list[str]:
        """Build a first-turn or resume command using only supported flags.

        User config is deliberately ignored: benchmark conditions must not
        inherit personal MCP servers, hooks, model defaults, or reasoning
        settings. Authentication still comes from CODEX_HOME.
        """
        if self.thread_id:
            cmd = [codex, "exec", "resume", self.thread_id,
                   "--json", "--skip-git-repo-check", "--ignore-user-config"]
        else:
            cmd = [codex, "exec", "--json", "--skip-git-repo-check",
                   "--ignore-user-config", "-C", str(self.sandbox_dir)]
            if not self.protected_paths:
                cmd += ["-s", self.sandbox_mode]
        cmd += self._protected_path_config_args()
        if self.model:
            cmd += ["-m", self.model]
        if self.reasoning_effort:
            cmd += ["-c", "model_reasoning_effort="
                    + json.dumps(self.reasoning_effort)]
        cmd += self._mcp_config_args()
        cmd += [prompt]
        return cmd

    def _isolated_codex_home(self) -> str:
        """A per-trial CODEX_HOME holding only what auth needs.

        HERMETIC INSTRUCTIONS. Codex auto-injects `$CODEX_HOME/AGENTS.md` into
        every session as model-facing instructions, and `--ignore-user-config`
        does NOT suppress it -- verified against codex-cli 0.146.0, as were
        `project_doc_max_bytes=0` and `experimental_instructions_file=""`,
        which also leave it in place. So anything in the operator's
        ~/.codex/AGENTS.md would join the measured configuration of every arm
        of every run, unrecorded: exactly the hazard ~/.claude/skills posed for
        the claude_code runtime. Pointing CODEX_HOME at a per-trial directory
        is the only thing found to block it (same probe, answer flips YES->NO),
        and it drops personal MCP servers and model defaults with it.

        auth.json is SYMLINKED rather than copied so a token refresh during a
        long campaign writes through to the real file instead of expiring
        inside a throwaway directory.
        """
        home = Path(self.sandbox_dir).parent / ".codex_home"
        home.mkdir(parents=True, exist_ok=True)
        real = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        link = home / "auth.json"
        src = real / "auth.json"
        if src.exists() and not link.exists():
            try:
                link.symlink_to(src)
            except OSError:
                shutil.copy2(src, link)     # symlinks unavailable: copy
        return str(home)

    def _protected_path_config_args(self) -> list[str]:
        """Build a native Codex permission profile with unreadable paths.

        Codex 0.145+ can express path-level read denial in the same filesystem
        policy that implements workspace/read-only confinement. This avoids
        nesting macOS Seatbelt around the Codex driver (which breaks both
        ``sandbox_apply`` and TLS trust services) and lets Codex select its
        platform backend on macOS, Linux, and Windows.
        """
        if not self.protected_paths:
            return []
        profile = "toolbench_benchmark"
        args = ["-c", "default_permissions=" + json.dumps(profile)]
        builtins = {
            "workspace-write": ":workspace",
            "read-only": ":read-only",
        }
        parent = builtins.get(self.sandbox_mode)
        if parent:
            args += ["-c", f"permissions.{profile}.extends="
                     + json.dumps(parent)]
            filesystem_entries = []
        else:
            # A custom root write grant retains danger-full-access filesystem
            # semantics while the explicit benchmark paths remain unreadable.
            args += [
                "-c", f"permissions.{profile}.network.enabled=true",
            ]
            filesystem_entries = [(str(Path("/")), "write")]
        filesystem_entries.extend((path, "deny")
                                  for path in self.protected_paths)
        # Dynamic absolute paths cannot safely be placed in a dotted `-c` key:
        # Codex's override parser preserves the quote marks as part of the path.
        # An inline TOML table keeps each path as a proper string key.
        table = "{" + ",".join(
            f"{json.dumps(path)}={json.dumps(access)}"
            for path, access in filesystem_entries) + "}"
        args += ["-c", f"permissions.{profile}.filesystem={table}"]
        return args

    def _protect_ground_truth_reads(self, cmd: list[str]) -> list[str]:
        """Compatibility shim: protection is now part of ``cmd`` itself."""
        return cmd

    def _accumulate_usage(self, usage: dict) -> None:
        """Fold one Codex ``turn.completed.usage`` payload into totals."""
        if not usage:
            return
        total_in = int(usage.get("input_tokens", 0) or 0)
        cached = int(usage.get("cached_input_tokens",
                               usage.get("cache_read_input_tokens", 0)) or 0)
        cache_write = int(usage.get("cache_write_input_tokens",
                                    usage.get("cache_creation_input_tokens", 0)) or 0)
        fresh = max(0, total_in - cached)
        output = int(usage.get("output_tokens", 0) or 0)
        self.token_usage["input"] += fresh
        self.token_usage["cache_read"] += cached
        self.token_usage["cache_creation"] += cache_write
        self.token_usage["output"] += output
        if not self._usage_seen:
            self.token_usage["initial_input"] = total_in
            self._usage_seen = True

    # -- event parsing --------------------------------------------------
    @staticmethod
    def _tool_view(item: dict):
        """Map a Codex `item` to (name, input_dict, result_str, is_error) for
        the trajectory, or None for non-tool items (agent_message, reasoning,
        todo_list, …). Handles command_execution and mcp_tool_call; unknown
        tool-shaped items degrade gracefully via best-effort field reads."""
        itype = item.get("type")
        if itype in (None, "agent_message", "reasoning", "todo_list"):
            return None
        if itype == "command_execution":
            cmd = item.get("command") or ""
            name = "bash"
            exit_code = item.get("exit_code")
            is_err = exit_code not in (0, None)
            return name, {"command": cmd}, str(item.get("aggregated_output") or ""), is_err
        if itype == "mcp_tool_call":
            # Keep `<toolkit>__<tool>` intact in the authoritative transcript;
            # reports may additionally normalize it for cross-backend plots.
            name = (item.get("tool") or item.get("name")
                    or item.get("server") or "mcp_tool")
            args = item.get("arguments") or item.get("input") or {}
            out = (item.get("result") or item.get("output")
                   or item.get("aggregated_output") or "")
            out_text = str(out)
            # Some MCP servers return application errors as successful
            # transport payloads (e.g. content text "Input validation error")
            # while Codex leaves `error` null. Preserve that distinction in the
            # trajectory instead of reporting a healthy domain-tool call.
            lowered = out_text.lower()
            payload_error = any(marker in lowered for marker in (
                "input validation error", "execution error",
                "'iserror': true", '"iserror": true',
            ))
            return (name, args, out_text,
                    bool(item.get("is_error") or item.get("error")
                         or payload_error))
        if itype == "file_change":
            out = (item.get("output") or item.get("result")
                   or item.get("error") or "")
            failed = (item.get("status") in
                      {"failed", "error", "cancelled"}
                      or bool(item.get("is_error") or item.get("error")))
            changes = item.get("changes") or item.get("input") or []
            # Trajectory consumers expect a mapping (script extraction calls
            # ``args.get``). Codex represents file changes as a list.
            return ("file_change", {"changes": changes}, str(out), failed)
        # Unknown tool-shaped item: record it rather than drop it silently.
        name = (item.get("tool") or item.get("name") or itype)
        return str(name), (item.get("input") or {}), str(item.get("output") or ""), False

    # -- run ------------------------------------------------------------
    def run(self, message: str, max_iterations: int | None = None, **llm_kwargs):
        codex = shutil.which("codex")
        if codex is None:
            raise RuntimeError(
                "codex runtime: the `codex` CLI is not on PATH. "
                "Install Codex and log in (`codex login`, ChatGPT subscription)."
            )
        # No --append-system-prompt in codex exec: fold the system prompt into
        # the first prompt. On resume the thread already carries it.
        if self.thread_id:
            prompt = message
        else:
            prompt = (f"{self.system_prompt}\n\n---\n\n{message}"
                      if self.system_prompt else message)
        cmd = self._protect_ground_truth_reads(
            self._build_command(codex, prompt))

        # Subscription auth via the logged-in CLI: never inject an API key.
        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)
        env["CODEX_HOME"] = self._isolated_codex_home()
        _apply_harness_env(env, self.harness_env)

        proc = subprocess.Popen(
            cmd, cwd=str(self.sandbox_dir), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        killer = threading.Timer(_CLAUDE_CODE_TIMEOUT_S, proc.kill)
        killer.start()
        # Drain stderr concurrently so a chatty child can't fill the pipe
        # and deadlock the stdout stream (see _drain_stream).
        stderr_chunks: list[str] = []
        stderr_reader = threading.Thread(
            target=_drain_stream, args=(proc.stderr, stderr_chunks),
            daemon=True)
        stderr_reader.start()

        hook = self.traj_hook
        started: dict = {}            # item id -> tool name (for after_call)
        result_text = None
        turn_done = False
        try:
            for raw in proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type")
                if etype == "thread.started" and ev.get("thread_id"):
                    self.thread_id = ev["thread_id"]
                elif etype == "item.started":
                    view = self._tool_view(ev.get("item") or {})
                    if view and hook is not None:
                        name, inp, _res, _err = view
                        started[(ev.get("item") or {}).get("id")] = name
                        try:
                            hook.before_call(name, inp)
                        except Exception:
                            pass
                elif etype == "item.completed":
                    item = ev.get("item") or {}
                    if item.get("type") == "agent_message":
                        result_text = item.get("text") or result_text
                        continue
                    view = self._tool_view(item)
                    if view and hook is not None:
                        name, inp, res, is_err = view
                        name = started.get(item.get("id"), name)
                        if is_err:
                            res = "Error: Execution Error\n- Reason: " + res
                        # If we never saw item.started (some items only complete),
                        # emit before_call now so the pair is balanced.
                        if item.get("id") not in started:
                            try:
                                hook.before_call(name, inp)
                            except Exception:
                                pass
                        try:
                            hook.after_call(name, res)
                        except Exception:
                            pass
                elif etype == "turn.completed":
                    turn_done = True
                    self._accumulate_usage(ev.get("usage") or {})
            proc.wait(timeout=60)
        finally:
            killer.cancel()
            stderr_reader.join(timeout=10)
            stderr = "".join(stderr_chunks)

        if proc.returncode != 0:
            raise RuntimeError(
                f"codex runtime: codex exited {proc.returncode}. "
                f"stderr:\n{stderr[:2000]}"
            )
        if result_text is None and not turn_done:
            raise RuntimeError(
                "codex runtime: stream ended with no agent message "
                f"(exit {proc.returncode}). stderr:\n{stderr[:2000]}"
            )
        return _ClaudeCodeResponse(result_text or "")


def _codex_factory(*, system_prompt, sandbox_dir=None, harness=None,
                   loadout=None, tool_hooks=None, llm=None,
                   protected_paths=None, **_):
    if not sandbox_dir:
        raise ValueError(
            "codex runtime requires sandbox_dir (the runner passes it)."
        )
    model, call_timeout_s, tb_loadout, project_root, traj_hook, env_overrides = \
        _cli_runtime_common(harness, loadout, tool_hooks)
    sandbox_mode = _CODEX_DEFAULT_SANDBOX
    if harness is not None:
        runtime_cfg = getattr(harness, "runtime", None) or {}
        if runtime_cfg.get("sandbox"):
            sandbox_mode = str(runtime_cfg["sandbox"])
    # The run matrix's --models value is carried by SubscriptionLLM. It must
    # override a harness default; otherwise differently-labelled cells silently
    # execute the same Codex model.
    requested_model = getattr(llm, "model", None)
    if requested_model:
        model = requested_model
    reasoning_effort = None
    if harness is not None:
        reasoning_effort = (getattr(harness, "runtime", None) or {}).get(
            "reasoning_effort")
    return CodexAgent(
        system_prompt=system_prompt, sandbox_dir=sandbox_dir, model=model,
        loadout=tb_loadout, project_root=project_root, call_timeout_s=call_timeout_s,
        sandbox_mode=sandbox_mode, traj_hook=traj_hook, env=env_overrides,
        reasoning_effort=reasoning_effort, protected_paths=protected_paths,
    )


register_runtime("codex", _codex_factory)
