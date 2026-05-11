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
    """
    ledger = resolve_ledger_path(workspace)
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
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


def _read_ledger(ledger_path: Path) -> list[dict]:
    """Tolerant JSONL reader. Skips malformed lines silently — promise_check
    is responsible for surfacing parse errors; this reader must not crash
    the cycle loop."""
    if not ledger_path.exists():
        return []
    events: list[dict] = []
    for raw in ledger_path.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
            if isinstance(ev, dict):
                events.append(ev)
        except json.JSONDecodeError:
            continue
    return events


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

    # Group by milestone, sort each group by ts.
    by_mid: dict[str, list[dict]] = {}
    for ev in events:
        by_mid.setdefault(ev.get("milestone_id") or "_unknown", []).append(ev)
    for evs in by_mid.values():
        evs.sort(key=lambda e: e.get("ts", ""))

    selected: list[dict] = []
    seen_event_ids: set[str] = set()

    for mid, evs in by_mid.items():
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
    selected.sort(key=lambda e: (e.get("ts", ""), e.get("milestone_id", "")))

    lines: list[str] = []
    lines.append("# Promise Ledger Summary")
    lines.append(
        f"Total events: {len(events)}, distinct milestones: {len(by_mid)}, "
        f"shown: {len(selected)} (latest-per-milestone + in-progress + low-confidence)"
    )
    lines.append("")
    for ev in selected:
        mid = ev.get("milestone_id", "?")
        status = ev.get("status", "?")
        conf = ev.get("confidence") or {}
        if not isinstance(conf, dict):
            conf = {}
        level = conf.get("level", "?")
        cycle = ev.get("cycle", "?")
        agent = ev.get("agent", "?")
        ts = ev.get("ts", "")
        narrative = (ev.get("narrative") or "").strip().replace("\n", " ")
        if len(narrative) > 200:
            narrative = narrative[:197] + "..."
        artifacts = ev.get("artifacts") or []
        art_str = f" artifacts={len(artifacts)}" if artifacts else ""
        lines.append(
            f"- [{mid}] {status}/{level} (cycle {cycle}, {agent}, {ts}){art_str}"
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
