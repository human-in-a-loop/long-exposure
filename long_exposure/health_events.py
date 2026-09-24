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

Kinds emitted today (each name is a stable `kind` string; keep this list
in sync when adding a call site — it is the only index of what can appear
in the log):

  Agent turns and compaction
  - `input_unavailable`             conductor input fallback ([UNAVAILABLE])
  - `compaction_empty_summary`      model returned empty/whitespace summary
  - `compaction_xml_invalid`        XML well-formedness check failed
  - `compaction_xml_unrecoverable`  REPL retries exhausted; stored as-is
  - `checkpoint_empty_summary`      REPL checkpoint produced nothing
  - `checkpoint_xml_invalid`        REPL checkpoint XML malformed

  End-of-run stages
  - `file_gate_rescue`              stage rescued from the [OUTPUT] block
  - `file_gate_rescue_failed`       rescue write itself failed
  - `file_gate_rescue_refused`      rescue declined (too short / would shrink)
  - `pdf_render_failed`             pandoc/tectonic returned non-zero

  Run memoir (L1 narrative memory)
  - `memoir_over_cap`              a memoir exceeded memoir.max_tokens; injected truncated
  - `memoir_branches_over_cap`     collapsed branch memoirs exceeded the cap; truncated
  - `memoir_clone_write`           a clone wrote the ROOT memoir instead of its shadow
  - `memoir_archive_failed`        copying a changed memoir to memoir/history/ failed
  - `memoir_store_failed`          writing the memoir archive row to sessions.db failed

  Accounts, pool and rotation
  - `account_state_save_failed`     OSError on _save_account_state
  - `account_mismatch_drop`         clone session dropped (account changed)
  - `out_of_cycle_rotation`         rotation from an out-of-cycle agent
  - `planned_rotation`              24h planned primary rotation fired
  - `planned_rotation_skipped`      planned rotation skipped (too recent)
  - `reporter_skipped_cooling`      reporter skipped; pinned account cooling
  - `usage_recording_failed`        pool usage write failed
  - `pool_clone_bootstrap_skipped`  clone slot bootstrap skipped
  - `pool_clone_self_retag_missed`  clone slot retag did not apply
  - `codex_clone_session_drop`      Codex thread id not inherited by a clone
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


_CONFIGURED_DIR: Path | None = None


def configure(data_dir: Path | str | None) -> None:
    """Set the default log directory for this process.

    `run_exploration` calls this with the run's data dir so root runs write
    `health_events.jsonl` next to their state file. Without it, only fan-out
    clones logged anything: they are the only processes that get
    `AGENT_INSTANCE_DIR` in their environment, so every root-run event
    (silent fallbacks, rescues, retries) was dropped unless the operator
    exported that variable by hand.
    """
    global _CONFIGURED_DIR
    _CONFIGURED_DIR = Path(data_dir) if data_dir else None


def _resolve_log_path(data_dir: Path | str | None = None) -> Path | None:
    """Resolve the log path, most specific source first: the explicitly
    passed `data_dir`, then this process's `configure()` directory, then
    `AGENT_INSTANCE_DIR` (set for fan-out clones, and the only source for a
    standalone tool invocation that never called `configure`).

    Returns None if no usable directory is available — caller skips logging.
    """
    if data_dir:
        return Path(data_dir) / "health_events.jsonl"
    if _CONFIGURED_DIR is not None:
        return _CONFIGURED_DIR / "health_events.jsonl"
    instance_dir = os.environ.get("AGENT_INSTANCE_DIR", "").strip()
    if instance_dir:
        return Path(instance_dir) / "health_events.jsonl"
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
