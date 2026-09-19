"""Run memoir: the L1 tier of narrative memory.

One agent-owned file, ``MEMOIR.md``, in the workspace. The auditor edits it
with minimal changes at the end of each cycle; the researcher and worker
receive its contents as the ``run_memory`` input at the start of the next.
Every version the auditor changes is archived under ``memoir/history/`` and
as a ``record_type='memoir'`` row in ``sessions.db``, so it is searchable
but never injected. Design and the decisions behind it:
``docs/tiered-memory-plan.md``.

Everything here is best-effort. A missing, unreadable or over-long memoir
degrades the injected input; it never raises into the cycle loop.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from long_exposure import health_events, paths
from long_exposure.stage_io import atomic_write_text

DEFAULT_MAX_TOKENS = 3000
INPUT_NAME = "run_memory"        # researcher + worker: contents, in-window
PATH_INPUT_NAME = "memoir_path"  # auditor: the path only
RECORD_TYPE = "memoir"
_TRUNCATED_MARKER = "\n\n[memoir over cap — truncated here; auditor must trim]"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _cfg(config: dict | None) -> dict:
    block = (config or {}).get("memoir")
    return block if isinstance(block, dict) else {}


def enabled(config: dict | None) -> bool:
    """`memoir.enabled` — defaults to True when the block is absent."""
    return bool(_cfg(config).get("enabled", True))


def max_tokens(config: dict | None) -> int:
    try:
        value = int(_cfg(config).get("max_tokens", DEFAULT_MAX_TOKENS))
    except (TypeError, ValueError):
        value = DEFAULT_MAX_TOKENS
    return value if value > 0 else DEFAULT_MAX_TOKENS


def strip_inputs(agents: dict) -> None:
    """Remove the memoir inputs from every agent definition (disabled mode).

    Without this a score that declares ``run_memory`` would render
    ``[UNAVAILABLE: run_memory]`` on every call and log a health event each
    time. Stripping at load keeps the prompt identical to a pre-memoir run.
    """
    for agent_def in (agents or {}).values():
        inputs = agent_def.get("inputs")
        if isinstance(inputs, list):
            agent_def["inputs"] = [
                name for name in inputs if name not in (INPUT_NAME, PATH_INPUT_NAME)
            ]


# ---------------------------------------------------------------------------
# File
# ---------------------------------------------------------------------------


def seed_if_missing(workspace: Path) -> bool:
    """Write the skeleton if MEMOIR.md does not exist. Returns True if written.

    Lazy rather than bootstrap-only so a workspace that predates the feature
    gets a memoir on its next resumed cycle.
    """
    target = paths.memoir_path(workspace)
    if target.exists():
        return False
    from long_exposure.workspace_bootstrap import render_template
    body = render_template(
        "memoir_template.md",
        created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    atomic_write_text(target, body)
    return True


def snapshot(workspace: Path) -> str | None:
    """The live memoir's content, or None if absent — taken just before the
    auditor's turn so `archive_if_changed` can compare content afterwards.

    Content, not `(size, mtime_ns)`: a signature answers "was the file
    written to", which archives a duplicate on an identical rewrite and
    depends on the kernel's inode-timestamp granularity. The file is at
    most a few KB, so comparing bytes is both cheaper to reason about and
    exact.
    """
    try:
        return paths.memoir_path(workspace).read_text()
    except OSError:
        return None


def _mtime_iso(path: Path) -> str:
    try:
        ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return ts.isoformat(timespec="seconds")
    except OSError:
        return "unknown"


def read_for_injection(workspace: Path, config: dict | None) -> str:
    """The `run_memory` input value: a one-line header plus the memoir.

    The header is added here, not stored in the file, so the live file stays
    purely agent-owned. Content over `memoir.max_tokens` is cut at the last
    paragraph boundary before the cap (see `_truncate`), with a marker and a
    `memoir_over_cap` health event — the agent still gets the head, and the
    prompt stays bounded whatever the auditor did.
    """
    target = paths.memoir_path(workspace)
    try:
        seed_if_missing(workspace)
        text = target.read_text()
    except OSError as exc:
        return f"[Run memoir unavailable: {exc}]"
    header = (
        f"[Run memoir — advisory narrative back-reference; last updated "
        f"{_mtime_iso(target)}. Older versions: memoir/history/ and "
        f"search_sessions (record_type: memoir). Where it conflicts with "
        f"plan_of_record.md or the promise ledger, they win.]"
    )
    cap = max_tokens(config)
    # Same chars/4 estimate as orchestrator.estimate_tokens, kept local so
    # this module has no dependency on the orchestrator.
    if len(text) // 4 > cap:
        text = _truncate(text, cap * 4) + _TRUNCATED_MARKER
        health_events.append_event(
            "memoir_over_cap",
            detail=f"memoir at {target} exceeds {cap} tokens; injected truncated",
        )
    return f"{header}\n\n{text}"


def _truncate(text: str, limit: int) -> str:
    """Cut `text` to at most `limit` characters at a paragraph boundary.

    Backs up to the last blank line before `limit` so the agent never sees a
    sentence sliced mid-word. If the only boundary in the head would throw
    away more than half the allowed budget (a memoir written as one giant
    paragraph), fall back to a hard cut at `limit` — bounded prompt first,
    tidy edge second.
    """
    if len(text) <= limit:
        return text
    cut = text.rfind("\n\n", 0, limit)
    if cut < limit // 2:
        cut = limit
    return text[:cut].rstrip()


def path_input_value(workspace: Path, *, is_clone: bool) -> str:
    """The `memoir_path` input value for the auditor.

    A fan-out clone shares the workspace, so it reads the memoir for free —
    but three clone auditors editing one file is a race. Clones get a
    read-only note; only the root auditor gets a bare path to edit.
    """
    target = paths.memoir_path(workspace)
    if is_clone:
        return (
            f"[Read-only in a fan-out branch — do NOT edit: {target}. "
            f"The root auditor folds branch results into the memoir after merge.]"
        )
    return str(target)


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


def _archive_name(cycle: int, now: datetime) -> str:
    # Six digits keeps lexical order == cycle order to 999,999 cycles,
    # which is unbounded in practice even with max_cycles: null.
    return f"cycle-{int(cycle):06d}_{now.strftime('%Y-%m-%dT%H%M%SZ')}.md"


def archive_if_changed(
    workspace: Path,
    cycle: int,
    before: str | None,
    conn=None,
) -> Path | None:
    """Archive the live memoir if its content changed during the auditor's turn.

    `before` is `snapshot()` taken just before the auditor ran. On change:
    copy to `memoir/history/cycle-NNNNNN_<ts>.md` and, when `conn` is given,
    store a `record_type='memoir'` row so `search_sessions` finds it. An
    unchanged file — including one rewritten with identical content —
    leaves no trace; that is the normal outcome of minimal-edit discipline,
    not an event. Returns the archive path or None.
    """
    target = paths.memoir_path(workspace)
    try:
        text = target.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        health_events.append_event("memoir_archive_failed", detail=f"read: {exc}")
        return None
    if text == before:
        return None

    now = datetime.now(timezone.utc)
    archive = paths.memoir_history_dir(workspace) / _archive_name(cycle, now)
    try:
        atomic_write_text(archive, text)
    except OSError as exc:
        health_events.append_event("memoir_archive_failed", detail=f"write: {exc}")
        return None

    if conn is not None:
        _store_row(conn, cycle, now, text)
    return archive


def _store_row(conn, cycle: int, now: datetime, text: str) -> None:
    """One sessions.db row per archived version, the way lemmas.py stores
    lemma rows. `summary_xml` is FTS-indexed, so the memoir body is what
    `search_sessions` matches against."""
    from auto_compact.db import store_session
    summary_xml = (
        f'<memoir cycle="{int(cycle)}" archived="{now.isoformat(timespec="seconds")}">\n'
        f"{escape(text)}\n"
        f"</memoir>"
    )
    try:
        store_session(
            conn,
            session_id=str(uuid.uuid4()),
            parent_id=None,
            depth=0,
            timestamp=now.isoformat(),
            summary_xml=summary_xml,
            record_type=RECORD_TYPE,
            topic=RECORD_TYPE,
            subtopic=f"cycle-{int(cycle):06d}",  # same spelling as the archive filename
            keywords=None,
            fork_id=None,
        )
    except Exception as exc:  # never crash the cycle
        health_events.append_event("memoir_store_failed", detail=repr(exc))
