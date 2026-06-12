"""Tests for the opt-in interactive Claude transport.

No real Claude/tmux session is launched. The driver session is mocked; the
worker subagent is simulated by a thread that writes the response + .done
marker when a task file appears. The bridge protocol is tested end-to-end as a
subprocess speaking JSON-RPC over stdio (the real MCP stdio path).
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from long_exposure import interactive_transport as it

REPO = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Enablement / wiring (default-off guarantee)
# --------------------------------------------------------------------------- #
def test_disabled_by_default():
    assert it.is_enabled({"llm_provider": "claude"}) is False
    assert it.is_enabled({"llm_provider": "claude",
                          "claude_transport": "headless"}) is False


def test_enabled_only_for_claude():
    assert it.is_enabled({"llm_provider": "claude",
                          "claude_transport": "interactive"}) is True
    # Non-Claude providers ignore the flag entirely.
    assert it.is_enabled({"llm_provider": "codex",
                          "claude_transport": "interactive"}) is False
    assert it.is_enabled({"llm_provider": "gemini",
                          "claude_transport": "interactive"}) is False


def test_state_dir_and_session_name_stable(tmp_path):
    cfg = {"compact_db": str(tmp_path / "data" / "sessions.db")}
    d1 = it._state_dir(cfg)
    d2 = it._state_dir(cfg)
    assert d1 == d2
    assert d1.name == "interactive"
    assert it._session_name(d1) == it._session_name(d2)
    assert it._session_name(d1).startswith("le-int-")


def test_state_dir_absolute_for_relative_compact_db(tmp_path, monkeypatch):
    """The config default compact_db is relative; the state dir crosses
    process boundaries (bridge env, hooks, worker brief) whose cwd is the
    working_directory, so _state_dir must anchor it to the orchestrator's
    cwd by returning an absolute path."""
    monkeypatch.chdir(tmp_path)
    d = it._state_dir({"compact_db": "./data/sessions.db"})
    assert d.is_absolute()
    assert d == (tmp_path / "data" / "interactive").resolve()
    # Fallback path (no compact_db) must be absolute too.
    d2 = it._state_dir({"working_directory": "."})
    assert d2.is_absolute()
    assert d2 == (tmp_path / "data" / "interactive").resolve()


# --------------------------------------------------------------------------- #
# run_turn end-to-end with a mocked session + simulated worker
# --------------------------------------------------------------------------- #
def _simulate_worker(state_dir: Path, output: str, stop: threading.Event):
    """Watch for a task and write its response file + .done marker."""
    reqdir = state_dir / "requests"
    while not stop.is_set():
        tasks = list(reqdir.glob("*.task.json")) if reqdir.exists() else []
        for tf in tasks:
            try:
                t = json.loads(tf.read_text())
            except Exception:
                continue
            rf = Path(t["response_file"])
            # The brief must have been written and reference this response file.
            assert Path(t["prompt_file"]).exists()
            rf.parent.mkdir(parents=True, exist_ok=True)
            rf.write_text(output)
            Path(str(rf) + ".done").write_text("done")
            return
        time.sleep(0.02)


@pytest.fixture
def mocked_session(tmp_path, monkeypatch):
    cfg = {
        "llm_provider": "claude",
        "claude_transport": "interactive",
        "compact_db": str(tmp_path / "data" / "sessions.db"),
        "working_directory": str(tmp_path),
        "interactive_turn_timeout_seconds": 15,
    }
    state_dir = it._state_dir(cfg)

    def fake_launch(sd, config):
        for sub in ("requests", "responses", "prompts"):
            (sd / sub).mkdir(parents=True, exist_ok=True)
        return {"name": "fake-session", "state_dir": sd, "turns": 0,
                "recycle_at": int(config.get("interactive_recycle_turns", 40))}

    monkeypatch.setattr(it, "_launch", fake_launch)
    monkeypatch.setattr(it, "_session_alive", lambda name: True)
    it._session = None
    yield cfg, state_dir
    it._session = None


def test_run_turn_returns_envelope(mocked_session):
    cfg, state_dir = mocked_session
    out = "WORKER FINAL\n[OUTPUT: research_brief]\nhello world\n[END OUTPUT]"
    stop = threading.Event()
    th = threading.Thread(target=_simulate_worker, args=(state_dir, out, stop))
    th.start()
    try:
        env = it.run_turn(
            system_prompt="SYSTEM-PROMPT-MARKER",
            user_prompt="USER-PROMPT-MARKER",
            model="opus",
            agent_name="researcher",
            timeout=15,
            config=cfg,
        )
    finally:
        stop.set()
        th.join()
    assert out in env["result"]
    # usage carries a chars/4 output estimate (exhaustion-detector signal);
    # input/context fields stay absent so compaction/reanchor no-op.
    assert env["usage"]["output_tokens"] == max(1, len(env["result"]) // 4)
    assert "input_tokens" not in env["usage"]
    assert env["session_id"]
    assert env["duration_ms"] >= 0
    # Transient files cleaned up.
    assert not list((state_dir / "requests").glob("*.task.json"))
    assert not list((state_dir / "prompts").glob("*.txt"))


def test_run_turn_brief_contains_prompts(mocked_session, monkeypatch):
    cfg, state_dir = mocked_session
    captured = {}

    def capture_worker(stop):
        reqdir = state_dir / "requests"
        while not stop.is_set():
            tasks = list(reqdir.glob("*.task.json")) if reqdir.exists() else []
            if tasks:
                t = json.loads(tasks[0].read_text())
                captured["brief"] = Path(t["prompt_file"]).read_text()
                rf = Path(t["response_file"])
                rf.write_text("ok")
                Path(str(rf) + ".done").write_text("done")
                return
            time.sleep(0.02)

    stop = threading.Event()
    th = threading.Thread(target=capture_worker, args=(stop,))
    th.start()
    try:
        it.run_turn(system_prompt="SYSTEM-PROMPT-MARKER",
                    user_prompt="USER-PROMPT-MARKER", model="opus",
                    agent_name="worker", timeout=15, config=cfg)
    finally:
        stop.set()
        th.join()
    brief = captured["brief"]
    assert "SYSTEM-PROMPT-MARKER" in brief
    assert "USER-PROMPT-MARKER" in brief
    assert "RESPONSE PROTOCOL" in brief
    assert ".done" in brief


def test_run_turn_times_out_when_no_worker(mocked_session):
    cfg, state_dir = mocked_session
    from long_exposure.orchestrator import ClaudeCliError
    cfg = dict(cfg)
    cfg["interactive_turn_timeout_seconds"] = 1
    with pytest.raises(ClaudeCliError):
        it.run_turn(system_prompt="s", user_prompt="u", model="opus",
                    agent_name="auditor", timeout=1, config=cfg)
    # Stale task dropped so a recycled driver won't re-run it.
    assert not list((state_dir / "requests").glob("*.task.json"))


def test_run_turn_fails_fast_when_session_dies(mocked_session, monkeypatch):
    cfg, state_dir = mocked_session
    from long_exposure.orchestrator import ClaudeCliError
    monkeypatch.setattr(it, "_session_alive", lambda name: False)
    with pytest.raises(ClaudeCliError):
        it.run_turn(system_prompt="s", user_prompt="u", model="opus",
                    agent_name="researcher", timeout=15, config=cfg)


def test_run_turn_probes_session_liveness_sparingly(mocked_session, monkeypatch):
    """_session_alive forks a tmux subprocess; the wait loop must probe it
    only every ~10th 1s iteration, not every second (the done-file stat
    stays at 1s granularity)."""
    cfg, state_dir = mocked_session
    from long_exposure.orchestrator import ClaudeCliError
    clock = {"t": 0.0}
    monkeypatch.setattr(it.time, "time", lambda: clock["t"])

    def fake_sleep(s):
        clock["t"] += s

    monkeypatch.setattr(it.time, "sleep", fake_sleep)
    calls = {"n": 0}

    def alive(name):
        calls["n"] += 1
        return True

    monkeypatch.setattr(it, "_session_alive", alive)
    monkeypatch.setattr(it, "_kill", lambda sess: None)
    with pytest.raises(ClaudeCliError):  # no worker => times out
        it.run_turn(system_prompt="s", user_prompt="u", model="opus",
                    agent_name="auditor", timeout=100, config=cfg)
    # 100 one-second iterations, probed every 10th => 10 calls, not ~100.
    assert calls["n"] == 10


def test_run_turn_raises_on_abandon_sentinel(mocked_session):
    """When the bridge abandons a task at the redispatch cap it writes the
    error sentinel through the normal completion channel; run_turn must turn
    that into a ClaudeCliError instead of returning it as parseable output."""
    cfg, state_dir = mocked_session
    from long_exposure.orchestrator import ClaudeCliError
    out = (f"{it._ABANDON_SENTINEL} task abandoned after 3 dispatch "
           "attempts — driver could not complete it")
    stop = threading.Event()
    th = threading.Thread(target=_simulate_worker, args=(state_dir, out, stop))
    th.start()
    try:
        with pytest.raises(ClaudeCliError, match="abandoned after 3"):
            it.run_turn(system_prompt="s", user_prompt="u", model="opus",
                        agent_name="researcher", timeout=15, config=cfg)
    finally:
        stop.set()
        th.join()
    # Transient files cleaned up like any completed turn.
    assert not list((state_dir / "requests").glob("*.task.json"))
    assert not list((state_dir / "responses").iterdir())


def test_timeout_kills_session_for_fresh_relaunch(mocked_session, monkeypatch):
    """A timed-out turn may leave the driver wedged in the worker subagent:
    the timeout path must kill the session and null the singleton so the
    retry launches a fresh driver instead of queueing behind it."""
    cfg, state_dir = mocked_session
    from long_exposure.orchestrator import ClaudeCliError
    killed = []
    monkeypatch.setattr(it, "_kill", lambda sess: killed.append(sess["name"]))
    with pytest.raises(ClaudeCliError):
        it.run_turn(system_prompt="s", user_prompt="u", model="opus",
                    agent_name="auditor", timeout=1, config=cfg)
    assert killed == ["fake-session"]
    assert it._session is None


def test_run_turn_purges_orphaned_response_files(mocked_session):
    """Response files written by a late worker after an abandoned turn must
    not accumulate: run_turn purges the responses dir at start."""
    cfg, state_dir = mocked_session
    (state_dir / "responses").mkdir(parents=True, exist_ok=True)
    (state_dir / "responses" / "stale.out").write_text("late write")
    (state_dir / "responses" / "stale.out.done").write_text("done")
    out = "fresh turn output"
    stop = threading.Event()
    th = threading.Thread(target=_simulate_worker, args=(state_dir, out, stop))
    th.start()
    try:
        env = it.run_turn(system_prompt="s", user_prompt="u", model="opus",
                          agent_name="researcher", timeout=15, config=cfg)
    finally:
        stop.set()
        th.join()
    assert out in env["result"]
    assert not (state_dir / "responses" / "stale.out").exists()
    assert not (state_dir / "responses" / "stale.out.done").exists()


def test_recycle_relaunches_after_threshold(tmp_path, monkeypatch):
    cfg = {
        "llm_provider": "claude", "claude_transport": "interactive",
        "compact_db": str(tmp_path / "data" / "sessions.db"),
        "working_directory": str(tmp_path), "interactive_recycle_turns": 3,
    }
    launches = {"n": 0}

    def fake_launch(sd, config):
        launches["n"] += 1
        for sub in ("requests", "responses", "prompts"):
            (sd / sub).mkdir(parents=True, exist_ok=True)
        return {"name": f"s{launches['n']}", "state_dir": sd, "turns": 0,
                "recycle_at": 3}

    monkeypatch.setattr(it, "_launch", fake_launch)
    monkeypatch.setattr(it, "_session_alive", lambda name: True)
    monkeypatch.setattr(it, "_kill", lambda sess: None)
    it._session = None
    s = it.ensure_session(cfg)
    assert launches["n"] == 1
    s["turns"] = 3  # hit threshold
    it.ensure_session(cfg)
    assert launches["n"] == 2  # recycled
    it._session = None


# --------------------------------------------------------------------------- #
# Bridge protocol (real subprocess, JSON-RPC over stdio)
# --------------------------------------------------------------------------- #
def test_bridge_protocol_end_to_end(tmp_path):
    state = tmp_path / "interactive"
    (state / "requests").mkdir(parents=True)
    (state / "responses").mkdir(parents=True)
    env = dict(os.environ)
    env["LONG_EXPOSURE_INTERACTIVE_DIR"] = str(state)
    env["LONG_EXPOSURE_INTERACTIVE_FETCH_WINDOW"] = "2"
    p = subprocess.Popen(
        [sys.executable, str(REPO / "long_exposure" / "interactive_bridge.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
    )
    _id = {"n": 0}

    def call(method, params=None):
        _id["n"] += 1
        msg = {"jsonrpc": "2.0", "method": method, "id": _id["n"]}
        if params is not None:
            msg["params"] = params
        p.stdin.write((json.dumps(msg) + "\n").encode())
        p.stdin.flush()
        return json.loads(p.stdout.readline())

    try:
        init = call("initialize", {})
        assert init["result"]["serverInfo"]["name"] == "le-interactive-bridge"
        tools = [t["name"] for t in call("tools/list", {})["result"]["tools"]]
        assert tools == ["fetch_next_task"]

        # Idle when empty (returns after the 2s bounded window).
        r = json.loads(call("tools/call", {"name": "fetch_next_task",
                                           "arguments": {}})["result"]["content"][0]["text"])
        assert r.get("idle") is True

        # Seed a task; fetch should return it and mark it dispatched.
        task = {"turn_id": "abc", "status": "pending",
                "prompt_file": "/p", "response_file": "/r", "model": "opus"}
        (state / "requests" / "abc.task.json").write_text(json.dumps(task))
        r = json.loads(call("tools/call", {"name": "fetch_next_task",
                                           "arguments": {}})["result"]["content"][0]["text"])
        assert r.get("task") is True and r["turn_id"] == "abc"
        assert json.loads((state / "requests" / "abc.task.json").read_text())["status"] == "dispatched"

        # Shutdown sentinel -> done.
        (state / "shutdown").write_text("1")
        r = json.loads(call("tools/call", {"name": "fetch_next_task",
                                           "arguments": {}})["result"]["content"][0]["text"])
        assert r.get("done") is True
    finally:
        p.stdin.close()
        p.terminate()
        p.wait(timeout=5)


def _start_bridge(state: Path, window: str = "2"):
    """Spawn the bridge subprocess; returns (proc, call) for JSON-RPC calls."""
    env = dict(os.environ)
    env["LONG_EXPOSURE_INTERACTIVE_DIR"] = str(state)
    env["LONG_EXPOSURE_INTERACTIVE_FETCH_WINDOW"] = window
    p = subprocess.Popen(
        [sys.executable, str(REPO / "long_exposure" / "interactive_bridge.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
    )
    _id = {"n": 0}

    def call(method, params=None):
        _id["n"] += 1
        msg = {"jsonrpc": "2.0", "method": method, "id": _id["n"]}
        if params is not None:
            msg["params"] = params
        p.stdin.write((json.dumps(msg) + "\n").encode())
        p.stdin.flush()
        return json.loads(p.stdout.readline())

    return p, call


def _fetch(call):
    r = call("tools/call", {"name": "fetch_next_task", "arguments": {}})
    return json.loads(r["result"]["content"][0]["text"])


def test_bridge_returns_done_when_owner_dead(tmp_path):
    """Orphan guard: a dead owner.pid makes the long-poll return done=true so
    a SIGKILL-orphaned driver stops instead of polling forever."""
    state = tmp_path / "interactive"
    (state / "requests").mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (state / "owner.pid").write_text(str(dead.pid))
    p, call = _start_bridge(state, window="10")
    try:
        call("initialize", {})
        assert _fetch(call).get("done") is True
    finally:
        p.stdin.close()
        p.terminate()
        p.wait(timeout=5)


def test_bridge_treats_missing_or_bad_owner_pid_as_alive(tmp_path):
    """Missing/unparseable owner.pid must NOT stop the driver (live smoke
    runs never write one)."""
    state = tmp_path / "interactive"
    (state / "requests").mkdir(parents=True)
    (state / "owner.pid").write_text("not-a-pid")
    p, call = _start_bridge(state, window="1")
    try:
        call("initialize", {})
        assert _fetch(call).get("idle") is True  # polled the full window
    finally:
        p.stdin.close()
        p.terminate()
        p.wait(timeout=5)


def test_bridge_redispatches_stale_dispatched_task(tmp_path):
    """A task stuck in 'dispatched' (driver-side Task failure) is offered
    again after the staleness window, with a capped redispatch counter."""
    state = tmp_path / "interactive"
    (state / "requests").mkdir(parents=True)
    (state / "responses").mkdir(parents=True)
    tf = state / "requests" / "abc.task.json"
    task = {"turn_id": "abc", "status": "dispatched",
            "dispatched_at": time.time() - 10000,
            "prompt_file": "/p", "response_file": "/r", "model": "opus"}
    tf.write_text(json.dumps(task))
    p, call = _start_bridge(state, window="2")
    try:
        call("initialize", {})
        r = _fetch(call)
        assert r.get("task") is True and r["turn_id"] == "abc"
        on_disk = json.loads(tf.read_text())
        assert on_disk["status"] == "dispatched"
        assert on_disk["redispatches"] == 1

        # At the cap, a stale dispatched task is NOT offered again.
        on_disk["dispatched_at"] = time.time() - 10000
        on_disk["redispatches"] = 2
        on_disk["response_file"] = str(state / "responses" / "abc.out")
        tf.write_text(json.dumps(on_disk))
        assert _fetch(call).get("idle") is True
    finally:
        p.stdin.close()
        p.terminate()
        p.wait(timeout=5)


def test_bridge_abandons_task_at_redispatch_cap(tmp_path):
    """A task that exhausts the redispatch cap must be failed through the
    normal completion channel (sentinel response + .done + status=failed) so
    the blocked run_turn returns promptly instead of waiting out its full
    turn timeout."""
    state = tmp_path / "interactive"
    (state / "requests").mkdir(parents=True)
    (state / "responses").mkdir(parents=True)
    rf = state / "responses" / "abc.out"
    tf = state / "requests" / "abc.task.json"
    task = {"turn_id": "abc", "status": "dispatched",
            "dispatched_at": time.time() - 10000, "redispatches": 2,
            "prompt_file": "/p", "response_file": str(rf), "model": ""}
    tf.write_text(json.dumps(task))
    p, call = _start_bridge(state, window="2")
    try:
        call("initialize", {})
        assert _fetch(call).get("idle") is True  # not offered again
        assert rf.read_text().startswith("[INTERACTIVE TRANSPORT ERROR]")
        assert "3 dispatch attempts" in rf.read_text()
        assert (state / "responses" / "abc.out.done").exists()
        assert json.loads(tf.read_text())["status"] == "failed"
    finally:
        p.stdin.close()
        p.terminate()
        p.wait(timeout=5)


# --------------------------------------------------------------------------- #
# Launch hygiene (stale-queue purge + owner.pid)
# --------------------------------------------------------------------------- #
def test_launch_purges_stale_queue_and_writes_owner_pid(tmp_path, monkeypatch):
    """A crashed prior run leaves pending task files behind; _launch must
    purge the queue dirs so the new driver cannot replay an obsolete brief,
    and must record this process as the owner for the bridge's orphan guard."""
    cfg = {"compact_db": str(tmp_path / "data" / "sessions.db"),
           "working_directory": str(tmp_path)}
    state_dir = it._state_dir(cfg)
    for sub in ("requests", "prompts", "responses"):
        (state_dir / sub).mkdir(parents=True, exist_ok=True)
    (state_dir / "requests" / "old.task.json").write_text(
        json.dumps({"turn_id": "old", "status": "pending"}))
    (state_dir / "prompts" / "old.txt").write_text("obsolete brief")
    (state_dir / "responses" / "old.out").write_text("late write")

    monkeypatch.setattr(it, "_have_tmux", lambda: True)
    monkeypatch.setattr(
        it, "_tmux",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(it, "_await_ready", lambda *a, **k: None)

    it._launch(state_dir, cfg)
    assert not list((state_dir / "requests").iterdir())
    assert not list((state_dir / "prompts").iterdir())
    assert not list((state_dir / "responses").iterdir())
    assert (state_dir / "owner.pid").read_text() == str(os.getpid())


def test_seed_prompt_mentions_model_passthrough():
    assert "model" in it._seed_prompt()


# --------------------------------------------------------------------------- #
# Readiness / startup dialogs
# --------------------------------------------------------------------------- #
def _await_ready_with_screens(monkeypatch, screens, scoped):
    """Run _await_ready against a scripted pane-capture sequence; returns the
    tmux send-keys calls made."""
    seq = list(screens)
    state = {"scr": seq[-1]}

    def fake_capture(name):
        state["scr"] = seq.pop(0) if seq else state["scr"]
        return state["scr"]

    sent = []

    def fake_tmux(*args, check=False):
        sent.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(it, "_capture", fake_capture)
    monkeypatch.setattr(it, "_session_alive", lambda name: True)
    monkeypatch.setattr(it, "_tmux", fake_tmux)
    monkeypatch.setattr(it.time, "sleep", lambda s: None)
    it._await_ready("sess", scoped, timeout=30)
    return [a for a in sent if a[0] == "send-keys"]


def test_await_ready_accepts_bypass_dialog_in_skip_mode(monkeypatch):
    """Skip mode shows a one-time first-run 'Bypass Permissions mode' dialog
    on a fresh account/machine; _await_ready must accept it (2 + Enter)
    instead of wedging until the readiness timeout."""
    dialog = ("WARNING: Claude Code running in Bypass Permissions mode\n"
              "By proceeding, you accept all responsibility ...\n"
              "  1. No, exit\n> 2. Yes, I accept")
    keys = _await_ready_with_screens(
        monkeypatch, [dialog, "? for shortcuts"], scoped=False)
    assert ("send-keys", "-t", "sess", "2") in keys
    assert ("send-keys", "-t", "sess", "Enter") in keys


def test_await_ready_skip_mode_no_dialog(monkeypatch):
    """When the bypass dialog never appears (already accepted), readiness
    must not wait on it."""
    keys = _await_ready_with_screens(
        monkeypatch, ["? for shortcuts"], scoped=False)
    assert keys == []


def test_await_ready_scoped_trust_dialog_still_handled(monkeypatch):
    keys = _await_ready_with_screens(
        monkeypatch,
        ["Do you trust this folder?\n> 1. Yes", "? for shortcuts"],
        scoped=True)
    assert ("send-keys", "-t", "sess", "1") in keys


# --------------------------------------------------------------------------- #
# Stop hook (subprocess; env guard behavior)
# --------------------------------------------------------------------------- #
_STOP_HOOK = REPO / "long_exposure" / "interactive_stop_hook.py"


def _run_stop_hook(env):
    return subprocess.run(
        [sys.executable, str(_STOP_HOOK)], input="{}",
        capture_output=True, text=True, env=env, timeout=15,
    )


def test_stop_hook_allows_stop_without_env(tmp_path):
    """No LONG_EXPOSURE_INTERACTIVE_DIR => allow the stop (no block output).
    Guards against the inverted Path('') truthiness bug."""
    env = dict(os.environ)
    env.pop("LONG_EXPOSURE_INTERACTIVE_DIR", None)
    r = _run_stop_hook(env)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_stop_hook_blocks_while_run_live(tmp_path):
    env = dict(os.environ)
    env["LONG_EXPOSURE_INTERACTIVE_DIR"] = str(tmp_path)
    r = _run_stop_hook(env)
    assert r.returncode == 0
    assert json.loads(r.stdout)["decision"] == "block"


def test_stop_hook_allows_stop_on_shutdown_sentinel(tmp_path):
    (tmp_path / "shutdown").write_text("1")
    env = dict(os.environ)
    env["LONG_EXPOSURE_INTERACTIVE_DIR"] = str(tmp_path)
    r = _run_stop_hook(env)
    assert r.returncode == 0
    assert r.stdout.strip() == ""
