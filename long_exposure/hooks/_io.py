"""The stdin/stdout protocol shared by Claude Code and Codex hooks.

Kept deliberately small and dependency-free: these run as subprocesses on
every matching tool call, so import cost is on the hot path.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# Set by the harness on every agent turn it spawns (orchestrator's
# _add_hook_env). Its presence is how a hook tells "I am inside a
# long-exposure agent turn" from "an operator is using this CLI for
# something else entirely".
#
# This gate is not cosmetic. A live test of the Stop envelope hook without
# it produced exactly the wrong behaviour: a bare `claude -p` turn that was
# never asked for an [OUTPUT:] block got nudged to emit one, and the model
# said so — "no output type was ever declared to me in this conversation,
# I'll use a generic label". Installed in an operator's home config, an
# ungated hook taxes every unrelated turn they take.
ENV_ACTIVE = "LONG_EXPOSURE_HOOK_ACTIVE"

# Exit codes. 0 = the JSON on stdout decides; 2 = block, reason on stderr.
EXIT_OK = 0
EXIT_BLOCK = 2


def read_payload() -> dict[str, Any]:
    """Parse the hook payload from stdin. Never raises.

    A malformed or empty payload returns `{}`, which every hook here treats
    as "no opinion" — a hook that crashed on bad input would turn a parsing
    quirk into a blocked tool call.
    """
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def tool_name(payload: dict) -> str:
    return str(payload.get("tool_name") or "")


def tool_input(payload: dict) -> dict:
    ti = payload.get("tool_input")
    return ti if isinstance(ti, dict) else {}


def event_name(payload: dict, default: str = "") -> str:
    return str(payload.get("hook_event_name") or default)


def _emit(obj: dict) -> None:
    try:
        json.dump(obj, sys.stdout)
        sys.stdout.write("\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def allow() -> int:
    """No opinion: say nothing and let the normal flow apply."""
    return EXIT_OK


def deny(event: str, reason: str) -> int:
    """Block a tool call, with the reason surfaced to the agent.

    Emits the JSON form rather than relying on exit 2, because the JSON
    carries `permissionDecisionReason` — the agent is told *why*, which is
    what lets it choose a different approach instead of retrying blindly.
    """
    _emit({
        "hookSpecificOutput": {
            "hookEventName": event or "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    })
    return EXIT_OK


def continue_turn(reason: str) -> int:
    """For Stop / SubagentStop: keep the turn going instead of ending it.

    `{"decision": "block"}` means "do not stop" on both Claude and Codex —
    counter-intuitive, but it is the documented contract on both.
    """
    _emit({"decision": "block", "reason": reason})
    return EXIT_OK


def add_context(event: str, text: str) -> int:
    """Hand the agent extra context without changing control flow."""
    _emit({
        "hookSpecificOutput": {
            "hookEventName": event or "SessionStart",
            "additionalContext": text,
        }
    })
    return EXIT_OK


def harness_turn() -> bool:
    """True iff this hook was invoked inside a long-exposure agent turn."""
    return os.environ.get(ENV_ACTIVE, "").strip().lower() in (
        "1", "true", "yes", "on",
    )
