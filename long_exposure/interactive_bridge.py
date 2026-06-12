#!/usr/bin/env python3
"""MCP stdio bridge for long-exposure's opt-in interactive Claude transport.

Launched as a subprocess by the persistent interactive Claude session via
`--mcp-config`. Exposes a single long-poll tool, ``fetch_next_task``, which
hands the driver the next agent turn enqueued by ``interactive_transport``.

Control inversion: the interactive session PULLS work. The Python side
(``interactive_transport.run_turn``) writes a task file and blocks until the
subagent writes the response file; this server only serves the queue, so it
holds no orchestration state of its own.

Why no ``submit_result`` tool: the worker subagent writes its output directly
to the response file (Write tool) plus a ``.done`` marker, and Python polls for
the marker. That keeps the contract independent of whether subagents can call
MCP tools, and avoids JSON-escaping a large deliverable through a tool argument.

Protocol: JSON-RPC 2.0 over stdio, newline-delimited framing (MCP stdio spec,
revision 2024-11-05+), mirroring ``mcp_search_server.py``. See
``docs/gaps_interactive_mode.md``.

State directory comes from ``LONG_EXPOSURE_INTERACTIVE_DIR``. Layout:
    <dir>/requests/<turn_id>.task.json   # {turn_id, status, prompt_file, response_file, model}
    <dir>/responses/<turn_id>.out        # written by the worker subagent
    <dir>/responses/<turn_id>.out.done   # completion marker
    <dir>/shutdown                       # sentinel: drain and stop
    <dir>/owner.pid                      # orchestrator PID; gone => stop
"""
import json
import os
import sys
import time
from pathlib import Path


def _state_dir() -> Path:
    d = os.environ.get("LONG_EXPOSURE_INTERACTIVE_DIR", "")
    if not d:
        # Fail loud-but-safe: an unconfigured bridge serves an empty queue.
        d = "/tmp/long-exposure-interactive-unset"
    return Path(d)


STATE = _state_dir()
REQ = STATE / "requests"
LOG = STATE / "bridge.log"
# Bounded server-side wait so the driver re-polls instead of blocking forever
# (keeps the model turn from appearing wedged inside one tool call).
POLL_WINDOW = float(os.environ.get("LONG_EXPOSURE_INTERACTIVE_FETCH_WINDOW", "30"))
# A dispatched task whose worker never completed (Task-tool failure on the
# driver side) is offered again after this long, at most _MAX_REDISPATCH times.
STALE_DISPATCH = 3 * POLL_WINDOW
_MAX_REDISPATCH = 2
# Prefix written into the response file when a task exhausts the redispatch
# cap (keep in sync with interactive_transport._ABANDON_SENTINEL, which turns
# it into a prompt ClaudeCliError instead of parseable output).
_ABANDON_SENTINEL = "[INTERACTIVE TRANSPORT ERROR]"


def log(msg: str) -> None:
    try:
        with LOG.open("a") as f:
            f.write(f"{time.time():.0f} {msg}\n")
    except OSError:
        pass


def read_message():
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    return json.loads(line)


def write_message(msg) -> None:
    body = json.dumps(msg, separators=(",", ":"))
    sys.stdout.buffer.write(body.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()


TOOLS = [
    {
        "name": "fetch_next_task",
        "description": (
            "Fetch the next long-exposure agent task. Returns a JSON object. "
            "If it has done=true, reply DONE and stop. If idle=true, call this "
            "tool again. If task=true, it includes turn_id, prompt_file, "
            "response_file, and model: spawn ONE general-purpose subagent (Task "
            "tool) instructed to read prompt_file, perform the brief it "
            "contains, then write its complete final output to response_file "
            "and finally create response_file + '.done'. After the subagent "
            "returns, call fetch_next_task again."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    }
]


def _owner_alive() -> bool:
    """False only when owner.pid names a process that no longer exists.

    Missing/unparseable owner.pid => assume alive (don't break manual smoke
    runs that never wrote one).
    """
    try:
        pid = int((STATE / "owner.pid").read_text().strip())
    except (OSError, ValueError):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass  # e.g. EPERM: process exists but is not ours
    return True


def _next_pending():
    if not REQ.exists():
        return None
    try:
        files = sorted(REQ.glob("*.task.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    for p in files:
        try:
            t = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        status = t.get("status")
        if status == "dispatched":
            # Re-offer a dispatched task whose worker never completed, with a
            # redispatch cap so a poison task cannot loop forever.
            age = time.time() - float(t.get("dispatched_at") or 0)
            n = int(t.get("redispatches") or 0)
            if age > STALE_DISPATCH:
                if n < _MAX_REDISPATCH:
                    t["status"] = "pending"
                    t["redispatches"] = n + 1
                    try:
                        p.write_text(json.dumps(t))
                    except OSError:
                        continue
                    log(f"redispatch {t.get('turn_id')} (n={n + 1})")
                    status = "pending"
                else:
                    # Cap exhausted: fail the task through the normal
                    # completion channel so the blocked run_turn returns
                    # promptly instead of waiting out its full turn timeout.
                    _abandon(p, t, n)
                    continue
        if status == "pending":
            return p, t
    return None


def _abandon(p: Path, t: dict, n: int) -> None:
    """Write the error sentinel response + .done marker; mark the task failed."""
    sentinel = (
        f"{_ABANDON_SENTINEL} task abandoned after {n + 1} dispatch "
        "attempts — driver could not complete it"
    )
    rf = str(t.get("response_file") or "")
    try:
        Path(rf).write_text(sentinel)
        Path(rf + ".done").write_text("done")
    except OSError:
        return  # retried on the next poll
    t["status"] = "failed"
    try:
        p.write_text(json.dumps(t))
    except OSError:
        pass
    log(f"abandon {t.get('turn_id')} after {n + 1} dispatches")


def fetch_next_task() -> dict:
    deadline = time.time() + POLL_WINDOW
    while time.time() < deadline:
        if (STATE / "shutdown").exists():
            return {"done": True}
        # Orphan guard: if the orchestrator that owns this run is gone (hard
        # kill — atexit never wrote the shutdown sentinel), stop the driver
        # instead of burning subscription tokens forever.
        if not _owner_alive():
            log("owner pid gone -> done")
            return {"done": True}
        hit = _next_pending()
        if hit:
            p, t = hit
            t["status"] = "dispatched"
            t["dispatched_at"] = time.time()
            try:
                p.write_text(json.dumps(t))
            except OSError:
                pass
            log(f"dispatch {t['turn_id']}")
            return {
                "task": True,
                "turn_id": t["turn_id"],
                "prompt_file": t["prompt_file"],
                "response_file": t["response_file"],
                "model": t.get("model", ""),
            }
        time.sleep(0.5)
    return {"idle": True}


def main() -> None:
    log("=== bridge start ===")
    while True:
        try:
            msg = read_message()
        except json.JSONDecodeError:
            continue  # tolerate a malformed line; keep serving
        if msg is None:
            break
        method = msg.get("method", "")
        mid = msg.get("id")
        if method == "initialize":
            write_message({
                "jsonrpc": "2.0", "id": mid,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "le-interactive-bridge", "version": "1.0.0"},
                },
            })
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            write_message({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            try:
                res = fetch_next_task()
            except Exception as e:  # never crash the bridge mid-run
                res = {"idle": True, "error": str(e)[:200]}
            write_message({
                "jsonrpc": "2.0", "id": mid,
                "result": {"content": [{"type": "text", "text": json.dumps(res)}]},
            })
        elif mid is not None:
            write_message({
                "jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            })
    log("=== bridge end ===")


if __name__ == "__main__":
    main()
