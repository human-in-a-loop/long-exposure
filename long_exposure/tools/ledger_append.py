#!/usr/bin/env python3
"""ledger_append — clone-safe ledger event append.

Routes writes to the per-clone shadow ledger when ``AGENT_FORK_ID`` is set;
otherwise writes directly to the workspace main ledger. Used by agents
(via Bash) and Python helpers in clones to avoid concurrent-append
contention on the workspace's main ``promise_ledger.jsonl``.

Usage:
    python3 -m long_exposure.tools.ledger_append --workspace /path/to/ws \\
        --event '{"event_id":"…","ts":"…","run_id":"…",...}'

The harness merges per-clone shadow ledgers into the main ledger after
the fan-out barrier collapses (Plan 1 §6).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from long_exposure.workspace_bootstrap import append_ledger_event


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=".", help="Workspace root.")
    parser.add_argument(
        "--event",
        required=True,
        help="JSON-encoded event object (single line).",
    )
    args = parser.parse_args(argv)

    try:
        event = json.loads(args.event)
    except json.JSONDecodeError as e:
        print(f"ledger_append: invalid JSON event: {e}", file=sys.stderr)
        return 2
    if not isinstance(event, dict):
        print("ledger_append: event must be a JSON object", file=sys.stderr)
        return 2

    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        print(f"ledger_append: not a directory: {workspace}", file=sys.stderr)
        return 2

    append_ledger_event(workspace, event)
    print("ledger_append: event appended.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
