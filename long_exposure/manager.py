"""Cron-polled manager for long-exposure runs.

The manager is intentionally sidecar infrastructure. It reads state and
workspace artifacts, writes an assessment log on every poll, and only invokes a
manager agent when deterministic counters say the run needs action. A manager
failure must never block the main researcher -> worker -> auditor loop.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from long_exposure import provider as _provider
from long_exposure import telemetry
from long_exposure.conductor import parse_outputs
from long_exposure.exploration import (
    DEFAULT_SCORE_PATH,
    _call_agent_with_rotation,
    _resolve_state_path,
    load_exploration_score,
    load_state,
)
from long_exposure.orchestrator import load_config, resolve_instance_dir
from long_exposure.tools import promise_check
from long_exposure.workspace_bootstrap import append_ledger_event, summarize_ledger


VERDICT_HEALTHY = "healthy"
VERDICT_WATCH = "watch"
VERDICT_ACT = "act"
VERDICT_ESCALATE = "escalate"

GUIDE_FILE = "long-exposure.guide"
PAUSE_FILE = "long-exposure.pause-for-user"
NOTIFICATIONS_FILE = "manager_notifications.jsonl"


@dataclass(frozen=True)
class ManagerDecision:
    verdict: str
    event_class: str
    pattern: str
    rationale: str
    guidance: str
    evidence: list[str]


def _utc_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for raw in path.read_text().splitlines():
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


def _safe_read(path: Path, max_chars: int = 80_000) -> str:
    try:
        text = path.read_text()
    except OSError as exc:
        return f"[read error: {path}: {exc}]"
    if len(text) > max_chars:
        return text[-max_chars:] + "\n[truncated to recent tail]"
    return text


def _latest_reports(workspace: Path, max_reports: int = 3) -> list[str]:
    from long_exposure import paths
    reports = sorted(
        paths.iter_cycle_report_paths(workspace),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
    )
    return [
        f"{p.relative_to(workspace)}\n{_safe_read(p, max_chars=20_000)}"
        for p in reports[-max_reports:]
    ]


def _latest_manager_events(events: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    manager = [
        ev for ev in events
        if str(ev.get("milestone_id") or "").startswith("_manager/")
    ]
    manager.sort(key=lambda e: str(e.get("ts") or ""))
    return manager[-limit:]


def _active_milestone_streaks(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    terminal = {"validated", "deferred", "superseded", "invalidated"}
    by_mid: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        mid = str(ev.get("milestone_id") or "")
        if not mid or mid.startswith("_"):
            continue
        by_mid.setdefault(mid, []).append(ev)

    out: dict[str, dict[str, Any]] = {}
    for mid, evs in by_mid.items():
        evs.sort(key=lambda e: (str(e.get("ts") or ""), int(e.get("_line") or 0)))
        latest_terminal = -1
        for i, ev in enumerate(evs):
            if ev.get("status") in terminal:
                latest_terminal = i
        active = evs[latest_terminal + 1:]
        cycles = sorted({
            int(ev["cycle"])
            for ev in active
            if isinstance(ev.get("cycle"), int)
        })
        mechanism_parts = []
        for ev in active:
            confidence = ev.get("confidence")
            rationale = (
                confidence.get("rationale")
                if isinstance(confidence, dict)
                else ""
            )
            mechanism_parts.append(
                str(ev.get("narrative") or "") + "\n" + str(rationale or "")
            )
        mechanism_text = "\n".join(mechanism_parts).lower()
        has_mechanism = any(
            marker in mechanism_text
            for marker in (
                "<mechanism",
                "mechanism statement",
                "falsification_criteria",
                "special_points_evaluated",
            )
        )
        if cycles:
            out[mid] = {
                "active_cycles": len(cycles),
                "cycles": cycles,
                "latest_status": active[-1].get("status") if active else None,
                "has_mechanism_marker": has_mechanism,
            }
    return out


def _extract_brief_contract(results: dict[str, Any]) -> dict[str, Any]:
    brief = str(results.get("research_brief") or "")
    lower = brief.lower()
    return {
        "present": bool(brief.strip()),
        "axis_varied": "<axis_varied>" in lower,
        "axes_held_constant": "<axes_held_constant>" in lower,
        "cycle_kind": "<cycle_kind>" in lower,
        "mechanism": "<mechanism" in lower,
    }


def build_manager_snapshot(
    *,
    workspace: Path,
    state_path: Path,
    data_dir: Path,
    strict: bool = False,
) -> dict[str, Any]:
    state = load_state(state_path) or {}
    events = _read_jsonl(workspace / "promise_ledger.jsonl")
    findings = promise_check.run(workspace, strict=strict)
    health_events = _read_jsonl(data_dir / "health_events.jsonl")
    active = _active_milestone_streaks(events)

    stale_milestones = {
        mid: info for mid, info in active.items()
        if info["active_cycles"] >= 3 and not info["has_mechanism_marker"]
    }
    repeated_manager = False
    manager_events = _latest_manager_events(events, limit=3)
    recent_required = [
        ev for ev in manager_events
        if ev.get("status") == "action_required"
    ]
    if len(recent_required) >= 2:
        classes = {
            str(ev.get("milestone_id") or "").split("/", 1)[-1]
            for ev in recent_required
        }
        repeated_manager = len(classes) == 1

    return {
        "poll_ts": _utc_iso(),
        "workspace": str(workspace),
        "state_path": str(state_path),
        "cycle": state.get("cycle", 0),
        "run_id": state.get("run_id") or "run-unknown",
        "state_timestamp": state.get("timestamp"),
        "promise_check": {
            "errors": findings.errors,
            "warnings": findings.warnings,
            "notes": findings.notes,
        },
        "ledger": {
            "events": len(events),
            "latest_cycle": max(
                (ev.get("cycle") for ev in events if isinstance(ev.get("cycle"), int)),
                default=0,
            ),
            "active_milestones": active,
            "stale_milestones": stale_milestones,
            "manager_events": manager_events,
            "repeated_recent_manager_action": repeated_manager,
        },
        "latest_research_brief_contract": _extract_brief_contract(
            state.get("results") or {}
        ),
        "recent_health_events": health_events[-10:],
        "latest_reports": _latest_reports(workspace),
        "promise_ledger_summary": summarize_ledger(workspace, max_chars=16_000),
    }


def decide_from_snapshot(snapshot: dict[str, Any]) -> ManagerDecision:
    promise = snapshot.get("promise_check") or {}
    ledger = snapshot.get("ledger") or {}
    stale = ledger.get("stale_milestones") or {}
    brief = snapshot.get("latest_research_brief_contract") or {}

    if ledger.get("repeated_recent_manager_action"):
        return ManagerDecision(
            verdict=VERDICT_ESCALATE,
            event_class="repeated-intervention",
            pattern="Manager intervention repeated without apparent resolution.",
            rationale=(
                "The same manager action appeared at least twice in the recent "
                "manager history. Repeating guidance is unlikely to help."
            ),
            guidance=(
                "<manager_intervention verdict=\"escalate\" "
                "event_class=\"repeated-intervention\">\n"
                "Pause for human review before another same-pattern intervention. "
                "Summarize the unresolved manager guidance, the current cycle, and "
                "what evidence would clear the intervention.\n"
                "</manager_intervention>"
            ),
            evidence=["promise_ledger.jsonl"],
        )

    if promise.get("errors"):
        return ManagerDecision(
            verdict=VERDICT_ACT,
            event_class="ledger-integrity",
            pattern="Promise ledger or plan integrity errors are present.",
            rationale="promise_check reported schema or lifecycle errors.",
            guidance=(
                "<manager_intervention verdict=\"act\" "
                "event_class=\"ledger-integrity\">\n"
                "Before opening a new research direction, repair or explicitly "
                "supersede the plan/ledger inconsistency reported by promise_check. "
                "The next research_brief must cite the affected milestone IDs.\n"
                "</manager_intervention>"
            ),
            evidence=["promise_ledger.jsonl", "plan_of_record.md"],
        )

    if stale:
        mid, info = sorted(
            stale.items(),
            key=lambda item: item[1].get("active_cycles", 0),
            reverse=True,
        )[0]
        return ManagerDecision(
            verdict=VERDICT_ACT,
            event_class="mechanism-overdue",
            pattern=f"{mid} has {info['active_cycles']} active cycles without a mechanism marker.",
            rationale=(
                "A milestone has accumulated three or more active cycles since "
                "the latest terminal event without ledger evidence of a mechanism "
                "statement."
            ),
            guidance=(
                "<manager_intervention verdict=\"act\" "
                "event_class=\"mechanism-overdue\">\n"
                f"Next researcher brief must make {mid} an analytical or mixed "
                "cycle unless the auditor explicitly supersedes this intervention. "
                "Include <cycle_kind>, <axis_varied>, <axes_held_constant>, and a "
                "<mechanism> block with equations or mechanism statement, special "
                "points evaluated, and falsification criteria. Do not run another "
                "same-axis empirical variant first.\n"
                "</manager_intervention>"
            ),
            evidence=["promise_ledger.jsonl"],
        )

    if int(snapshot.get("cycle") or 0) >= 2 and brief.get("present") and not all(
        brief.get(k) for k in ("axis_varied", "axes_held_constant", "cycle_kind")
    ):
        return ManagerDecision(
            verdict=VERDICT_WATCH,
            event_class="brief-contract-missing",
            pattern="Latest research_brief is missing one or more investigation-discipline tags.",
            rationale=(
                "The conditioning now asks for axis and cycle-kind fields. This is "
                "watch-only unless a stale milestone counter also fires."
            ),
            guidance="",
            evidence=["exploration_state.json"],
        )

    if promise.get("warnings"):
        return ManagerDecision(
            verdict=VERDICT_WATCH,
            event_class="validator-warnings",
            pattern="promise_check warnings are present.",
            rationale="Warnings exist but no manager intervention threshold fired.",
            guidance="",
            evidence=["promise_ledger.jsonl", "plan_of_record.md"],
        )

    return ManagerDecision(
        verdict=VERDICT_HEALTHY,
        event_class="no-action",
        pattern="No multi-cycle oversight counters fired.",
        rationale="Counters are green or below intervention threshold.",
        guidance="",
        evidence=[],
    )


def _format_snapshot_for_agent(snapshot: dict[str, Any]) -> str:
    compact = dict(snapshot)
    reports = compact.pop("latest_reports", [])
    text = json.dumps(compact, indent=2, default=str)
    if reports:
        text += "\n\n# Latest Periodic Reports\n\n" + "\n\n---\n\n".join(reports)
    return text


def _call_manager_agent(
    *,
    agent_def: dict[str, Any],
    task: str,
    config: dict[str, Any],
    snapshot: dict[str, Any],
    state: dict[str, Any],
) -> str | None:
    manager_sessions = dict(state.get("manager_agent_sessions") or {})
    manager_summaries = dict(state.get("manager_agent_summaries") or {})
    result = _call_agent_with_rotation(
        agent_name="manager",
        agent_def=agent_def,
        sessions_dict=manager_sessions,
        task=task,
        config=config,
        results={
            "manager_snapshot": _format_snapshot_for_agent(snapshot),
            "promise_ledger_summary": snapshot.get("promise_ledger_summary", ""),
        },
        score_inputs={"directive": task},
        agent_summaries=manager_summaries,
    )
    if result.get("status") != "ok":
        return None
    outputs = result.get("outputs") or {}
    intervention = outputs.get("manager_intervention")
    if intervention and intervention.strip():
        return intervention.strip()
    raw = result.get("raw") or result.get("result")
    if raw:
        parsed = parse_outputs(str(raw), ["manager_intervention"])
        return (parsed.get("manager_intervention") or "").strip() or None
    return None


def _write_assessment(
    data_dir: Path,
    snapshot: dict[str, Any],
    decision: ManagerDecision,
    intervention_text: str | None,
) -> Path:
    out_dir = data_dir / "manager_assessments"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"manager_assessment_{_utc_slug()}.md"
    promise = snapshot.get("promise_check") or {}
    body = [
        "# Manager Assessment",
        "",
        f"- Poll: {snapshot.get('poll_ts')}",
        f"- Cycle: {snapshot.get('cycle')}",
        f"- Verdict: {decision.verdict}",
        f"- Event class: {decision.event_class}",
        f"- Pattern: {decision.pattern}",
        "",
        "## Rationale",
        decision.rationale,
        "",
        "## Counters",
        f"- promise_check errors: {len(promise.get('errors') or [])}",
        f"- promise_check warnings: {len(promise.get('warnings') or [])}",
        f"- ledger events: {(snapshot.get('ledger') or {}).get('events', 0)}",
        f"- stale milestones: {len((snapshot.get('ledger') or {}).get('stale_milestones') or {})}",
        "",
        "## Intervention",
        intervention_text or "[none]",
        "",
    ]
    path.write_text("\n".join(body))
    return path


def _append_manager_event(
    workspace: Path,
    snapshot: dict[str, Any],
    decision: ManagerDecision,
    assessment_path: Path,
    intervention_text: str | None,
) -> None:
    if decision.verdict == VERDICT_HEALTHY:
        status = "validated"
        level = "high"
    elif decision.verdict == VERDICT_WATCH:
        status = "in-progress"
        level = "medium"
    else:
        status = "action_required"
        level = "high" if decision.verdict == VERDICT_ESCALATE else "medium"

    event = {
        "event_id": str(uuid.uuid4()),
        "ts": _utc_iso(),
        "run_id": snapshot.get("run_id") or "run-unknown",
        "cycle": int(snapshot.get("cycle") or 0),
        "agent": "manager",
        "milestone_id": f"_manager/{decision.event_class}",
        "status": status,
        "confidence": {
            "level": level,
            "rationale": decision.rationale,
            "assessor": "manager",
        },
        "narrative": intervention_text or decision.pattern,
        "manager_verdict": decision.verdict,
        "evidence": decision.evidence,
        "manager_assessment_path": str(assessment_path),
    }
    try:
        event["artifacts"] = [assessment_path.relative_to(workspace).as_posix()]
    except ValueError:
        event["process_artifacts"] = [str(assessment_path)]
    append_ledger_event(workspace, event)


def _write_guidance(data_dir: Path, text: str) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / GUIDE_FILE
    existing = ""
    if path.exists():
        existing = path.read_text().strip()
    body = text.strip()
    if existing:
        body = existing + "\n\n" + body
    path.write_text(body.strip() + "\n")
    return path


def _append_notification(
    data_dir: Path,
    snapshot: dict[str, Any],
    decision: ManagerDecision,
    assessment_path: Path | None,
    *,
    guide_written: bool = False,
) -> Path:
    """Append one structured poll notice for launchers/tails.

    This is intentionally disk-backed JSONL rather than a direct terminal or
    CLI integration. Cron, foreground launchers, and future CLI adapters can
    all consume the same artifact without the manager needing to know who
    launched the run.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / NOTIFICATIONS_FILE
    if decision.guidance:
        summary = decision.guidance.splitlines()[0].strip()
    else:
        summary = decision.pattern or decision.rationale
    record = {
        "ts": _utc_iso(),
        "cycle": int(snapshot.get("cycle") or 0),
        "run_id": snapshot.get("run_id"),
        "verdict": decision.verdict,
        "event_class": decision.event_class,
        "summary": summary[:500],
        "assessment_path": (
            str(assessment_path) if assessment_path is not None else None
        ),
        "guide_written": bool(guide_written),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return path


@contextlib.contextmanager
def _poll_lock(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / "manager.lock"
    with open(lock_path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def run_manager_poll(
    *,
    score_path: Path,
    config_path: Path | None,
    state_path: Path,
    instance_dir: Path | None,
    force_agent: bool = False,
    no_agent: bool = False,
    allow_pause_signal: bool = False,
) -> int:
    score = load_exploration_score(score_path)
    config = load_config(config_path)
    _provider.configure_provider(config)
    if instance_dir is not None:
        config["instance_dir"] = str(instance_dir)
    workspace = Path(config.get("working_directory") or os.getcwd()).resolve()
    data_dir = state_path.parent
    state = load_state(state_path) or {}
    task = state.get("task") or score.get("task", "")
    telemetry.configure(config, data_dir, state.get("run_id"))

    with _poll_lock(data_dir) as acquired:
        if not acquired:
            telemetry.emit(
                "manager_poll_skipped",
                phase="manager",
                status="skipped",
                data={"reason": "lock_not_acquired", "state_path": str(state_path)},
            )
            return 0
        snapshot = build_manager_snapshot(
            workspace=workspace,
            state_path=state_path,
            data_dir=data_dir,
        )
        decision = decide_from_snapshot(snapshot)
        intervention_text = decision.guidance or None

        should_call_agent = (
            not no_agent
            and score.get("agents", {}).get("manager")
            and (force_agent or decision.verdict in {VERDICT_ACT, VERDICT_ESCALATE})
        )
        if should_call_agent:
            try:
                agent_text = _call_manager_agent(
                    agent_def=score["agents"]["manager"],
                    task=task,
                    config=config,
                    snapshot=snapshot,
                    state=load_state(state_path) or {},
                )
                if agent_text:
                    intervention_text = agent_text
            except Exception as exc:
                intervention_text = (
                    (intervention_text or decision.guidance or "")
                    + "\n\n"
                    + f"[manager-agent failed; deterministic fallback used: {exc}]"
                ).strip()

        assessment_path = _write_assessment(
            data_dir,
            snapshot,
            decision,
            intervention_text,
        )
        _append_manager_event(workspace, snapshot, decision, assessment_path, intervention_text)

        guide_written = False
        if decision.verdict in {VERDICT_ACT, VERDICT_ESCALATE} and intervention_text:
            _write_guidance(data_dir, intervention_text)
            guide_written = True
        _append_notification(
            data_dir,
            snapshot,
            decision,
            assessment_path,
            guide_written=guide_written,
        )
        telemetry.emit(
            "manager_poll_end",
            phase="manager",
            cycle=int(snapshot.get("cycle") or 0),
            provider=config.get("llm_provider"),
            model=config.get("model"),
            status=decision.verdict,
            data={
                "event_class": decision.event_class,
                "agent_called": should_call_agent,
                "guide_written": guide_written,
                "assessment_path": str(assessment_path),
            },
        )
        if decision.verdict == VERDICT_ESCALATE and allow_pause_signal:
            (data_dir / PAUSE_FILE).write_text(
                f"manager escalation at {snapshot.get('poll_ts')}: {decision.rationale}\n"
            )
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="long-exposure-manager",
        description="Run one cron-safe manager poll for a long-exposure instance.",
    )
    parser.add_argument("--score", default=str(DEFAULT_SCORE_PATH))
    parser.add_argument("--config", default=None)
    parser.add_argument("--state", default=None)
    parser.add_argument("--instance-dir", default=None)
    parser.add_argument(
        "--force-agent",
        action="store_true",
        help="Call the manager agent even when counters are healthy/watch.",
    )
    parser.add_argument(
        "--no-agent",
        action="store_true",
        help="Disable agentic intervention and use deterministic decisions only.",
    )
    parser.add_argument(
        "--allow-pause-signal",
        action="store_true",
        help="On escalation, write long-exposure.pause-for-user for the main loop.",
    )
    args = parser.parse_args(argv)

    instance_dir = resolve_instance_dir(args.instance_dir)
    state_path = _resolve_state_path(args.state, instance_dir)

    try:
        return run_manager_poll(
            score_path=Path(args.score),
            config_path=Path(args.config) if args.config else None,
            state_path=state_path,
            instance_dir=instance_dir,
            force_agent=args.force_agent,
            no_agent=args.no_agent,
            allow_pause_signal=args.allow_pause_signal,
        )
    except Exception as exc:
        print(f"long-exposure-manager: poll failed gracefully: {exc}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
