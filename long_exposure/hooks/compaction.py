"""PreCompact / PostCompact: make the provider's own compaction visible.

## Why this exists

The harness runs its own compaction at `compact_threshold` and knows exactly
when it did. The CLI *also* compacts internally, on its own schedule, and
the harness cannot currently see that at all.

Two observations from the 2026-09-25 live run say this matters. The run's
only health event was `compaction_xml_invalid`. And the turn that crossed the
spend cap was `worker#compaction` at **$3.45** — provider-side compaction is
both the most expensive single call in the cycle and the one the harness has
no record of.

This hook is pure instrumentation: one `health_events` row per provider
compaction, no behaviour change, nothing blocked. It is the cheapest of the
three and the only one with no failure mode worse than a missing log line.

Both Claude and Codex fire `PreCompact` and `PostCompact` with a
`trigger_reason` / matcher of `manual` or `auto`. Gemini has neither, so
under Gemini the harness keeps today's blind spot.
"""

from __future__ import annotations

import os
import sys

from long_exposure.hooks import _io

ENV_STATE_DIR = "LONG_EXPOSURE_HOOK_STATE_DIR"
ENV_AGENT = "LONG_EXPOSURE_HOOK_AGENT"
ENV_CYCLE = "LONG_EXPOSURE_HOOK_CYCLE"
ENV_DISABLE = "LONG_EXPOSURE_COMPACTION_OFF"

KIND_PRE = "provider_compaction_started"
KIND_POST = "provider_compaction_finished"


def _cycle() -> int | None:
    try:
        return int(os.environ.get(ENV_CYCLE, "").strip())
    except (TypeError, ValueError):
        return None


def record(payload: dict) -> str:
    """Append one health event for this compaction. Returns the kind used."""
    event = _io.event_name(payload, "PreCompact")
    kind = KIND_POST if "post" in event.lower() else KIND_PRE
    reason = str(payload.get("trigger_reason") or payload.get("trigger") or "unknown")
    detail = f"event={event} trigger={reason}"
    session = str(payload.get("session_id") or "")
    if session:
        detail += f" session={session[:8]}"
    try:
        from long_exposure import health_events

        health_events.append_event(
            kind,
            detail,
            cycle=_cycle(),
            agent=os.environ.get(ENV_AGENT, "").strip() or None,
            data_dir=os.environ.get(ENV_STATE_DIR, "").strip() or None,
        )
    except Exception:
        # Instrumentation must never be able to disturb a turn.
        pass
    return kind


def main(argv: list[str] | None = None) -> int:
    if os.environ.get(ENV_DISABLE, "").strip().lower() in ("1", "true", "yes", "on"):
        return _io.allow()
    record(_io.read_payload())
    return _io.allow()


if __name__ == "__main__":
    sys.exit(main())
