"""Campaign anti-pattern prompt block from invalidated ledger events."""

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape


MAX_ENTRIES = 5
MAX_RATIONALE_CHARS = 200
ELIGIBLE_CONFIDENCE = {"high", "medium"}


def _safe_read(ledger_path: Path) -> list[dict]:
    if not ledger_path.exists():
        return []
    events: list[dict] = []
    try:
        lines = ledger_path.read_text().splitlines()
    except OSError:
        return []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _select(events: list[dict], max_entries: int = MAX_ENTRIES) -> list[dict]:
    by_mid: dict[str, list[dict]] = {}
    for event in events:
        mid = event.get("milestone_id")
        if mid:
            by_mid.setdefault(str(mid), []).append(event)
    for rows in by_mid.values():
        rows.sort(key=lambda event: str(event.get("ts") or ""))

    selected: list[dict] = []
    for rows in by_mid.values():
        latest = rows[-1]
        if latest.get("status") != "invalidated":
            continue
        confidence = latest.get("confidence") or {}
        if not isinstance(confidence, dict):
            continue
        if confidence.get("level") not in ELIGIBLE_CONFIDENCE:
            continue
        if not str(confidence.get("rationale") or "").strip():
            continue
        selected.append(latest)
    selected.sort(key=lambda event: str(event.get("ts") or ""), reverse=True)
    return selected[:max_entries]


def _render(entries: list[dict], max_rationale_chars: int = MAX_RATIONALE_CHARS) -> str:
    if not entries:
        return ""
    lines = [
        f'<campaign_anti_patterns count="{len(entries)}" '
        'note="Confirmed invalidated approaches from this campaign. Avoid '
        'replicating. If a re-attempt is needed, justify why this time differs.">'
    ]
    for event in entries:
        confidence = event.get("confidence") or {}
        rationale = str(confidence.get("rationale") or "").strip()
        if len(rationale) > max_rationale_chars:
            rationale = rationale[: max(0, max_rationale_chars - 3)] + "..."
        lines.append(
            f'  <anti_pattern milestone="{escape(str(event.get("milestone_id") or "?"))}" '
            f'cycle="{escape(str(event.get("cycle") or "?"))}" '
            f'confidence="{escape(str(confidence.get("level") or "?"))}">'
        )
        lines.append(f"    {escape(rationale)}")
        lines.append("  </anti_pattern>")
    lines.append("</campaign_anti_patterns>")
    return "\n".join(lines)


def build_block(
    workspace: Path,
    *,
    max_entries: int = MAX_ENTRIES,
    max_rationale_chars: int = MAX_RATIONALE_CHARS,
) -> str:
    try:
        events = _safe_read(Path(workspace) / "promise_ledger.jsonl")
        return _render(
            _select(events, max_entries=max_entries),
            max_rationale_chars=max_rationale_chars,
        )
    except Exception:
        return ""
