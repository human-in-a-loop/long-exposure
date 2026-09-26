"""The stdin/stdout protocol shared by Claude Code and Codex hooks.

Kept deliberately small and dependency-free. A hook is a fresh subprocess
spawned by the vendor CLI on each matching event, so its import cost is paid
every time — `Stop` fires once per turn, `PreCompact`/`PostCompact` only when
the CLI compacts, but the same module is the one a tool-scoped hook would
import if one is ever added.
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

# Both remaining hooks are non-blocking, so exit 0 is the only code either
# of them returns: the JSON on stdout carries whatever opinion they have.
# (The vendors also define exit 2 as "block, reason on stderr". Nothing here
# uses it; the `PreToolUse` fence that did was removed.)
EXIT_OK = 0


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


def continue_turn(reason: str) -> int:
    """For Stop / SubagentStop: keep the turn going instead of ending it.

    `{"decision": "block"}` means "do not stop" on both Claude and Codex —
    counter-intuitive, but it is the documented contract on both.
    """
    _emit({"decision": "block", "reason": reason})
    return EXIT_OK


def harness_turn() -> bool:
    """True iff this hook was invoked inside a long-exposure agent turn."""
    return os.environ.get(ENV_ACTIVE, "").strip().lower() in (
        "1", "true", "yes", "on",
    )
