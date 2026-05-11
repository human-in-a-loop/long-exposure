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
import uuid
from pathlib import Path

from long_exposure.tools.promise_check import (
    ASSESSORS,
    CONFIDENCE_LEVELS,
    REQUIRED_EVENT_FIELDS,
    STATUS_VALUES,
)
from long_exposure.workspace_bootstrap import append_ledger_event


def _valid_uuid(value: object) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError, AttributeError):
        return False


def _validate_event(event: dict) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_EVENT_FIELDS:
        if field not in event:
            errors.append(f"missing required field {field!r}")

    if "event_id" in event and not _valid_uuid(event.get("event_id")):
        errors.append("event_id is not a valid UUID")

    status = event.get("status")
    if status is not None and status not in STATUS_VALUES:
        errors.append(f"status {status!r} is not in the unified vocabulary")

    confidence = event.get("confidence")
    if confidence is not None:
        if not isinstance(confidence, dict):
            errors.append("confidence must be an object")
        else:
            if confidence.get("level") not in CONFIDENCE_LEVELS:
                errors.append("confidence.level is not recognized")
            if not str(confidence.get("rationale") or "").strip():
                errors.append("confidence.rationale is empty")
            if confidence.get("assessor") not in ASSESSORS:
                errors.append("confidence.assessor is not recognized")

    artifacts = event.get("artifacts")
    if artifacts is not None and (
        not isinstance(artifacts, list)
        or not all(isinstance(item, str) for item in artifacts)
    ):
        errors.append("artifacts must be a list of strings")

    return errors


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
    validation_errors = _validate_event(event)
    if validation_errors:
        print("ledger_append: invalid event:", file=sys.stderr)
        for error in validation_errors:
            print(f"  - {error}", file=sys.stderr)
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
