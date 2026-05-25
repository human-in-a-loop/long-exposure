#!/usr/bin/env python3
"""promise_check — validate plan_of_record.md + promise_ledger.jsonl coherence.

Stdlib-only. Reviewable in one sitting. See docs/workspace-conventions.md
for the spec; the artifact-tracking checks are documented there too.

Exit codes:
  0  — green (no schema/cross-ref/lifecycle errors)
  1  — errors found (warnings alone exit 0)
  2  — bad invocation (missing workspace, etc.)

Typical use, from any agent's Bash tool or from the human shell:

    python -m long_exposure.tools.promise_check /path/to/workspace
    python -m long_exposure.tools.promise_check /path/to/workspace --json
    python -m long_exposure.tools.promise_check /path/to/workspace --strict

Design principle (matching the plans): SURFACE, never enforce. This script
emits findings; the cycle loop never blocks on its output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Vocabulary — the unified status taxonomy and confidence levels.
# ---------------------------------------------------------------------------

STATUS_VALUES = {
    "not-started",
    "in-progress",
    "action_required",
    "validated",
    "deferred",
    "reopened",
    "superseded",
    "invalidated",
}

CONFIDENCE_LEVELS = {"high", "medium", "low", "provisional"}

ASSESSORS = {
    "auditor",
    "researcher",
    "worker",
    "human",
    "manager",
    "harness",
    "final_auditor",
}

REQUIRED_EVENT_FIELDS = (
    "event_id",
    "ts",
    "run_id",
    "cycle",
    "agent",
    "milestone_id",
    "status",
    "confidence",
    "narrative",
)

RESERVED_NAMESPACES = (
    "_plan/",
    "_run/",
    "_archive/",
    "_orphan/",
    "_manager/",
    "_infra/",
)
IMMUTABLE_EXCEPTION_PATH = Path("reports/promise_check_immutable_exceptions.json")


# ---------------------------------------------------------------------------
# Findings model
# ---------------------------------------------------------------------------


class Findings:
    """Accumulator for errors and warnings. Errors raise non-zero exit."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _load_ledger(ledger_path: Path, findings: Findings) -> list[dict]:
    """Parse ledger JSONL. Returns the list of well-formed events."""
    if not ledger_path.exists():
        return []
    events: list[dict] = []
    for line_no, raw in enumerate(ledger_path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError as exc:
            findings.err(f"ledger:line {line_no}: malformed JSON ({exc.msg})")
            continue
        if not isinstance(ev, dict):
            findings.err(f"ledger:line {line_no}: top-level not a JSON object")
            continue
        ev["_line"] = line_no
        ev["_raw_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
        events.append(ev)
    return events


def _load_immutable_exceptions(workspace: Path, findings: Findings) -> set[tuple]:
    """Load exact immutable-history exception fingerprints.

    Exceptions are intentionally narrow: they only suppress a named validator
    error when the historical row still matches the recorded line fingerprint.
    """
    path = workspace / IMMUTABLE_EXCEPTION_PATH
    if not path.exists():
        return set()
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        findings.err(f"{IMMUTABLE_EXCEPTION_PATH}: malformed JSON ({exc.msg})")
        return set()
    rows = doc.get("exceptions") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        findings.err(f"{IMMUTABLE_EXCEPTION_PATH}: expected object with exceptions list")
        return set()
    exceptions: set[tuple] = set()
    required = ("line", "event_id", "ts", "milestone_id", "raw_sha256", "error")
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            findings.err(f"{IMMUTABLE_EXCEPTION_PATH}: exception {i} is not an object")
            continue
        missing = [field for field in required if field not in row]
        if missing:
            findings.err(
                f"{IMMUTABLE_EXCEPTION_PATH}: exception {i} missing {', '.join(missing)}"
            )
            continue
        exceptions.add(tuple(row[field] for field in required))
    return exceptions


def _is_immutable_exception(
    ev: dict,
    error: str,
    immutable_exceptions: set[tuple],
) -> bool:
    return (
        ev.get("_line"),
        ev.get("event_id"),
        ev.get("ts"),
        ev.get("milestone_id"),
        ev.get("_raw_sha256"),
        error,
    ) in immutable_exceptions


def _parse_plan_milestones(plan_path: Path, findings: Findings) -> set[str]:
    """Extract Milestone IDs from the plan-of-record's Milestones table."""
    if not plan_path.exists():
        return set()
    text = plan_path.read_text()
    # Find a markdown table whose first column header is 'Milestone ID' (case-insensitive).
    # We look for any pipe-delimited section after a '## Milestones' heading.
    m = re.search(r"##\s+Milestones\s*\n(.+?)(?:\n##\s|\Z)", text, re.DOTALL | re.IGNORECASE)
    if not m:
        findings.warn("plan_of_record.md: no '## Milestones' section found")
        return set()
    section = m.group(1)
    ids: set[str] = set()
    saw_separator = False
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        # skip header row (contains 'Milestone ID') and separator row (---)
        if any("milestone id" in c.lower() for c in cells):
            continue
        if all(set(c) <= set("-: ") for c in cells if c):
            saw_separator = True
            continue
        if not saw_separator:
            # tolerate plans without a clean separator — accept first column anyway
            pass
        first = cells[0]
        if first and first not in ("Milestone ID",):
            ids.add(first)
    if not ids:
        findings.warn("plan_of_record.md: '## Milestones' section parsed but no IDs extracted")
    return ids


# ---------------------------------------------------------------------------
# Schema integrity (see docs/workspace-conventions.md)
# ---------------------------------------------------------------------------


def _check_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _check_event_schema(
    events: list[dict],
    findings: Findings,
    immutable_exceptions: set[tuple] | None = None,
) -> None:
    immutable_exceptions = immutable_exceptions or set()
    seen_ids: set[str] = set()
    for ev in events:
        line = ev.get("_line", "?")
        for field in REQUIRED_EVENT_FIELDS:
            if field not in ev:
                findings.err(f"ledger:line {line}: missing required field {field!r}")

        eid = ev.get("event_id")
        if eid is not None:
            if not _check_uuid(eid):
                error = "event_id is not a valid UUID"
                if _is_immutable_exception(ev, error, immutable_exceptions):
                    findings.note(
                        f"immutable exception consumed for ledger:line {line}: {error}"
                    )
                else:
                    findings.err(f"ledger:line {line}: {error}")
            elif eid in seen_ids:
                findings.err(f"ledger:line {line}: duplicate event_id {eid!r}")
            else:
                seen_ids.add(eid)

        status = ev.get("status")
        if status is not None and status not in STATUS_VALUES:
            findings.err(
                f"ledger:line {line}: status {status!r} not in unified vocabulary "
                f"({sorted(STATUS_VALUES)})"
            )

        conf = ev.get("confidence")
        if conf is not None:
            if not isinstance(conf, dict):
                findings.err(f"ledger:line {line}: confidence must be an object")
            else:
                level = conf.get("level")
                if level not in CONFIDENCE_LEVELS:
                    findings.err(
                        f"ledger:line {line}: confidence.level {level!r} not in "
                        f"{sorted(CONFIDENCE_LEVELS)}"
                    )
                if not (conf.get("rationale") or "").strip():
                    findings.err(f"ledger:line {line}: confidence.rationale is empty")
                if conf.get("assessor") not in ASSESSORS:
                    findings.err(
                        f"ledger:line {line}: confidence.assessor {conf.get('assessor')!r} "
                        f"not recognized"
                    )

        # artifacts (Plan 4): optional list of strings
        artifacts = ev.get("artifacts")
        if artifacts is not None:
            if not isinstance(artifacts, list) or not all(isinstance(a, str) for a in artifacts):
                findings.err(f"ledger:line {line}: artifacts must be a list of strings")
            else:
                for a in artifacts:
                    if a.startswith("./") or a.endswith("/") or "\\" in a:
                        findings.warn(
                            f"ledger:line {line}: artifact path {a!r} not canonicalized"
                        )

        supersedes = ev.get("supersedes")
        if supersedes is not None:
            if isinstance(supersedes, str):
                pass
            elif isinstance(supersedes, list) and all(
                isinstance(ref, str) for ref in supersedes
            ):
                pass
            else:
                findings.err(
                    f"ledger:line {line}: supersedes must be a string or list of strings"
                )


# ---------------------------------------------------------------------------
# Cross-reference integrity
# ---------------------------------------------------------------------------


def _is_reserved(milestone_id: str) -> bool:
    return any(milestone_id.startswith(ns) for ns in RESERVED_NAMESPACES)


def _check_cross_references(
    events: list[dict],
    plan_milestones: set[str],
    findings: Findings,
) -> None:
    seen_ids = {ev.get("event_id") for ev in events if ev.get("event_id")}
    seen_milestones: set[str] = set()
    for ev in events:
        line = ev.get("_line", "?")
        mid = ev.get("milestone_id") or ""
        if mid:
            seen_milestones.add(mid)
            if not _is_reserved(mid) and plan_milestones and mid not in plan_milestones:
                findings.err(
                    f"ledger:line {line}: milestone_id {mid!r} not in plan_of_record.md "
                    f"and not in a reserved namespace ({list(RESERVED_NAMESPACES)})"
                )

        sup = ev.get("supersedes")
        if sup is not None:
            if isinstance(sup, str):
                refs = [sup]
            elif isinstance(sup, list) and all(isinstance(ref, str) for ref in sup):
                refs = sup
            else:
                continue
            for ref in refs:
                if ref not in seen_ids:
                    findings.err(
                        f"ledger:line {line}: supersedes references unknown event_id {ref!r}"
                    )

    # Plan-side: every plan milestone should have at least one ledger event after first cycle.
    if plan_milestones and events:
        for mid in plan_milestones:
            if mid not in seen_milestones:
                findings.warn(
                    f"plan milestone {mid!r} has no ledger events yet"
                )


# ---------------------------------------------------------------------------
# Lifecycle integrity — supersession, reopen, invalidation discipline
# ---------------------------------------------------------------------------


def _check_lifecycle(events: list[dict], findings: Findings) -> None:
    # Group by milestone_id, sorted by ts (lexicographic ISO 8601 sort works).
    by_milestone: dict[str, list[dict]] = {}
    for ev in events:
        mid = ev.get("milestone_id") or ""
        by_milestone.setdefault(mid, []).append(ev)

    for mid, evs in by_milestone.items():
        evs.sort(key=lambda e: (e.get("ts", ""), e.get("_line", 0)))
        prev_status = None
        for ev in evs:
            line = ev.get("_line", "?")
            status = ev.get("status")
            if prev_status == "validated" and status == "in-progress":
                findings.err(
                    f"ledger:line {line}: {mid!r} transitioned validated -> in-progress "
                    f"without an intervening 'reopened' event"
                )
            if status == "superseded" and not ev.get("supersedes"):
                findings.err(
                    f"ledger:line {line}: superseded event for {mid!r} missing 'supersedes' "
                    f"field"
                )
            if status == "invalidated":
                rationale = (ev.get("confidence") or {}).get("rationale", "")
                if not rationale.strip():
                    findings.err(
                        f"ledger:line {line}: invalidated event for {mid!r} missing "
                        f"rationale (confidence.rationale must explain what was wrong)"
                    )
            prev_status = status


# ---------------------------------------------------------------------------
# Plan / mtime integrity — silent-edit detection
# ---------------------------------------------------------------------------


def _check_plan_mtime(
    plan_path: Path,
    events: list[dict],
    findings: Findings,
) -> None:
    if not plan_path.exists():
        return
    plan_mtime = int(plan_path.stat().st_mtime)
    plan_events = [
        ev for ev in events
        if (ev.get("milestone_id") or "").startswith("_plan/")
    ]
    # Did any _plan/ event land after the plan's mtime - 60s?
    # We use a 60s tolerance because mtime granularity and append-after-edit
    # racing make exact equality impossible in practice.
    if not plan_events:
        # The first cycle won't have any _plan/ events yet, and a workspace
        # whose plan never changed legitimately has none. Suppress this warn
        # when a `_run/start` bootstrap event already anchors the run, since
        # that is the convention's intended starter event.
        has_run_start = any(
            (ev.get("milestone_id") or "").startswith("_run/start")
            for ev in events
        )
        if events and plan_mtime > 0 and not has_run_start:
            findings.warn(
                f"plan_of_record.md present but no '_plan/' or '_run/start' "
                f"event recorded — a bootstrap event should anchor the plan "
                f"to the ledger"
            )
        return
    latest_plan_event_ts = max(
        (_parse_iso_to_epoch(ev.get("ts", "")) or 0) for ev in plan_events
    )
    if plan_mtime - latest_plan_event_ts > 600:  # 10 min tolerance for human edits
        findings.warn(
            f"plan_of_record.md mtime is {plan_mtime - latest_plan_event_ts}s "
            f"newer than the latest '_plan/' ledger event — possible silent edit"
        )


def _parse_iso_to_epoch(ts: str) -> int | None:
    """Best-effort ISO 8601 → epoch seconds. Returns None on parse failure."""
    if not ts:
        return None
    try:
        from datetime import datetime
        # Accept Z-suffix
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return int(datetime.fromisoformat(ts).timestamp())
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Confidence calibration warnings (§7.17–19 of plan)
# ---------------------------------------------------------------------------


def _check_confidence_calibration(
    events: list[dict],
    findings: Findings,
    strict: bool = False,
) -> None:
    by_milestone: dict[str, list[dict]] = {}
    for ev in events:
        by_milestone.setdefault(ev.get("milestone_id") or "", []).append(ev)
    for mid, evs in by_milestone.items():
        evs.sort(key=lambda e: (e.get("ts", ""), e.get("_line", 0)))
        # Stale low-confidence closure: most recent event is `validated`
        # with low/provisional confidence, and the latest cycle in the
        # ledger is much newer.
        if not evs:
            continue
        last = evs[-1]
        if last.get("status") == "validated":
            level = (last.get("confidence") or {}).get("level")
            if level in ("low", "provisional"):
                last_cycle = last.get("cycle", 0)
                ledger_max = max((ev.get("cycle", 0) for ev in events), default=last_cycle)
                if ledger_max - last_cycle >= 30:
                    msg = (
                        f"{mid!r}: validated/{level} at cycle {last_cycle}; ledger "
                        f"has reached cycle {ledger_max} — stale low-confidence closure"
                    )
                    if strict:
                        findings.err(msg)
                    else:
                        findings.warn(msg)


# ---------------------------------------------------------------------------
# Multi-cycle investigation discipline — surface, never enforce.
# ---------------------------------------------------------------------------


_TERMINAL_STATUSES = {"validated", "deferred", "superseded", "invalidated"}
_MECHANISM_RE = re.compile(
    r"<mechanism\b|mechanism\s+statement|falsification_criteria|"
    r"special_points_evaluated",
    re.IGNORECASE,
)


def _non_reserved_events(events: list[dict]) -> list[dict]:
    return [
        ev for ev in events
        if not _is_reserved(str(ev.get("milestone_id") or ""))
    ]


def _events_since_latest_terminal(evs: list[dict]) -> list[dict]:
    evs = sorted(evs, key=lambda e: (e.get("ts", ""), e.get("_line", 0)))
    latest_terminal_idx = -1
    for i, ev in enumerate(evs):
        if ev.get("status") in _TERMINAL_STATUSES:
            latest_terminal_idx = i
    return evs[latest_terminal_idx + 1:]


def _has_mechanism_evidence(evs: list[dict]) -> bool:
    for ev in evs:
        haystack = "\n".join(
            str(ev.get(key) or "")
            for key in ("narrative", "scope", "reopen_conditions")
        )
        conf = ev.get("confidence")
        if isinstance(conf, dict):
            haystack += "\n" + str(conf.get("rationale") or "")
        if _MECHANISM_RE.search(haystack):
            return True
    return False


def _check_cycles_per_finding(events: list[dict], findings: Findings) -> None:
    """Warn when a milestone/finding has accumulated many active cycles.

    This intentionally derives from the existing ledger instead of requiring a
    new finding database. The manager agent uses the same signal as a counter;
    promise_check only surfaces it for the auditor and human.
    """
    by_mid: dict[str, list[dict]] = {}
    for ev in _non_reserved_events(events):
        mid = str(ev.get("milestone_id") or "")
        if mid:
            by_mid.setdefault(mid, []).append(ev)

    for mid, evs in sorted(by_mid.items()):
        active = _events_since_latest_terminal(evs)
        cycles = {
            ev.get("cycle")
            for ev in active
            if isinstance(ev.get("cycle"), int)
        }
        in_progress_cycles = {
            ev.get("cycle")
            for ev in active
            if ev.get("status") in {"in-progress", "reopened", "action_required"}
            and isinstance(ev.get("cycle"), int)
        }
        if len(in_progress_cycles) >= 3 and not _has_mechanism_evidence(active):
            findings.warn(
                f"{mid!r}: {len(in_progress_cycles)} active cycle(s) since latest "
                "terminal event with no mechanism marker in ledger narratives; "
                "next researcher brief should include a <mechanism> block or an "
                "analytical probe"
            )
        elif len(cycles) >= 5:
            findings.note(
                f"{mid!r}: {len(cycles)} active cycle(s) since latest terminal event"
            )


def _check_manager_interventions(events: list[dict], findings: Findings) -> None:
    manager_events = [
        ev for ev in events
        if str(ev.get("milestone_id") or "").startswith("_manager/")
    ]
    if not manager_events:
        return
    manager_events.sort(key=lambda e: (e.get("ts", ""), e.get("_line", 0)))
    recent = manager_events[-3:]
    recent_action = [
        ev for ev in recent
        if ev.get("status") == "action_required"
    ]
    if len(recent_action) >= 2:
        classes = [
            str(ev.get("milestone_id") or "").split("/", 1)[-1]
            for ev in recent_action
        ]
        if len(set(classes)) == 1:
            findings.warn(
                f"manager intervention {classes[-1]!r} repeated "
                f"{len(recent_action)} time(s) in the last 3 manager events; "
                "if the pattern persists, escalate rather than repeat guidance"
            )


# ---------------------------------------------------------------------------
# Deliberate non-implementation: markdown YAML frontmatter check.
#
# Plan 4 §4.2 prescribes YAML frontmatter on every agent-authored .md file.
# A validator check ("warn on managed .md files newer than first ledger event
# without a `---` block") was considered and deliberately NOT implemented.
# Reasons (assessed against robust + simple > clever):
#   1. Soft-guidance for an aesthetic convention. The plan itself §9 admits
#      it is the lowest-value artifact; a validator-enforced version would
#      generate noise on docs/ files (methodology, design notes) that have
#      no canonical agent author and pre-date the convention's adoption.
#   2. Forward-only / backfill rules (Plan 4 §9 Phase 2) require the check
#      to know each file's first-touched cycle. Without that bookkeeping it
#      either over-warns on legacy files or silently accepts new ones.
#   3. The ledger's `artifacts` field already answers "when / who / which
#      cycle" — frontmatter is convenience, not essential metadata.
# Skipping this keeps the validator tight and avoids walking every .md file
# on every cycle's auditor invocation. Revisit only if a real run reveals
# the convention is silently slipping AND the slip blocks downstream work.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Artifact coherence (Plan 4 §7) — workspace + ledger agreement.
# ---------------------------------------------------------------------------


_DEFAULT_IGNORE_DIRS = {
    ".venv",
    ".git",
    "__pycache__",
    "node_modules",
    "stale",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "venv",
}

_DEFAULT_TRACKED_ROOT_FILES = {
    "MANIFEST.md",
    "STRUCTURE.md",
    "plan_of_record.md",
    "promise_ledger.jsonl",
    "LESSONS.md",
    "REFERENCES.md",
}

# Standard folders from docs/workspace-conventions.md.
_MANAGED_FOLDERS = ("reports", "scripts", "tests", "data", "docs", "tools")


def _check_artifact_coherence(
    workspace: Path,
    events: list[dict],
    findings: Findings,
) -> None:
    """Single-walk check that the ledger and the workspace agree on artifacts.

    Two failure modes, one walk (root-cause fix for gap 2.3):

      1. ORPHAN: a file exists in a managed path but no ledger event
         references it — the worker forgot to log its work.
      2. MISSING: an event's `artifacts` reference a path that doesn't
         exist on disk and that has not been archived via an `_archive`
         event with `supersedes_path`. Either the file was deleted (which
         the conventions forbid — the human deletes after exploration) or
         a move happened without the convention's archive ritual.

    Files moved to `stale/` legitimately are reachable via `_archive` events
    (Plan 4 §7.1). Their `supersedes_path` covers the original location, so
    those paths are not flagged as missing.
    """
    referenced: set[str] = set()
    archived_supersedes: set[str] = set()

    for ev in events:
        for path in ev.get("artifacts") or []:
            if Path(path).is_absolute():
                continue
            referenced.add(_canon(path))
        sup = ev.get("supersedes_path")
        mid = ev.get("milestone_id") or ""
        if sup:
            # `_archive/*` events with supersedes_path explain a moved/archived
            # original location — exempt that path from the missing-file check.
            archived_supersedes.add(_canon(sup))
            if not mid.startswith("_archive/"):
                # supersedes_path outside an _archive/* event is unusual but
                # still treated as evidence of an intentional move.
                pass

    # Walk managed paths once, building the present set.
    present: set[str] = set()
    for folder in _MANAGED_FOLDERS:
        d = workspace / folder
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if p.is_file() and not _ignored(p, workspace):
                present.add(p.relative_to(workspace).as_posix())

    # Orphan check — silent on early runs (no references yet).
    if referenced:
        for rel in sorted(present - referenced):
            findings.warn(
                f"orphan artifact in managed path: {rel} (no ledger event references it)"
            )

    # Missing check — fires for ANY referenced relative path that isn't on
    # disk, regardless of folder. Plan 06 §4.6 needs this to cover figures
    # in domain folders (e.g., benchmark-04/fig1.png) — restricting to
    # managed folders silently let figure paths drift. Absolute paths and
    # paths leaving the workspace are still skipped (out of scope).
    for rel in sorted(referenced - present - archived_supersedes):
        if rel.startswith("/") or ".." in Path(rel).parts:
            continue
        # Verify the path doesn't exist anywhere under the workspace
        # (managed-folder walk above only populated `present` from
        # _MANAGED_FOLDERS — figures in domain folders need a direct check).
        if (workspace / rel).exists():
            continue
        findings.warn(
            f"ledger-tracked artifact missing: {rel} (referenced by an event "
            f"but not on disk and no '_archive/*' event explains the move)"
        )


def _canon(path: str) -> str:
    """Canonicalise a workspace-relative path: drop ./ prefix, trailing slashes."""
    return path.lstrip("./").rstrip("/")


def _is_in_managed_folder(rel: str) -> bool:
    parts = rel.split("/", 1)
    return parts and parts[0] in _MANAGED_FOLDERS


def _ignored(p: Path, workspace: Path) -> bool:
    parts = p.relative_to(workspace).parts
    if any(part in _DEFAULT_IGNORE_DIRS for part in parts):
        return True
    if p.name.startswith("."):
        return True
    return False


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def run(workspace: Path, *, strict: bool = False) -> Findings:
    findings = Findings()
    plan_path = workspace / "plan_of_record.md"
    ledger_path = workspace / "promise_ledger.jsonl"

    if not plan_path.exists() and not ledger_path.exists():
        findings.note(
            "no plan_of_record.md and no promise_ledger.jsonl — workspace not "
            "yet bootstrapped (graceful absence)"
        )
        return findings

    events = _load_ledger(ledger_path, findings)
    plan_milestones = _parse_plan_milestones(plan_path, findings)

    immutable_exceptions = _load_immutable_exceptions(workspace, findings)

    _check_event_schema(events, findings, immutable_exceptions)
    _check_cross_references(events, plan_milestones, findings)
    _check_lifecycle(events, findings)
    _check_plan_mtime(plan_path, events, findings)
    _check_confidence_calibration(events, findings, strict=strict)
    _check_cycles_per_finding(events, findings)
    _check_manager_interventions(events, findings)
    _check_artifact_coherence(workspace, events, findings)

    findings.note(f"events: {len(events)}, plan milestones: {len(plan_milestones)}")
    return findings


def format_text(findings: Findings) -> str:
    out: list[str] = []
    for n in findings.notes:
        out.append(f"  {n}")
    for w in findings.warnings:
        out.append(f"! WARNING: {w}")
    for e in findings.errors:
        out.append(f"x ERROR:   {e}")
    if not findings.errors and not findings.warnings:
        out.append("OK: promise_check green.")
    return "\n".join(out) + "\n"


def format_json(findings: Findings) -> str:
    return json.dumps(
        {
            "errors": findings.errors,
            "warnings": findings.warnings,
            "notes": findings.notes,
            "ok": not findings.errors,
        },
        indent=2,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate plan_of_record.md + promise_ledger.jsonl coherence."
    )
    parser.add_argument("workspace", help="Workspace root containing the artifacts.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Promote calibration warnings to errors (non-zero exit).",
    )
    args = parser.parse_args(argv)

    ws = Path(args.workspace).resolve()
    if not ws.is_dir():
        print(f"promise_check: not a directory: {ws}", file=sys.stderr)
        return 2

    findings = run(ws, strict=args.strict)
    if args.json:
        print(format_json(findings))
    else:
        print(format_text(findings))

    return 1 if findings.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
