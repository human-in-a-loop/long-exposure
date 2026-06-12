#!/usr/bin/env python3
"""Opt-in interactive transport for the Claude provider.

Routes each agent turn through a persistent *interactive* Claude Code session
(real TTY, via tmux) instead of a `claude -p` subprocess. Default-off; enabled
only when ``config['claude_transport'] == 'interactive'`` and the active
provider is Claude. See ``docs/gaps_interactive_mode.md`` for rationale,
billing context, compliance caveat, and known limitations.

Contract: ``run_turn(...)`` returns the SAME envelope shape as
``orchestrator._invoke_claude`` — ``{result, usage, duration_ms, session_id}`` —
so the long-exposure cycle loop is otherwise unchanged. Control flow stays in
Python (push model): we enqueue a task file and block until the worker subagent
writes the response, then return.

Architecture:
  * One persistent interactive session = the *driver*. A seed prompt + a Stop
    hook keep it looping: it calls the ``fetch_next_task`` MCP tool and, per
    task, spawns ONE general-purpose subagent (fresh context — the `claude -p`
    equivalent) that reads the brief and writes the response file.
  * ``interactive_bridge.py`` serves the queue; the two hook modules keep the
    driver looping (Stop) and prevent unattended permission-prompt hangs in
    scoped mode (PreToolUse).
  * The driver holds no run state, so we recycle the session every N turns to
    bound its context growth.

Deferred (guarded elsewhere): multi-account pooling, parallel fan-out,
per-turn compaction (always-fresh context makes it unnecessary), per-agent tool
scoping, a pty fallback when tmux is absent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

_MODULE_DIR = Path(__file__).resolve().parent
_BRIDGE = _MODULE_DIR / "interactive_bridge.py"
_STOP_HOOK = _MODULE_DIR / "interactive_stop_hook.py"
_PRETOOL_HOOK = _MODULE_DIR / "interactive_pretool_hook.py"

# Tools the scoped-mode PreToolUse allowlist permits (driver + worker union).
_SCOPED_ALLOW = [
    "mcp__le-interactive-bridge__fetch_next_task",
    "Task", "Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebSearch",
]

# Prefix the bridge writes into a response file when it abandons a task at
# the redispatch cap (keep in sync with interactive_bridge._ABANDON_SENTINEL).
_ABANDON_SENTINEL = "[INTERACTIVE TRANSPORT ERROR]"

# Per-process singleton session state.
_session: dict | None = None
# One-time advisory when an agent's model differs from the driver model.
_model_advisory_printed = False


# --------------------------------------------------------------------------- #
# Enablement / paths
# --------------------------------------------------------------------------- #
def is_enabled(config: dict) -> bool:
    """True only for the Claude provider with interactive transport selected."""
    if str(config.get("llm_provider", "claude")).lower() != "claude":
        return False
    return str(config.get("claude_transport", "headless")).lower() == "interactive"


def _state_dir(config: dict) -> Path:
    """Per-run state dir, alongside sessions.db so it is stable across cycles.

    Always absolute: every path derived from it crosses a process boundary
    (bridge env var, hook settings, mcp config, worker brief, task json) into
    processes whose cwd is ``working_directory``, not the orchestrator's
    launch cwd — a relative ``compact_db`` (the config default) must not make
    those resolve against the wrong tree.
    """
    db = config.get("compact_db") or ""
    base = Path(db).parent if db else (
        Path(config.get("working_directory") or ".") / "data"
    )
    return (base / "interactive").resolve()


def _session_name(state_dir: Path) -> str:
    # Stable, collision-resistant per working tree.
    h = uuid.uuid5(uuid.NAMESPACE_URL, str(state_dir.resolve())).hex[:10]
    return f"le-int-{h}"


# --------------------------------------------------------------------------- #
# tmux helpers
# --------------------------------------------------------------------------- #
def _tmux(*args, check: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["tmux", *args], capture_output=True, text=True, check=check
        )
    except FileNotFoundError:
        # tmux absent: never crash teardown/health checks on it.
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="")


def _have_tmux() -> bool:
    return shutil.which("tmux") is not None


def _session_alive(name: str) -> bool:
    return _tmux("has-session", "-t", name).returncode == 0


def _capture(name: str) -> str:
    return _tmux("capture-pane", "-t", name, "-p").stdout


# --------------------------------------------------------------------------- #
# Session lifecycle
# --------------------------------------------------------------------------- #
def _seed_prompt() -> str:
    return (
        "You are the long-exposure interactive driver, running unattended. You "
        "orchestrate ONLY via the MCP tool "
        "mcp__le-interactive-bridge__fetch_next_task and the Task tool. NEVER "
        "use Bash or any shell yourself. Loop now and forever: "
        "(1) Call mcp__le-interactive-bridge__fetch_next_task. "
        "(2) If the JSON result has done=true, reply DONE and stop. "
        "(3) If it has idle=true, call the tool again. "
        "(4) If it has task=true, use the Task tool to spawn ONE general-purpose "
        "subagent whose entire prompt is: \"Read the file <PROMPT_FILE> and "
        "carry out everything it instructs, in full, including writing the "
        "response files it specifies at the very end.\" — substituting "
        "<PROMPT_FILE> with the result's prompt_file value. If the result "
        "includes a non-empty model value, pass it as the Task tool's model "
        "parameter when supported (otherwise proceed at the default model). "
        "(5) When the subagent returns, immediately call "
        "mcp__le-interactive-bridge__fetch_next_task again. Begin now."
    )


def _write_session_config(state_dir: Path, config: dict) -> tuple[Path, Path]:
    """Write mcp.json + settings.json for the driver session. Returns paths."""
    scoped = str(config.get("interactive_permission_mode", "skip")).lower() == "scoped"
    fetch_window = str(config.get("interactive_fetch_window_seconds", 30))

    mcp = {
        "mcpServers": {
            "le-interactive-bridge": {
                "command": "python3",
                "args": [str(_BRIDGE)],
                "env": {
                    "LONG_EXPOSURE_INTERACTIVE_DIR": str(state_dir),
                    "LONG_EXPOSURE_INTERACTIVE_FETCH_WINDOW": fetch_window,
                },
            }
        }
    }
    hooks: dict = {
        "Stop": [{"hooks": [{"type": "command",
                             "command": f"python3 {_STOP_HOOK}"}]}]
    }
    if scoped:
        hooks["PreToolUse"] = [{"matcher": "*", "hooks": [
            {"type": "command", "command": f"python3 {_PRETOOL_HOOK}"}]}]
    settings = {"hooks": hooks}

    mcp_path = state_dir / "mcp.json"
    settings_path = state_dir / "settings.json"
    mcp_path.write_text(json.dumps(mcp, indent=2))
    settings_path.write_text(json.dumps(settings, indent=2))
    return mcp_path, settings_path


def _launch(state_dir: Path, config: dict) -> dict:
    """Launch a fresh interactive driver session. Returns session state dict."""
    if not _have_tmux():
        from .orchestrator import ClaudeCliError
        raise ClaudeCliError(
            "interactive transport requires tmux on PATH (not found). "
            "Install tmux or use claude_transport: headless."
        )
    name = _session_name(state_dir)
    (state_dir / "requests").mkdir(parents=True, exist_ok=True)
    (state_dir / "responses").mkdir(parents=True, exist_ok=True)
    (state_dir / "prompts").mkdir(parents=True, exist_ok=True)
    # Clear stale control files from a prior session.
    for f in ("shutdown", ".stop_hook_n"):
        try:
            (state_dir / f).unlink()
        except OSError:
            pass
    # Purge stale queue files from a crashed prior run: a leftover pending
    # task would otherwise replay an obsolete brief against the workspace
    # (with skip-permissions, no less) the moment the new driver fetches it.
    for sub in ("requests", "prompts", "responses"):
        _purge_dir(state_dir / sub)
    # Owner liveness: the bridge's long-poll checks this PID and returns
    # done=true when the orchestrator is gone, so an orphaned driver stops
    # cleanly (atexit never runs on SIGKILL).
    (state_dir / "owner.pid").write_text(str(os.getpid()))

    mcp_path, settings_path = _write_session_config(state_dir, config)
    scoped = str(config.get("interactive_permission_mode", "skip")).lower() == "scoped"
    driver_model = config.get("interactive_driver_model", "sonnet")
    # Absolute: used both for the in-session `cd` and scoped `--add-dir`.
    cwd = str(Path(config.get("working_directory") or os.getcwd()).resolve())

    _tmux("kill-session", "-t", name)
    _tmux("new-session", "-d", "-s", name, "-x", "210", "-y", "50")

    # Env for the claude process (hooks read LONG_EXPOSURE_INTERACTIVE_DIR).
    env_prefix = (
        f"cd {_q(cwd)} && CLAUDECODE= "
        f"LONG_EXPOSURE_INTERACTIVE_DIR={_q(str(state_dir))} "
    )
    flags = [
        "claude",
        "--mcp-config", _q(str(mcp_path)),
        "--settings", _q(str(settings_path)),
        "--model", _q(str(driver_model)),
    ]
    if scoped:
        flags += ["--add-dir", _q(cwd),
                  "--allowedTools", *[_q(t) for t in _SCOPED_ALLOW]]
        env_prefix += (
            "LONG_EXPOSURE_INTERACTIVE_ALLOW="
            f"{_q(','.join(_SCOPED_ALLOW))} "
        )
    else:
        flags += ["--dangerously-skip-permissions"]
    launch_cmd = env_prefix + " ".join(flags)
    _tmux("send-keys", "-t", name, launch_cmd, "Enter")

    _await_ready(name, scoped)
    # Send the driver seed via literal keys (no shell quoting concerns).
    _tmux("send-keys", "-t", name, "-l", _seed_prompt())
    time.sleep(0.5)
    _tmux("send-keys", "-t", name, "Enter")

    return {
        "name": name,
        "state_dir": state_dir,
        "turns": 0,
        "recycle_at": int(config.get("interactive_recycle_turns", 40)),
    }


def _await_ready(name: str, scoped: bool, timeout: float = 60.0) -> None:
    """Wait for the session prompt; answer startup dialogs along the way."""
    from .orchestrator import ClaudeCliError
    trust_done = not scoped  # skip mode bypasses trust via skip-permissions
    # Skip mode instead shows a one-time first-run "Bypass Permissions mode
    # ... Yes, I accept" dialog on a fresh account/machine. Answered below by
    # the same screen-scrape-brittle pane match as the trust dialog (no flag
    # suppresses it). bypass_done only gates the dialog handler — readiness
    # must not wait on a dialog that usually never appears.
    bypass_done = scoped
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(2)
        scr = _capture(name).lower()
        if not _session_alive(name):
            raise ClaudeCliError("interactive driver session died during launch")
        if not trust_done and "trust this folder" in scr:
            _tmux("send-keys", "-t", name, "1")
            time.sleep(0.6)
            _tmux("send-keys", "-t", name, "Enter")
            trust_done = True
            continue
        if not bypass_done and "bypass permissions" in scr and "accept" in scr:
            _tmux("send-keys", "-t", name, "2")  # "2. Yes, I accept"
            time.sleep(0.6)
            _tmux("send-keys", "-t", name, "Enter")
            bypass_done = True
            continue
        if trust_done and ("? for shortcuts" in scr or "for agents" in scr
                           or "try " in scr or "welcome" in scr):
            return
    raise ClaudeCliError(
        f"interactive driver session not ready within {timeout:.0f}s"
    )


def _q(s: str) -> str:
    """Minimal shell single-quote."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def ensure_session(config: dict):
    """Ensure a live driver session, launching or recycling as needed."""
    global _session
    state_dir = _state_dir(config)
    if _session is not None and _session_alive(_session["name"]):
        # Recycle to bound driver context growth (driver is stateless).
        if _session["turns"] >= _session["recycle_at"] > 0:
            _kill(_session)
            _session = _launch(state_dir, config)
        return _session
    _session = _launch(state_dir, config)
    return _session


def _kill(sess: dict) -> None:
    try:
        (sess["state_dir"] / "shutdown").write_text("1")
    except OSError:
        pass
    _tmux("kill-session", "-t", sess["name"])
    try:
        (sess["state_dir"] / "shutdown").unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Turn execution (the seam)
# --------------------------------------------------------------------------- #
def run_turn(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
    agent_name: str,
    timeout: int | None,
    config: dict,
) -> dict:
    """Execute one agent turn via the interactive session.

    Returns ``{result, usage, duration_ms, session_id}``. ``usage`` carries a
    conservative chars/4 ``output_tokens`` estimate only (no token counts
    cross the bridge); input/context fields are deliberately absent so the
    compaction/reanchor paths keep no-op'ing on 0.
    """
    from .orchestrator import ClaudeCliError
    global _session, _model_advisory_printed

    sess = ensure_session(config)
    state_dir = sess["state_dir"]
    # Purge response files orphaned by earlier timed-out/abandoned turns (a
    # late worker can write them after _cleanup already ran).
    _purge_dir(state_dir / "responses")
    driver_model = str(config.get("interactive_driver_model", "sonnet"))
    if model and str(model) != driver_model and not _model_advisory_printed:
        print(
            f"[long-exposure] interactive: agent model '{model}' is advisory; "
            "worker runs at driver default unless the Task tool honors it"
        )
        _model_advisory_printed = True
    turn_id = uuid.uuid4().hex
    prompt_file = state_dir / "prompts" / f"{turn_id}.txt"
    response_file = state_dir / "responses" / f"{turn_id}.out"
    done_file = Path(str(response_file) + ".done")

    brief = (
        f"{system_prompt}\n\n"
        f"========================= TASK =========================\n\n"
        f"{user_prompt}\n\n"
        f"=================== RESPONSE PROTOCOL ===================\n"
        f"You are running as a long-exposure worker. Do the work above using "
        f"your tools in the current working directory, exactly as instructed "
        f"(write any deliverable files, emit any [OUTPUT: name] ... "
        f"[END OUTPUT] blocks the brief asks for).\n"
        f"As your FINAL two actions, use the Write tool to:\n"
        f"  1. Write your COMPLETE final response text (including any "
        f"[OUTPUT: ...][END OUTPUT] blocks) to:\n     {response_file}\n"
        f"  2. Write the single word done to:\n     {done_file}\n"
        f"Do not skip these two writes — they signal completion."
    )
    prompt_file.write_text(brief)

    task = {
        "turn_id": turn_id,
        "status": "pending",
        "prompt_file": str(prompt_file),
        "response_file": str(response_file),
        "model": model or "",
    }
    task_file = state_dir / "requests" / f"{turn_id}.task.json"
    _atomic_write(task_file, json.dumps(task))

    limit = float(timeout) if timeout else float(
        config.get("interactive_turn_timeout_seconds", 1800)
    )
    started = time.time()
    deadline = started + limit
    ticks = 0
    while time.time() < deadline:
        if done_file.exists():
            result = _read_text(response_file)
            _cleanup(turn_id, state_dir)
            if result.startswith(_ABANDON_SENTINEL):
                # The bridge failed this task at its redispatch cap; surface
                # it as a CLI error so the caller's retry/cooldown path runs
                # instead of feeding the sentinel text to output parsing.
                raise ClaudeCliError(f"{result.strip()} (agent={agent_name})")
            sess["turns"] += 1
            return {
                "result": result,
                # chars/4 estimate: no token counts cross the bridge, but the
                # relative low-output exhaustion detector and telemetry need a
                # real output signal. Input/context fields are deliberately
                # absent so compaction/reanchor keep no-op'ing on 0.
                "usage": {"output_tokens": max(1, len(result) // 4)},
                "duration_ms": int((time.time() - started) * 1000),
                "session_id": turn_id,
            }
        # If the driver session died, fail fast so the caller can recover.
        # The liveness probe forks a tmux subprocess, so run it every ~10th
        # iteration; the cheap done-file stat stays at 1s granularity.
        if ticks % 10 == 0 and not _session_alive(sess["name"]):
            _cleanup(turn_id, state_dir)
            _session = None  # force relaunch on next call
            raise ClaudeCliError("interactive driver session died mid-turn")
        ticks += 1
        time.sleep(1.0)

    # Timeout: drop the task so a recycled driver does not re-run it, and kill
    # the driver — it may still be wedged inside the worker subagent, which
    # would queue the next turn behind it. The retry relaunches fresh.
    _cleanup(turn_id, state_dir)
    _kill(sess)
    _session = None  # force relaunch on next call
    raise ClaudeCliError(
        f"interactive turn timed out after {limit:.0f}s (agent={agent_name})"
    )


def _cleanup(turn_id: str, state_dir: Path) -> None:
    for p in (
        state_dir / "requests" / f"{turn_id}.task.json",
        state_dir / "prompts" / f"{turn_id}.txt",
        state_dir / "responses" / f"{turn_id}.out",
        Path(str(state_dir / "responses" / f"{turn_id}.out") + ".done"),
    ):
        try:
            p.unlink()
        except OSError:
            pass


def _purge_dir(d: Path) -> None:
    """Best-effort removal of every file in a queue directory."""
    try:
        entries = list(d.iterdir())
    except OSError:
        return
    for p in entries:
        try:
            p.unlink()
        except OSError:
            pass


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def shutdown() -> None:
    """Tear down the driver session (idempotent; safe to call at exit)."""
    global _session
    if _session is not None:
        _kill(_session)
        _session = None
