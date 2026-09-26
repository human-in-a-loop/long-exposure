"""Workspace bootstrap + plan/ledger helpers.

Stdlib-only. Called once per fresh run from exploration.py to lay down the
standard folder skeleton, render plan_of_record.md and STRUCTURE.md from
templates, and append a `_run/start` bootstrap event to promise_ledger.jsonl.
On resume of an existing run, this is a no-op (graceful by design — see
docs/workspace-conventions.md).

Also provides the cycle-input summarizer used to inject a token-bounded
view of the ledger into each cycle's agent prompts.
"""

from __future__ import annotations

import json
import re
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

STANDARD_FOLDERS = ("reports", "audits", "scripts", "tests", "data", "docs", "tools", "stale")

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug_from_directive(directive: str, max_len: int = 60) -> str:
    """Cheap slug for a Plan-of-Record title."""
    text = (directive or "exploration").strip().splitlines()[0] if directive else "exploration"
    text = text[:max_len].strip()
    return text or "exploration"


def is_fresh_start(workspace: Path, current_cycle: int) -> bool:
    """A run qualifies for bootstrap iff cycle == 1 AND no prior plan exists.

    Per docs/workspace-conventions.md, mid-run resumes never bootstrap.
    """
    if current_cycle > 1:
        return False
    if (workspace / "plan_of_record.md").exists():
        return False
    return True


def ensure_skeleton(workspace: Path) -> list[str]:
    """Create the standard folder skeleton if missing. Returns folders created.

    Idempotent — calling on an already-bootstrapped workspace is a no-op.
    """
    created = []
    for folder in STANDARD_FOLDERS:
        d = workspace / folder
        if not d.exists():
            try:
                d.mkdir(parents=True, exist_ok=True)
                created.append(folder)
            except OSError:
                pass
    try:
        from long_exposure.paths import ensure_layout
        ensure_layout(workspace)
    except OSError:
        pass
    return created


def render_template(name: str, **subs: str) -> str:
    """Read a template file and substitute {placeholders}. Returns the body."""
    path = _TEMPLATE_DIR / name
    text = path.read_text()
    return text.format(**subs)


def write_plan_of_record(workspace: Path, directive: str, run_id: str) -> Path:
    """Render and write plan_of_record.md if missing. Returns the path."""
    plan = workspace / "plan_of_record.md"
    if plan.exists():
        return plan
    body = render_template(
        "plan_of_record_template.md",
        created=_now_iso(),
        run_id=run_id,
        title=_slug_from_directive(directive),
        directive=directive.strip(),
    )
    plan.write_text(body)
    return plan


def write_structure_md(workspace: Path, run_id: str) -> Path:
    """Render and write STRUCTURE.md if missing. Returns the path."""
    s = workspace / "STRUCTURE.md"
    if s.exists():
        return s
    body = render_template(
        "structure_template.md",
        created=_now_iso(),
        run_id=run_id,
    )
    s.write_text(body)
    return s


def resolve_ledger_path(workspace: Path) -> Path:
    """Pick the right ledger file for the calling process (Plan 1 §6).

    Clones — detected via the AGENT_FORK_ID env var that the fan-out conductor
    sets when spawning each clone subprocess — write to their per-clone
    shadow ledger at ``<instance_dir>/promise_ledger.jsonl``. The fan-out
    conductor concatenates these into the workspace's main ledger after the
    barrier collapses (see ``fanout._concat_clone_ledgers``).

    Root processes (and any caller without ``AGENT_FORK_ID``) write directly
    to the workspace main ledger.
    """
    if os.environ.get("AGENT_FORK_ID"):
        instance_dir = os.environ.get("AGENT_INSTANCE_DIR")
        if instance_dir:
            d = Path(instance_dir)
            d.mkdir(parents=True, exist_ok=True)
            return d / "promise_ledger.jsonl"
    return workspace / "promise_ledger.jsonl"


def append_ledger_event(workspace: Path, event: dict) -> None:
    """Append a single event to the appropriate ledger file atomically.

    Routes clone-side writes to a per-clone shadow ledger (Plan 1 §6) so
    concurrent clones never interleave bytes into the workspace main file.
    JSONL appends are mostly atomic at the OS level for small lines (POSIX
    O_APPEND + a single write() under PIPE_BUF); shadow ledgers eliminate
    the small-line contention boundary entirely.

    Stamps `operator` when the event does not already carry one. This is the
    single chokepoint every ledger writer goes through — the cycle loop, the
    manager, the bootstrap event and the agent-facing `ledger_append` tool —
    which is why identity is added here rather than at each call site, and
    why agents never have to know or supply it. See `federation` for the
    measured defect this closes (git-federation.md §7.1).
    """
    try:
        from long_exposure import federation as _federation

        _federation.stamp(event)
    except Exception:
        # Identity is useful, not load-bearing. A ledger event that records
        # the research must never be lost because stamping it failed.
        pass
    ledger = resolve_ledger_path(workspace)
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    # NOTE: deliberately a single unconditional write, with no inspection of
    # the file first. An earlier version read the last byte and prepended a
    # newline when one was missing, to stop a newline-less file welding two
    # JSON objects onto one line. That was wrong twice over: read-then-write is
    # not atomic, so under concurrent appends it produced a spurious blank line
    # in ~5% of 40-thread trials; and the weld case it targeted is produced by
    # `merge=union`, where GIT does the concatenation, so no writer-side check
    # could prevent it anyway. A weld is recovered at read time instead — see
    # `_read_ledger`.
    #
    # O_APPEND ensures the kernel performs the seek+write atomically per call.
    fd = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def concat_clone_ledgers(workspace: Path, fork_dir: Path) -> int:
    """Merge clone shadow ledgers into the workspace main ledger.

    Called by the fan-out conductor after the barrier collapses. Walks
    ``fork_dir/clone-*/promise_ledger.jsonl`` files, reads all events,
    deduplicates by ``event_id`` (idempotent — re-running concat after
    a partial run never produces duplicate lines), then appends the
    new events to the workspace main ledger in timestamp order.

    Returns the count of newly-appended events.
    """
    main_ledger = workspace / "promise_ledger.jsonl"

    seen_ids: set[str] = set()
    if main_ledger.exists():
        for raw in main_ledger.read_text().splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                if isinstance(ev, dict) and ev.get("event_id"):
                    seen_ids.add(ev["event_id"])
            except json.JSONDecodeError:
                continue

    new_events: list[dict] = []
    if not fork_dir.exists():
        return 0
    for clone_ledger in sorted(fork_dir.glob("clone-*/promise_ledger.jsonl")):
        for raw in clone_ledger.read_text().splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            eid = ev.get("event_id")
            if not eid or eid in seen_ids:
                continue
            seen_ids.add(eid)
            new_events.append(ev)

    if not new_events:
        return 0

    new_events.sort(key=lambda e: (e.get("ts", ""), e.get("event_id", "")))
    with main_ledger.open("a") as f:
        for ev in new_events:
            f.write(json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(new_events)


def emit_run_start_event(workspace: Path, run_id: str, directive: str) -> str:
    """Append the canonical bootstrap event. Returns the event_id."""
    eid = str(uuid.uuid4())
    event = {
        "event_id": eid,
        "ts": _now_iso(),
        "run_id": run_id,
        "cycle": 1,
        "agent": "researcher",
        "milestone_id": "_run/start",
        "status": "in-progress",
        "confidence": {
            "level": "high",
            "rationale": "run boot — directive recorded, plan-of-record drafted",
            "assessor": "researcher",
        },
        "narrative": (directive or "").strip().splitlines()[0][:240] or "run started",
        "artifacts": ["plan_of_record.md", "STRUCTURE.md"],
    }
    append_ledger_event(workspace, event)
    return eid


def bootstrap_workspace(
    workspace: Path,
    directive: str,
    run_id: str,
    cycle: int,
) -> dict:
    """Run cycle-1 bootstrap if applicable. Returns a small status dict.

    No-op on resume (cycle > 1 or plan_of_record.md already exists).
    """
    status = {
        "ran": False,
        "folders_created": [],
        "wrote_plan": False,
        "wrote_structure": False,
        "ledger_event_id": None,
    }
    if not is_fresh_start(workspace, cycle):
        return status

    status["folders_created"] = ensure_skeleton(workspace)

    plan_path = workspace / "plan_of_record.md"
    if not plan_path.exists():
        write_plan_of_record(workspace, directive, run_id)
        status["wrote_plan"] = True

    struct_path = workspace / "STRUCTURE.md"
    if not struct_path.exists():
        write_structure_md(workspace, run_id)
        status["wrote_structure"] = True

    if not (workspace / "promise_ledger.jsonl").exists():
        status["ledger_event_id"] = emit_run_start_event(workspace, run_id, directive)

    status["ran"] = True
    return status


# ---------------------------------------------------------------------------
# Ledger summary for cycle-input injection
# ---------------------------------------------------------------------------


_DECODER = json.JSONDecoder()


def decode_line(line: str) -> list[dict]:
    """Every JSON object on one physical line. Usually exactly one.

    A line can carry more than one because `promise_ledger.jsonl` is declared
    `merge=union` in a federated repo (`docs/git-federation.md` §5.1). Union
    merge concatenates both sides' added hunks, and if either side's file does
    not end in a newline — a hand edit, a truncated write, a tool that trims
    trailing whitespace — the join lands mid-line and two events share one:

        {"event_id":"a",...}{"event_id":"b",...}

    Treating that as one malformed line loses BOTH events, and losing a
    validated finding silently is the failure this whole area exists to avoid.
    `raw_decode` walks the line instead, so both are recovered.

    Fixing it here rather than in the writer is deliberate. The writer cannot
    prevent this — git performs the concatenation, not us — and an earlier
    attempt to have the writer check for a trailing newline introduced a race:
    read-then-write is not atomic, and concurrent appends produced a spurious
    blank line in ~5% of 40-thread trials.

    Never raises. Trailing junk stops the walk and keeps whatever was decoded.
    """
    events: list[dict] = []
    text = (line or "").strip()
    index = 0
    length = len(text)
    while index < length:
        try:
            value, end = _DECODER.raw_decode(text, index)
        except ValueError:
            break
        if isinstance(value, dict):
            events.append(value)
        if end <= index:          # no forward progress; refuse to spin
            break
        index = end
        while index < length and text[index] in " \t":
            index += 1
    return events


def _read_ledger(ledger_path: Path) -> list[dict]:
    """Tolerant JSONL reader. Skips malformed lines silently — promise_check
    is responsible for surfacing parse errors; this reader must not crash
    the cycle loop. Recovers welded lines via `decode_line`."""
    if not ledger_path.exists():
        return []
    events: list[dict] = []
    for raw in ledger_path.read_text().splitlines():
        if raw.strip():
            events.extend(decode_line(raw))
    return events


_UUID_SHAPE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def is_evidenced(event: dict, workspace: Path) -> bool:
    """True if the event cites something that actually backs it.

    Evidence is any entry in `evidence` or `artifacts` that is either a UUID —
    a citation of a prior ledger event, as `ledger_graph` reads it — or a
    workspace-relative path that exists. A path must stay inside the workspace:
    `../../etc/hosts` exists, but it is not evidence for a research claim.
    """
    for key in ("evidence", "artifacts"):
        items = event.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, str) or not item.strip():
                continue
            if _UUID_SHAPE.match(item.strip()):
                return True
            from long_exposure.paths import canonical_rel_path

            rel = canonical_rel_path(item)
            if not rel or ".." in Path(rel).parts:
                continue
            try:
                if (Path(workspace) / rel).exists():
                    return True
            except OSError:
                continue
    return False


def _displayed_level(event: dict, workspace: Path) -> str:
    """The confidence level to SHOW, which may differ from the one claimed.

    The evidence gate. An auditor is the same model grading its own worker, so
    a confident wrong result can be rubber-stamped and every later cycle reads
    it as settled. A `validated` research milestone claimed at `high` with
    nothing behind it — no produced file that exists, no cited event — is shown
    to later cycles as medium, with the claim kept visible.

    Deliberately small, as asked. It is read-side only: the ledger keeps the
    agent's claim exactly as written, because agents may append by writing the
    file directly, bypassing `append_ledger_event`, so a write-side gate would
    miss them, and because an audit trail should record what was claimed, not
    what the harness thought of it. It checks that evidence exists, not that it
    is any good — the harness treats the model as faithful, and the failure this
    catches is the honest one: a result claimed from an output that was never
    written. Bookkeeping milestones (`_plan/`, `_run/`, `_manager/`, ...) are
    exempt: a plan revision is validated by being decided, and has no file
    behind it in the sense a finding does.
    """
    conf = event.get("confidence") or {}
    level = conf.get("level", "?") if isinstance(conf, dict) else "?"
    if event.get("status") != "validated" or level != "high":
        return level
    from long_exposure.tools.promise_check import RESERVED_NAMESPACES

    mid = str(event.get("milestone_id") or "")
    if mid.startswith(RESERVED_NAMESPACES):
        return level
    if is_evidenced(event, workspace):
        return level
    return "medium (claimed high; no evidence found)"


def summarize_ledger(workspace: Path, max_chars: int = 32_000) -> str:
    """Produce a token-bounded summary of the ledger for cycle-input injection.

    Strategy (per plan §5):
      - For each unique milestone_id, emit the most recent event.
      - Always include any in-progress events, regardless of recency.
      - Always include validated/superseded events with low or provisional
        confidence (these are the items that need re-verification).
      - Truncate at max_chars (~8K tokens at ~4 chars/token).

    Returns a single string ready to inject as `promise_ledger_summary`.
    """
    ledger_path = workspace / "promise_ledger.jsonl"
    events = _read_ledger(ledger_path)
    if not events:
        return "[promise_ledger.jsonl is empty or absent]"

    # Group by (operator, milestone), sort each group by ts.
    #
    # The operator is part of the key because two operators federating over
    # one repository reach the same milestone independently, and keying on
    # milestone_id alone made the later event HIDE the earlier one — a
    # contradicting result vanished from the summary rather than being
    # flagged (git-federation.md §7.1, measured). A missing `operator` reads
    # as the local one, so a single-operator ledger — including every ledger
    # written before this field existed — groups exactly as it did before.
    from long_exposure import federation as _federation

    local_operator = _federation.operator_name()
    by_key: dict[tuple[str, str], list[dict]] = {}
    for ev in events:
        key = (
            _federation.event_operator(ev, local_operator),
            ev.get("milestone_id") or "_unknown",
        )
        by_key.setdefault(key, []).append(ev)
    for evs in by_key.values():
        evs.sort(key=lambda e: e.get("ts", ""))

    # Only name operators when there is genuinely more than one. A
    # single-operator run keeps byte-identical summary text, which matters
    # because this string is injected into the prompt.
    operators = sorted({op for op, _ in by_key})
    show_operator = len(operators) > 1
    distinct_milestones = len({mid for _, mid in by_key})

    selected: list[dict] = []
    seen_event_ids: set[str] = set()

    for evs in by_key.values():
        latest = evs[-1]
        if latest.get("event_id") and latest["event_id"] not in seen_event_ids:
            selected.append(latest)
            seen_event_ids.add(latest["event_id"])

    # In-progress backfill + low-confidence validated/superseded backfill.
    for ev in events:
        eid = ev.get("event_id")
        if not eid or eid in seen_event_ids:
            continue
        status = ev.get("status")
        level = (ev.get("confidence") or {}).get("level")
        if status == "in-progress":
            selected.append(ev)
            seen_event_ids.add(eid)
        elif status in ("validated", "superseded") and level in ("low", "provisional"):
            selected.append(ev)
            seen_event_ids.add(eid)

    # Sort the final set chronologically.
    selected.sort(key=lambda e: (
        e.get("ts", ""),
        e.get("milestone_id", ""),
        _federation.event_operator(e, local_operator),
    ))

    lines: list[str] = []
    lines.append("# Promise Ledger Summary")
    lines.append(
        f"Total events: {len(events)}, distinct milestones: "
        f"{distinct_milestones}, shown: {len(selected)} "
        f"(latest-per-milestone + in-progress + low-confidence)"
    )
    if show_operator:
        lines.append(
            f"Operators on this ledger: {', '.join(operators)}. A milestone "
            "reached by more than one operator shows one line per operator; "
            "where those disagree, that disagreement is the finding."
        )
    lines.append("")
    for ev in selected:
        mid = ev.get("milestone_id", "?")
        status = ev.get("status", "?")
        level = _displayed_level(ev, workspace)
        cycle = ev.get("cycle", "?")
        agent = ev.get("agent", "?")
        ts = ev.get("ts", "")
        narrative = (ev.get("narrative") or "").strip().replace("\n", " ")
        if len(narrative) > 200:
            narrative = narrative[:197] + "..."
        artifacts = ev.get("artifacts") or []
        art_str = f" artifacts={len(artifacts)}" if artifacts else ""
        who = (
            f", {_federation.event_operator(ev, local_operator)}"
            if show_operator else ""
        )
        lines.append(
            f"- [{mid}] {status}/{level} (cycle {cycle}, {agent}{who}, {ts})"
            f"{art_str}"
        )
        if narrative:
            lines.append(f"    {narrative}")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... [truncated; full ledger at promise_ledger.jsonl]"
    return text


def derive_run_id(state_dir: Path | None = None) -> str:
    """A simple run_id: ISO timestamp of the run's first cycle."""
    return f"run-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ')}"
