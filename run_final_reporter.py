#!/usr/bin/env python3
"""Run the end-of-run pipeline directly, bypassing the exploration loop.

Usage:
    python run_final_reporter.py [--score SCORE] [--config CONFIG] \
        [--state STATE] [--instance-dir DIR] [--skip-auditor]

Loads the saved exploration state and invokes _run_final_auditor,
_run_final_reporter and _run_curator in that order, exactly as the main
loop would after topic_exhausted, honouring the score's `loop.end_of_run`
switches. Pass --skip-auditor to re-render a report against the existing
audit summary.

Pass --instance-dir to target a named concurrent-session instance; the
state file, output dir, and MCP config path will resolve under it exactly
as they do for `python -m long_exposure.exploration`.
"""

import argparse
import os
from pathlib import Path

from long_exposure import agent_routing, health_events, paths, provider, telemetry
from long_exposure import exploration as _exploration
from long_exposure.auditing import _run_final_auditor
from long_exposure.exploration import (
    _end_of_run_enabled,
    _render_final_pdf,
    _resolve_output_dir,
    _resolve_state_path,
    _run_curator,
    _run_final_reporter,
    load_exploration_score,
    load_state,
    save_state,
    update_status_file,
)
from long_exposure.orchestrator import load_config, resolve_instance_dir
from long_exposure.workspace_bootstrap import derive_run_id
from long_exposure.reporting import _pdf_needs_render
from auto_compact.db import init_db


def main():
    parser = argparse.ArgumentParser(description="Run final reporter + curator")
    parser.add_argument("--score", default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--state", default=None)
    parser.add_argument(
        "--instance-dir",
        default=None,
        help=(
            "Per-session workspace dir (same semantics as "
            "`python -m long_exposure.exploration --instance-dir`). When set, "
            "--state / --output default to <instance-dir>/ subpaths and "
            "the MCP config is written to <instance-dir>/mcp_config.json."
        ),
    )
    parser.add_argument(
        "--skip-auditor",
        action="store_true",
        help=(
            "Skip the final auditor and reuse the existing "
            "final_audit_summary.json (report-only re-render)."
        ),
    )
    args = parser.parse_args()

    instance_dir = resolve_instance_dir(args.instance_dir)
    # Score path default: the one that ships with this repo. Must be computed
    # after parsing so --instance-dir doesn't change what "default score" means.
    score_path = args.score or str(
        Path(__file__).resolve().parent / "long_exposure" / "exploration-score.yaml"
    )

    score = load_exploration_score(score_path)
    config = load_config(args.config)
    # Propagate instance_dir so nested call_agent_with_session → generate_mcp_config
    # scopes the MCP config file per-instance.
    config["instance_dir"] = str(instance_dir) if instance_dir is not None else None

    state_path = _resolve_state_path(args.state, instance_dir)
    output_dir = _resolve_output_dir(None, instance_dir)

    state = load_state(state_path)
    if not state:
        print("[run_final] No saved state found. Nothing to do.")
        return

    cycle = state["cycle"]
    results = state["results"]
    last_session_id = state.get("last_session_id")
    agent_sessions = state.get("agent_sessions", {})
    agent_summaries = state.get("agent_summaries", {})
    consecutive_failures = state.get("failures", {})
    # This is a fresh process: seed the in-memory usage ledger from the saved
    # run so the save_state calls below extend the run's totals instead of
    # overwriting them with only this pass's reporter/curator spend.
    _exploration._usage.load(state.get("usage_totals") or {})

    agents = score["agents"]
    # Prefer the directive saved in state (which reflects any resume-with-
    # override the exploration did) over the score YAML's current task,
    # which may have drifted. Fall back to score YAML if state pre-dates
    # the task-persistence change.
    task = state.get("task") or score["task"]
    score_inputs = {"directive": task}
    # Keep results["directive"] in sync with task (build_agent_prompt
    # prefers results over score_inputs).
    results["directive"] = task

    # Apply score-level tool restrictions
    if "allowed_tools" in score:
        config["allowed_tools"] = score["allowed_tools"]

    # Same per-agent routing the loop sets up. Without this a heterogeneous
    # score's final agents all ran on the global provider here (the pinning
    # context manager is a no-op unless the flag is set), and the usage
    # ledger attributed their spend to that provider's price table.
    agent_routing.apply_agent_models(config, score)
    config["_per_agent_pinned"] = agent_routing.per_agent_pinned(config, score)
    if config["_per_agent_pinned"]:
        os.environ["LONG_EXPOSURE_PER_AGENT_PINNED"] = "1"
    else:
        # Clear it as the loop does: a stale value inherited from a parent
        # process would make sibling machinery treat a homogeneous run as
        # pinned.
        os.environ.pop("LONG_EXPOSURE_PER_AGENT_PINNED", None)
    provider.configure_provider(config)

    # Inject shared citations
    shared_citations = score.get("citations", "")
    if shared_citations:
        for agent_def in agents.values():
            role = agent_def.get("role", "")
            if "<citations>" not in role:
                agent_def["role"] = role.rstrip() + "\n\n" + shared_citations.strip() + "\n"

    context_window = config.get("context_window", 1_000_000)
    compact_threshold = config.get("compact_threshold", 0.90)
    compact_at = int(context_window * compact_threshold)
    data_dir = state_path.parent

    conn = init_db(Path(config["compact_db"]))

    # Same run identity as the loop would use. The final auditor keys its
    # ledger cycle count and reconciliation event uuid5s on run_id, so a
    # synthesized-per-invocation id would stick the lessons cap at 1 and
    # break idempotency across passes. Prefer state, then the one the loop
    # persisted inside results, and only then synthesize.
    run_id = state.get("run_id") or results.get("run_id") or derive_run_id()
    results["run_id"] = run_id

    # Observability sinks. run_exploration configures both; this entrypoint
    # bypasses it, so without these the whole standalone pipeline emitted no
    # telemetry (usage_recorded included) and dropped every health event
    # (file-gate rescues, PDF render failures) on the floor.
    telemetry.configure(config, data_dir, run_id)
    health_events.configure(data_dir)

    print(f"[run_final] Loaded state: cycle {cycle}")
    print(f"[run_final] Working dir: {config.get('working_directory')}")

    loop_cfg = score.get("loop", {}) or {}
    # The status renderer and usage_summary.json read these module globals
    # for the budget and spend-limit figures; only run_exploration sets
    # them, so without this the standalone pipeline wrote a status file with
    # no budget line and no spend-limit block.
    _exploration._current_loop_cfg = loop_cfg
    _exploration._current_run_config = config

    def _save():
        """Persist state, carrying forward every field this entrypoint does
        not own. save_state writes the full document, so omitting these
        silently erased run identity, daily-sync bookkeeping and the
        exhaustion-detector calibration from the state file — a later
        `--resume` would then re-fire the daily sync on cycle 1 and start
        the low-output streak over."""
        save_state(
            state_path, cycle, results, consecutive_failures,
            last_session_id, agent_sessions, agent_summaries,
            post_merge_pending=bool(state.get("post_merge_pending")),
            task=task,
            run_id=run_id,
            last_daily_sync_at=state.get("last_daily_sync_at"),
            daily_sync_count=state.get("daily_sync_count", 0) or 0,
            daily_sync_in_progress=bool(state.get("_daily_sync_in_progress")),
            reanchor_emitted=state.get("_reanchor_emitted") or {},
            agent_context_tokens=state.get("agent_context_tokens") or {},
            peak_cycle_output=state.get("peak_cycle_output", 0) or 0,
            low_output_streak=state.get("low_output_streak", 0) or 0,
            usage_basis=state.get("usage_basis"),
        )

    # --- Final Auditor ---
    # Runs BEFORE the reporter, matching the main pipeline: the reporter
    # ingests final_audit_summary.json, so skipping the auditor here shipped
    # a report narrated from a stale (or absent) audit.
    final_auditor_def = agents.get("final_auditor")
    if final_auditor_def and not _end_of_run_enabled(loop_cfg, "final_auditor"):
        print("[run_final] Final auditor disabled by loop.end_of_run — skipping.")
    elif final_auditor_def and args.skip_auditor:
        print("[run_final] --skip-auditor: reusing the existing audit summary.")
    elif final_auditor_def:
        agent_sessions.pop("final_auditor", None)
        agent_summaries.pop("final_auditor", None)
        try:
            last_session_id = _run_final_auditor(
                final_auditor_def, task, config, results, score_inputs,
                conn, cycle, last_session_id,
                context_window, compact_at,
                data_dir=data_dir,
                agent_sessions=agent_sessions,
                agent_summaries=agent_summaries,
                run_id=run_id,
            )
        except Exception as e:
            # Same isolation as the main pipeline: the reporter still runs
            # with whatever audit artifacts exist.
            print(f"[run_final] Final auditor failed (non-fatal): {e!r}")
        _save()
    else:
        print("[run_final] No final_auditor defined in score. Skipping.")

    # --- Final Reporter ---
    final_reporter_def = agents.get("final_reporter")
    if final_reporter_def and not _end_of_run_enabled(loop_cfg, "final_reporter"):
        print("[run_final] Final reporter disabled by loop.end_of_run — skipping.")
        final_reporter_def = None
    if final_reporter_def:
        # Clear any stale final_reporter session so it starts fresh
        agent_sessions.pop("final_reporter", None)
        agent_summaries.pop("final_reporter", None)

        last_session_id = _run_final_reporter(
            final_reporter_def, task, config, results, score_inputs,
            conn, cycle, last_session_id,
            context_window, compact_at,
            data_dir=data_dir,
            agent_sessions=agent_sessions,
            agent_summaries=agent_summaries,
        )
        _save()

        # Ensure PDF was rendered (the exploration loop handles this, but
        # run_final_reporter.py bypasses the loop so we check here too)
        working_dir = config.get("working_directory", "/tmp")
        final_pdf = paths.final_report_pdf_path(working_dir)
        final_md = paths.final_report_path(working_dir)
        # Missing OR stale (md newer than pdf) — a re-run that revised the
        # markdown must not ship the previous pass's PDF.
        if final_md.exists() and _pdf_needs_render(final_md, final_pdf):
            print("[run_final] PDF missing or stale — rendering now.")
            _render_final_pdf(working_dir)
    else:
        print("[run_final] No final_reporter defined in score. Skipping.")

    # --- Curator / Skill Packager ---
    curator_def = agents.get("curator")
    if curator_def and not _end_of_run_enabled(loop_cfg, "curator"):
        print("[run_final] Curator disabled by loop.end_of_run — skipping.")
        curator_def = None
    if curator_def:
        # Clear any stale curator session
        agent_sessions.pop("curator", None)
        agent_summaries.pop("curator", None)

        last_session_id = _run_curator(
            curator_def, task, config, results, score_inputs,
            conn, cycle, last_session_id,
            agent_sessions=agent_sessions,
            agent_summaries=agent_summaries,
        )
        _save()
    else:
        print("[run_final] No curator defined in score. Skipping.")

    update_status_file(output_dir, cycle, "completed", consecutive_failures)
    conn.close()
    print(f"\n[run_final] Done. State saved at {state_path}")


if __name__ == "__main__":
    main()
