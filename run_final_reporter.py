#!/usr/bin/env python3
"""Run the final reporter + curator directly, bypassing the exploration loop.

Usage:
    python run_final_reporter.py [--score SCORE] [--config CONFIG] \
        [--state STATE] [--instance-dir DIR]

Loads the saved exploration state and invokes _run_final_reporter followed
by _run_curator, exactly as the main loop would after topic_exhausted.

Pass --instance-dir to target a named concurrent-session instance; the
state file, output dir, and MCP config path will resolve under it exactly
as they do for `python -m long_exposure.exploration`.
"""

import argparse
from pathlib import Path

from long_exposure import paths
from long_exposure.exploration import (
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

    print(f"[run_final] Loaded state: cycle {cycle}")
    print(f"[run_final] Working dir: {config.get('working_directory')}")

    # --- Final Reporter ---
    final_reporter_def = agents.get("final_reporter")
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
        save_state(state_path, cycle, results, consecutive_failures,
                   last_session_id, agent_sessions, agent_summaries,
                   task=task)

        # Ensure PDF was rendered (the exploration loop handles this, but
        # run_final_reporter.py bypasses the loop so we check here too)
        working_dir = config.get("working_directory", "/tmp")
        final_pdf = paths.final_report_pdf_path(working_dir)
        final_md = paths.final_report_path(working_dir)
        if final_md.exists() and not final_pdf.exists():
            print("[run_final] PDF missing — rendering now.")
            _render_final_pdf(working_dir)
    else:
        print("[run_final] No final_reporter defined in score. Skipping.")

    # --- Curator / Skill Packager ---
    curator_def = agents.get("curator")
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
        save_state(state_path, cycle, results, consecutive_failures,
                   last_session_id, agent_sessions, agent_summaries,
                   task=task)
    else:
        print("[run_final] No curator defined in score. Skipping.")

    update_status_file(output_dir, cycle, "completed", consecutive_failures)
    conn.close()
    print(f"\n[run_final] Done. State saved at {state_path}")


if __name__ == "__main__":
    main()
