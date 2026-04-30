"""Final auditor — end-of-run trajectory verification.

Architectural twin of `reporting.py:_run_final_reporter`. Runs once at end of
run (topic exhaustion or clean stop), BEFORE the final reporter. Reads the
plan-of-record + promise ledger + periodic reports + closure documents,
verifies the run's claims against its evidence, emits reconciliation events
(if any), and writes:

  * final_audit_report.md   — human-readable narrative
  * final_audit_summary.json — machine-readable structured input for the reporter

See docs/end-of-run-pipeline.md for the design. Key invariants this module
preserves:

  * Single-N stage heuristic: same metric and same implementation as the
    reporter (`min(max(1, tokens // 20_000), 5)` → 2N+2 stages).
  * Wall-clock cap shared with the reporter via `long_exposure.limits`.
  * File-gate rescue mirrors `reporting.py:_rescue_stage_file`.
  * Reconciliation events committed transactionally at the document stage.
  * Lessons (Plan 5) emitted from the document stage's findings, capped at
    `max(1, ceil(total_cycles / 3))`.
"""

from __future__ import annotations

import json
import math
import re as _re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from long_exposure.limits import WALL_CAP_SECONDS

# Lazy-import wrappers for cycle-loop helpers, mirroring reporting.py's pattern.
# Importing at module-load time creates a circular import (exploration.py
# re-exports this module).


def _call_agent_with_rotation(*args, **kwargs):
    from long_exposure.exploration import _call_agent_with_rotation as _impl
    return _impl(*args, **kwargs)


def _check_signal_files(*args, **kwargs):
    from long_exposure.exploration import _check_signal_files as _impl
    return _impl(*args, **kwargs)


def _compact_agent_session(*args, **kwargs):
    from long_exposure.exploration import _compact_agent_session as _impl
    return _impl(*args, **kwargs)


def _store_agent_output(*args, **kwargs):
    from long_exposure.exploration import _store_agent_output as _impl
    return _impl(*args, **kwargs)


def _total_context_tokens(*args, **kwargs):
    from long_exposure.exploration import _total_context_tokens as _impl
    return _impl(*args, **kwargs)


def _is_stop_requested() -> bool:
    import long_exposure.exploration as _exploration
    return bool(_exploration._stop_requested)


# ---------------------------------------------------------------------------
# Stage planning — single-N heuristic, mirrors reporter exactly.
# ---------------------------------------------------------------------------


_TOKEN_THRESHOLD = 20_000
_N_MAX = 5  # Stage 3 §4.4: cap removed; constant kept (one-line revertable).


def _count_tokens(path: Path) -> int:
    """~4 chars per token. Best-effort; never raises."""
    try:
        return len(path.read_text()) // 4
    except (OSError, UnicodeDecodeError):
        return 0


def _find_closure_docs(workspace: Path) -> list[Path]:
    """Heuristic: filenames containing CLOSURE / closure_letter / SUPERSEDES."""
    if not workspace.exists():
        return []
    docs: list[Path] = []
    for p in workspace.rglob("*.md"):
        name = p.name.upper()
        if "CLOSURE" in name or "SUPERSEDES" in name:
            docs.append(p)
    return docs


_FIGURE_SUFFIXES = (".png", ".svg", ".jpg", ".jpeg", ".gif")
_FIGURE_IGNORE_DIRS = {
    ".venv", "venv", ".git", "__pycache__", "node_modules",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "stale",
}


def _compute_figure_coverage(workspace: Path) -> dict:
    """Count figure files in the workspace and cross-reference the ledger.

    Returns:
      {
        "figures_present": <int>,         # files on disk
        "figures_in_ledger": <int>,       # paths referenced via ledger artifacts
        "missing_figures": [...],         # ledger references with no file on disk
        "orphan_figures": [...]           # files on disk not referenced by ledger
      }

    Best-effort and bounded: walks managed folders + domain folders only,
    skips hidden / cache / stale paths. The agent producing the real
    summary may override this with its own richer assessment.
    """
    if not workspace.exists():
        return {
            "figures_present": 0, "figures_in_ledger": 0,
            "missing_figures": [], "orphan_figures": [],
        }

    present: set[str] = set()
    try:
        for p in workspace.rglob("*"):
            # Short-circuit by relative-path parts BEFORE expensive checks
            # (rglob walks .git/objects/ on full repos — O(100k) blobs there
            # would each trigger a stat+suffix check otherwise).
            try:
                parts = p.relative_to(workspace).parts
            except ValueError:
                continue
            if any(part in _FIGURE_IGNORE_DIRS or part.startswith(".") for part in parts):
                continue
            if p.suffix.lower() not in _FIGURE_SUFFIXES:
                continue
            if not p.is_file():
                continue
            present.add("/".join(parts))
    except OSError:
        pass

    ledger_path = workspace / "promise_ledger.jsonl"
    referenced: set[str] = set()
    if ledger_path.exists():
        try:
            for line in ledger_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for art in (ev.get("artifacts") or []):
                    if not isinstance(art, str):
                        continue
                    canon = art.lstrip("./").rstrip("/")
                    if canon.lower().endswith(_FIGURE_SUFFIXES):
                        referenced.add(canon)
        except OSError:
            pass

    missing = sorted(referenced - present)
    orphans = sorted(present - referenced)
    return {
        "figures_present": len(present),
        "figures_in_ledger": len(referenced),
        "missing_figures": missing[:50],   # cap so the JSON stays bounded
        "orphan_figures": orphans[:50],
    }


def _estimate_audit_input_tokens(workspace: Path) -> int:
    plan = _count_tokens(workspace / "plan_of_record.md")
    ledger = _count_tokens(workspace / "promise_ledger.jsonl")
    reports = sum(_count_tokens(p) for p in workspace.rglob("report_cycles_*.md"))
    closures = sum(_count_tokens(p) for p in _find_closure_docs(workspace))
    return plan + ledger + reports + closures


def _final_auditor_stage_count(input_tokens: int) -> tuple[int, int]:
    """Returns (N, total_stages) where total = explore (1) + N verify + N test + document (1).

    Stage 3 §4.4: the explicit N cap was removed so multi-day runs with
    ~1M tokens of prior reports can spend the stages they need. Wall-cap
    (limits.WALL_CAP_SECONDS) remains the real ceiling.
    """
    n = max(1, input_tokens // _TOKEN_THRESHOLD)
    return n, 1 + n + n + 1


def _stage_label(stage: int, n: int) -> str:
    """Human-readable label per stage_index (1-based)."""
    if stage == 1:
        return "explore"
    if stage == 2 + 2 * n:
        return "document"
    if 2 <= stage <= 1 + n:
        return f"verify ({stage - 1}/{n})"
    if 2 + n <= stage <= 1 + 2 * n:
        return f"test ({stage - 1 - n}/{n})"
    return f"stage-{stage}"


def _expected_file_for_stage(stage: int, n: int, workspace: Path) -> Path:
    """Each stage writes one file; the file is the gate."""
    label = _stage_label(stage, n)
    if label == "explore":
        return workspace / "final_audit_explore.md"
    if label == "document":
        return workspace / "final_audit_report.md"
    safe = label.replace("(", "").replace(")", "").replace(" ", "_").replace("/", "of")
    return workspace / f"final_audit_{safe}.md"


# ---------------------------------------------------------------------------
# File-gate rescue (mirrors reporting.py)
# ---------------------------------------------------------------------------


def _extract_audit_content(output_text: str) -> str:
    m = _re.search(
        r"\[OUTPUT:\s*final_audit_stage\]\s*\n(.*?)(?:\[END OUTPUT)",
        output_text,
        _re.DOTALL,
    )
    text = m.group(1).strip() if m else output_text.strip()
    heading = _re.search(r"^(#+ )", text, _re.MULTILINE)
    if heading:
        text = text[heading.start():]
    return text


def _rescue_audit_stage_file(expected: Path, output_text: str) -> bool:
    content = _extract_audit_content(output_text)
    if not content:
        return False
    try:
        if expected.exists():
            with expected.open("a") as f:
                f.write("\n\n" + content + "\n")
        else:
            expected.write_text(content + "\n")
        return True
    except OSError as e:
        print(f"[long-exposure]   Audit rescue write failed: {e}", flush=True)
        return False


# ---------------------------------------------------------------------------
# Findings / lessons file management
# ---------------------------------------------------------------------------


_FINDINGS_FILE = "final_audit_findings.jsonl"
_LESSON_CANDIDATES_FILE = "final_audit_lessons.jsonl"


def _findings_path(workspace: Path) -> Path:
    return workspace / _FINDINGS_FILE


def _read_findings(workspace: Path) -> list[dict]:
    p = _findings_path(workspace)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------------------
# Lesson-emission helpers (Plan 5)
# ---------------------------------------------------------------------------


def _max_lessons_for_run(total_cycles: int) -> int:
    """Plan 5 §2.2 cap: 1 lesson per 3 cycles, minimum 1."""
    return max(1, math.ceil(max(1, total_cycles) / 3))


def _count_total_cycles(workspace: Path, run_id: str | None) -> int:
    """Count root + fan-out cycles for the current run via the promise ledger.

    Falls back to the highest cycle number across all events when run_id is
    missing or empty (graceful — Plan 5 §2.2).
    """
    ledger = workspace / "promise_ledger.jsonl"
    if not ledger.exists():
        return 1
    cycles: set[tuple[int, str]] = set()
    for line in ledger.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if run_id and ev.get("run_id") != run_id:
            continue
        c = ev.get("cycle", 0)
        fid = ev.get("fork_id") or "_root"
        try:
            cycles.add((int(c), str(fid)))
        except (TypeError, ValueError):
            continue
    return max(1, len(cycles))


def _commit_lessons(
    workspace: Path,
    conn,
    run_id: str | None,
    total_cycles: int,
) -> list[dict]:
    """Read final_audit_lessons.jsonl, take the first N (in agent's stated rank
    order), and commit them to sessions.db with record_type='lesson' and to
    LESSONS.md. Returns the list of committed lesson dicts.

    Hybrid enforcement (Plan 5 §2.2): the agent's role text states the cap and
    asks for ranking; this function enforces it deterministically regardless.
    """
    p = workspace / _LESSON_CANDIDATES_FILE
    if not p.exists():
        return []

    candidates: list[dict] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(c, dict) and c.get("slug") and c.get("content"):
            candidates.append(c)

    if not candidates:
        return []

    cap = _max_lessons_for_run(total_cycles)
    chosen = candidates[:cap]
    if len(candidates) > cap:
        print(
            f"[long-exposure]   Lessons: {len(candidates)} candidates → committing "
            f"top {cap} (cap = max(1, ceil({total_cycles}/3)))",
            flush=True,
        )

    # Commit to sessions.db. Idempotent: if a lesson with the same topic
    # slug already exists in the DB (e.g., this auditor was re-run after
    # a crash, or a prior run on this workspace already emitted the same
    # lesson), skip silently. Plan 5 §2.2's full lookup-and-merge protocol
    # is a future enhancement; this simpler "exists → skip" guarantees
    # we never duplicate lessons under the most common failure mode.
    try:
        from auto_compact.db import store_session
    except ImportError:
        store_session = None  # type: ignore

    existing_slugs: set[str] = set()
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT topic FROM sessions WHERE record_type = 'lesson'"
            ).fetchall()
            existing_slugs = {r[0] for r in rows if r[0]}
        except Exception:
            pass

    committed: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for lesson in chosen:
        slug = lesson["slug"]
        content = lesson["content"]
        topic = f"lesson/{slug}"
        if topic in existing_slugs:
            print(
                f"[long-exposure]   Lesson skipped (slug already in DB): {slug}",
                flush=True,
            )
            continue
        keywords = lesson.get("keywords", "lesson")
        sid = str(uuid.uuid4())
        if store_session is not None and conn is not None:
            try:
                store_session(
                    conn,
                    session_id=sid,
                    parent_id=None,
                    depth=0,
                    timestamp=now_iso,
                    summary_xml=content,
                    record_type="lesson",
                    topic=topic,
                    subtopic=lesson.get("subtopic"),
                    tools=lesson.get("tools"),
                    keywords=keywords,
                )
                existing_slugs.add(topic)
            except Exception as e:
                print(f"[long-exposure]   Lesson commit failed: {e}", flush=True)
                continue
        committed.append({**lesson, "session_id": sid, "committed_at": now_iso})

    # Mirror to LESSONS.md (latest-per-slug; rewrite from scratch).
    if committed:
        _write_lessons_md(workspace, committed)

    return committed


def _write_lessons_md(workspace: Path, committed: list[dict]) -> None:
    """Render LESSONS.md showing latest version per unique slug."""
    by_slug: dict[str, dict] = {}
    for lesson in committed:
        by_slug[lesson["slug"]] = lesson  # last-wins
    lines = ["# Cross-Cutting Lessons", ""]
    lines.append(
        "Curated findings across runs. Updated by the final auditor at run end. "
        "The DB record (record_type='lesson') is canonical; this file mirrors "
        "for human readability."
    )
    lines.append("")
    for slug, lesson in by_slug.items():
        lines.append("---")
        lines.append("")
        lines.append(f"## Lesson: {slug}")
        ts = lesson.get("committed_at", "")
        lines.append(f"*Committed: {ts}*")
        lines.append("")
        lines.append(lesson["content"].strip())
        lines.append("")
    try:
        (workspace / "LESSONS.md").write_text("\n".join(lines) + "\n")
    except OSError as e:
        print(f"[long-exposure]   LESSONS.md write failed: {e}", flush=True)


# ---------------------------------------------------------------------------
# Reconciliation event commit (transactional at document stage)
# ---------------------------------------------------------------------------


def _commit_reconciliation_events(workspace: Path, run_id: str, cycle: int) -> int:
    """Read findings, take entries marked `reconcile: true`, append as ledger
    events with agent='final_auditor'. Idempotent on resume — UUID event_ids
    prevent duplicates if the same finding is re-processed.

    Returns count of events committed.
    """
    findings = _read_findings(workspace)
    if not findings:
        return 0

    ledger = workspace / "promise_ledger.jsonl"
    seen_ids: set[str] = set()
    if ledger.exists():
        for line in ledger.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                if isinstance(ev, dict) and ev.get("event_id"):
                    seen_ids.add(ev["event_id"])
            except json.JSONDecodeError:
                continue

    committed = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    # Deterministic namespace for derived event_ids — fixed UUID so the
    # same finding (run_id + milestone_id + narrative) hashes to the same
    # event_id across re-runs. This is what makes the document-stage
    # commit truly idempotent even when the agent omits explicit
    # event_ids on its findings.
    _RECONCILE_NS = uuid.UUID("11111111-1111-5111-8111-111111111111")
    with ledger.open("a") as f:
        for finding in findings:
            if not finding.get("reconcile"):
                continue
            if finding.get("event_id"):
                eid = finding["event_id"]
            else:
                # Derive a stable UUIDv5 from the finding's identifying fields.
                key = "|".join((
                    str(run_id),
                    str(finding.get("milestone_id", "")),
                    str(finding.get("status", "")),
                    str(finding.get("narrative", ""))[:200],
                ))
                eid = str(uuid.uuid5(_RECONCILE_NS, key))
            if eid in seen_ids:
                continue  # idempotent
            event = {
                "event_id": eid,
                "ts": finding.get("ts", now_iso),
                "run_id": run_id,
                "cycle": cycle,
                "agent": "final_auditor",
                "milestone_id": finding.get("milestone_id", "_run/reconciliation"),
                "status": finding.get("status", "in-progress"),
                "confidence": finding.get(
                    "confidence",
                    {
                        "level": "medium",
                        "rationale": "final auditor reconciliation",
                        "assessor": "final_auditor",
                    },
                ),
                "narrative": finding.get("narrative", "reconciliation event"),
            }
            for opt_field in ("supersedes", "evidence", "artifacts", "scope"):
                if opt_field in finding:
                    event[opt_field] = finding[opt_field]
            f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            seen_ids.add(eid)
            committed += 1
    return committed


# ---------------------------------------------------------------------------
# Reading final_audit_summary.json — used by the reporter.
# ---------------------------------------------------------------------------


def read_final_audit_summary(workspace: Path) -> dict | None:
    """Return the parsed final_audit_summary.json, or None if absent/invalid."""
    p = workspace / "final_audit_summary.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Stage runner
# ---------------------------------------------------------------------------


def _run_final_auditor(
    final_auditor_def: dict,
    task: str,
    config: dict,
    results: dict,
    score_inputs: dict,
    conn,
    cycle: int,
    last_session_id: str | None,
    context_window: int,
    compact_at: int,
    data_dir: Path | None = None,
    agent_sessions: dict | None = None,
    agent_summaries: dict | None = None,
) -> str | None:
    """Execute the final auditor in 2+2N stages with shared wall-cap."""
    workspace = Path(config.get("working_directory") or ".")
    run_id = (
        results.get("run_id")
        or score_inputs.get("run_id")
        or f"run-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ')}"
    )

    input_tokens = _estimate_audit_input_tokens(workspace)
    n, total_stages = _final_auditor_stage_count(input_tokens)
    document_stage = total_stages  # last

    print(f"\n{'='*60}", flush=True)
    print("[long-exposure] === Final Auditor ===", flush=True)
    print(
        f"[long-exposure] Audit inputs: ~{input_tokens:,} tokens; "
        f"N={n}, total stages={total_stages} (1 explore + {n} verify + {n} test + 1 document)",
        flush=True,
    )

    audit_sessions: dict = {}
    audit_summaries: dict = {}
    pending_rescue_warning: str | None = None
    start_ts = time.monotonic()

    # Reset per-run findings file at start so a re-run on resume cannot
    # double-commit reconciliation events from a stale prior pass.
    fp = _findings_path(workspace)
    if fp.exists() and not (data_dir and (data_dir / "long-exposure.resume").exists()):
        try:
            fp.unlink()
        except OSError:
            pass

    for stage in range(1, total_stages + 1):
        if data_dir:
            _check_signal_files(data_dir)
        if _is_stop_requested():
            print("[long-exposure]   Stop signal — ending final audit early.", flush=True)
            break

        # Wall-cap pre-check. Document stage always runs (it's the commit step).
        #
        # IMPLEMENTATION NOTE: same pattern as reporting.py. We re-bind the
        # loop variable to `document_stage` and run the document body in
        # this iteration. The `if wall_cap_hit and stage == document_stage:
        # break` at the bottom of the loop is load-bearing — without it,
        # the for-loop would assign the next mid-stage value and re-run the
        # document body, overwriting final_audit_report.md. A while-loop
        # refactor was considered and rejected as not
        # worth the churn.
        if stage < document_stage and (time.monotonic() - start_ts) > WALL_CAP_SECONDS:
            print(
                f"[long-exposure]   FINAL AUDIT WALL CAP exceeded "
                f"({WALL_CAP_SECONDS}s) — skipping to document stage.",
                flush=True,
            )
            stage = document_stage
            label = "document"
            expected = _expected_file_for_stage(stage, n, workspace)
            wall_cap_hit = True
        else:
            label = _stage_label(stage, n)
            expected = _expected_file_for_stage(stage, n, workspace)
            wall_cap_hit = False

        stage_results = dict(results)
        stage_results["stage"] = f"{stage} of {total_stages} ({label})"
        stage_results["total_stages"] = str(total_stages)
        stage_results["stage_index"] = str(stage)
        stage_results["expected_file"] = str(expected)
        stage_results["working_dir"] = str(workspace)
        stage_results["rescue_warning"] = pending_rescue_warning or "(none)"
        stage_results["findings_file"] = str(_findings_path(workspace))
        stage_results["lesson_candidates_file"] = str(workspace / _LESSON_CANDIDATES_FILE)
        stage_results["wall_cap_hit"] = "true" if wall_cap_hit else "false"
        pending_rescue_warning = None

        print(
            f"\n[long-exposure] --- Final Audit Stage {stage}/{total_stages} ({label}) ---",
            flush=True,
        )

        result = _call_agent_with_rotation(
            agent_name="final_auditor",
            agent_def=final_auditor_def,
            sessions_dict=audit_sessions,
            task=task,
            config=config,
            results=stage_results,
            score_inputs=score_inputs,
            agent_summaries=audit_summaries,
        )

        if result["status"] != "ok":
            err = result.get("error", "unknown")
            print(f"[long-exposure]   final_auditor stage {stage}: FAILED — {err}", flush=True)
            # Drop session and try once more with a fresh start
            audit_sessions.pop("final_auditor", None)
            result = _call_agent_with_rotation(
                agent_name="final_auditor",
                agent_def=final_auditor_def,
                sessions_dict=audit_sessions,
                task=task,
                config=config,
                results=stage_results,
                score_inputs=score_inputs,
                agent_summaries=audit_summaries,
            )

        if result["status"] == "ok":
            usage = result.get("usage", {})
            dur = result.get("duration_ms", 0) / 1000
            total_ctx = _total_context_tokens(usage)
            print(
                f"[long-exposure]   final_auditor: ok ({dur:.1f}s, ctx:{total_ctx:,}tok)",
                flush=True,
            )
            output_text = "\n\n".join(result["outputs"].values())
            last_session_id = _store_agent_output(
                conn, "final_auditor", final_auditor_def, output_text,
                cycle, last_session_id,
                current_topic="Final Audit",
            )

            # FILE GATE
            if expected and not expected.exists():
                print(
                    f"[long-exposure]   FILE GATE: {expected.name} missing — "
                    f"rescuing from output.",
                    flush=True,
                )
                if _rescue_audit_stage_file(expected, output_text):
                    print(
                        f"[long-exposure]   FILE GATE: rescued "
                        f"{expected.name} ({expected.stat().st_size:,}b)",
                        flush=True,
                    )
                pending_rescue_warning = (
                    f"STAGE {stage} FILE GATE FAILED. Expected {expected} not "
                    f"written; orchestrator rescued from [OUTPUT] block. In the "
                    f"next stage, verify and overwrite {expected} if the "
                    f"rescued content is incomplete."
                )

            # Compaction
            if total_ctx >= compact_at:
                try:
                    last_session_id = _compact_agent_session(
                        "final_auditor", final_auditor_def, config,
                        audit_sessions, audit_summaries,
                        conn, cycle, last_session_id,
                    )
                except Exception as e:
                    print(
                        f"[long-exposure]   final_auditor compact rate-limited "
                        f"(non-fatal): {e!r}", flush=True,
                    )
        else:
            print(
                f"[long-exposure]   final_auditor stage {stage}: skipped after retry.",
                flush=True,
            )

        # If we just executed a forced document stage from wall-cap, stop.
        if wall_cap_hit and stage == document_stage:
            break

    # ----- Document stage post-processing: reconcile + lessons + summary -----
    total_cycles = _count_total_cycles(workspace, run_id)
    reconciliations = 0
    try:
        reconciliations = _commit_reconciliation_events(workspace, run_id, cycle)
        if reconciliations:
            print(
                f"[long-exposure]   Reconciliation: {reconciliations} event(s) "
                f"committed to promise_ledger.jsonl",
                flush=True,
            )
    except Exception as e:
        print(f"[long-exposure]   Reconciliation commit failed: {e!r}", flush=True)

    lessons = []
    try:
        lessons = _commit_lessons(workspace, conn, run_id, total_cycles)
        if lessons:
            print(
                f"[long-exposure]   Lessons: {len(lessons)} committed (cap "
                f"{_max_lessons_for_run(total_cycles)})",
                flush=True,
            )
    except Exception as e:
        print(f"[long-exposure]   Lesson commit failed: {e!r}", flush=True)

    # FINAL GATE: render final_audit_report.pdf if the markdown landed.
    # Mirrors reporting.py's PDF gate. Best-effort; absence of pandoc/tectonic
    # is non-fatal — markdown is always usable.
    audit_md = workspace / "final_audit_report.md"
    audit_pdf = workspace / "final_audit_report.pdf"
    if audit_md.exists() and not audit_pdf.exists():
        try:
            from long_exposure.reporting import render_pdf
            render_pdf(str(workspace), stem="final_audit_report")
        except Exception as e:
            print(f"[long-exposure]   Audit PDF render error: {e}", flush=True)

    # Ensure final_audit_summary.json exists, even if the agent skipped it.
    summary_path = workspace / "final_audit_summary.json"
    if not summary_path.exists():
        # Synthesize a minimal summary so the reporter can ingest something.
        synth = {
            "run_id": run_id,
            "milestone_status_distribution": {},
            "plan_milestone_state": {},
            "residual_debt": [],
            "future_work": [],
            "findings": {},
            "reconciliation_events_emitted": reconciliations,
            "lessons_emitted": [l["slug"] for l in lessons],
            "promise_check_status": "unknown",
            "wall_cap_exceeded": (time.monotonic() - start_ts) > WALL_CAP_SECONDS,
            # Plan 06 §4.7: figure-coverage metric. Best-effort count from a
            # workspace walk; the agent overrides with its richer assessment
            # when it writes the summary itself. Single-glance count for
            # the operator: how many figure files exist on disk.
            "figure_coverage": _compute_figure_coverage(workspace),
        }
        try:
            summary_path.write_text(json.dumps(synth, indent=2) + "\n")
            print(
                f"[long-exposure]   final_audit_summary.json synthesized "
                f"(agent did not write it).",
                flush=True,
            )
        except OSError:
            pass

    # Merge auditor sessions back so they persist across resumes.
    if agent_sessions is not None:
        agent_sessions.update(audit_sessions)
    if agent_summaries is not None:
        agent_summaries.update(audit_summaries)

    print("[long-exposure] Final audit complete.", flush=True)
    return last_session_id
