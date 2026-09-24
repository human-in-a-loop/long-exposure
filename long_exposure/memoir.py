"""Run memoir: the L1 tier of narrative memory.

One agent-owned file, ``MEMOIR.md``, in the workspace. The auditor edits it
with minimal changes at the end of each cycle; the researcher and worker
receive its contents as the ``run_memory`` input at the start of the next.
Every version the auditor changes is archived under ``memoir/history/`` and
as a ``record_type='memoir'`` row in ``sessions.db``, so it is searchable
but never injected.

Fan-out gets per-clone **shadow memoirs**, mirroring the shadow-ledger
pattern (``workspace_bootstrap.resolve_ledger_path`` /
``concat_clone_ledgers``): a clone writes ``<instance_dir>/MEMOIR.md``,
never the root file, so N concurrent clone auditors cannot interleave
writes on one unlocked file. A clone reads the root memoir *and* its own
shadow; the shadow starts as a blank skeleton so it holds only
branch-local knowledge. After the barrier collapses, shadows newer than
the root memoir are handed to the root auditor as ``branch_memoirs`` and
folded in with its normal minimal-edit discipline — no extra LLM call and
no state field, because the files on disk carry the signal.

Design and the decisions behind it: ``docs/tiered-memory-plan.md``.

Everything here is best-effort. A missing, unreadable or over-long memoir
degrades the injected input; it never raises into the cycle loop.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from long_exposure import health_events, paths
from long_exposure.stage_io import atomic_write_text

DEFAULT_MAX_TOKENS = 3000
INPUT_NAME = "run_memory"            # researcher + worker: contents, in-window
PATH_INPUT_NAME = "memoir_path"      # auditor: the write path only
BRANCH_INPUT_NAME = "branch_memoirs"  # root auditor: collapsed branch shadows
RECORD_TYPE = "memoir"
MEMOIR_FILENAME = "MEMOIR.md"
_SKELETON_BODY: str | None = None  # lazy cache, see _skeleton_body()
_ALL_INPUTS = (INPUT_NAME, PATH_INPUT_NAME, BRANCH_INPUT_NAME)
_TRUNCATED_MARKER = "\n\n[memoir over cap — truncated here; auditor must trim]"
_NO_BRANCHES = "[No collapsed fan-out branches to fold this cycle.]"
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


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
                name for name in inputs if name not in _ALL_INPUTS
            ]


# ---------------------------------------------------------------------------
# File
# ---------------------------------------------------------------------------


def shadow_path() -> Path | None:
    """This clone's shadow memoir, or None when the caller is a root process.

    Detected exactly as `workspace_bootstrap.resolve_ledger_path` detects a
    clone — the AGENT_FORK_ID / AGENT_INSTANCE_DIR pair the fan-out conductor
    sets per clone. The shadow sits beside that clone's `merge_report.md` and
    `promise_ledger.jsonl` in its instance dir, which is under the ROOT
    INSTANCE DIR, not the workspace (`fanout._fork_dir`) — so shadows never
    touch the workspace and never reach a curated package.
    """
    if not os.environ.get("AGENT_FORK_ID"):
        return None
    instance_dir = os.environ.get("AGENT_INSTANCE_DIR", "").strip()
    if not instance_dir:
        return None
    return Path(instance_dir) / MEMOIR_FILENAME


def write_path(workspace: Path) -> Path:
    """The memoir this process may write: its shadow in a clone, else the root."""
    return shadow_path() or paths.memoir_path(workspace)


def _skeleton() -> str:
    from long_exposure.workspace_bootstrap import render_template
    return render_template(
        "memoir_template.md",
        created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def seed_if_missing(workspace: Path, target: Path | None = None) -> bool:
    """Write the skeleton if the target memoir does not exist.

    Lazy rather than bootstrap-only so a workspace that predates the feature
    gets a memoir on its next resumed cycle, and so a clone's shadow appears
    the first time that branch needs one. A clone's shadow starts BLANK, not
    as a copy of the root: branch-local-delta semantics, the same as the
    shadow ledger, which keeps the merge fold from re-presenting the root's
    own thesis back to it.
    """
    target = target or paths.memoir_path(workspace)
    if target.exists():
        return False
    atomic_write_text(target, _skeleton())
    return True


def snapshot(workspace: Path, target: Path | None = None) -> str | None:
    """A memoir's content, or None if absent — taken just before the auditor's
    turn so `archive_if_changed` can compare content afterwards, and before a
    fan-out spawn so the root-frozen invariant can be checked at collapse.

    Content, not `(size, mtime_ns)`: a signature answers "was the file
    written to", which archives a duplicate on an identical rewrite and
    depends on the kernel's inode-timestamp granularity. The file is at
    most a few KB, so comparing bytes is both cheaper to reason about and
    exact.
    """
    try:
        return (target or paths.memoir_path(workspace)).read_text()
    except OSError:
        return None


def _mtime_iso(path: Path) -> str:
    try:
        ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return ts.isoformat(timespec="seconds")
    except OSError:
        return "unknown"


def _capped(text: str, cap: int, label: str) -> str:
    """Cut `text` to the cap, at a paragraph boundary, logging once if it fires."""
    # Same chars/4 estimate as orchestrator.estimate_tokens, kept local so
    # this module has no dependency on the orchestrator.
    if len(text) // 4 <= cap:
        return text
    health_events.append_event(
        "memoir_over_cap",
        detail=f"{label} exceeds {cap} tokens; injected truncated",
    )
    return _truncate(text, cap * 4) + _TRUNCATED_MARKER


def read_for_injection(workspace: Path, config: dict | None) -> str:
    """The `run_memory` input value: a header plus the memoir the agent should
    read — and, in a fan-out branch, its own shadow as a second section.

    The header is added here, not stored in the file, so live files stay
    purely agent-owned. A branch reads BOTH: the root memoir for run-wide
    context (frozen for the fork's duration, since no root auditor runs
    while clones are out) and its shadow for branch-local progress. Each
    section is capped independently at `memoir.max_tokens`, so a branch
    prompt is bounded at 2x the cap in the worst case and neither section can
    starve the other.
    """
    root = paths.memoir_path(workspace)
    try:
        seed_if_missing(workspace, root)
        root_text = root.read_text()
    except OSError as exc:
        return f"[Run memoir unavailable: {exc}]"
    cap = max_tokens(config)
    header = (
        f"[Run memoir — advisory narrative back-reference; last updated "
        f"{_mtime_iso(root)}. Older versions: memoir/history/ and "
        f"search_sessions (record_type: memoir). Where it conflicts with "
        f"plan_of_record.md or the promise ledger, they win.]"
    )
    root_text = _capped(root_text, cap, f"memoir at {root}")

    shadow = shadow_path()
    if shadow is None:
        return f"{header}\n\n{root_text}"

    # Fan-out branch: root memoir (read-only here) + this branch's own shadow.
    try:
        seed_if_missing(workspace, shadow)
        shadow_text = _capped(shadow.read_text(), cap, f"branch memoir at {shadow}")
    except OSError as exc:
        shadow_text = f"[Branch memoir unavailable: {exc}]"
    return (
        f"{header}\n\n"
        f"== RUN MEMOIR (root, read-only in this branch) ==\n\n{root_text}\n\n"
        f"== THIS BRANCH'S MEMOIR (branch-local; your auditor maintains it) ==\n\n"
        f"{shadow_text}"
    )


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


def path_input_value(workspace: Path) -> str:
    """The `memoir_path` input value for the auditor: the file it may write.

    In a fan-out branch that is the branch's own shadow, so the auditor's
    role guidance ("make minimal edits to the memoir at this path") is true
    in every process — the earlier design handed a clone the ROOT path with a
    do-not-edit note, contradicting its own role text while leaving it the
    means to act on it. One writer per file, by construction.
    """
    target = write_path(workspace)
    seed_if_missing(workspace, target)
    if shadow_path() is not None:
        return (
            f"{target}\n"
            f"[This is your BRANCH memoir. The run memoir shown in run_memory "
            f"is read-only here; the root auditor folds your branch memoir in "
            f"after the merge.]"
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
    # Deliberately the ROOT memoir, never write_path(): archiving is
    # root-only (the caller also gates on _is_clone), and a shadow must not
    # land in the shared history or the DB under a clone's cycle number.
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


# ---------------------------------------------------------------------------
# Fan-out: collapsed branch memoirs, and the root-frozen invariant
# ---------------------------------------------------------------------------


def _strip_comments(text: str) -> str:
    return _HTML_COMMENT_RE.sub("", text).strip()


def _skeleton_body() -> str:
    """The blank skeleton with its instruction comment removed, for comparison.

    Cached: called once per branch per cycle. Stripping the comment also
    removes the `created` timestamp (it lives inside the comment), so an
    unedited shadow compares exactly equal regardless of when it was seeded.
    """
    global _SKELETON_BODY
    if _SKELETON_BODY is None:
        try:
            _SKELETON_BODY = _strip_comments(_skeleton())
        except Exception:
            _SKELETON_BODY = ""
    return _SKELETON_BODY


def _fold_body(text: str) -> str | None:
    """A branch shadow reduced to what is worth folding, or None if nothing is.

    Two reductions, both measured on a real fan-out. The template's
    instruction comment is ~420 tokens, so three branches would spend a third
    of the fold budget re-reading boilerplate the root auditor already has.
    And a branch whose auditor never edited its shadow contributes a bare
    skeleton, which should cost nothing — detected by comparing against the
    skeleton itself rather than guessing at placeholder strings, so it stays
    correct when the template changes.
    """
    body = _strip_comments(text)
    if not body or body == _skeleton_body():
        return None
    return body


def _branch_shadows(root_instance_dir: Path) -> list[Path]:
    """Every clone shadow memoir under the root instance dir, oldest fork first.

    Mirrors `concat_clone_ledgers`, which globs `fork-*/clone-*/` for the
    same reason: the files on disk are the record, so no state field is
    needed to know a fork happened.
    """
    try:
        return sorted(Path(root_instance_dir).glob(f"fork-*/clone-*/{MEMOIR_FILENAME}"))
    except OSError:
        return []


def branch_memoirs_for_injection(
    workspace: Path, root_instance_dir: Path | None, config: dict | None
) -> str:
    """The `branch_memoirs` input: shadows newer than the root memoir.

    Closes the fan-out blind spot. Fan-out replaces the worker AND auditor
    for its cycle, and the post-merge cycle is worker-only, so no root
    auditor runs for two cycles around a fork; by the time one does, the
    merge text has been displaced out of `results` entirely. The branch
    shadows are still on disk, already distilled into the same six sections,
    so they are handed to the root auditor to fold.

    Stateless and self-clearing: "newer than the root memoir" is true exactly
    from a fork's collapse until the root auditor next edits the memoir. The
    whole block is capped at `memoir.max_tokens`, truncated at a branch
    boundary, so three branches cannot blow the prompt.
    """
    if root_instance_dir is None:
        return _NO_BRANCHES
    root = paths.memoir_path(workspace)
    try:
        root_mtime = root.stat().st_mtime
    except OSError:
        root_mtime = 0.0

    sections: list[str] = []
    for shadow in _branch_shadows(root_instance_dir):
        try:
            if shadow.stat().st_mtime <= root_mtime:
                continue
            text = _fold_body(shadow.read_text())
        except OSError:
            continue
        if text is None:
            continue
        # <root_instance>/fork-<id>/clone-<k>/MEMOIR.md
        label = f"{shadow.parent.parent.name}/{shadow.parent.name}"
        sections.append(f"== BRANCH {label} ==\n\n{text}")
    if not sections:
        return _NO_BRANCHES

    body = "\n\n".join(sections)
    cap = max_tokens(config)
    if len(body) // 4 > cap:
        health_events.append_event(
            "memoir_branches_over_cap",
            detail=f"{len(sections)} branch memoir(s) exceed {cap} tokens; truncated",
        )
        body = _truncate(body, cap * 4) + _TRUNCATED_MARKER
    return (
        f"[{len(sections)} fan-out branch memoir(s) collapsed since the run "
        f"memoir was last updated. Fold what matters — dead ends especially — "
        f"into the run memoir; they are advisory like it is.]\n\n{body}"
    )


def assert_root_frozen_during_fork(
    workspace: Path, before: str | None, fork_id: str
) -> bool:
    """Check the invariant that a fork leaves the root memoir untouched.

    `before` is `snapshot()` taken just before clones were spawned. Clones
    write shadows and their `memoir_path` points there, so a change here
    means a clone wrote the root file anyway. Best-effort evidence, not
    enforcement: returns True when the invariant held.
    """
    after = snapshot(workspace)
    if after == before:
        return True
    health_events.append_event(
        "memoir_clone_write",
        detail=(
            f"root memoir changed during fork {fork_id}; a clone wrote the "
            f"root file instead of its shadow"
        ),
    )
    return False
