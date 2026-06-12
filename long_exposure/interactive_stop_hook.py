#!/usr/bin/env python3
"""Stop hook for the interactive transport's driver session.

Fires when the driver finishes a turn. While the run is live and turns may
still arrive, it returns decision=block to force the driver to keep looping
(call fetch_next_task again) — this is what makes an unattended interactive
session keep working without a human typing.

Stops looping when the ``shutdown`` sentinel exists (graceful teardown) or a
high safety cap is reached (backstop against a wedged driver). Reads its state
directory from ``LONG_EXPOSURE_INTERACTIVE_DIR``. Best-effort: any error allows
the stop (never wedges the session on a hook bug).
"""
import json
import os
import sys
from pathlib import Path


def main() -> None:
    try:
        json.load(sys.stdin)  # consume hook payload (unused)
    except Exception:
        pass

    state_env = os.environ.get("LONG_EXPOSURE_INTERACTIVE_DIR", "")
    if not state_env:
        return  # allow stop: no bridge configured for this session
    state = Path(state_env)
    if (state / "shutdown").exists():
        return  # allow stop

    # Safety backstop: a high cap so a genuinely stuck driver can eventually
    # exit instead of spinning forever. The transport recycles the session well
    # before this under normal operation.
    cap = int(os.environ.get("LONG_EXPOSURE_INTERACTIVE_STOP_CAP", "5000"))
    counter = state / ".stop_hook_n"
    try:
        n = int(counter.read_text()) if counter.exists() else 0
    except (OSError, ValueError):
        n = 0
    if n >= cap:
        return  # allow stop

    try:
        counter.write_text(str(n + 1))
    except OSError:
        pass
    print(json.dumps({
        "decision": "block",
        "reason": (
            "Do not stop — the long-exposure run is still active. Call the "
            "mcp__le-interactive-bridge__fetch_next_task tool now. If it returns "
            "a task, handle it with a subagent as instructed; if idle, call it "
            "again; only stop when it returns done=true."
        ),
    }))


if __name__ == "__main__":
    main()
