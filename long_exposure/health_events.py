"""Centralised off-nominal events log.

Records silent-fallback events to `<data_dir>/health_events.jsonl` so the
operator can surface what went wrong silently with a single
`tail -n 50 health_events.jsonl`, without trawling the full log.

Strict invariants — load-bearing:

  - Every public function catches all exceptions internally; NEVER raises.
  - File writes are best-effort (any OSError → silent no-op).
  - Append-only; no reads, no rotation, no deletion.
  - Control flow MUST NEVER branch on this log.

The log is intentionally not part of the control plane — it's pure
observability. If the file is corrupt, missing, or unwritable, the run
continues identically.

Sites that should record events (each name is a stable `kind` string):
  - `input_unavailable`         conductor input fallback ([UNAVAILABLE])
  - `topic_extract_failed`      _extract_topic regex returned None
  - `verdict_extract_failed`    _extract_verdict regex returned default
  - `compaction_empty_summary`  model returned empty/whitespace summary
  - `compaction_xml_invalid`    XML parse failed; about to retry
  - `compaction_xml_unrecoverable`  all retries exhausted; stored as-is
  - `file_gate_rescue`          report stage rescued from [OUTPUT] block
  - `pdf_render_failed`         pandoc/tectonic returned non-zero
  - `account_state_save_failed` OSError on _save_account_state
  - `pool_slot_repair`          slot leak detected and reclaimed
  - `fork_metadata_unparsed`    post-merge brief regex fell back to fid=unknown
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _resolve_log_path(data_dir: Path | str | None = None) -> Path | None:
    """Resolve the log path. Honours `AGENT_INSTANCE_DIR` for clones so each
    clone has its own log; falls back to the explicitly-passed `data_dir`.

    Returns None if no usable directory is available — caller skips logging.
    """
    instance_dir = os.environ.get("AGENT_INSTANCE_DIR", "").strip()
    if instance_dir:
        return Path(instance_dir) / "health_events.jsonl"
    if data_dir:
        return Path(data_dir) / "health_events.jsonl"
    return None


def append_event(
    kind: str,
    detail: str = "",
    *,
    cycle: int | None = None,
    agent: str | None = None,
    data_dir: Path | str | None = None,
) -> None:
    """Append one JSON record to the off-nominal events log.

    Best-effort: never raises, never blocks meaningfully. Failure to append
    is itself silent — we don't want a logging fault to mask the underlying
    event.

    `kind` is a stable short string (see module docstring for the list).
    `detail` is a one-line free-form string; capped at 1000 chars to keep
    the log scannable.
    """
    try:
        path = _resolve_log_path(data_dir)
        if path is None:
            return
        record: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": str(kind),
        }
        if cycle is not None:
            record["cycle"] = int(cycle)
        if agent:
            record["agent"] = str(agent)
        if detail:
            record["detail"] = str(detail)[:1000]
        line = json.dumps(record, default=str) + "\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        # O_APPEND is atomic for small writes on Linux; clones can append
        # concurrently without explicit locking.
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        # Truly never raise from observability code.
        pass
