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
                loadout=None) -> Any:
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
                   sandbox_dir=sandbox_dir, harness=harness, loadout=loadout)


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
# No hardcoded toolkit: the MCP profile a CLI runtime serves is derived from the
# benchmark's loadout (its `toolbase: {profile: ...}` source) via
# `_loadout_toolbase_profile`. None => serve no MCP server (the `core` baseline
# runs with only the builtin tools below).
_CLAUDE_CODE_PROFILE = None
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


def _loadout_toolbase_profile(loadout) -> tuple[str | None, str | None]:
    """The toolbase ``(profile, project_root)`` a CLI runtime should serve over
    MCP, taken from the benchmark's loadout — its first ``toolbase: {profile,
    project_root}`` source. Returns ``(None, None)`` when the loadout has no
    toolbase source (e.g. the ``core`` baseline), in which case the CLI runs
    with only its builtin tools. This is what keeps the CLI runtimes generic:
    the served toolkit follows the loadout, never a hardcoded profile name."""
    for src in (getattr(loadout, "sources", None) or []):
        if getattr(src, "backend", None) == "toolbase":
            cfg = src.config if isinstance(src.config, dict) else {}
            return cfg.get("profile"), cfg.get("project_root")
    return None, None


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
                 model: str | None = None, profile: str | None = _CLAUDE_CODE_PROFILE,
                 project_root: str | None = None,
                 call_timeout_s: int = _CLAUDE_CODE_CALL_TIMEOUT_S,
                 traj_hook=None):
        self.system_prompt = system_prompt or ""
        self.sandbox_dir = Path(sandbox_dir).resolve()
        self.model = model or "claude-haiku-4-5"
        self.profile = profile
        self.project_root = project_root
        self.call_timeout_s = int(call_timeout_s)
        # The runner's TrajectoryHook: firing before_call/after_call on it per
        # streamed tool call records the call onto the trajectory (-> transcript
        # + artifact dump) AND emits the orchestral-format line to console.log,
        # live. None in non-runner contexts (degrades to no streaming).
        self.traj_hook = traj_hook
        self.session_id: str | None = None
        self._mcp_config_path: Path | None = None

    # -- MCP wiring -----------------------------------------------------
    def _ensure_mcp_config(self) -> Path | None:
        """Write `<sandbox>/.mcp.json` wiring the loadout's toolbase profile
        as a stdio MCP server, or return None when the loadout serves no
        toolbase profile (the agent then runs with only its builtin tools).
        `toolbase serve` resolves its toolkits' `base_directory` (default
        `${CWD}`) from the cwd we launch `claude` in — the sandbox — so the
        served tools operate inside the sandbox."""
        if not self.profile:
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
        args = ["serve", "--profile", self.profile,
                "--call-timeout", str(self.call_timeout_s)]
        path = self.sandbox_dir / ".mcp.json"
        config = {
            "mcpServers": {
                _TOOLBASE_MCP_SERVER: {
                    "type": "stdio",
                    "command": "toolbase",
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
        # When the loadout serves a toolbase profile, allow its MCP tools;
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
            # stream-json (NDJSON) lets us record each tool call onto the
            # trajectory AS IT HAPPENS, so the trial's console.log + transcript
            # show the live tool-call timeline (same format as orchestral) and
            # are spot-checkable for fabrication. Requires --verbose in -p mode.
            "--output-format", "stream-json", "--verbose",
        ]
        if self.session_id:
            cmd += ["--resume", self.session_id]

        # Subscription auth via the logged-in CLI: never inject an API key.
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        # Client-side MCP timeouts as a backstop; the SERVER-side wall that
        # actually fires is `toolbase serve --call-timeout` (set in the
        # .mcp.json above). Keep them consistent so neither side cuts a long
        # but legitimate Pythia/Delphes call short.
        env.setdefault("MCP_TOOL_TIMEOUT", str(self.call_timeout_s * 1000))
        env.setdefault("MCP_TIMEOUT", str(60 * 1000))

        import threading
        proc = subprocess.Popen(
            cmd, cwd=str(self.sandbox_dir), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        # Hard wall even if the stream goes silent (the overall trial ceiling).
        killer = threading.Timer(_CLAUDE_CODE_TIMEOUT_S, proc.kill)
        killer.start()

        hook = self.traj_hook
        id2name: dict = {}
        result_data = None
        stderr = ""
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
            try:
                stderr = proc.stderr.read() or ""
            except Exception:
                pass

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
        return _ClaudeCodeResponse(result_data.get("result") or "")


def _cli_runtime_common(harness, loadout, tool_hooks):
    """Shared factory plumbing for the CLI-driven runtimes (claude_code,
    codex): the model + per-call timeout from the harness, the served toolbase
    profile from the loadout, and the runner's TrajectoryHook (records tool
    calls onto the trajectory + emits the styled console line; other hooks like
    TruncateOutputHook are ignored — they only matter for an in-process model).
    Returns (model, call_timeout_s, profile, project_root, traj_hook)."""
    model = None
    call_timeout_s = _CLAUDE_CODE_CALL_TIMEOUT_S
    if harness is not None:
        provider = getattr(harness, "provider", None) or {}
        model = provider.get("model")
        runtime_cfg = getattr(harness, "runtime", None) or {}
        if runtime_cfg.get("call_timeout_s") is not None:
            call_timeout_s = int(runtime_cfg["call_timeout_s"])
    profile, project_root = _loadout_toolbase_profile(loadout)
    traj_hook = None
    for h in (tool_hooks or []):
        if (hasattr(h, "before_call") and hasattr(h, "after_call")
                and hasattr(h, "trajectory")):
            traj_hook = h
            break
    return model, call_timeout_s, profile, project_root, traj_hook


def _claude_code_factory(*, system_prompt, sandbox_dir=None, harness=None,
                         loadout=None, tool_hooks=None, **_):
    # `**_` absorbs the orchestral-shaped kwargs (llm, tools, display_hook)
    # this runtime doesn't use. We DO use tool_hooks: the runner's
    # TrajectoryHook is in there, and firing it per streamed tool call gives
    # us the same console.log + transcript as orchestral.
    if not sandbox_dir:
        raise ValueError(
            "claude_code runtime requires sandbox_dir (the runner passes it)."
        )
    model, call_timeout_s, profile, project_root, traj_hook = _cli_runtime_common(
        harness, loadout, tool_hooks)
    return ClaudeCodeAgent(
        system_prompt=system_prompt, sandbox_dir=sandbox_dir, model=model,
        profile=profile, project_root=project_root,
        call_timeout_s=call_timeout_s, traj_hook=traj_hook,
    )


register_runtime("claude_code", _claude_code_factory)


# ── codex runtime ───────────────────────────────────────────────────
#
# Drives a benchmark trial with the OpenAI Codex CLI (`codex exec --json`)
# under the user's logged-in ChatGPT subscription (never an API key, so no
# per-token API cost). Mirrors the claude_code runtime: the loadout's
# toolbase profile is served over MCP (wired via `-c mcp_servers.*` TOML
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
                 model: str | None = None, profile: str | None = None,
                 project_root: str | None = None,
                 call_timeout_s: int = _CLAUDE_CODE_CALL_TIMEOUT_S,
                 sandbox_mode: str = _CODEX_DEFAULT_SANDBOX, traj_hook=None):
        self.system_prompt = system_prompt or ""
        self.sandbox_dir = Path(sandbox_dir).resolve()
        self.model = model            # None => codex uses its configured default
        self.profile = profile
        self.project_root = project_root
        self.call_timeout_s = int(call_timeout_s)
        self.sandbox_mode = sandbox_mode or _CODEX_DEFAULT_SANDBOX
        self.traj_hook = traj_hook
        self.thread_id: str | None = None

    # -- MCP wiring -----------------------------------------------------
    def _mcp_config_args(self) -> list[str]:
        """`-c mcp_servers.toolbase.*` overrides serving the loadout's toolbase
        profile, or [] when the loadout serves no profile (the agent then runs
        with only Codex's builtin shell). Values are TOML (JSON is valid TOML
        for strings/arrays). `toolbase serve` resolves config from its cwd
        (the sandbox), which the codex process — and thus its MCP child —
        runs in."""
        if not self.profile:
            return []
        serve_args = ["serve", "--profile", self.profile,
                      "--call-timeout", str(self.call_timeout_s)]
        return [
            "-c", f"mcp_servers.{_TOOLBASE_MCP_SERVER}.command="
                  + json.dumps("toolbase"),
            "-c", f"mcp_servers.{_TOOLBASE_MCP_SERVER}.args="
                  + json.dumps(serve_args),
        ]

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
            name = (item.get("tool") or item.get("name")
                    or item.get("server") or "mcp_tool").split("__")[-1]
            args = item.get("arguments") or item.get("input") or {}
            out = (item.get("result") or item.get("output")
                   or item.get("aggregated_output") or "")
            return name, args, str(out), bool(item.get("is_error") or item.get("error"))
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
            cmd = [codex, "exec", "resume", self.thread_id]
        else:
            prompt = (f"{self.system_prompt}\n\n---\n\n{message}"
                      if self.system_prompt else message)
            cmd = [codex, "exec"]
        cmd += ["--json", "--skip-git-repo-check",
                "-s", self.sandbox_mode, "-C", str(self.sandbox_dir)]
        if self.model:
            cmd += ["-m", self.model]
        cmd += self._mcp_config_args()
        cmd += [prompt]

        # Subscription auth via the logged-in CLI: never inject an API key.
        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)

        import threading
        proc = subprocess.Popen(
            cmd, cwd=str(self.sandbox_dir), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        killer = threading.Timer(_CLAUDE_CODE_TIMEOUT_S, proc.kill)
        killer.start()

        hook = self.traj_hook
        started: dict = {}            # item id -> tool name (for after_call)
        result_text = None
        turn_done = False
        stderr = ""
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
            proc.wait(timeout=60)
        finally:
            killer.cancel()
            try:
                stderr = proc.stderr.read() or ""
            except Exception:
                pass

        if result_text is None and not turn_done:
            raise RuntimeError(
                "codex runtime: stream ended with no agent message "
                f"(exit {proc.returncode}). stderr:\n{stderr[:2000]}"
            )
        return _ClaudeCodeResponse(result_text or "")


def _codex_factory(*, system_prompt, sandbox_dir=None, harness=None,
                   loadout=None, tool_hooks=None, **_):
    if not sandbox_dir:
        raise ValueError(
            "codex runtime requires sandbox_dir (the runner passes it)."
        )
    model, call_timeout_s, profile, project_root, traj_hook = _cli_runtime_common(
        harness, loadout, tool_hooks)
    sandbox_mode = _CODEX_DEFAULT_SANDBOX
    if harness is not None:
        runtime_cfg = getattr(harness, "runtime", None) or {}
        if runtime_cfg.get("sandbox"):
            sandbox_mode = str(runtime_cfg["sandbox"])
    return CodexAgent(
        system_prompt=system_prompt, sandbox_dir=sandbox_dir, model=model,
        profile=profile, project_root=project_root, call_timeout_s=call_timeout_s,
        sandbox_mode=sandbox_mode, traj_hook=traj_hook,
    )


register_runtime("codex", _codex_factory)
