#!/usr/bin/env python3
"""Continuous Autonomous Exploration Conductor.

Runs a sequential three-agent loop (research → worker → audit) until
stopped via Ctrl+C or max_cycles reached. Each agent maintains a
persistent Claude Code session via --session-id / --resume, giving
continuous context across cycles without clearing.

Auto-compact integration: when an agent's context exceeds the compact
threshold, the session is compacted (summary generated, stored in
sessions.db, fresh session created with summary as bootstrap).

Control:
    Start:  long-exposure start --score score.yaml
    Stop:   Ctrl+C  OR  touch data/long-exposure.stop
    Clear:  touch data/long-exposure.clear  (stops + clears context)
    Resume: long-exposure resume
    Fresh:  long-exposure clear  (then start)
"""

import argparse
import json
import os
import re as _re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from long_exposure.conductor import (
    build_agent_config,
    build_agent_prompt,
    build_role_block,
    parse_outputs,
    _OUTPUT_RE,
    _session_transcript_text,
    BRANCH_COMPLETE_SIGNAL,
    BRANCH_COMPLETE_RE,
)
from long_exposure.workspace_bootstrap import (
    append_ledger_event,
    bootstrap_workspace,
    derive_run_id,
    summarize_ledger,
)
from long_exposure.report_formatting import (
    normalize_report_markdown,
    render_report_pdf,
)
from long_exposure.orchestrator import (
    PHILOSOPHY_EFFORT_MAP,
    PHILOSOPHY_PRESETS,
    ClaudeCliError,
    ClaudeRateLimitError,
    _active_account_dir,
    _active_account_index,
    _codex_permission_flags,
    _gemini_permission_flags,
    _invoke_claude,
    _parse_accounts,
    _resolve_force_account,
    agent_teams_enabled,
    assemble_system_prompt,
    build_allowed_tools_flags,
    call_local_llm,
    estimate_tokens,
    generate_mcp_config,
    generate_gemini_project_settings,
    load_config,
    resolve_instance_dir,
    rotate_to_next_account,
)
from long_exposure import pool
from long_exposure import paths
from long_exposure import provider as _provider
from long_exposure import telemetry
from long_exposure import unified_pool
from long_exposure import interactive_transport
from auto_compact.db import init_db, store_session

SCRIPT_DIR = Path(__file__).resolve().parent


def _user_writable_data_dir() -> Path:
    """Return a writable directory for runtime data.

    Editable / dev installs put the package in a tree the user owns;
    `SCRIPT_DIR/data` is fine. Wheel installs land the package under
    site-packages, which is typically read-only. In that case fall back
    to ~/.long-exposure/data.

    Determined once per process by trying to mkdir+touch under
    SCRIPT_DIR/data. The result is cached so concurrent calls don't race
    on the probe write.
    """
    global _DATA_DIR_CACHE
    try:
        return _DATA_DIR_CACHE  # type: ignore[name-defined]
    except NameError:
        pass
    candidate = SCRIPT_DIR / "data"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write_probe"
        probe.touch()
        probe.unlink(missing_ok=True)
        _DATA_DIR_CACHE = candidate
    except OSError:
        # Read-only install (e.g. wheel under site-packages). Fall back to
        # the user's home directory; mkdir is best-effort here too.
        fallback = Path.home() / ".long-exposure" / "data"
        fallback.mkdir(parents=True, exist_ok=True)
        _DATA_DIR_CACHE = fallback
    return _DATA_DIR_CACHE


DEFAULT_OUTPUT_DIR = _user_writable_data_dir().parent / "output"
DEFAULT_STATE_PATH = _user_writable_data_dir() / "exploration_state.json"


def _resolve_state_path(
    state_flag: str | None,
    instance_dir: Path | None,
) -> Path:
    """Pick the exploration state file path.

    Precedence: explicit --state flag > instance dir default > legacy default.
    Preserving the legacy default (None instance_dir, None flag) is what keeps
    pre-concurrency resume working without any new syntax.
    """
    if state_flag:
        return Path(state_flag)
    if instance_dir is not None:
        return instance_dir / "exploration_state.json"
    return DEFAULT_STATE_PATH


def _resolve_output_dir(
    output_flag: str | None,
    instance_dir: Path | None,
) -> Path:
    """Pick the exploration output directory path.

    Same precedence as _resolve_state_path.
    """
    if output_flag:
        return Path(output_flag)
    if instance_dir is not None:
        return instance_dir / "output"
    return DEFAULT_OUTPUT_DIR

# ---------------------------------------------------------------------------
# Graceful shutdown via SIGINT / SIGTERM / signal files
# ---------------------------------------------------------------------------

_stop_requested = False
_clear_requested = False
_graceful_stop_requested = False  # Stage 1 §6.4 — finish current cycle, then exit.
_unified_root_holder: unified_pool.UnifiedHolder | None = None

# Agents already warned (once per run) that interactive transport dropped
# their score-requested session-search MCP.
_INTERACTIVE_MCP_NOTICE_SEEN: set[str] = set()


def _pin_unified_holder_env(holder: unified_pool.UnifiedHolder) -> None:
    """Pin the current process to a unified-pool holder's provider/account."""
    for prv in unified_pool.SUPPORTED_PROVIDERS_FOR_POOL:
        os.environ.pop(_provider.force_account_env(prv), None)
    os.environ["LONG_EXPOSURE_LLM_PROVIDER"] = holder.provider
    os.environ[_provider.force_account_env(holder.provider)] = holder.account_dir


def _release_unified_root_holder() -> None:
    global _unified_root_holder
    holder = _unified_root_holder
    if holder is None:
        return
    try:
        unified_pool.release_slot_by_holder(holder)
    except Exception as exc:
        print(f"[long-exposure] Unified pool release warning: {exc}", flush=True)
    finally:
        _unified_root_holder = None


def _rotate_unified_root_after_rate_limit(
    provider_preference: list[str] | None = None,
) -> unified_pool.UnifiedHolder | None:
    """Mark the current unified root account cooling and acquire a fresh slot."""
    global _unified_root_holder
    old = _unified_root_holder
    if old is not None:
        try:
            with unified_pool.swap_active_provider(old.provider):
                pool.mark_rate_limited(old.account_dir)
        finally:
            _release_unified_root_holder()
    else:
        # Defensive fallback for resumed/legacy unified state where the env is
        # pinned but this process did not create a holder object.
        cur_provider = _provider.current_provider()
        force_env = _provider.force_account_env(cur_provider)
        pinned = os.environ.get(force_env, "").strip()
        if pinned:
            with unified_pool.swap_active_provider(cur_provider):
                pool.mark_rate_limited(pinned)

    try:
        holder = unified_pool.acquire_slot(
            role="root",
            pid=os.getpid(),
            provider_preference=provider_preference,
        )
    except Exception as exc:
        print(f"[long-exposure] Unified pool rotation failed: {exc}", flush=True)
        return None
    _unified_root_holder = holder
    _pin_unified_holder_env(holder)
    return holder


def _on_signal(signum, frame):
    global _stop_requested
    _stop_requested = True
    print("\n[long-exposure] Stop requested. Finishing current agent...", flush=True)


signal.signal(signal.SIGINT, _on_signal)
signal.signal(signal.SIGTERM, _on_signal)


# Run-control signal filenames, grouped by semantics. Single source of truth
# consumed by BOTH _check_signal_files and the startup stale-signal sweep in
# run_exploration, so a newly added signal is swept automatically instead of
# surviving startup as a stale file (the exact recurrence the sweep was built
# to prevent). External writers reference these literal names: fanout.py
# writes long-exposure.stop / long-exposure.graceful-stop into clone instance
# dirs, and manager.py writes long-exposure.pause-for-user. Guide files
# (long-exposure.guide / exploration.guide) are deliberately excluded —
# operator guidance for the first cycle must survive startup.
_STOP_SIGNAL_FILENAMES = ("long-exposure.stop", "exploration.stop")  # (new, legacy)
_CLEAR_SIGNAL_FILENAMES = ("long-exposure.clear", "exploration.clear")  # (new, legacy)
_GRACEFUL_STOP_SIGNAL_FILENAME = "long-exposure.graceful-stop"
_PAUSE_SIGNAL_FILENAME = "long-exposure.pause-for-user"
_RUN_SIGNAL_FILENAMES = (
    *_STOP_SIGNAL_FILENAMES,
    *_CLEAR_SIGNAL_FILENAMES,
    _GRACEFUL_STOP_SIGNAL_FILENAME,
    _PAUSE_SIGNAL_FILENAME,
)


def _check_signal_files(data_dir: Path) -> None:
    """Check for stop/clear/graceful-stop signal files. Sets global flags and
    removes files.

    Backward-compat: accepts BOTH the new long-exposure.* filenames (the
    canonical post-rename form) AND the legacy exploration.* names so a
    workspace that was running under the agent-conditioning era keeps
    responding to user-touched stop signals during the rename window.
    Drop the legacy branch in v0.3 once no users have lingering scripts
    pointing at the old names.

    long-exposure.graceful-stop is the new (Stage 1) signal: clones written
    when their pinned overflow account rate-limits, and clones finish their
    current cycle before exiting. This is distinct from .stop (which exits
    immediately at the next agent boundary) so a clone doesn't drop a
    half-finished cycle's work.
    """
    global _stop_requested, _clear_requested, _graceful_stop_requested

    clear_file_new = data_dir / _CLEAR_SIGNAL_FILENAMES[0]
    clear_file_old = data_dir / _CLEAR_SIGNAL_FILENAMES[1]
    stop_file_new = data_dir / _STOP_SIGNAL_FILENAMES[0]
    stop_file_old = data_dir / _STOP_SIGNAL_FILENAMES[1]
    graceful_file = data_dir / _GRACEFUL_STOP_SIGNAL_FILENAME
    pause_file = data_dir / _PAUSE_SIGNAL_FILENAME
    guide_paths = (
        data_dir / "long-exposure.guide",
        data_dir / "exploration.guide",
    )

    clear_file = clear_file_new if clear_file_new.exists() else (
        clear_file_old if clear_file_old.exists() else None
    )
    stop_file = stop_file_new if stop_file_new.exists() else (
        stop_file_old if stop_file_old.exists() else None
    )

    if clear_file is not None:
        clear_file.unlink()
        for g in guide_paths:
            g.unlink(missing_ok=True)
        _stop_requested = True
        _clear_requested = True
        print("[long-exposure] Clear signal received.", flush=True)
    elif stop_file is not None:
        stop_file.unlink()
        for g in guide_paths:
            g.unlink(missing_ok=True)
        _stop_requested = True
        print("[long-exposure] Stop signal received.", flush=True)
    elif graceful_file.exists():
        graceful_file.unlink(missing_ok=True)
        _graceful_stop_requested = True
        print(
            "[long-exposure] Graceful-stop signal received; "
            "will exit after current cycle.",
            flush=True,
        )
    elif pause_file.exists():
        pause_file.unlink(missing_ok=True)
        _graceful_stop_requested = True
        print(
            "[long-exposure] Manager pause-for-user signal received; "
            "will exit at this cycle boundary and preserve state for resume.",
            flush=True,
        )


def _consume_guide_file(data_dir: Path) -> str | None:
    """Read and delete the guide file if present. Returns guidance text or None.

    Backward-compat: looks at both long-exposure.guide and the legacy
    exploration.guide. New writes always go to the new name.
    """
    for name in ("long-exposure.guide", "exploration.guide"):
        guide_file = data_dir / name
        if guide_file.exists():
            try:
                text = guide_file.read_text().strip()
                guide_file.unlink(missing_ok=True)
                if text:
                    return text
            except OSError:
                pass
    return None


# ---------------------------------------------------------------------------
# Score loading
# ---------------------------------------------------------------------------


def load_exploration_score(path: str | Path) -> dict:
    """Load and validate an exploration score YAML.

    Validates the score structure AND every agent's declared inputs against
    a known-source check. See `validate_score_inputs` for the rules; in
    short, every input must be (a) on the runtime-injected allowlist,
    (b) a score-level / seed input, or (c) the output of any agent in
    the score. Typos in input names fail loudly here, before any cycle
    runs, instead of becoming silent `[UNAVAILABLE: ...]` markers in
    agent prompts.
    """
    with open(path) as f:
        score = yaml.safe_load(f)
    for key in ("task", "agents", "flow"):
        if key not in score:
            raise ValueError(f"Exploration score missing required key: {key}")
    validate_score_inputs(score)
    return score


# Runtime-injected inputs that the harness sets before invoking an agent.
# Any input named here is considered always-available regardless of cycle
# flow position. Extend this set when adding a new harness-supplied input;
# the validator below uses it as one of three legitimate input sources.
#
# Grouped by source for maintainability — all groups are merged into a flat
# set at validation time.
RUNTIME_INPUTS: frozenset[str] = frozenset({
    # Cycle-level (set in the main loop before researcher / worker / auditor)
    "directive",
    "audit_report",
    "live_guidance",
    "plan_of_record",
    "promise_ledger_summary",
    # Cron-polled manager inputs (set by long_exposure.manager)
    "manager_snapshot",
    # Per-cycle reporter inputs (set in _run_reporter)
    "cycle_range",
    "cycle_sessions",
    "report_basename",
    "working_dir",
    # Final-reporter / final-auditor staging inputs
    "stage",
    "total_stages",
    "stage_index",
    "expected_file",
    "rescue_warning",
    "outline_path",
    "draft_path",
    "final_report_path",
    "report_glob",
    "final_report_dir",
    "prior_reports",
    "final_audit_summary",
    "final_audit_headline",
    "ledger_causal_summary",
    "wall_cap_hit",
    "findings_file",
    "lesson_candidates_file",
    "audit_dir",
    # Curator inputs
    "clone_artifacts",
    # Seed inputs that may not appear in score-level `seed:` mapping if the
    # score uses null-as-default; declared here so a score that DOES name them
    # validates whether or not seed: contains them.
    "starting_subtopic",
    "starting_tools",
})


def validate_score_inputs(score: dict) -> None:
    """Verify every agent's declared inputs have a known source.

    A declared input is valid iff it appears in any of:
      (a) RUNTIME_INPUTS — harness-supplied at runtime,
      (b) score-level top-level `inputs:` mapping (rare; usually empty),
      (c) score-level `seed:` mapping,
      (d) any agent's `outputs:` list (cycle-order is NOT enforced because
          daily-sync agents and reporter run outside the main flow; it is
          the responsibility of the cycle loop to ensure the input is
          populated by the time the consumer runs).

    Raises ValueError listing every offending (agent, input) pair on the
    first failure. Robust + simple: one allowlist, one set union, one
    pass over agents.
    """
    raw_agents = score.get("agents")
    if raw_agents is None:
        agents: dict = {}
    elif isinstance(raw_agents, dict):
        agents = raw_agents
    else:
        raise ValueError(
            f"Score 'agents' must be a YAML mapping (got "
            f"{type(raw_agents).__name__})"
        )

    # Build the union of legitimate input sources.
    score_level_inputs = set((score.get("inputs") or {}).keys())
    seed_inputs = set((score.get("seed") or {}).keys())
    all_outputs: set[str] = set()
    for agent_def in agents.values():
        if not isinstance(agent_def, dict):
            continue
        for out in agent_def.get("outputs", []) or []:
            if isinstance(out, str):
                all_outputs.add(out)

    legitimate = (
        set(RUNTIME_INPUTS)
        | score_level_inputs
        | seed_inputs
        | all_outputs
    )

    bad: list[tuple[str, str]] = []
    for agent_name, agent_def in agents.items():
        if not isinstance(agent_def, dict):
            continue
        for inp in agent_def.get("inputs", []) or []:
            if not isinstance(inp, str):
                bad.append((agent_name, repr(inp)))
                continue
            if inp not in legitimate:
                bad.append((agent_name, inp))

    if bad:
        lines = "\n".join(
            f"  - agent {a!r}: input {i!r} has no known source"
            for a, i in bad
        )
        raise ValueError(
            "Score input validation failed. The following inputs are not "
            "defined as runtime-injected, score-level, seed, or any agent's "
            "output:\n" + lines + "\n\n"
            "Fix typos at the call site, or add the new harness-supplied "
            "input to RUNTIME_INPUTS in long_exposure/exploration.py."
        )


# ---------------------------------------------------------------------------
# State persistence (stop / resume)
# ---------------------------------------------------------------------------


def save_state(path: Path, cycle: int, results: dict, failures: dict,
               last_session_id: str | None = None,
               agent_sessions: dict | None = None,
               agent_summaries: dict | None = None,
               post_merge_pending: bool = False,
               task: str | None = None,
               run_id: str | None = None,
               last_daily_sync_at: str | None = None,
               daily_sync_count: int = 0,
               daily_sync_in_progress: bool = False,
               reanchor_emitted: dict | None = None,
               agent_context_tokens: dict | None = None,
               peak_cycle_output: int = 0,
               low_output_streak: int = 0,
               usage_basis: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Tag agent_sessions with the provider/account that created them so resume
    # after a provider switch or cross-account rotation can start fresh instead
    # of passing a Claude/Codex/Gemini-native id to the wrong CLI.
    state = {
        "cycle": cycle,
        "results": results,
        "failures": failures,
        "last_session_id": last_session_id,
        "agent_sessions": agent_sessions or {},
        "agent_sessions_provider": _provider.current_provider(),
        "agent_sessions_account": _active_account_index(),
        "unified_pool_active": unified_pool.is_unified_active(),
        "agent_summaries": agent_summaries or {},
        "post_merge_pending": post_merge_pending,
        "task": task,
        "run_id": run_id,
        # Stage 3: daily-sync tracking. Carried across resume; backward-
        # compatible because load_state callers already use .get().
        "last_daily_sync_at": last_daily_sync_at,
        "daily_sync_count": daily_sync_count,
        "_daily_sync_in_progress": daily_sync_in_progress,
        "_reanchor_emitted": reanchor_emitted or {},
        "agent_context_tokens": agent_context_tokens or {},
        # Exhaustion-detector calibration. Without these a stop/resume reset
        # the relative low-output threshold and streak, deferring closure.
        "peak_cycle_output": peak_cycle_output,
        "low_output_streak": low_output_streak,
        # Usage basis the calibration was measured on: "interactive" (chars/4
        # estimate of the final response only) or a headless provider name
        # (full-turn output_tokens). Resume resets peak/streak when this
        # differs from the resuming run's basis — the two scales are not
        # comparable. Callers pass "interactive" explicitly; headless runs
        # default to the provider active at save time (tracks mid-run
        # unified-pool provider rotation).
        "usage_basis": usage_basis or _provider.current_provider(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    # Atomic write: temp file + rename prevents corruption on crash
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    os.replace(tmp, path)


def _daily_sync_due(
    last_daily_sync_at: str | None,
    interval_hours: float,
) -> bool:
    """Return True iff (now - last_daily_sync_at) >= interval_hours.

    Defensive: if interval_hours is 0 or negative, daily sync is disabled
    (returns False). If last_daily_sync_at is missing or unparseable, treat
    as "never synced" → fire immediately.
    """
    if not interval_hours or interval_hours <= 0:
        return False
    if not last_daily_sync_at:
        return True
    try:
        prev = datetime.fromisoformat(
            last_daily_sync_at.replace("Z", "+00:00")
        )
    except (ValueError, TypeError):
        return True
    delta_s = (datetime.now(timezone.utc) - prev).total_seconds()
    return delta_s >= interval_hours * 3600


def _snapshot_account_usage() -> dict[str, dict]:
    """Snapshot per-account token totals keyed by account dir.

    Used for daily-sync delta computation (Plan A). Returns {} when
    pool inactive. Best-effort; never raises.
    """
    try:
        from long_exposure import pool as _pool
        if not _pool.is_active():
            return {}
        return {
            entry["dir"]: {
                "tokens_input": entry.get("tokens_input", 0),
                "tokens_output": entry.get("tokens_output", 0),
                "tokens_cache_read": entry.get("tokens_cache_read", 0),
                "tokens_cache_creation": entry.get("tokens_cache_creation", 0),
            }
            for entry in _pool.get_usage_snapshot()
        }
    except Exception:
        return {}


def _print_account_usage_delta(before: dict[str, dict], header: str) -> None:
    """Print per-account usage delta vs `before`, with delta-share %.

    Plan A's operator-facing summary at daily-sync boundaries. Helps
    spot imbalance: "primary used 5x more than overflow this window."
    Best-effort; never raises.

    Weighting for delta-share %: input + cache_read*0.1 + cache_creation*1.25
    + output*5.0. This is a rough quota-burn proxy; raw four-field totals
    are also surfaced for the audit trail.
    """
    try:
        from long_exposure import pool as _pool
        if not _pool.is_active():
            return
        after = _snapshot_account_usage()
        if not after:
            return

        # Compute weighted delta per account.
        rows = []
        total_weighted = 0.0
        for dir_path, after_vals in after.items():
            before_vals = before.get(dir_path, {})
            d_in = after_vals["tokens_input"] - before_vals.get("tokens_input", 0)
            d_out = after_vals["tokens_output"] - before_vals.get("tokens_output", 0)
            d_cr = after_vals["tokens_cache_read"] - before_vals.get("tokens_cache_read", 0)
            d_cc = after_vals["tokens_cache_creation"] - before_vals.get("tokens_cache_creation", 0)
            weighted = d_in + d_cr * 0.1 + d_cc * 1.25 + d_out * 5.0
            total_weighted += max(0.0, weighted)
            rows.append({
                "dir": dir_path,
                "name": Path(dir_path).name or dir_path,
                "d_in": d_in, "d_out": d_out,
                "d_cr": d_cr, "d_cc": d_cc,
                "weighted": weighted,
            })

        if not rows or total_weighted <= 0:
            return  # nothing to report (no calls happened, or all-zero)

        print(f"[long-exposure] {header}", flush=True)
        for r in rows:
            share = (r["weighted"] / total_weighted * 100.0) if total_weighted > 0 else 0.0
            print(
                f"[long-exposure]   {r['name']:<24}"
                f"in:{_pool._human_tokens(r['d_in']):>6} "
                f"cr:{_pool._human_tokens(r['d_cr']):>6} "
                f"cc:{_pool._human_tokens(r['d_cc']):>6} "
                f"out:{_pool._human_tokens(r['d_out']):>6}  "
                f"(share: {share:5.1f}%)",
                flush=True,
            )
    except Exception:
        pass


def _run_daily_sync(
    *,
    agents: dict,
    task: str,
    config: dict,
    results: dict,
    score_inputs: dict,
    conn,
    cycle: int,
    last_session_id: str | None,
    context_window: int,
    compact_at: int,
    data_dir: Path,
    agent_sessions: dict,
    agent_summaries: dict,
) -> str | None:
    """Stage 3: run final auditor → final reporter → curator on a wall-clock
    cadence (revise mode). Each agent reads its prior outputs and updates
    them; the curator package gets a timestamp suffix and a
    `<slug>_package_latest.zip` symlink.

    Failure isolation: any of the three failing logs and continues — the
    operator still has the prior good artifacts, the run keeps cycling,
    and the next interval re-attempts.
    """
    timestamp_suffix = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M")
    print(f"\n[long-exposure] === Daily sync ({timestamp_suffix}) ===", flush=True)

    # Plan A: snapshot per-account usage at sync entry; print delta+share
    # at sync exit. Helps the operator spot account-usage imbalance.
    usage_at_sync_start = _snapshot_account_usage()

    # 1. Final auditor — best-effort.
    final_auditor_def = agents.get("final_auditor")
    if final_auditor_def:
        try:
            from long_exposure.auditing import _run_final_auditor
            last_session_id = _run_final_auditor(
                final_auditor_def, task, config, results, score_inputs,
                conn, cycle, last_session_id,
                context_window, compact_at,
                data_dir=data_dir,
                agent_sessions=agent_sessions,
                agent_summaries=agent_summaries,
            )
        except Exception as e:
            print(
                f"[long-exposure] Daily sync: final auditor failed (non-fatal): "
                f"{e!r}",
                flush=True,
            )

    # 2. Final reporter — file-gate rescue keeps prior final_report.md if it
    #    fails to assemble.
    final_reporter_def = agents.get("final_reporter")
    if final_reporter_def:
        try:
            last_session_id = _run_final_reporter(
                final_reporter_def, task, config, results, score_inputs,
                conn, cycle, last_session_id,
                context_window, compact_at,
                data_dir=data_dir,
                agent_sessions=agent_sessions,
                agent_summaries=agent_summaries,
            )
        except Exception as e:
            print(
                f"[long-exposure] Daily sync: final reporter failed (non-fatal): "
                f"{e!r}",
                flush=True,
            )

    # 3. Curator — pass timestamp_suffix so packages accumulate (latest-symlink
    #    points to the freshest one).
    curator_def = agents.get("curator")
    if curator_def:
        try:
            last_session_id = _run_curator(
                curator_def, task, config, results, score_inputs,
                conn, cycle, last_session_id,
                agent_sessions=agent_sessions,
                agent_summaries=agent_summaries,
                timestamp_suffix=timestamp_suffix,
            )
        except Exception as e:
            print(
                f"[long-exposure] Daily sync: curator failed (non-fatal): "
                f"{e!r}",
                flush=True,
            )

    # Plan A: per-account usage delta over this sync window.
    _print_account_usage_delta(
        usage_at_sync_start,
        header=f"Account usage delta since last sync ({timestamp_suffix}):",
    )

    print(f"[long-exposure] === Daily sync done ({timestamp_suffix}) ===\n", flush=True)
    return last_session_id


def _should_run_final_synthesis(
    *,
    topic_exhausted: bool,
    max_cycles_reached: bool = False,
    stop_requested: bool,
    clear_requested: bool,
) -> bool:
    """Single trigger condition for end-of-run agents (final auditor + final
    reporter + curator). Captured here so all three call sites use exactly the
    same predicate (docs/end-of-run-pipeline.md).

    Run on natural exhaustion or an operator stop. `_clear_requested` is the
    explicit carve-out (a clear archives state and the synthesis would be
    stranded). A stop signal is a graceful end-of-run request, so the final
    auditor/reporter/curator should run after any in-flight cycle and periodic
    report finish.
    """
    if clear_requested:
        return False
    return bool(topic_exhausted or max_cycles_reached or stop_requested)


def _clear_stop_flag_for_final_synthesis(
    *,
    should_run_final: bool,
    stop_requested: bool,
    clear_requested: bool,
) -> bool:
    """Allow final stages to run after an operator stop.

    The same module-level `_stop_requested` flag that exits the cycle loop is
    also read by final_auditor/final_reporter between their stages. If it
    remains set, a graceful stop can correctly enter end-of-run synthesis but
    then immediately short-circuit the final audit/report stages. Once the
    cycle loop is already exiting, clearing the flag is safe: we preserve the
    original stop state in local variables for status/telemetry, while letting
    the finalization pipeline complete.
    """
    global _stop_requested
    if should_run_final and stop_requested and not clear_requested:
        _stop_requested = False
        return True
    return False


def load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        # Graceful but loud: archive the corrupt file for inspection instead
        # of silently treating it as a fresh start (the next save_state would
        # overwrite the evidence).
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        archive = path.with_name(f"{path.name}.corrupt-{ts}")
        try:
            os.replace(path, archive)
            print(
                f"[long-exposure] WARNING: state file is corrupt JSON; "
                f"archived to {archive.name}; starting fresh.",
                flush=True,
            )
        except OSError as e:
            print(
                f"[long-exposure] WARNING: state file is corrupt JSON and "
                f"archiving failed ({e}); starting fresh.",
                flush=True,
            )
        return None
    except OSError:
        return None


def _archive_state(state_path: Path) -> Path | None:
    """Copy current state file to a timestamped archive. Returns archive path."""
    if not state_path.exists():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    archive = state_path.with_name(f"exploration_state_{ts}.json")
    try:
        archive.write_text(state_path.read_text())
        print(f"[long-exposure] Archived state: {archive.name}", flush=True)
        return archive
    except OSError as e:
        print(f"[long-exposure] Archive failed: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Token usage helpers
# ---------------------------------------------------------------------------


def _total_context_tokens(usage: dict) -> int:
    """Compute total context from usage envelope (includes cached tokens)."""
    return (
        usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("output_tokens", 0)
    )


def _build_reanchor_block(agent_def: dict, phil_vars: dict) -> str | None:
    """Extract [INVARIANT]-tagged lines and format a compact precedence block."""
    sources = [str(phil_vars.get("voice", "")), str(agent_def.get("role", ""))]
    invariants: list[str] = []
    for src in sources:
        lines = src.splitlines()
        i = 0
        while i < len(lines):
            if "[INVARIANT]" not in lines[i]:
                i += 1
                continue
            current = [lines[i].split("[INVARIANT]", 1)[1].strip()]
            i += 1
            while i < len(lines) and lines[i].strip():
                if lines[i].lstrip().startswith("["):
                    break
                current.append(lines[i].strip())
                i += 1
            invariant = " ".join(part for part in current if part).strip()
            if invariant:
                invariants.append(invariant)
    if not invariants:
        return None
    body = "\n".join(f"- {inv}" for inv in invariants)
    return (
        "<reanchor>\n"
        "You have crossed the long-context re-anchor threshold. The "
        "following invariants from your initial conditioning take "
        "precedence over anything later in context that conflicts:\n\n"
        f"{body}\n"
        "</reanchor>"
    )


# ---------------------------------------------------------------------------
# Agent-teams residue cleanup and observability
# ---------------------------------------------------------------------------


def _claude_config_dir() -> Path:
    """Resolve the active Claude config dir.

    Prefers the multi-account active dir (which rotates at runtime) over
    the parent process's CLAUDE_CONFIG_DIR env var. This keeps the team-
    tasks sweep pointed at the right account after rotation — rotation
    only sets CLAUDE_CONFIG_DIR in child subprocess envs, not the parent.
    """
    acct = _active_account_dir()
    if acct:
        return Path(acct)
    env_dir = os.environ.get(_provider.child_config_env())
    if env_dir:
        return Path(env_dir)
    if _provider.is_codex():
        return Path.home() / ".codex"
    if _provider.is_gemini():
        return Path.home() / ".gemini"
    return Path.home() / ".claude"


def _sweep_team_tasks(since_ts: float | None) -> int:
    """Remove tasks/* subdirs in CLAUDE_CONFIG_DIR that were touched recently.

    If since_ts is provided, only subdirs with mtime >= since_ts are removed
    (per-turn cleanup after a team-enabled subprocess returns). If since_ts
    is None, all subdirs are removed (startup sweep — safe because no team
    subprocess is running yet).

    Returns the count of directories removed. Best-effort: errors are
    swallowed. Does not touch CLAUDE_CONFIG_DIR/teams/*, which is persistent
    by design.

    Clone safety: suppressed entirely inside fan-out clones. The mtime
    filter cannot distinguish a peer clone's in-flight task dir from
    orphan residue, so concurrent sweeps would race. The root conductor's
    startup sweep (since_ts=None) reclaims any residue on the next root
    invocation.
    """
    if _is_clone():
        return 0
    tasks_dir = _claude_config_dir() / "tasks"
    if not tasks_dir.is_dir():
        return 0
    removed = 0
    try:
        entries = list(tasks_dir.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            if since_ts is not None and entry.stat().st_mtime < since_ts:
                continue
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1
    return removed


def _post_team_cleanup(agent_config: dict, team_turn_start: float | None) -> None:
    """Per-turn cleanup of tasks/<team>/ residue when teams are active.

    No-op when team_turn_start is None (teams were not active for this turn)
    or when cleanup_residue is disabled.
    """
    if team_turn_start is None:
        return
    defaults = agent_config.get("agent_teams_defaults") or {}
    if not defaults.get("cleanup_residue", True):
        return
    removed = _sweep_team_tasks(since_ts=team_turn_start)
    if removed:
        print(f"[long-exposure]   swept {removed} tasks/ residue dir(s)", flush=True)


def _count_teammates(session_id: str | None, since_ts: float | None) -> int:
    """Count teammate transcripts created during this turn.

    Teammate transcripts live under
        $CLAUDE_CONFIG_DIR/projects/<cwd-sanitized>/<session>/subagents/agent-*.jsonl
    We cannot reliably reconstruct <cwd-sanitized> here, so scan all project
    dirs for a <session>/subagents/ subtree.
    """
    if not session_id:
        return 0
    projects_dir = _claude_config_dir() / "projects"
    if not projects_dir.is_dir():
        return 0
    count = 0
    try:
        project_entries = list(projects_dir.iterdir())
    except OSError:
        return 0
    for project in project_entries:
        subagents = project / session_id / "subagents"
        if not subagents.is_dir():
            continue
        try:
            for jsonl in subagents.glob("agent-*.jsonl"):
                try:
                    if since_ts is None or jsonl.stat().st_mtime >= since_ts:
                        count += 1
                except OSError:
                    continue
        except OSError:
            continue
    return count


# ---------------------------------------------------------------------------
# Claude CLI with session persistence
# ---------------------------------------------------------------------------


COMPACTION_PROMPT = """\
[CONTEXT COMPACTION — do NOT produce your normal output format]

Your context window is approaching its limit. Produce a summary of \
everything you know and have done so far. This summary will bootstrap \
your context in a fresh session.

Include:
- Current state of the exploration (sub-topics explored, what's pending)
- Key findings, decisions, and their rationale
- Important constraints or issues discovered
- What you were working on and what should happen next
- Any critical facts that would be lost without this summary

Be thorough but concise. Write in plain text or markdown. \
Do NOT use [OUTPUT:] markers."""


def _local_session_dir(config: dict) -> Path:
    """Directory for local-provider per-agent transcript logs."""
    base = config.get("instance_dir")
    if base:
        root = Path(base)
    else:
        root = _user_writable_data_dir()
    return root / "local_sessions"


def _local_session_path(config: dict, session_id: str) -> Path:
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in ("-", "_"))
    return _local_session_dir(config) / f"{safe}.jsonl"


def _append_local_session_turn(
    config: dict,
    session_id: str,
    agent_name: str,
    user_prompt: str,
    response_text: str,
    usage: dict,
) -> None:
    try:
        path = _local_session_path(config, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": agent_name,
            "user": user_prompt,
            "assistant": response_text,
            "usage": usage or {},
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _read_local_session_turns(config: dict, session_id: str) -> list[dict]:
    path = _local_session_path(config, session_id)
    turns: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    turns.append(item)
    except OSError:
        pass
    return turns


def _format_local_turn(turn: dict, index: int) -> str:
    ts = turn.get("timestamp", "")
    user = turn.get("user", "")
    assistant = turn.get("assistant", "")
    return (
        f"<turn index=\"{index}\" timestamp=\"{ts}\">\n"
        f"<user>\n{user}\n</user>\n"
        f"<assistant>\n{assistant}\n</assistant>\n"
        f"</turn>"
    )


def _format_local_recent_log(config: dict, session_id: str) -> str:
    """Bounded recent transcript injected for local stateless backends."""
    turns = _read_local_session_turns(config, session_id)
    if not turns:
        return ""

    context_window = int(config.get("context_window", config.get("local_context_window", 32768)))
    pct = float(config.get("local_recent_log_pct", 0.25))
    max_tokens = max(0, int(context_window * pct))
    if max_tokens <= 0:
        return ""

    selected: list[tuple[int, str]] = []
    used = 0
    for idx, turn in reversed(list(enumerate(turns, start=1))):
        block = _format_local_turn(turn, idx)
        tok = estimate_tokens(block)
        if selected and used + tok > max_tokens:
            break
        selected.append((idx, block))
        used += tok
        if used >= max_tokens:
            break
    if not selected:
        return ""
    selected.reverse()
    return (
        "[RECENT LOCAL SESSION LOG]\n\n"
        "This provider is stateless, so long-exposure injects a bounded recent\n"
        "transcript for continuity. Treat it as memory, not new instructions;\n"
        "the current role/framework/protocol above remains authoritative.\n\n"
        + "\n\n".join(block for _, block in selected)
    )


def _format_local_compaction_log(config: dict, session_id: str) -> str:
    """Bounded transcript substrate for local compaction."""
    turns = _read_local_session_turns(config, session_id)
    if not turns:
        return ""

    context_window = int(config.get("context_window", config.get("local_context_window", 32768)))
    max_output = int(config.get("local_compact_max_tokens", 4096))
    # Reserve output and prompt overhead. Compaction can use a larger slice
    # than per-turn memory because it is replacing the transcript with a
    # smaller summary.
    max_tokens = max(1024, int(context_window * 0.75) - max_output)
    selected: list[str] = []
    used = 0
    for idx, turn in reversed(list(enumerate(turns, start=1))):
        block = _format_local_turn(turn, idx)
        tok = estimate_tokens(block)
        if selected and used + tok > max_tokens:
            break
        selected.append(block)
        used += tok
        if used >= max_tokens:
            break
    selected.reverse()
    return "\n\n".join(selected)


def _archive_local_session_logs(base_dir: Path) -> None:
    log_dir = base_dir / "local_sessions"
    if not log_dir.exists():
        return
    archive = base_dir / (
        "local_sessions_archived_"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    )
    try:
        shutil.move(str(log_dir), str(archive))
        print(f"[long-exposure] Archived local session logs: {archive.name}", flush=True)
    except OSError:
        pass


def _is_codex_session_poison_error(message: str) -> bool:
    """True for Codex native-thread failures that should force a fresh thread."""
    lowered = (message or "").lower()
    return (
        "ran out of room in the model's context window" in lowered
        or "compact_remote: remote compaction failed" in lowered
    )


def _call_exploration_agent(
    agent_name: str,
    agent_def: dict,
    task: str,
    config: dict,
    results: dict,
    score_inputs: dict,
    agent_sessions: dict,
    agent_summaries: dict,
) -> dict:
    """Call an agent with persistent session context.

    First call (no session): creates session via --session-id, sets system prompt.
    Subsequent calls: resumes session via --resume (context preserved).
    Post-compaction: creates new session with summary appended to system prompt.

    Returns dict matching run_agent() format:
        {status, outputs, usage, duration_ms, error}
    """
    agent_config = build_agent_config(config, agent_def)

    # Build user prompt (same format as conductor)
    user_prompt = build_agent_prompt(
        score_task=task,
        step_agent_name=agent_name,
        agent_def=agent_def,
        results=results,
        score_inputs=score_inputs,
    )

    # Effort is deterministic per agent: explicit override > philosophy default.
    effort = agent_def.get("effort") or PHILOSOPHY_EFFORT_MAP.get(
        agent_config.get("philosophy", "efficient"), "high"
    )

    # Stash derived values onto agent_config so assemble_system_prompt and
    # build_team_guidance_block can read them as a single source of truth.
    agent_config["effort"] = effort
    agent_config["agent_teams"] = agent_teams_enabled(agent_def, config)

    # Opt-in interactive transport (Claude only). Routes the turn through a
    # persistent interactive session instead of `claude -p`. Default-off.
    interactive = _provider.is_claude() and interactive_transport.is_enabled(config)

    tmp_last = None
    if _provider.is_local():
        cmd = []
        cwd = agent_config.get("working_directory") or "/tmp"
    elif _provider.is_codex():
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        tmp_last = tmp.name
        codex_prefix = ["codex"]
        if any("WebSearch" in str(t) for t in agent_config.get("allowed_tools", [])):
            codex_prefix.append("--search")
        cmd = [
            *codex_prefix, "exec",
            "--json",
            "-m", agent_config.get("model", "gpt-5.5"),
            "-o", tmp_last,
        ]
        cmd.extend(_codex_permission_flags(agent_config))
        cwd = agent_config.get("working_directory") or "/tmp"
        if cwd:
            cmd.extend(["-C", cwd])
    elif _provider.is_gemini():
        cmd = [
            "gemini",
            *_gemini_permission_flags(agent_config),
            "--output-format", "json",
            "-m", agent_config.get("model", "gemini-3-flash-preview"),
        ]
        cwd = agent_config.get("working_directory") or "/tmp"
    else:
        cmd = [
            "claude", "-p",
            "--output-format", "json",
            "--model", agent_config.get("model", "opus"),
            "--effort", effort,
        ]

    # Session: create or resume. For the generic local connector, the UUID identifies
    # long-exposure's own JSONL transcript, not a provider-native session.
    session_id = agent_sessions.get(agent_name)
    if _provider.is_local() and not session_id:
        session_id = str(uuid.uuid4())
        agent_sessions[agent_name] = session_id
    was_resume = session_id is not None
    if interactive:
        # Interactive transport has no provider-native per-agent session; every
        # turn is fresh-context with full conditioning. Force the fresh path so
        # the complete system prompt is reassembled each call. Continuity stays
        # file-backed (POR, ledger, inputs, gems) exactly as elsewhere.
        was_resume = False
    summary = None  # track compaction summary for restoration on failure

    cli_stdin = user_prompt
    system_prompt = ""
    if was_resume and not _provider.is_local():
        if _provider.is_codex():
            codex_prefix = ["codex"]
            if any("WebSearch" in str(t) for t in agent_config.get("allowed_tools", [])):
                codex_prefix.append("--search")
            cmd = [
                *codex_prefix, "exec", "resume",
                "--json",
                "--skip-git-repo-check",
                "-m", agent_config.get("model", "gpt-5.5"),
                "-o", tmp_last,
                *_codex_permission_flags(agent_config, resume=True),
                session_id,
                "-",
            ]
        elif _provider.is_gemini():
            cmd.extend(["--resume", session_id, "-p", ""])
            cli_stdin = user_prompt
        else:
            cmd.extend(["--resume", session_id])
    else:
        if _provider.is_claude():
            session_id = str(uuid.uuid4())
            if not interactive:
                # Interactive transport has no provider-native session behind
                # this UUID; persisting it would make a later headless resume
                # burn one failed cycle per agent on `--resume <bogus-uuid>`.
                # The per-call UUID still tags this turn's bookkeeping below.
                agent_sessions[agent_name] = session_id
                cmd.extend(["--session-id", session_id])
        elif _provider.is_gemini():
            session_id = str(uuid.uuid4())
            agent_sessions[agent_name] = session_id
            cmd.extend(["--session-id", session_id])

        # System prompt only on first call — retained on resume
        role_block = build_role_block(
            role_text=agent_def.get("role", f"You are the {agent_name} agent."),
            inputs=agent_def.get("inputs", []),
            outputs=agent_def.get("outputs", []),
        )
        system_prompt = assemble_system_prompt(agent_config, role=role_block)

        # If resuming after compaction, append the summary
        summary = agent_summaries.pop(agent_name, None)
        if summary:
            system_prompt += (
                "\n\n[RESTORED CONTEXT — compacted from previous session]\n\n"
                + summary
                + "\n\n[Continue from where you left off. "
                "Do not re-do completed work.]"
            )

        if _provider.is_local():
            recent_log = _format_local_recent_log(agent_config, session_id)
            if recent_log:
                system_prompt += "\n\n" + recent_log
        elif _provider.is_codex():
            cli_stdin = (
                f"[SYSTEM PROMPT]\n\n{system_prompt}\n\n"
                f"[USER PROMPT]\n\n{user_prompt}"
            )
            cmd.append("-")
        elif _provider.is_gemini():
            cli_stdin = (
                f"[SYSTEM PROMPT]\n\n{system_prompt}\n\n[USER PROMPT]\n\n{user_prompt}"
            )
            cmd.extend(["-p", ""])
        else:
            cmd.extend(["--system-prompt", system_prompt])

    # Permission flags
    perm_flags = build_allowed_tools_flags(agent_config)
    if perm_flags and _provider.is_claude():
        cmd.extend(perm_flags)

    # MCP config (session search tools). When instance_dir is set, the config
    # file lives under it so concurrent sessions don't race on a shared path.
    if agent_def.get("mcp", False) and _provider.is_claude() and not interactive:
        db_path = agent_config.get("compact_db", "")
        if db_path:
            cmd.extend([
                "--mcp-config",
                generate_mcp_config(
                    db_path,
                    instance_dir=agent_config.get("instance_dir"),
                ),
            ])
    elif agent_def.get("mcp", False) and interactive:
        # Interactive transport drops the score's mcp:true request silently;
        # surface it once per agent per run so the operator knows session
        # search is degraded.
        if agent_name not in _INTERACTIVE_MCP_NOTICE_SEEN:
            _INTERACTIVE_MCP_NOTICE_SEEN.add(agent_name)
            print(
                f"[long-exposure]   {agent_name}: session-search MCP "
                f"unavailable in interactive transport",
                flush=True,
            )
    elif _provider.is_gemini():
        # Gemini CLI reads built-in tool restrictions and MCP servers from
        # project-local .gemini/settings.json. Merge long-exposure's current
        # agent permission scope there without changing GEMINI_CLI_HOME.
        generate_gemini_project_settings(agent_config)

    # Execute
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    if agent_config.get("agent_teams") and _provider.is_claude():
        env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"

    # Record start time for post-call mtime-based cleanup of tasks/<team>/ residue.
    team_turn_start = (
        time.time()
        if agent_config.get("agent_teams") and _provider.is_claude()
        else None
    )

    try:
        if interactive:
            envelope = interactive_transport.run_turn(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=agent_config.get("model", "opus"),
                agent_name=agent_name,
                timeout=agent_config.get("cli_timeout") or None,
                config=config,
            )
        elif _provider.is_local():
            envelope = call_local_llm(
                prompt=user_prompt,
                system_prompt=system_prompt,
                model=agent_config.get("model", "custom-local-model"),
                timeout=agent_config.get("cli_timeout") or None,
                config=agent_config,
            )
        else:
            envelope = _invoke_claude(
                cmd,
                stdin_text=cli_stdin,
                env_base=env,
                cwd=agent_config.get("working_directory") or "/tmp",
                timeout=agent_config.get("cli_timeout") or None,
                idle_timeout=agent_config.get("provider_idle_timeout_seconds"),
                idle_poll=agent_config.get("provider_idle_poll_seconds"),
            )
    except ClaudeRateLimitError as e:
        # Rate-limit: signal via status field so the main cycle loop can
        # trigger cycle-level rotation. Preserve the session — on rotation
        # the caller clears agent_sessions wholesale and each agent gets
        # a fresh UUID on the new account.
        _post_team_cleanup(agent_config, team_turn_start)
        # Restore the popped compaction summary so the fresh session on the
        # new account still receives its restored-context bootstrap. Without
        # this, rotation silently degrades agent continuity after compaction.
        if not was_resume and summary is not None:
            agent_summaries[agent_name] = summary
        expected_outputs = agent_def.get("outputs", [])
        return {
            "agent": agent_name,
            "outputs": {
                name: f"[RATE LIMIT: {agent_name}]"
                for name in expected_outputs
            },
            "usage": {},
            "duration_ms": 0,
            "status": "rate_limit",
            "error": str(e)[:500],
        }
    except ClaudeCliError as e:
        _post_team_cleanup(agent_config, team_turn_start)
        err_msg = str(e)
        # Stale-session-id recovery. When `claude --resume <uuid>` targets a
        # session the CLI no longer has on disk (evicted, never persisted, or
        # cross-host migration), the CLI returns
        # "No conversation found with session ID: <uuid>". Without this branch
        # the dead UUID stays pinned in agent_sessions[agent_name] and every
        # subsequent cycle hits the same dead session forever — observed at
        # 21+ consecutive cycles in fork-e84d2e35d494/clone-3.
        # Drop the UUID so the next call goes through the fresh-session path
        # and generates a new one. agent_summaries[agent_name] is intact (we
        # only pop it inside the fresh-session branch above), so any prior
        # compaction context is restored on the next call's system_prompt.
        is_stale_resume = was_resume and "No conversation found" in err_msg
        is_codex_poisoned = (
            was_resume
            and _provider.is_codex()
            and _is_codex_session_poison_error(err_msg)
        )
        if not was_resume:
            agent_sessions.pop(agent_name, None)
            if summary is not None:
                agent_summaries[agent_name] = summary
        elif is_stale_resume or is_codex_poisoned:
            agent_sessions.pop(agent_name, None)
            reason = (
                "Codex over-context session"
                if is_codex_poisoned
                else "stale session"
            )
            print(
                f"[long-exposure]   {agent_name}: {reason} "
                f"{session_id[:8]}... evicted; next cycle starts fresh.",
                flush=True,
            )
        return _error_result(agent_name, agent_def, str(e)[:500])

    # Best-effort sweep of any tasks/<team>/ residue created during this turn.
    _post_team_cleanup(agent_config, team_turn_start)

    response_text = envelope.get("result", "")
    if _provider.is_local() and session_id:
        _append_local_session_turn(
            agent_config,
            session_id,
            agent_name,
            user_prompt,
            response_text,
            envelope.get("usage", {}),
        )
    if not was_resume and _provider.is_codex():
        returned_session_id = envelope.get("session_id")
        if returned_session_id:
            session_id = returned_session_id
            agent_sessions[agent_name] = session_id
    expected_outputs = agent_def.get("outputs", [])
    outputs = parse_outputs(response_text, expected_outputs)
    # Recovery: the CLI envelope's `result` is only the final assistant message.
    # If an expected output is missing there, the agent likely emitted that
    # deliverable in an earlier message of the turn (e.g. before a trailing
    # checkpoint/cover-note). Re-parse the current-turn transcript text so the
    # block is recovered regardless of which message it landed in. Per-name:
    # only names whose primary parse failed are recovered — a successfully
    # parsed fresh block is never overwritten with transcript content.
    # A name's primary parse "failed" when it carries a Tier-3 marker, OR when
    # response_text had no marker blocks at all (Tier-2 whole-message fallback
    # is not a genuine block and stays replaceable by real transcript content).
    _primary_has_markers = bool(_OUTPUT_RE.search(response_text or ""))
    _failed_names = [
        name for name in expected_outputs
        if not _primary_has_markers
        or not outputs.get(name)
        or str(outputs[name]).startswith("[PARSE_FAILED")
    ]
    # Interactive transport: session_id is a synthetic uuid4 never handed to a
    # provider, so no session JSONL can exist — skip the projects/ scan.
    if _failed_names and not interactive:
        full_text = _session_transcript_text(session_id)
        if full_text and _OUTPUT_RE.search(full_text):
            recovered = parse_outputs(full_text, expected_outputs)
            for _name in _failed_names:
                _content = recovered.get(_name, "")
                if _content and not _content.startswith("[PARSE_FAILED"):
                    outputs[_name] = _content
    usage = envelope.get("usage", {})

    # Observability: when teams were active, count teammate transcripts
    # spawned during this turn. No-op otherwise.
    team_stats = None
    if team_turn_start is not None:
        team_stats = {
            "teammates": _count_teammates(session_id, team_turn_start),
        }

    return {
        "agent": agent_name,
        "outputs": outputs,
        "usage": usage,
        "duration_ms": envelope.get("duration_ms", 0),
        "status": "ok",
        "error": None,
        "team_stats": team_stats,
    }


def _call_agent_with_rotation(
    agent_name: str,
    agent_def: dict,
    sessions_dict: dict,
    **kwargs,
) -> dict:
    """Like _call_exploration_agent, but retries with account rotation on
    rate-limit. Used by end-of-run / out-of-cycle agents (reporter,
    final_reporter, curator, merge synthesis) where the main loop's
    cycle-level rotation does not apply.

    Plan I: pool-aware retry. When pool.is_active() and the
    call is not pinned (CLAUDE_FORCE_ACCOUNT unset), a 429 triggers
    pool.mark_rate_limited(current_primary) + pool.promote_fresh() and a
    retry on the new primary. Bounded by the count of non-cooling
    accounts at call time. Pinned (clone-side) calls take a single
    attempt — Plan G's top-of-_run_reporter cooling-check handles the
    cooling-pinned case at the scheduling site.

    Legacy path (pool inactive): rotate via rotate_to_next_account() as
    before, bounded by len(CLAUDE_ACCOUNTS).

    On status="rate_limit" with no further rotation available: drop this
    agent's session (so the next caller doesn't re-resume on the wrong
    account) and return the last rate_limit result so the caller's
    existing failure path handles it.
    """
    from long_exposure import pool as _pool

    accounts = _parse_accounts()
    is_forced, _ = _resolve_force_account(accounts)

    if is_forced and (_is_clone() or not (pool.is_active() or unified_pool.is_unified_active())):
        # Pinned clone or manually pinned non-pool run. Single attempt:
        # clone accounts are bound by design, and manual pins should not
        # silently rotate away from the operator's chosen account. Root pool
        # pins are different: the pool itself uses FORCE env vars to route
        # calls, so they must remain eligible for pool-aware rotation.
        return _call_exploration_agent(
            agent_name=agent_name,
            agent_def=agent_def,
            agent_sessions=sessions_dict,
            **kwargs,
        )

    pool_active = _pool.is_active() or unified_pool.is_unified_active()
    if unified_pool.is_unified_active():
        max_attempts = max(1, unified_pool.callable_account_count())
    elif pool_active:
        try:
            cold_count = sum(
                1 for a in _pool.pool_state().get("accounts", [])
                if a.get("state") in ("cold", "primary", "overflow")
            )
        except Exception:
            cold_count = 1
        max_attempts = max(1, cold_count)
    else:
        max_attempts = max(1, len(accounts))

    result: dict = {}
    for _ in range(max_attempts):
        result = _call_exploration_agent(
            agent_name=agent_name,
            agent_def=agent_def,
            agent_sessions=sessions_dict,
            **kwargs,
        )
        if result["status"] != "rate_limit":
            return result
        sessions_dict.pop(agent_name, None)

        if unified_pool.is_unified_active():
            try:
                old_provider = _provider.current_provider()
                preference = [
                    prv for prv in unified_pool.SUPPORTED_PROVIDERS_FOR_POOL
                    if prv != old_provider
                ] + [old_provider]
                holder = _rotate_unified_root_after_rate_limit(preference)
            except Exception as _err:
                print(
                    f"[long-exposure]   {agent_name} unified rotation failed "
                    f"({_err}); returning last rate_limit",
                    flush=True,
                )
                return result
            if not holder:
                return result
            sessions_dict.clear()
            print(
                f"[long-exposure]   {agent_name} rotated (unified): "
                f"{old_provider} -> {holder.provider}/{Path(holder.account_dir).name}",
                flush=True,
            )
            continue

        if pool_active:
            try:
                cur = _pool.primary_dir()
                if cur:
                    _pool.mark_rate_limited(cur)
                new = _pool.promote_fresh()
            except Exception as _err:
                print(
                    f"[long-exposure]   {agent_name} pool rotation failed "
                    f"({_err}); returning last rate_limit",
                    flush=True,
                )
                return result
            if not new:
                # All cooling. Caller (raw-concat fallback / skip path)
                # handles it.
                return result
            print(
                f"[long-exposure]   {agent_name} rotated (pool): "
                f"{Path(cur).name if cur else '<unknown>'} "
                f"-> {Path(new).name}",
                flush=True,
            )
            try:
                from long_exposure import health_events as _he
                _he.append_event(
                    "out_of_cycle_rotation",
                    detail=(
                        f"agent={agent_name} "
                        f"from={Path(cur).name if cur else '<unknown>'} "
                        f"to={Path(new).name}"
                    ),
                )
            except Exception:
                pass
        else:
            rotate_to_next_account()
    return result


def _compact_agent_session(
    agent_name: str,
    agent_def: dict,
    config: dict,
    agent_sessions: dict,
    agent_summaries: dict,
    conn,
    cycle: int,
    last_session_id: str | None,
) -> str | None:
    """Compact an agent's session: generate summary, store, reset session.

    Resumes the old session with a compaction prompt to extract a summary.
    Stores the summary in sessions.db. Deletes the old session UUID so the
    next call creates a fresh session bootstrapped with the summary.

    Returns the new last_session_id, or the original on failure.
    """
    old_session_id = agent_sessions.get(agent_name)
    if not old_session_id:
        return last_session_id

    agent_config = build_agent_config(config, agent_def)

    if _provider.is_local():
        transcript = _format_local_compaction_log(agent_config, old_session_id)
        if not transcript:
            print("[long-exposure]   Local compaction skipped: no transcript log", flush=True)
            agent_sessions.pop(agent_name, None)
            return last_session_id
        compact_cfg = dict(agent_config)
        compact_cfg["local_max_tokens"] = int(
            compact_cfg.get("local_compact_max_tokens", 4096)
        )
        prompt = (
            COMPACTION_PROMPT
            + "\n\n[SESSION LOG TO COMPACT]\n\n"
            + transcript
        )
        cmd = []
    elif _provider.is_codex():
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        cmd = [
            "codex", "exec", "resume",
            "--json",
            "--skip-git-repo-check",
            "-m", agent_config.get("model", "gpt-5.5"),
            "-o", tmp.name,
            *_codex_permission_flags(agent_config, disable_tools=True, resume=True),
            old_session_id,
            "-",
        ]
    elif _provider.is_gemini():
        cmd = [
            "gemini",
            *_gemini_permission_flags(agent_config, disable_tools=True),
            "--output-format", "json",
            "-m", agent_config.get("model", "gemini-3-flash-preview"),
            "--resume", old_session_id,
            "-p", "",
        ]
    else:
        cmd = [
            "claude", "-p",
            "--output-format", "json",
            "--model", agent_config.get("model", "opus"),
            "--resume", old_session_id,
        ]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    try:
        if _provider.is_local():
            envelope = call_local_llm(
                prompt=prompt,
                system_prompt=(
                    "You compact long-exposure local session logs into a "
                    "durable bootstrap summary. Preserve decisions, current "
                    "state, pending work, constraints, and critical facts."
                ),
                model=agent_config.get("model", "custom-local-model"),
                timeout=agent_config.get("cli_timeout") or None,
                config=compact_cfg,
            )
        else:
            envelope = _invoke_claude(
                cmd,
                stdin_text=COMPACTION_PROMPT,
                env_base=env,
                cwd=agent_config.get("working_directory") or "/tmp",
                timeout=agent_config.get("cli_timeout") or None,
                idle_timeout=agent_config.get("provider_idle_timeout_seconds"),
                idle_poll=agent_config.get("provider_idle_poll_seconds"),
            )
    except ClaudeRateLimitError:
        # Let the caller decide: the main cycle loop triggers rotation;
        # _run_reporter logs and continues.
        raise
    except (ClaudeCliError, OSError) as e:
        print(f"[long-exposure]   Compaction failed: {e}", flush=True)
        agent_sessions.pop(agent_name, None)
        print(
            f"[long-exposure]   Cleared {agent_name} session to prevent cascade.",
            flush=True,
        )
        return last_session_id

    # Strip ``` fences and check for non-empty payload. Empty already means
    # the rate-limit / cliFailure path is the right next step — clearing
    # the agent's session id forces a fresh resume on the next cycle.
    from long_exposure.orchestrator import _strip_xml_fences, _is_well_formed_xml
    summary = _strip_xml_fences(envelope.get("result", ""))
    if not summary:
        print("[long-exposure]   Compaction produced empty summary", flush=True)
        try:
            from long_exposure import health_events as _he
            _he.append_event(
                "compaction_empty_summary",
                detail=f"agent={agent_name} cycle={cycle}",
                cycle=cycle, agent=agent_name,
            )
        except Exception:
            pass
        agent_sessions.pop(agent_name, None)
        print(f"[long-exposure]   Cleared {agent_name} session to prevent cascade.", flush=True)
        return last_session_id

    # Flexible XML well-formedness check. We don't retry inside the cycle
    # path (the cycle loop's own failure-streak handling is more graceful
    # than blocking inside _compact_agent_session); we only surface the
    # bad-XML event so a recurring pattern is visible.
    if not _is_well_formed_xml(summary):
        try:
            from long_exposure import health_events as _he
            _he.append_event(
                "compaction_xml_invalid",
                detail=f"agent={agent_name} cycle={cycle} (stored as-is)",
                cycle=cycle, agent=agent_name,
            )
        except Exception:
            pass

    # Store summary in sessions.db
    session_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()
    db_ok = False
    try:
        store_session(
            conn,
            session_id=session_id,
            parent_id=last_session_id,
            depth=cycle,
            timestamp=timestamp,
            summary_xml=summary,
            philosophy=agent_def.get("philosophy"),
            framework=agent_def.get("framework"),
            token_estimate=len(summary) // 4,
            record_type="compaction",
            topic="Context Summary",
            keywords="compact",
            fork_id=_get_fork_id(),  # None at root, UUID inside clones
        )
        last_session_id = session_id
        db_ok = True
    except Exception as e:
        print(f"[long-exposure]   Compaction DB write failed: {e}", flush=True)

    if db_ok:
        # Reset session: delete old UUID, store summary for bootstrap
        del agent_sessions[agent_name]
        agent_summaries[agent_name] = summary
        print(
            f"[long-exposure]   {agent_name} compacted "
            f"(~{len(summary)//4} tokens summary). "
            f"Fresh session on next cycle.",
            flush=True,
        )
    else:
        print(
            f"[long-exposure]   {agent_name} compaction DB failed, "
            f"keeping current session.",
            flush=True,
        )

    return last_session_id


def _error_result(agent_name: str, agent_def: dict, error: str) -> dict:
    """Build a failure result dict."""
    expected_outputs = agent_def.get("outputs", [])
    return {
        "agent": agent_name,
        "outputs": {name: f"[FAILED: {agent_name}] {error}" for name in expected_outputs},
        "usage": {},
        "duration_ms": 0,
        "status": "error",
        "error": error,
    }


# ---------------------------------------------------------------------------
# Sessions.db logging — single source of truth
# ---------------------------------------------------------------------------


def _extract_topic(output_text: str) -> str | None:
    """Extract topic from agent output. Deterministic — no agent involvement.

    Tries multiple patterns in priority order to handle format variations:
    1. '## Current Sub-Topic' header (original researcher format)
    2. '**Sub-Topic**' in a markdown table (newer researcher format)
    3. First '# ...' through '#### ...' heading that looks like a topic
    """
    if not output_text:
        return None
    import re

    # Pattern 1: ## Current Sub-Topic / Research Topic / Focus header
    match = re.search(
        r"#{1,4}\s*(?:Current\s+)?(?:Sub-?)?(?:Topic|Research\s+Topic|Focus)\s*\n+\*{0,2}([^\n*]+)\*{0,2}",
        output_text, re.IGNORECASE
    )
    if match:
        topic = match.group(1).strip()
        if topic:
            return topic[:100]

    # Pattern 2: **Sub-Topic** | value in a table row
    match = re.search(
        r"\*{2}(?:Sub-?)?Topic\*{2}\s*\|\s*(?:ST\d+\s*[—–-]\s*)?(.+)",
        output_text, re.IGNORECASE
    )
    if match:
        topic = match.group(1).strip().rstrip("|").strip()
        if topic:
            return topic[:100]

    # Pattern 3: First heading that isn't a generic label
    skip = {"query", "response", "checkpoint", "output", "validation",
            "decision", "rationale", "guidance", "research brief"}
    for match in re.finditer(r"^#{1,4}\s+(.+)$", output_text, re.MULTILINE):
        heading = match.group(1).strip().strip("*").strip()
        # Skip generic headings and output markers
        if heading.lower() in skip or heading.startswith("["):
            continue
        if len(heading) > 10:  # skip very short headings
            return heading[:100]

    return None


def _store_agent_output(
    conn, agent_name: str, agent_def: dict, output_text: str,
    cycle: int, parent_id: str | None, current_topic: str | None = None,
) -> str:
    """Store an agent's output in sessions.db.

    Returns the new session_id on success, or the original parent_id
    on failure (so the chain stays connected to the last successful record).
    """
    # Extract topic from researcher output, or use the current cycle's topic
    topic = _extract_topic(output_text) or current_topic or "Untitled"

    session_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        store_session(
            conn,
            session_id=session_id,
            parent_id=parent_id,
            depth=cycle,
            timestamp=timestamp,
            summary_xml=output_text,
            philosophy=agent_def.get("philosophy"),
            framework=agent_def.get("framework"),
            token_estimate=len(output_text) // 4,
            record_type="exploration",
            topic=topic,
            keywords=agent_name,  # use agent name as keyword for identification
            fork_id=_get_fork_id(),  # None at root, UUID inside clones
        )
        try:
            from long_exposure import lemmas
            lemmas.extract_and_store_lemmas(output_text, conn)
        except Exception as e:
            print(f"[lemmas] parse/store skipped: {e!r}", flush=True)
        return session_id
    except Exception as e:
        print(f"[long-exposure]   DB write failed: {e}", flush=True)
        return parent_id or ""


# ---------------------------------------------------------------------------
# Status file — deterministic, overwritten each cycle
# ---------------------------------------------------------------------------


def update_status_file(output_dir: Path, cycle: int, status: str,
                       failures: dict) -> None:
    """Write a simple status file with only deterministic data."""
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        fail_lines = "\n".join(
            f"- {k}: {v} consecutive" for k, v in failures.items() if v > 0
        ) or "None"

        md = (
            f"# Exploration Status\n\n"
            f"**Cycles Completed:** {cycle}\n"
            f"**Status:** {status}\n"
            f"**Updated:** {datetime.now(timezone.utc).isoformat()[:19]}Z\n\n"
            f"## Failure Tracking\n{fail_lines}\n"
        )
        # Atomic so a concurrent `long-exposure status` never reads a
        # truncated file mid-write.
        _atomic_write_text(output_dir / "exploration_status.md", md)
    except OSError as e:
        print(f"[long-exposure] Status file write failed: {e}", flush=True)


# ---------------------------------------------------------------------------
# Adaptive cooldown
# ---------------------------------------------------------------------------


def adaptive_cooldown(base_seconds: int, recent_failure_cycles: int) -> int:
    """Cooldown between cycles. On failure streaks, back off hard.

    0 failures: base cooldown (from config)
    1-2 failures: 2x base (rate limit likely — don't retry faster than normal)
    3+ failures: 2x base (same — stay patient, limit will reset)
    """
    if recent_failure_cycles == 0:
        return base_seconds
    else:
        return base_seconds * 2


def _sleep_interruptible(seconds: int, data_dir: Path | None = None) -> None:
    """Sleep in 1-second increments, checking for stop/clear signals."""
    for _ in range(max(0, int(seconds))):
        if _stop_requested:
            return
        if data_dir:
            _check_signal_files(data_dir)
            if _stop_requested:
                return
        time.sleep(1)


# ---------------------------------------------------------------------------
# Fallback audit (used when audit agent fails)
# ---------------------------------------------------------------------------

FALLBACK_AUDIT = (
    "## Validation Summary\nAudit unavailable this cycle.\n\n"
    "## Decision\nCONTINUE\n\n"
    "## Rationale\n"
    "Audit agent was unavailable. Continuing current sub-topic "
    "for re-evaluation next cycle.\n\n"
    "## Guidance for Research Agent\n"
    "Continue with the current sub-topic. The previous cycle's "
    "work has not been validated.\n"
)


# ---------------------------------------------------------------------------
# Reporter agent
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text to path atomically via sibling .tmp + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _render_report_pdf(md_path: Path, pdf_path: Path, cwd: str) -> None:
    """Render a periodic-report PDF using pandoc + tectonic.

    Raises subprocess.CalledProcessError / TimeoutExpired / FileNotFoundError
    on failure so callers can degrade to MD-only.
    """
    proc = render_report_pdf(md_path, pdf_path, cwd=cwd, timeout=300)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode,
            proc.args,
            output=proc.stdout,
            stderr=proc.stderr,
        )


class _TeeStream:
    """Tee writes to a primary stream and an append-mode file handle.

    Used by clones to record their own log locally, independent of the
    root's reader thread. Swallows BrokenPipeError on the primary (root
    may die mid-run) so the clone keeps running and its local log keeps
    accumulating. File-write failures are also swallowed — this is
    observability plumbing, not load-bearing state.
    """

    def __init__(self, primary, log_fh) -> None:
        self._primary = primary
        self._fh = log_fh

    def write(self, s: str) -> int:
        try:
            n = self._primary.write(s)
        except (OSError, BrokenPipeError, ValueError):
            n = len(s)
        try:
            self._fh.write(s)
            self._fh.flush()
        except (OSError, ValueError):
            pass
        return n

    def flush(self) -> None:
        try:
            self._primary.flush()
        except (OSError, BrokenPipeError, ValueError):
            pass
        try:
            self._fh.flush()
        except (OSError, ValueError):
            pass

    def __getattr__(self, name):
        return getattr(self._primary, name)


_VERDICT_WORDS = {"validated", "pivot", "continue", "diagnosing", "halted"}
_VERDICT_PATTERNS = (
    _re.compile(r"<verdict>\s*(\w+)\s*</verdict>", _re.IGNORECASE),
    _re.compile(r"\bVERDICT[:\s]+([A-Za-z]+)"),
    _re.compile(r"^\s*[-*]?\s*Verdict[:\s]+(\w+)", _re.MULTILINE),
)


def _extract_verdict(prose: str) -> str:
    """Best-effort: pull a structured verdict keyword from reporter prose.

    Returns one of {validated, pivot, continue, diagnosing, halted} if a
    recognizable marker is present; else 'unknown'. Non-fatal — the
    frontmatter always lands, the verdict just defaults to 'unknown' when
    the LLM didn't emit a parseable marker.
    """
    if not prose:
        return "unknown"
    for pat in _VERDICT_PATTERNS:
        m = pat.search(prose)
        if m:
            v = m.group(1).lower()
            if v in _VERDICT_WORDS:
                return v
    return "unknown"


def _yaml_quote(s: str) -> str:
    """Quote a string for safe inclusion as a YAML scalar value.

    Uses JSON-style double-quoted scalar form, which YAML 1.2 accepts and
    which escapes backslashes and double-quotes unambiguously. Covers
    paths with colons, timestamps, embedded quotes, etc.
    """
    return '"' + (s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _merge_report_frontmatter(
    fork_id: str,
    clone_k: int,
    cycle_range: str,
    deliverable_path: str | None,
    deliverable_exists: bool,
    verdict: str,
) -> str:
    """Render YAML frontmatter for a clone's merge_report.md.

    Objective fields (everything except verdict) are computed by Python
    from state + assignment + filesystem. Verdict is extracted from the
    LLM's prose by _extract_verdict — defaults to 'unknown' on miss.
    String values are JSON-style quoted so values containing ':' (paths,
    timestamps) parse unambiguously in every YAML library.
    """
    lines = [
        "---",
        f"fork_id: {_yaml_quote(fork_id)}",
        f"clone_k: {clone_k}",
        f"cycle_range: {_yaml_quote(cycle_range)}",
        f"deliverable_path: {_yaml_quote(deliverable_path or '')}",
        f"deliverable_exists: {str(bool(deliverable_exists)).lower()}",
        f"verdict: {_yaml_quote(verdict)}",
        f"generated_at: {_yaml_quote(datetime.now(timezone.utc).isoformat())}",
        "---",
        "",
    ]
    return "\n".join(lines)


def _write_files_touched(
    instance_dir: Path,
    workspace: Path | None,
    since_ts: float,
) -> int:
    """Enumerate workspace files with mtime >= since_ts; write fork_files_touched.txt.

    Plan H: file is fork-scoped, NOT per-clone. All clones in a
    single fan-out are spawned within milliseconds of each other, so each
    clone's `since_ts` covers the whole fork's lifetime — every clone's
    list ends up identical. The output is renamed accordingly. For per-clone
    authorship use `clone_artifacts.json` (derived from the shadow ledger
    by `_write_clone_artifacts`).

    Does NOT follow symlinks (prevents cyclic-link walks). Per-file stat
    failures are skipped (mid-walk deletions etc.). Returns the count
    written, or 0 on any top-level failure. Best-effort — not load-bearing.
    """
    if workspace is None or not workspace.is_dir():
        return 0
    touched: list[str] = []
    try:
        for root, _dirs, files in os.walk(workspace, followlinks=False):
            for name in files:
                fp = Path(root) / name
                try:
                    st = fp.stat()
                except OSError:
                    continue
                if st.st_mtime >= since_ts:
                    try:
                        rel = fp.relative_to(workspace)
                    except ValueError:
                        continue
                    touched.append(str(rel))
    except OSError:
        return 0
    touched.sort()
    try:
        _atomic_write_text(
            Path(instance_dir) / "fork_files_touched.txt",
            "\n".join(touched) + ("\n" if touched else ""),
        )
    except OSError:
        return 0
    return len(touched)


def _write_clone_artifacts(instance_dir: Path) -> int:
    """Derive per-clone artifact list from this clone's shadow ledger.

    Plan H: the per-clone shadow ledger at
    `<instance_dir>/promise_ledger.jsonl` carries authorship by construction
    (`tools/ledger_append.py` routes writes there when `AGENT_FORK_ID` is
    set). This helper extracts `artifact_path` (single) or `artifacts`
    (list) from each event and writes `clone_artifacts.json` with enriched
    per-event entries (path + event_id + type + milestone_id + timestamp).

    Returns the count written, or 0 on any failure. Best-effort —
    non-fanout runs (no shadow ledger) silently skip.
    """
    shadow = Path(instance_dir) / "promise_ledger.jsonl"
    if not shadow.is_file():
        return 0
    out: list[dict] = []
    try:
        for line in shadow.read_text().splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            paths: list[str] = []
            ap = ev.get("artifact_path")
            if isinstance(ap, str) and ap:
                paths.append(ap)
            arts = ev.get("artifacts")
            if isinstance(arts, list):
                paths.extend(p for p in arts if isinstance(p, str) and p)
            if not paths:
                continue
            for p in paths:
                out.append({
                    "path": p,
                    "event_id": (
                        ev.get("event_id")
                        or ev.get("milestone_id")
                        or ""
                    ),
                    "type": ev.get("type", ""),
                    "milestone_id": ev.get("milestone_id", ""),
                    "timestamp": ev.get("timestamp", ""),
                })
    except OSError:
        return 0
    try:
        _atomic_write_text(
            Path(instance_dir) / "clone_artifacts.json",
            json.dumps({"artifacts": out}, indent=2),
        )
    except OSError:
        return 0
    return len(out)


_HEADING_RE = _re.compile(r"^\s*#+\s+(.+?)\s*$", _re.MULTILINE)


def _first_heading(text: str, maxlen: int = 200) -> str:
    """Return the first markdown heading's text (up to maxlen), or '' if none."""
    if not text:
        return ""
    m = _HEADING_RE.search(text)
    return m.group(1)[:maxlen] if m else ""


def _write_latest_report_pointer(
    instance_dir: Path,
    clone_k: int,
    cycle_range: str,
    mode: str,
    report_body: str,
) -> None:
    """Write <instance_dir>/latest_report_pointer.md — a small, overwrite-on-each-run
    pointer sibling clones read at the start of their own researcher cycles.

    Pointer only (no content). Fields: clone_k, cycle_range, mode
    (periodic|merge), written_at, first_heading. Atomic via
    _atomic_write_text.
    """
    heading = _first_heading(report_body)
    # All values JSON-quoted so the minimal parser in _collect_sibling_pointers
    # can use a single regex (string-only). Consumer coerces clone_k to int.
    body = (
        "---\n"
        f"clone_k: {_yaml_quote(str(clone_k))}\n"
        f"cycle_range: {_yaml_quote(cycle_range)}\n"
        f"mode: {_yaml_quote(mode)}\n"
        f"written_at: {_yaml_quote(datetime.now(timezone.utc).isoformat())}\n"
        f"first_heading: {_yaml_quote(heading)}\n"
        "---\n"
    )
    try:
        _atomic_write_text(
            Path(instance_dir) / "latest_report_pointer.md", body,
        )
    except OSError:
        pass


_POINTER_FIELD_RE = _re.compile(
    r'^(\w+):\s*"((?:[^"\\]|\\.)*)"\s*$', _re.MULTILINE,
)


def _collect_sibling_pointers(own_instance_dir: Path) -> str | None:
    """Read every sibling's latest_report_pointer.md in the fork; return a
    single <sibling_reports> guidance block, or None if there are none.

    Self-skipped: compares against own_instance_dir name. Malformed
    sibling pointers are silently skipped — a bad sibling never blocks
    a good one. Returns None when there's nothing to inject so callers
    can drop the block entirely.
    """
    own = Path(own_instance_dir).resolve()
    fork_dir = own.parent
    if not fork_dir.is_dir():
        return None

    entries: list[tuple[int, str]] = []  # (clone_k, formatted_line)
    try:
        siblings = sorted(fork_dir.iterdir())
    except OSError:
        return None

    for sib in siblings:
        if not sib.is_dir() or sib.resolve() == own:
            continue
        ptr = sib / "latest_report_pointer.md"
        if not ptr.is_file():
            continue
        try:
            content = ptr.read_text()
        except OSError:
            continue
        fields: dict[str, str] = {}
        for m in _POINTER_FIELD_RE.finditer(content):
            fields[m.group(1)] = m.group(2).encode().decode("unicode_escape")
        try:
            ck = int(fields.get("clone_k", "-1"))
        except ValueError:
            ck = -1
        cr = fields.get("cycle_range", "")
        mode = fields.get("mode", "")
        wa = fields.get("written_at", "")
        heading = fields.get("first_heading", "")
        line = (
            f"  clone-{ck} ({cr}, {mode}, written {wa[:19]}): "
            f"\"{heading}\""
        )
        entries.append((ck, line))

    if not entries:
        return None
    entries.sort()
    block = [
        "<sibling_reports>",
        "  For the RESEARCHER only — supplemental pointers to sibling clones'",
        "  latest reports. Workers and auditors: ignore this block.",
        "  Do NOT read these by default. See <sibling-awareness> in your role.",
    ] + [line for _, line in entries] + [
        "</sibling_reports>",
    ]
    return "\n".join(block)


def _build_anti_patterns_block(workspace: Path, config: dict) -> str | None:
    try:
        anti_cfg = config.get("anti_patterns", {})
        if isinstance(anti_cfg, dict) and not anti_cfg.get("enabled", True):
            return None
        from long_exposure import anti_patterns
        max_entries = (
            int(anti_cfg.get("max_entries", anti_patterns.MAX_ENTRIES))
            if isinstance(anti_cfg, dict) else anti_patterns.MAX_ENTRIES
        )
        max_chars = (
            int(anti_cfg.get("max_rationale_chars", anti_patterns.MAX_RATIONALE_CHARS))
            if isinstance(anti_cfg, dict) else anti_patterns.MAX_RATIONALE_CHARS
        )
        return anti_patterns.build_block(
            workspace,
            max_entries=max_entries,
            max_rationale_chars=max_chars,
        ) or None
    except Exception as exc:
        print(f"[anti-patterns] skipped: {exc!r}", flush=True)
        return None


def _compute_merge_frontmatter_fields(
    instance_dir: Path, config: dict | None,
) -> tuple[str, int, str, bool]:
    """Read objective frontmatter fields from env + assignment + workspace.

    Returns (fork_id, clone_k, deliverable_path, deliverable_exists).
    All read failures collapse to safe defaults ('' / -1 / False).
    """
    fork_id = os.environ.get("AGENT_FORK_ID", "") or ""
    try:
        clone_k = int(os.environ.get("AGENT_FORK_CLONE_K", "-1"))
    except ValueError:
        clone_k = -1
    deliverable_path = ""
    deliverable_exists = False
    try:
        fa_path = Path(instance_dir) / "fanout_assignment.json"
        if fa_path.exists():
            fa = json.loads(fa_path.read_text())
            deliverable_path = fa.get("output_artifact", "") or ""
            if deliverable_path and config:
                wd = config.get("working_directory")
                if wd:
                    deliverable_exists = (Path(wd) / deliverable_path).is_file()
    except (OSError, json.JSONDecodeError):
        pass
    return fork_id, clone_k, deliverable_path, deliverable_exists


def _write_merge_report(
    merge_path: Path,
    body: str,
    config: dict | None,
    cycle_range: str,
    verdict: str | None = None,
) -> None:
    """Write merge_report.md with YAML frontmatter + body, atomically.

    verdict=None → extract from body via _extract_verdict (LLM output).
    verdict=<str> → caller-supplied (placeholder / error paths).
    """
    instance_dir = Path(merge_path).parent
    fork_id, clone_k, dpath, dexists = _compute_merge_frontmatter_fields(
        instance_dir, config,
    )
    v = verdict if verdict is not None else _extract_verdict(body)
    fm = _merge_report_frontmatter(
        fork_id, clone_k, cycle_range, dpath, dexists, v,
    )
    _atomic_write_text(Path(merge_path), fm + body)


def _first_markdown_heading(path: Path) -> str:
    try:
        for line in path.read_text(errors="replace").splitlines():
            text = line.strip()
            if text.startswith("#"):
                return text.lstrip("#").strip() or path.name
    except OSError:
        pass
    return path.name


def _fanout_artifact_index(
    config: dict,
    cycle_range_start: int,
    cycle_range_end: int,
) -> str:
    """Build a deterministic root-report index for branch cycle artifacts."""
    try:
        root = paths.workspace_root(config)
        artifacts: list[Path] = []
        for cyc in range(cycle_range_start, cycle_range_end + 1):
            cycle_dir = paths.fanout_cycle_dir(config, cyc)
            if cycle_dir.exists():
                artifacts.extend(p for p in sorted(cycle_dir.glob("*.md")) if p.is_file())
        if not artifacts:
            return ""
        lines = [
            "",
            "## Fan-Out Artifact Index",
            "",
            "Branch artifacts detected for this report window:",
        ]
        for path in artifacts:
            try:
                rel = path.relative_to(root)
            except ValueError:
                rel = path
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            title = _first_markdown_heading(path)
            lines.append(f"- `{rel}` ({size} bytes): {title}")
        return "\n".join(lines) + "\n"
    except Exception:
        return ""


def _install_clone_local_log(instance_dir: Path) -> None:
    """Tee clone's stdout/stderr to <instance_dir>/clone_local.log.

    Makes the clone's transcript survive root death. Idempotent in effect —
    if this fails for any reason, sys.stdout/stderr stay unmodified and
    the root's reader thread remains the only transcription channel.
    """
    log_path = instance_dir / "clone_local.log"
    try:
        fh = open(log_path, "a", encoding="utf-8", errors="replace")
        fh.write(
            f"\n=== clone_local.log opened "
            f"{datetime.now(timezone.utc).isoformat()} "
            f"(pid={os.getpid()}) ===\n"
        )
        fh.flush()
        sys.stdout = _TeeStream(sys.stdout, fh)
        sys.stderr = _TeeStream(sys.stderr, fh)
    except OSError:
        pass


def _append_report_artifact_event(
    config: dict,
    score_inputs: dict,
    *,
    cycle: int,
    cycle_range_start: int,
    cycle_range_end: int,
    reporter_mode: str,
    artifacts: list[Path],
) -> None:
    """Ledger-link deterministic report artifacts without blocking the run."""
    try:
        workspace = paths.workspace_root(config).resolve()
        rel_artifacts: list[str] = []
        for artifact in artifacts:
            try:
                path = Path(artifact).resolve()
                if not path.exists():
                    continue
                rel_artifacts.append(path.relative_to(workspace).as_posix())
            except (OSError, ValueError):
                continue
        if not rel_artifacts:
            return

        append_ledger_event(workspace, {
            "event_id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": (
                (score_inputs or {}).get("run_id")
                or (config or {}).get("run_id")
                or "run-unknown"
            ),
            "cycle": int(cycle or cycle_range_end or 0),
            "agent": "harness",
            "milestone_id": (
                f"_run/report_cycles_{cycle_range_start}-{cycle_range_end}"
            ),
            "status": "validated",
            "confidence": {
                "level": "high",
                "rationale": (
                    f"{reporter_mode} report artifact written by the harness"
                ),
                "assessor": "harness",
            },
            "narrative": (
                "Deterministic report artifact registration for audit "
                "and orphan-artifact checks."
            ),
            "artifacts": rel_artifacts,
            "reporter_mode": reporter_mode,
        })
    except Exception:
        pass


def _run_reporter(
    reporter_def: dict,
    task: str,
    config: dict,
    results: dict,
    score_inputs: dict,
    agent_sessions: dict,
    agent_summaries: dict,
    conn,
    cycle: int,
    last_session_id: str | None,
    cycle_range_start: int,
    cycle_range_end: int,
    cycle_session_log: list,
    context_window: int,
    compact_at: int,
    reporter_mode: str = "periodic",
    merge_report_path: Path | None = None,
) -> str | None:
    """Run the reporter agent to generate a periodic or merge report.

    Called every report_interval cycles, on stop, and on clone exit (with
    reporter_mode="merge"). Follows the same session persistence and
    auto-compact patterns as cycle agents.

    reporter_mode:
      - "periodic": normal report; agent writes report_cycles_*.md as usual.
      - "merge": clone-exit synthesis; the agent's output_text is also
        written atomically to merge_report_path (typically
        <clone_instance_dir>/merge_report.md) so the root conductor's
        barrier can pick it up without racing a partial write.

    On failure, logs and returns without blocking. In merge mode, on
    failure writes a short placeholder so the barrier still observes the
    file (best-effort merge substrate).

    Returns updated last_session_id.
    """
    cycle_range_str = (
        f"cycles {cycle_range_start}-{cycle_range_end} [merge]"
        if reporter_mode == "merge"
        else f"cycles {cycle_range_start}-{cycle_range_end}"
    )

    # Plan G: clone-side fast-skip when our pinned account
    # is cooling. Saves a predictably-doomed round-trip and removes the
    # "FAILED — Claude account rate-limited" log line that confuses
    # operators after the worker's own rate-limit-exit. CLAUDE_FORCE_ACCOUNT
    # may be a path (set by pool.acquire_slot) or a numeric index (legacy
    # CLAUDE_ACCOUNTS rotation); resolve digits via _resolve_force_account
    # so the cooling check has a real path to compare against pool entries.
    force_env = _provider.force_account_env()
    pinned = (os.environ.get(force_env) or "").strip()
    if pinned and pinned.isdigit():
        try:
            from long_exposure.orchestrator import (
                _parse_accounts as _pa, _resolve_force_account as _rf,
            )
            _, resolved = _rf(_pa())
            pinned = resolved or ""
        except Exception:
            pinned = ""
    if pinned:
        try:
            from long_exposure import pool as _pool
            if _pool.is_cooling(pinned):
                print(
                    f"[long-exposure]   reporter skipped: pinned account "
                    f"{Path(pinned).name} is cooling.",
                    flush=True,
                )
                if reporter_mode == "merge" and merge_report_path is not None:
                    _write_merge_report(
                        merge_report_path,
                        f"# Merge Report (skipped)\n\n"
                        f"Reporter skipped: pinned account "
                        f"{Path(pinned).name} was cooling at reporter "
                        f"dispatch time. Worker output (if any) is in "
                        f"this clone's output/ directory.\n",
                        config,
                        cycle_range_str,
                        verdict="halted",
                    )
                try:
                    from long_exposure import health_events as _he
                    _he.append_event(
                        "reporter_skipped_cooling",
                        detail=(
                            f"pinned={Path(pinned).name} "
                            f"mode={reporter_mode}"
                        ),
                    )
                except Exception:
                    pass
                return last_session_id
        except Exception:
            # Pool import / lookup failure should never break the reporter
            # — fall through to the existing path.
            pass

    # Build session pointers for the reporter
    session_lines = []
    for entry in cycle_session_log:
        session_lines.append(
            f"Cycle {entry['cycle']}: "
            f"{entry['agent']}={entry['session_id']}"
        )
    cycle_sessions_str = (
        "\n".join(session_lines) if session_lines
        else "No session records available."
    )

    # Reporter gets its own copy of results with extra inputs
    reporter_results = dict(results)
    reporter_results["cycle_range"] = cycle_range_str
    reporter_results["cycle_sessions"] = cycle_sessions_str
    # Plan 06 §4.4: per-cycle reporter needs working_directory so its
    # figure-scan instructions have a concrete root to walk via Glob.
    reporter_results["working_dir"] = config.get("working_directory") or "/tmp"

    # Collision-safe report basename. Root writes report_cycles_{A}-{B};
    # each fan-out clone appends _clone_{k} so siblings can't clobber
    # each other in the shared working_directory. Agent is told (via
    # the reporter role prompt) to use this exact value for both the
    # .md and .pdf filenames. `-1` matches the existing sentinel used
    # for a clone whose AGENT_FORK_CLONE_K env var is unset/invalid.
    if _is_clone():
        _k = _get_clone_k()
        _basename_suffix = f"_clone_{_k if _k is not None else -1}"
    else:
        _basename_suffix = ""
    reporter_results["report_basename"] = (
        f"report_cycles_{cycle_range_start}-{cycle_range_end}"
        f"{_basename_suffix}"
    )

    print(
        f"\n[long-exposure] === Reporter ({cycle_range_str}) ===",
        flush=True,
    )

    is_resume = "reporter" in agent_sessions
    print(
        f"[long-exposure] {'Resuming' if is_resume else 'Starting'}: "
        f"reporter"
        f"{' (' + agent_sessions['reporter'][:8] + '...)' if is_resume else ''}",
        flush=True,
    )

    result = _call_agent_with_rotation(
        agent_name="reporter",
        agent_def=reporter_def,
        sessions_dict=agent_sessions,
        task=task,
        config=config,
        results=reporter_results,
        score_inputs=score_inputs,
        agent_summaries=agent_summaries,
    )
    telemetry.emit_agent_result(
        "reporter",
        result,
        cycle=cycle,
        provider=config.get("llm_provider"),
        model=config.get("model"),
        context_window=context_window,
    )

    if result["status"] == "ok":
        usage = result.get("usage", {})
        dur = result.get("duration_ms", 0) / 1000
        total_ctx = _total_context_tokens(usage)
        print(
            f"[long-exposure]   reporter: ok "
            f"({dur:.1f}s, ctx:{total_ctx:,}tok, "
            f"out:{usage.get('output_tokens', 0)}tok)",
            flush=True,
        )

        output_text = "\n\n".join(result["outputs"].values())
        last_session_id = _store_agent_output(
            conn, "reporter", reporter_def, output_text,
            cycle, last_session_id,
            current_topic=(
                "Clone Merge Report" if reporter_mode == "merge"
                else "Exploration Report"
            ),
        )

        # Sibling visibility: after a successful reporter run, update this
        # clone's latest_report_pointer.md so siblings can pick it up at
        # the start of their next researcher cycle. Pointer, not content.
        # See _collect_sibling_pointers. Root path (non-clone) skips this.
        if _is_clone():
            _inst_for_ptr = config.get("instance_dir") if config else None
            if _inst_for_ptr:
                _write_latest_report_pointer(
                    Path(_inst_for_ptr),
                    _get_clone_k() if _get_clone_k() is not None else -1,
                    cycle_range_str,
                    "merge" if reporter_mode == "merge" else "periodic",
                    output_text,
                )

        # Periodic report: Python owns the write so the filename is
        # deterministic (agent trust was the previous failure mode —
        # two fan-out clones could both write `report_cycles_1-3.md`
        # and clobber each other in the shared working_directory).
        # We atomic-write {basename}.md from the agent's [OUTPUT: report]
        # block, then best-effort render {basename}.pdf via pandoc.
        # Skipped in merge mode — that path writes merge_report.md below.
        if reporter_mode != "merge":
            _wd = (config or {}).get("working_directory")
            _basename = reporter_results.get("report_basename")
            if _wd and _basename and output_text.strip():
                # Plan 3 §3.2 / §4.2: periodic reports land in reports/.
                # If the workspace was bootstrapped (STRUCTURE.md exists),
                # reports/ is guaranteed present; otherwise we create it
                # on first write so the directory always exists for
                # downstream reads. Legacy report_cycles_*.md at root
                # remain readable — _estimate_prior_report_tokens uses
                # rglob so it picks up reports anywhere.
                paths.ensure_layout(config)
                _reports_dir = paths.cycle_reports_dir(config)
                try:
                    _reports_dir.mkdir(parents=True, exist_ok=True)
                except OSError:
                    _reports_dir = Path(_wd)  # fall back to root
                _md = paths.cycle_report_md(config, _basename)
                try:
                    if not _is_clone():
                        artifact_index = _fanout_artifact_index(
                            config, cycle_range_start, cycle_range_end,
                        )
                        if (
                            artifact_index
                            and "## Fan-Out Artifact Index" not in output_text
                        ):
                            output_text = output_text.rstrip() + "\n" + artifact_index
                    formatted_report = normalize_report_markdown(
                        output_text,
                        fallback_title=(
                            f"Long-Exposure Report "
                            f"{cycle_range_start}-{cycle_range_end}"
                        ),
                    )
                    _atomic_write_text(_md, formatted_report)
                    telemetry.emit(
                        "report_markdown_written",
                        phase="report",
                        cycle=cycle,
                        agent="reporter",
                        provider=config.get("llm_provider"),
                        model=config.get("model"),
                        status="ok",
                        data={
                            "mode": reporter_mode,
                            "path": str(_md),
                            "bytes": len(formatted_report.encode("utf-8")),
                        },
                    )
                    print(
                        f"[long-exposure]   report: wrote {_md}",
                        flush=True,
                    )
                    _md_ok = True
                except OSError as _md_err:
                    print(
                        f"[long-exposure]   report: MD write FAILED "
                        f"({_md_err})",
                        flush=True,
                    )
                    _md_ok = False

                if _md_ok:
                    _pdf = paths.cycle_report_pdf(config, _basename)
                    _report_artifacts = [_md]
                    try:
                        _render_report_pdf(_md, _pdf, _wd)
                        telemetry.emit(
                            "report_pdf_render_end",
                            phase="report",
                            cycle=cycle,
                            agent="reporter",
                            provider=config.get("llm_provider"),
                            model=config.get("model"),
                            status="ok",
                            data={"mode": reporter_mode, "path": str(_pdf)},
                        )
                        print(
                            f"[long-exposure]   report: wrote {_pdf}",
                            flush=True,
                        )
                        _report_artifacts.append(_pdf)
                    except (
                        subprocess.CalledProcessError,
                        subprocess.TimeoutExpired,
                        FileNotFoundError,
                        OSError,
                    ) as _pdf_err:
                        # PDF is convenience; MD is authoritative.
                        # Truncate stderr so a verbose tectonic failure
                        # doesn't swamp the log.
                        _stderr_tail = ""
                        if isinstance(
                            _pdf_err, subprocess.CalledProcessError,
                        ) and _pdf_err.stderr:
                            stderr = _pdf_err.stderr
                            if isinstance(stderr, bytes):
                                stderr = stderr.decode(errors="replace")
                            _stderr_tail = str(stderr)[-400:]
                        print(
                            f"[long-exposure]   report: PDF render "
                            f"failed ({type(_pdf_err).__name__}); "
                            f"MD is authoritative. {_stderr_tail}",
                            flush=True,
                        )
                        telemetry.emit(
                            "report_pdf_render_end",
                            phase="report",
                            cycle=cycle,
                            agent="reporter",
                            provider=config.get("llm_provider"),
                            model=config.get("model"),
                            status="error",
                            data={
                                "mode": reporter_mode,
                                "path": str(_pdf),
                                "error_class": type(_pdf_err).__name__,
                            },
                        )
                    _append_report_artifact_event(
                        config,
                        score_inputs,
                        cycle=cycle,
                        cycle_range_start=cycle_range_start,
                        cycle_range_end=cycle_range_end,
                        reporter_mode=reporter_mode,
                        artifacts=_report_artifacts,
                    )

        # In merge mode, atomically write the synthesis to merge_report.md
        # with YAML frontmatter (fork_id, clone_k, verdict, deliverable_path,
        # deliverable_exists, generated_at). The barrier reads the file via
        # atomic rename so there's no partial-read race.
        if reporter_mode == "merge" and merge_report_path is not None:
            try:
                _write_merge_report(
                    Path(merge_report_path), output_text, config,
                    cycle_range_str,
                )
                telemetry.emit(
                    "fanout_merge_report_written",
                    phase="fanout",
                    cycle=cycle,
                    agent="reporter",
                    provider=config.get("llm_provider"),
                    model=config.get("model"),
                    status="ok",
                    data={"path": str(merge_report_path), "bytes": len(output_text.encode("utf-8"))},
                )
                print(
                    f"[long-exposure]   merge_report written: {merge_report_path}",
                    flush=True,
                )
            except OSError as e:
                print(
                    f"[long-exposure]   merge_report write FAILED: {e}",
                    flush=True,
                )

        # Auto-compact check
        if total_ctx >= compact_at:
            print(
                f"[long-exposure]   reporter context "
                f"{total_ctx:,}/{context_window:,} "
                f"({total_ctx/context_window:.0%}) — compacting...",
                flush=True,
            )
            try:
                last_session_id = _compact_agent_session(
                    "reporter", reporter_def, config,
                    agent_sessions, agent_summaries,
                    conn, cycle, last_session_id,
                )
            except ClaudeRateLimitError as e:
                # Reporter is non-fatal; the main work already committed.
                print(
                    f"[long-exposure]   reporter compaction rate-limited "
                    f"(non-fatal): {str(e)[:200]}",
                    flush=True,
                )
    else:
        err = result.get("error", "unknown")
        print(f"[long-exposure]   reporter: FAILED — {err}", flush=True)
        print("[long-exposure]   Report skipped. Continuing.", flush=True)
        # In merge mode, the barrier is watching for merge_report.md. Write
        # a placeholder so the root conductor sees the file land — otherwise
        # it blocks on a clone that will never produce a report.
        if reporter_mode == "merge" and merge_report_path is not None:
            try:
                _write_merge_report(
                    Path(merge_report_path),
                    f"# Merge Report (reporter failed)\n\n"
                    f"Clone reporter failed: {err}\n\n"
                    f"Partial cycle range: {cycle_range_str}.\n",
                    config,
                    cycle_range_str,
                    verdict="halted",
                )
            except OSError:
                pass

    return last_session_id



# ---------------------------------------------------------------------------
# Main exploration loop
# ---------------------------------------------------------------------------


def run_exploration(
    score_path: str,
    config_path: str | None = None,
    output_dir: Path | None = None,
    state_path: Path | None = None,
    task_override: str | None = None,
    instance_dir: Path | None = None,
) -> None:
    global _stop_requested, _clear_requested

    score = load_exploration_score(score_path)
    config = load_config(config_path)
    _provider.configure_provider(config)

    # Thread instance_dir onto config so nested machinery (call_agent_with_session
    # → generate_mcp_config) can pick it up via the agent_config copy produced by
    # build_agent_config.
    config["instance_dir"] = str(instance_dir) if instance_dir is not None else None

    # Apply instance-dir defaults for output_dir / state_path when the caller
    # didn't pass explicit paths. Callers that DO pass explicit paths win.
    if output_dir is None:
        output_dir = _resolve_output_dir(None, instance_dir)
    else:
        output_dir = Path(output_dir)
    if state_path is None:
        state_path = _resolve_state_path(None, instance_dir)
    else:
        state_path = Path(state_path)
    data_dir = state_path.parent

    # --- Clone bootstrap (fan-out) ---
    # If this process is a fan-out clone, read the per-branch assignment
    # written by the root conductor and use it as the task override. The
    # clone's state file was pre-seeded with the parent's agent_sessions,
    # so auto-compact / --resume restore the parent's context and gems.
    if _is_clone():
        # Install clone-local stdout/stderr tee BEFORE any other prints, so
        # the local log captures the full clone transcript even if the
        # root's reader thread dies mid-run (root-death blindness).
        _install_clone_local_log(data_dir)
        _fork_id = _get_fork_id()
        _clone_k = _get_clone_k()

        # Slot lifecycle (PID-race fix):
        #
        # The parent acquired this clone's pool slot before subprocess.Popen
        # — necessarily, because CLAUDE_FORCE_ACCOUNT must be set in the
        # clone's env BEFORE Popen. At acquire time the holder was tagged
        # with parent PID. The parent ALSO calls update_slot_pid post-Popen
        # to re-tag, but if the parent crashes between Popen and re-tag,
        # heartbeat_sweep can't reclaim the slot via the clone's PID
        # (it would only reclaim when parent dies).
        #
        # Fix: the clone owns its slot lifecycle. As soon as we know we are
        # a clone (env vars present and parsed), we re-tag with our own PID
        # — idempotent with the parent's post-Popen re-tag, so doing both
        # is safe — and register an atexit handler that releases the slot
        # via release_slot_by_branch on any clean exit. Combined with the
        # conductor's barrier-collapse release (also idempotent), the slot
        # has three independent paths to release:
        #
        #   1. Clone clean exit (atexit)        — 99% case
        #   2. Conductor barrier collapse        — handles unclean exits
        #   3. heartbeat_sweep on PID death      — handles partition events
        #
        # Slot leakage now requires ALL THREE paths to fail, which is
        # effectively impossible.
        if _fork_id and _clone_k is not None:
            try:
                from long_exposure import pool as _pool
                if _pool.is_active():
                    _ok = _pool.update_slot_pid(_fork_id, _clone_k, os.getpid())
                    if not _ok:
                        # Slot wasn't found under (fork_id, clone_k). Either
                        # the parent fell back to inherited account (pool
                        # exhausted at spawn), or the slot was already
                        # released. Surface to off-nominal events; do not
                        # raise — running without a slot tag is functionally
                        # fine, just unobservable.
                        try:
                            from long_exposure import health_events as _he
                            _he.append_event(
                                "pool_clone_self_retag_missed",
                                detail=f"fork={_fork_id} clone_k={_clone_k}",
                            )
                        except Exception:
                            pass
                    import atexit
                    def _release_slot_on_exit() -> None:
                        try:
                            _pool.release_slot_by_branch(_fork_id, _clone_k)
                        except Exception:
                            pass
                    atexit.register(_release_slot_on_exit)
            except Exception as _slot_err:
                # Pool import or slot ops failed; not fatal for the clone.
                try:
                    from long_exposure import health_events as _he
                    _he.append_event(
                        "pool_clone_bootstrap_skipped",
                        detail=f"err={type(_slot_err).__name__}: {_slot_err}",
                    )
                except Exception:
                    pass

        _assignment_path = data_dir / "fanout_assignment.json"
        if _assignment_path.exists():
            try:
                _fa = json.loads(_assignment_path.read_text())
                task_override = _fa.get("assignment") or task_override
                print(
                    f"[long-exposure] Clone {_clone_k} of fork {_fork_id}: "
                    f"assignment loaded "
                    f"({len(task_override or '')} chars).",
                    flush=True,
                )
            except (OSError, json.JSONDecodeError) as _fa_err:
                print(
                    f"[long-exposure] Clone {_clone_k}: failed to load "
                    f"fanout_assignment.json: {_fa_err}",
                    flush=True,
                )
        else:
            print(
                f"[long-exposure] Clone {_clone_k}: "
                f"fanout_assignment.json missing; "
                f"running with score YAML task.",
                flush=True,
            )

    # Initialize sessions.db
    conn = init_db(Path(config["compact_db"]))

    loop_cfg = score.get("loop", {})
    max_cycles = loop_cfg.get("max_cycles")
    base_cooldown = loop_cfg.get("cycle_cooldown_seconds", 0)

    flow = score["flow"]  # list of agent name strings
    agents = score["agents"]

    # Load state once up-front so we can resolve `task` before using it.
    # Resolution priority: explicit override > saved state > score YAML.
    # On resume without an override, use the directive that was running
    # when the exploration was last saved, not the current YAML (which
    # may have drifted if the user edited the score file between stop
    # and resume).
    state = load_state(state_path)
    _saved_task = state.get("task") if state else None
    if task_override:
        task = task_override
        if _saved_task and _saved_task != task_override:
            print(
                f"[long-exposure] Directive changed on resume: "
                f"{_saved_task[:80]!r} -> {task_override[:80]!r}",
                flush=True,
            )
    elif _saved_task:
        task = _saved_task
    else:
        task = score["task"]
    score_inputs = {
        "directive": task,
        # Plan 1 cycle inputs — populated per-cycle in the loop, but seeded
        # here so build_agent_prompt's input-resolution never KeyErrors on
        # a fresh state file before the first per-cycle write.
        "plan_of_record": "[plan_of_record.md not yet read this cycle]",
        "promise_ledger_summary": "[promise_ledger.jsonl not yet read this cycle]",
    }
    report_interval = loop_cfg.get("report_interval", 3)
    reporter_def = agents.get("reporter")

    # Apply score-level tool restrictions (overrides config.yaml for all agents)
    if "allowed_tools" in score:
        config["allowed_tools"] = score["allowed_tools"]

    # Inject shared citations into agents that don't define their own
    shared_citations = score.get("citations", "")
    if shared_citations:
        for agent_def in agents.values():
            role = agent_def.get("role", "")
            if "<citations>" not in role:
                agent_def["role"] = role.rstrip() + "\n\n" + shared_citations.strip() + "\n"

    # Compaction config
    context_window = config.get("context_window", 1_000_000)
    compact_threshold = config.get("compact_threshold", 0.90)
    compact_at = int(context_window * compact_threshold)

    # Seed inputs
    seed = score.get("seed", {})
    if seed.get("starting_subtopic"):
        score_inputs["starting_subtopic"] = seed["starting_subtopic"]

    # Initialize or resume (state was loaded above for task resolution)
    if state:
        results = state["results"]
        cycle = state["cycle"]
        consecutive_failures = state.get("failures", {name: 0 for name in agents})
        # Normalize: ensure every currently-configured agent has a counter
        # even if a prior save wrote a partial dict. Protects against seeded
        # clone state (failures={}) and score edits that add a new agent not
        # present in an older state file. Without this, the failure branch's
        # `consecutive_failures[agent_name] += 1` KeyErrors on first failure.
        for _name in agents:
            consecutive_failures.setdefault(_name, 0)
        last_session_id = state.get("last_session_id")
        agent_sessions = state.get("agent_sessions", {})
        agent_summaries = state.get("agent_summaries", {})
        post_merge_pending = state.get("post_merge_pending", False)
        reanchor_emitted = dict(state.get("_reanchor_emitted") or {})
        agent_context_tokens = dict(state.get("agent_context_tokens") or {})
        # Stage 3 §5.3 (crash recovery): if a sync was in flight when the
        # process died, the persisted flag would otherwise stay True and
        # silently disable all future syncs. Always clear on resume; the
        # next interval check will reschedule normally.
        last_daily_sync_at = state.get("last_daily_sync_at")
        # Stage 3 §5.2 (backward compatibility): a state file written by a
        # pre-Stage-3 long-exposure has no last_daily_sync_at field, so the
        # `.get(...)` returns None. Without this initializer, _daily_sync_due
        # would fire on cycle 1 of resume (treating None as "never synced").
        # Seed to now so the first sync is interval-hours after resume, as
        # designed.
        if last_daily_sync_at is None:
            last_daily_sync_at = datetime.now(timezone.utc).isoformat()
        daily_sync_count = int(state.get("daily_sync_count") or 0)
        if state.get("_daily_sync_in_progress"):
            print(
                "[long-exposure] Resume: prior daily sync was interrupted; "
                "clearing in-progress flag.",
                flush=True,
            )
        # Provider/account-local session UUIDs only resolve under the provider
        # and account that created them. If either differs on resume, discard
        # native session ids — the next agent call will create fresh sessions
        # while restored summaries and workspace artifacts preserve continuity.
        _saved_provider = state.get("agent_sessions_provider")
        _cur_provider = _provider.current_provider()
        if (
            agent_sessions
            and (
                (_saved_provider is not None and _saved_provider != _cur_provider)
                or (_saved_provider is None and _cur_provider != _provider.CLAUDE)
            )
        ):
            print(
                f"[long-exposure] Resume: saved agent_sessions were from "
                f"{_saved_provider or 'legacy-claude'} but current provider is "
                f"{_cur_provider}; clearing sessions to avoid cross-provider "
                f"--resume failures.",
                flush=True,
            )
            agent_sessions = {}
        _saved_acct = state.get("agent_sessions_account")
        _cur_acct = _active_account_index()
        if agent_sessions and _saved_acct is not None and _saved_acct != _cur_acct:
            print(
                f"[long-exposure] Resume: saved agent_sessions were from "
                f"account #{_saved_acct} but current active is #{_cur_acct}; "
                f"clearing sessions to avoid --resume failures.",
                flush=True,
            )
            agent_sessions = {}
        print(f"[long-exposure] Resuming from cycle {cycle}", flush=True)
        for name, sid in agent_sessions.items():
            print(f"[long-exposure]   {name} session: {sid[:8]}...", flush=True)
    else:
        results = {
            "directive": task,
            "audit_report": (
                "[No prior audit — this is the first cycle. "
                "Choose the most foundational sub-topic to start.]"
            ),
        }
        cycle = 0
        consecutive_failures = {name: 0 for name in agents}
        last_session_id = None
        agent_sessions = {}
        agent_summaries = {}
        post_merge_pending = False
        reanchor_emitted = {}
        agent_context_tokens = {}
        # Stage 3: daily-sync state. Initialize last_daily_sync_at to "now"
        # so the first sync fires interval-hours after fresh start.
        last_daily_sync_at = datetime.now(timezone.utc).isoformat()
        daily_sync_count = 0
        print("[long-exposure] Starting fresh exploration", flush=True)

    # Keep results["directive"] in sync with the resolved task. Required
    # because build_agent_prompt prefers results[input_name] over
    # score_inputs[input_name], so on resume-with-override (or clone with
    # per-branch assignment) the parent's stale directive in results would
    # otherwise shadow the current one in agent prompts.
    results["directive"] = task

    # ---- Workspace bootstrap (Plan 1 + Plan 3) ----
    # Lay down the standard folder skeleton + plan_of_record.md +
    # STRUCTURE.md + a `_run/start` ledger event ON FRESH START ONLY.
    # Resumes (cycle > 1) and workspaces with an existing plan are no-ops
    # by design (docs/workspace-conventions.md). Clones inherit parent state
    # and skip bootstrap (the parent already ran it).
    workspace_root = paths.workspace_root(config.get("working_directory") or os.getcwd())
    config["working_directory"] = str(workspace_root)
    paths.ensure_layout(config)
    run_id = state.get("run_id") if state else None
    if not run_id:
        run_id = derive_run_id()
    telemetry.configure(config, data_dir, run_id)
    # The final auditor/reporter/curator read run_id from `results` (ledger
    # cycle counts and reconciliation uuid5 event-ids are keyed on it). It is
    # not an agent input, so it never reaches a prompt; persisting it inside
    # `results` also serves run_final_reporter.py, which restores results
    # from saved state.
    results["run_id"] = run_id
    telemetry.emit(
        "run_resume" if state else "run_start",
        phase="run",
        cycle=cycle,
        provider=config.get("llm_provider"),
        model=config.get("model"),
        status="ok",
        data={
            "score_path": str(score_path),
            "config_path": str(config_path) if config_path else None,
            "state_path": str(state_path),
            "output_dir": str(output_dir),
            "task_hash": telemetry.hash_value(task),
            "flow": flow,
            "is_clone": _is_clone(),
        },
    )
    if not _is_clone():
        try:
            _bs = bootstrap_workspace(
                workspace_root, task, run_id, cycle=max(1, cycle or 1)
            )
            if _bs["ran"]:
                print(
                    f"[long-exposure] Workspace bootstrapped: "
                    f"folders={_bs['folders_created']} "
                    f"plan={_bs['wrote_plan']} structure={_bs['wrote_structure']}",
                    flush=True,
                )
        except Exception as _bs_err:  # bootstrap is best-effort; never block the cycle
            print(f"[long-exposure] Bootstrap warning: {_bs_err}", flush=True)

    # Agent-teams residue sweep at startup. When the master switch is on
    # AND cleanup_residue is true, collect any orphaned tasks/<team>/
    # mailbox dirs left behind by prior crashed runs (the per-turn mtime
    # sweep cannot catch these — their mtime pre-dates any future turn's
    # t0). Safe here because no team subprocess is running yet.
    _team_defaults = (config.get("agent_teams_defaults") or {})
    if _team_defaults.get("enabled", False) and _team_defaults.get("cleanup_residue", True):
        _startup_removed = _sweep_team_tasks(since_ts=None)
        if _startup_removed:
            print(
                f"[long-exposure] Swept {_startup_removed} orphan tasks/ dir(s) "
                f"at startup",
                flush=True,
            )

    # ---- Interactive transport (opt-in) ----
    # When enabled, advanced features (multi-account pooling, parallel fan-out)
    # are intentionally deferred — interactive mode runs sequential cycles on
    # the active logged-in account. Tear the driver session down at exit.
    _interactive_mode = interactive_transport.is_enabled(config)
    if _interactive_mode:
        import atexit as _atexit
        _atexit.register(interactive_transport.shutdown)
        if not _is_clone() and (unified_pool.is_unified_active() or pool.is_active()):
            print(
                "[long-exposure] Interactive transport active: multi-account "
                "pooling is deferred and will be ignored this run.",
                flush=True,
            )

    # ---- Pool init (Stage 1) ----
    # Root only: initialize the pool ledger, pin the parent process to the
    # current primary via CLAUDE_FORCE_ACCOUNT, and reserve a slot for the
    # root agents (sequential calls share this one slot). Clones inherit
    # CLAUDE_FORCE_ACCOUNT from their fanout-spawn env and are already
    # accounted in the ledger by the fanout conductor.
    if not _is_clone() and not _interactive_mode and unified_pool.is_unified_active():
        try:
            unified_pool.init_all_pools()
            holder = unified_pool.acquire_slot(
                role="root",
                pid=os.getpid(),
                provider_preference=score.get("root_provider_preference"),
            )
            global _unified_root_holder
            _unified_root_holder = holder
            _pin_unified_holder_env(holder)
            import atexit as _atexit
            _atexit.register(_release_unified_root_holder)
            print(
                f"[long-exposure] Unified pool active: root pinned to "
                f"{holder.provider}/{Path(holder.account_dir).name}",
                flush=True,
            )
            print(f"[long-exposure] {unified_pool.format_unified_summary()}", flush=True)
        except Exception as _pool_err:
            print(f"[long-exposure] Unified pool init warning: {_pool_err}", flush=True)
    elif not _is_clone() and not _interactive_mode and pool.is_active():
        try:
            pool.init_pool()
            pool.thaw_eligible()
            primary = pool.primary_dir()
            if primary:
                os.environ[_provider.force_account_env()] = primary
                try:
                    pool.acquire_slot(role="root", pid=os.getpid())
                except pool.PoolExhausted as _ex:
                    # All accounts full — extremely unlikely at startup.
                    # Continue without an explicit root slot; pinning
                    # via CLAUDE_FORCE_ACCOUNT still works.
                    print(
                        f"[long-exposure] Pool: root could not acquire slot ({_ex}); "
                        "continuing pinned without ledger entry.",
                        flush=True,
                    )
                # Best-effort release on clean exit.
                import atexit as _atexit
                _atexit.register(pool.release_slot, os.getpid())
                print(
                    f"[long-exposure] Pool active: root pinned to {Path(primary).name}",
                    flush=True,
                )
                print(f"[long-exposure] {pool.format_pool_summary()}", flush=True)
        except Exception as _pool_err:  # pool is advisory — never block startup
            print(f"[long-exposure] Pool init warning: {_pool_err}", flush=True)
    telemetry.emit(
        "account_usage_snapshot",
        phase="provider",
        cycle=cycle,
        provider=config.get("llm_provider"),
        model=config.get("model"),
        status="ok",
        data={"accounts": telemetry.redact_account_usage(_snapshot_account_usage())},
    )

    total_failure_streak = 0
    cycles_since_last_report = 0
    cycle_session_log = []
    # Consecutive low-output cycles (relative threshold). Restored from state
    # so a stop/resume doesn't reset exhaustion progress (mirrors
    # _reanchor_emitted / agent_context_tokens handling above).
    low_output_streak = int(state.get("low_output_streak") or 0) if state else 0
    topic_exhausted = False  # set True when low-output streak or agent signal triggers closure
    max_cycles_reached = False
    # Low-output backstop is RELATIVE to the run's own peak cycle output, so it
    # self-calibrates to each branch's structured-output floor instead of a
    # fixed magic number. (A fixed 2000-tok floor failed: idle-but-verbose
    # clones produced ~2.4k tok/cycle and never tripped it — see the RCA.)
    # Persisted for the same reason as the streak: resetting peak to 0 on
    # resume re-inflates it from the next big cycle and defers closure.
    peak_cycle_output = int(state.get("peak_cycle_output") or 0) if state else 0
    # Usage-basis guard: peak/streak are calibrated in the transport's own
    # units — headless envelopes report full-turn output_tokens; interactive
    # turns report a chars/4 estimate of the final response only, far
    # smaller. Resuming across a transport (or provider) switch with the old
    # calibration would either trip a false topic-exhaustion within
    # LOW_OUTPUT_CLOSURE_COUNT cycles (headless -> interactive) or leave the
    # backstop far too lax (interactive -> headless). Reset and re-learn.
    _current_usage_basis = (
        "interactive" if _interactive_mode else _provider.current_provider()
    )
    _saved_usage_basis = state.get("usage_basis") if state else None
    _usage_basis_mismatch = (
        _saved_usage_basis != _current_usage_basis
        if _saved_usage_basis
        # Legacy state (no usage_basis key): written headless, or by a
        # pre-fix interactive build. Only the interactive side risks the
        # false-exhaustion failure, so reset there; headless resumes keep
        # their calibration (matching prior behavior).
        else _current_usage_basis == "interactive"
    )
    if _usage_basis_mismatch and (peak_cycle_output or low_output_streak):
        print(
            f"[long-exposure] Resume: exhaustion calibration was measured "
            f"on '{_saved_usage_basis or 'unknown'}' but this run reports "
            f"usage as '{_current_usage_basis}'; resetting "
            f"peak_cycle_output and low_output_streak.",
            flush=True,
        )
        peak_cycle_output = 0
        low_output_streak = 0
    # Passed to every in-loop save_state. None lets save_state stamp the
    # provider active at save time (headless basis).
    _usage_basis_arg = "interactive" if _interactive_mode else None
    LOW_OUTPUT_FRACTION = 0.05  # a cycle is "low" below 5% of peak observed output
    LOW_OUTPUT_ABS_FLOOR = 500  # absolute floor so early/degenerate cycles aren't mis-flagged
    LOW_OUTPUT_CLOSURE_COUNT = 2  # consecutive low-output cycles to trigger closure

    # Stale-signal sweep: a stop/clear/graceful-stop file written while
    # nothing was running would be consumed by the first loop-top
    # _check_signal_files and turn this run into a zero-cycle pass straight
    # into final synthesis. Guide files are NOT cleared — operator guidance
    # for the first cycle is legitimate.
    # Clones skip the sweep entirely: their instance dirs are freshly created
    # per fan-out, so stale signals from a previous session are impossible —
    # and the fan-out conductor may write a LIVE stop/graceful-stop signal
    # into a clone's dir while it is still booting; sweeping here would
    # delete that live signal.
    if not _is_clone():
        for _sig_name in _RUN_SIGNAL_FILENAMES:
            _sig = data_dir / _sig_name
            try:
                if _sig.exists():
                    _sig.unlink(missing_ok=True)
                    print(
                        f"[long-exposure] Cleared stale {_sig_name} signal "
                        f"from a previous session.",
                        flush=True,
                    )
            except OSError:
                pass

    # Banner
    print(f"[long-exposure] Task: {task.strip()[:120]}", flush=True)
    print(f"[long-exposure] Flow: {' -> '.join(flow)}", flush=True)
    print(f"[long-exposure] Max cycles: {max_cycles or 'unlimited'}", flush=True)
    print(f"[long-exposure] Compact at: {compact_at:,} tokens per agent", flush=True)
    if reporter_def:
        print(f"[long-exposure] Reporter: every {report_interval} cycles + on stop", flush=True)
    print(
        f"[long-exposure] Cooldown: {base_cooldown}s base "
        f"(adaptive — 2× on failure streaks; uniform across root, "
        f"post-merge, and clone cycles) | Ctrl+C to stop",
        flush=True,
    )
    print(f"[long-exposure] Signals: touch {data_dir}/exploration.stop|clear", flush=True)
    print("=" * 60, flush=True)

    # One-shot override: when set, skip the cycle loop entirely and go
    # straight to the final-reporter + curator exit path. Used to recover
    # from a crash that occurred after natural topic-exhaustion (the
    # final reporter never ran because of the bug at reporting.py:293).
    # Effect: no cycles burn, no periodic reporter sweep (counter is 0),
    # only the root final-synthesis path runs.
    _force_final_report = os.environ.get(
        "LE_FORCE_FINAL_REPORT", "",
    ).strip().lower() in ("1", "true", "yes")
    if _force_final_report:
        print(
            "[long-exposure] LE_FORCE_FINAL_REPORT set — skipping cycle "
            "loop, jumping straight to final reporter + curator.",
            flush=True,
        )
        topic_exhausted = True

    # --- Main loop ---
    while not _stop_requested and not _force_final_report:
        # Check for signal files at cycle boundary
        _check_signal_files(data_dir)
        if _stop_requested:
            break
        # Graceful-stop (Stage 1 §6.4): finish any in-flight cycle work, but
        # don't start a new one. This is what a clone receives when its
        # pinned overflow account rate-limits — the conductor signals it
        # rather than killing it.
        if _graceful_stop_requested:
            print(
                "[long-exposure] Graceful-stop honored; exiting cycle loop.",
                flush=True,
            )
            break

        # Pool maintenance (root only, cheap): reclaim orphan slots whose
        # PID is dead, and thaw cooling accounts whose cooldown has elapsed.
        # Both ops short-circuit when the pool is inactive. Interactive mode
        # never initialized the pool (init above is gated on
        # `not _interactive_mode`), so maintenance must skip too — it would
        # mutate a never-initialized ledger the transport ignores.
        if not _is_clone() and not _interactive_mode and unified_pool.is_unified_active():
            try:
                _swept, _thawed = unified_pool.heartbeat_and_thaw_all()
                if _swept or _thawed:
                    print(
                        f"[long-exposure] Unified pool maintenance: "
                        f"swept={_swept}, thawed={_thawed}",
                        flush=True,
                    )
            except Exception as _pool_err:
                print(f"[long-exposure] Unified pool maintenance skipped ({_pool_err})", flush=True)
        elif not _is_clone() and not _interactive_mode and pool.is_active():
            try:
                _swept = pool.heartbeat_sweep()
                _thawed = pool.thaw_eligible()
                if _swept:
                    print(
                        f"[long-exposure] Pool: swept {_swept} orphan slot(s)",
                        flush=True,
                    )
                if _thawed:
                    print(
                        f"[long-exposure] Pool: thawed {len(_thawed)} account(s)",
                        flush=True,
                    )
            except Exception as _pool_err:
                print(f"[long-exposure] Pool maintenance warning: {_pool_err}", flush=True)

        if max_cycles and cycle >= max_cycles:
            print(f"\n[long-exposure] Reached max cycles ({max_cycles}).", flush=True)
            max_cycles_reached = True
            break

        cycle += 1
        cycle_start = time.monotonic()
        cycle_topic = None  # set by researcher, inherited by worker/auditor
        cycle_ok = True
        cycle_output_tokens = 0  # track total output tokens for exhaustion detection
        cycle_forced_substantive = False  # post-merge/fan-out cycles never count as low-output
        agent_signaled_complete = False  # set when the auditor emits BRANCH_COMPLETE_SIGNAL

        # --- Post-merge mode ---
        # A cycle immediately after a fan-out runs worker-only (no researcher,
        # no auditor). The synthetic research_brief carries the merge content
        # so the worker has everything from one input. Flag is cleared at
        # cycle end only on cycle_ok, so a rate-limit rollback preserves the
        # post-merge framing for the retry.
        in_post_merge_cycle = post_merge_pending and not _is_clone()
        if in_post_merge_cycle:
            _fork_id_str, _k_branches = _extract_fork_metadata(
                results.get("audit_report", "")
            )
            results["research_brief"] = POST_MERGE_BRIEF_TEMPLATE.format(
                k=_k_branches or "K",
                fork_id=_fork_id_str,
                divergence_table=results.get("fanout_divergence_table", ""),
                merge=results.get("audit_report", "(merge content unavailable)"),
            )
            print(
                f"[long-exposure] Post-merge cycle {cycle} (fork "
                f"{_fork_id_str}, {_k_branches} branches): worker only.",
                flush=True,
            )
            flow_this_cycle = [a for a in flow if a == "worker"]
            if not flow_this_cycle:
                # Score has no worker in its flow — nothing to run. Clear the
                # flag and fall through; the normal flow (possibly researcher
                # only) will run instead. This is a score-config quirk, not
                # a runtime error.
                print(
                    "[long-exposure]   No 'worker' in flow — post-merge skip; "
                    "falling back to normal flow.",
                    flush=True,
                )
                post_merge_pending = False
                in_post_merge_cycle = False
                flow_this_cycle = flow
        else:
            flow_this_cycle = flow
        telemetry.emit(
            "cycle_start",
            phase="cycle",
            cycle=cycle,
            provider=config.get("llm_provider"),
            model=config.get("model"),
            status="started",
            data={
                "flow": flow_this_cycle,
                "post_merge_pending": post_merge_pending,
                "in_post_merge_cycle": in_post_merge_cycle,
                "is_clone": _is_clone(),
            },
        )

        # Check for live guidance from user
        guidance = _consume_guide_file(data_dir)

        # Inject fan-out guidance into live_guidance at root only, and only
        # when researcher is in the flow this cycle. Clones don't fan out
        # (parser short-circuits) and post-merge cycles skip the researcher,
        # so injection would be ~120 bytes of dead weight in both cases.
        # Use the dynamic (pool-aware) guidance so the researcher sees the
        # current branch cap; falls back to the legacy constant when the
        # pool is inactive.
        fanout_guide = (
            None
            if (_is_clone() or in_post_merge_cycle)
            else get_fanout_guidance()
        )

        # Sibling visibility: clones only, researcher-cycle only. Reads each
        # sibling's latest_report_pointer.md and formats a <sibling_reports>
        # block. Pointer-only (not content). Researcher role's
        # <sibling-awareness> block explains how to use them.
        sibling_block = (
            _collect_sibling_pointers(data_dir)
            if (_is_clone() and not in_post_merge_cycle)
            else None
        )
        anti_patterns_block = _build_anti_patterns_block(workspace_root, config)

        parts = [
            p for p in (fanout_guide, sibling_block, anti_patterns_block, guidance)
            if p
        ]
        base_live_guidance = (
            "\n\n".join(parts) if parts else "[No live guidance this cycle.]"
        )
        results["live_guidance"] = base_live_guidance
        if guidance:
            print(
                f"[long-exposure] Live guidance received ({len(guidance)} chars)",
                flush=True,
            )

        # ---- Inject plan + ledger summary as cycle inputs (Plan 1 §5) ----
        # Best-effort, token-bounded. Removing this block reverts the harness
        # to today's behaviour (graceful absence per Plan 1 principle #5).
        try:
            _plan_path = workspace_root / "plan_of_record.md"
            results["plan_of_record"] = (
                _plan_path.read_text() if _plan_path.exists()
                else "[No plan_of_record.md found in workspace.]"
            )
        except OSError as _e:
            results["plan_of_record"] = f"[Plan read error: {_e}]"
        try:
            results["promise_ledger_summary"] = summarize_ledger(workspace_root)
        except Exception as _e:  # never crash the cycle
            results["promise_ledger_summary"] = f"[Ledger summary error: {_e}]"

        print(f"\n{'='*60}", flush=True)
        _cycle_tag = " (post-merge)" if in_post_merge_cycle else ""
        print(f"[long-exposure] === Cycle {cycle}{_cycle_tag} ===", flush=True)

        # Cycle-level rotation retry. If any agent returns status="rate_limit",
        # rotate to the next account (clear sessions, restart cycle from the
        # top — researcher). Once every account has been
        # tried within this cycle, fall through with cycle_ok=False so the
        # existing failure-streak / adaptive_cooldown path handles the wait —
        # no new timer, no reset-time tracking.
        #
        # rotation_attempts is an in-memory counter, so the loop terminates
        # in exactly len(accounts) iterations regardless of account-state-file
        # write health.
        rotation_attempts = 0
        # Snapshot pre-cycle state so a rotation-restart truly starts from a
        # clean slate. sessions.db rows written by any partially-completed
        # attempt become orphan-but-harmless — parent chain re-anchors on
        # restored last_session_id.
        _pre_cycle_results = dict(results)
        _pre_cycle_last_session_id = last_session_id
        _pre_cycle_consecutive_failures = dict(consecutive_failures)
        _pre_cycle_reanchor_emitted = dict(reanchor_emitted)
        _pre_cycle_agent_context_tokens = dict(agent_context_tokens)
        while True:
            # Capture the account the flow is about to run under. If we
            # hit a 429, we pass this to rotate_to_next_account so peer
            # clones that already rotated past this account don't cause
            # us to burn a second rotation on top of theirs.
            stale_acct_idx = _active_account_index()
            rotation_triggered = False
            rate_limit_reason: str | None = None

            for i, agent_name in enumerate(flow_this_cycle):
                if _stop_requested:
                    break

                # Check signals between agents
                _check_signal_files(data_dir)
                if _stop_requested:
                    break

                agent_def = agents[agent_name]
                agent_live_parts: list[str] = []
                reanchor_at = int(context_window * compact_threshold * 5 / 6)
                if (
                    config.get("reanchor_enabled", True)
                    and int(agent_context_tokens.get(agent_name, 0) or 0) >= reanchor_at
                    and not reanchor_emitted.get(agent_name)
                ):
                    agent_config_for_reanchor = build_agent_config(config, agent_def)
                    if agent_config_for_reanchor.get("philosophy") == "custom":
                        phil_vars = dict(agent_config_for_reanchor.get("custom_philosophy", {}))
                        for key, val in PHILOSOPHY_PRESETS["efficient"].items():
                            phil_vars.setdefault(key, val)
                    else:
                        phil_vars = dict(
                            PHILOSOPHY_PRESETS.get(
                                agent_config_for_reanchor.get("philosophy", "efficient"),
                                PHILOSOPHY_PRESETS["efficient"],
                            )
                        )
                    reanchor = _build_reanchor_block(agent_def, phil_vars)
                    if reanchor:
                        agent_live_parts.append(reanchor)
                        reanchor_emitted[agent_name] = True
                        print(
                            f"[long-exposure]   {agent_name}: injecting "
                            "long-context re-anchor",
                            flush=True,
                        )
                if parts:
                    agent_live_parts.append(base_live_guidance)
                results["live_guidance"] = (
                    "\n\n".join(agent_live_parts)
                    if agent_live_parts
                    else "[No live guidance this cycle.]"
                )
                # Interactive transport never resumes provider sessions
                # (was_resume is forced False in _call_exploration_agent), so
                # a "Resuming" banner would be misleading there.
                is_resume = (
                    agent_name in agent_sessions and not _interactive_mode
                )
                print(
                    f"[long-exposure] "
                    f"{'Resuming' if is_resume else 'Starting'}: "
                    f"{agent_name}"
                    f"{' (' + agent_sessions[agent_name][:8] + '...)' if is_resume else ''}",
                    flush=True,
                )

                result = _call_exploration_agent(
                    agent_name=agent_name,
                    agent_def=agent_def,
                    task=task,
                    config=config,
                    results=results,
                    score_inputs=score_inputs,
                    agent_sessions=agent_sessions,
                    agent_summaries=agent_summaries,
                )
                telemetry.emit_agent_result(
                    agent_name,
                    result,
                    cycle=cycle,
                    provider=config.get("llm_provider"),
                    model=config.get("model"),
                    context_window=context_window,
                )

                if result["status"] == "rate_limit":
                    rotation_triggered = True
                    rate_limit_reason = result.get("error") or "rate limit"
                    print(
                        f"[long-exposure]   {agent_name}: RATE LIMIT — "
                        f"{rate_limit_reason[:200]}",
                        flush=True,
                    )
                    break  # exit agent flow; rotation handler below takes over

                usage = result.get("usage", {})
                dur = result.get("duration_ms", 0) / 1000

                if result["status"] == "ok":
                    results.update(result["outputs"])
                    consecutive_failures[agent_name] = 0

                    # Item 1: explicit agent-driven termination. The auditor is
                    # the closure authority; when it emits BRANCH_COMPLETE_SIGNAL
                    # the branch/topic is fully explored. Scope the check to the
                    # auditor's FRESH output this cycle (results persists across
                    # cycles; a later agent could echo the token). Line-anchored
                    # (BRANCH_COMPLETE_RE): an auditor merely DISCUSSING the
                    # token inline must not end the run.
                    if agent_name == "auditor" and any(
                        BRANCH_COMPLETE_RE.search(str(v))
                        for v in result["outputs"].values()
                    ):
                        agent_signaled_complete = True

                    total_ctx = _total_context_tokens(usage)
                    agent_context_tokens[agent_name] = total_ctx
                    out_tokens = usage.get("output_tokens", 0)
                    cycle_output_tokens += out_tokens
                    print(
                        f"[long-exposure]   {agent_name}: ok "
                        f"({dur:.1f}s, ctx:{total_ctx:,}tok, "
                        f"out:{out_tokens}tok)",
                        flush=True,
                    )

                    # Observability: team activity, when on for this agent.
                    team_stats = result.get("team_stats")
                    if team_stats is not None:
                        print(
                            f"[long-exposure]   {agent_name} team: "
                            f"teammates={team_stats['teammates']} "
                            f"wall={dur:.1f}s",
                            flush=True,
                        )

                    # Store output in sessions.db
                    output_text = "\n\n".join(result["outputs"].values())

                    # Extract topic from researcher; propagate downstream
                    extracted = _extract_topic(output_text)
                    if extracted:
                        cycle_topic = extracted
                    last_session_id = _store_agent_output(
                        conn, agent_name, agent_def, output_text,
                        cycle, last_session_id,
                        current_topic=cycle_topic,
                    )
                    cycle_session_log.append({
                        "cycle": cycle, "agent": agent_name,
                        "session_id": last_session_id,
                    })

                    # --- Auto-compact check ---
                    if total_ctx >= compact_at:
                        print(
                            f"[long-exposure]   {agent_name} context "
                            f"{total_ctx:,}/{context_window:,} "
                            f"({total_ctx/context_window:.0%}) — compacting...",
                            flush=True,
                        )
                        # Pop reanchor/context bookkeeping only if compaction
                        # actually rotated the session (entry removed; fresh
                        # UUID minted on next call). The DB-write-failure path
                        # keeps the old near-full session — there the next
                        # reanchor/compaction attempt must stay armed.
                        _session_before_compact = agent_sessions.get(agent_name)
                        try:
                            last_session_id = _compact_agent_session(
                                agent_name, agent_def, config,
                                agent_sessions, agent_summaries,
                                conn, cycle, last_session_id,
                            )
                            if agent_sessions.get(agent_name) != _session_before_compact:
                                reanchor_emitted.pop(agent_name, None)
                                agent_context_tokens.pop(agent_name, None)
                        except ClaudeRateLimitError as e:
                            rotation_triggered = True
                            rate_limit_reason = f"compaction: {e}"
                            print(
                                f"[long-exposure]   compaction: RATE LIMIT — "
                                f"{str(e)[:200]}",
                                flush=True,
                            )
                            break

                    # --- Fan-out trigger (researcher only, root only) ---
                    # Parser no-ops for clones; the _is_clone() short-circuit
                    # here avoids the extra regex work in the common case.
                    # Interactive transport defers parallel fan-out: clones would
                    # each need their own interactive session. Run sequentially.
                    if (agent_name == "researcher" and not _is_clone()
                            and not _interactive_mode):
                        _branches = _parse_fanout_block(
                            results.get("research_brief", "")
                        )
                        if _branches:
                            try:
                                from long_exposure import branchial_budget
                                _annotations = branchial_budget.score_branches(
                                    _branches,
                                    db_path=config.get("compact_db"),
                                )
                                for _br, _ann in zip(_branches, _annotations):
                                    _br["branchial_budget"] = _ann
                                _summary = ", ".join(
                                    f"c{_i}={_ann.get('novelty_class', 'unknown')}"
                                    for _i, _ann in enumerate(_annotations)
                                )
                                print(
                                    f"[long-exposure] branchial-budget: {_summary}",
                                    flush=True,
                                )
                            except Exception as _bb_err:
                                print(
                                    "[long-exposure] branchial-budget skipped: "
                                    f"{_bb_err!r}",
                                    flush=True,
                                )
                            _fanout = _run_fanout_conductor(
                                branches=_branches,
                                score_path=score_path,
                                config_path=config_path,
                                root_instance_dir=(
                                    instance_dir
                                    if instance_dir is not None
                                    else data_dir
                                ),
                                data_dir=data_dir,
                                task=task,
                                parent_results=results,
                                parent_agent_sessions=agent_sessions,
                                parent_agent_summaries=agent_summaries,
                                working_directory=config.get("working_directory"),
                                parent_run_id=run_id,
                                # Stage 2: pass reporter_def + config so the
                                # conductor can run the merge synthesis at
                                # the barrier when fan-out width >= threshold.
                                reporter_def=reporter_def,
                                config=config,
                                # Stage 9: pass the score's loop config so
                                # the conductor can read graceful-preemption
                                # tunables (min_clone_cycles_before_preempt,
                                # barrier_preempt_timeout_seconds).
                                loop_cfg=loop_cfg,
                            )
                            telemetry.emit(
                                "fanout_end",
                                phase="fanout",
                                cycle=cycle,
                                provider=config.get("llm_provider"),
                                model=config.get("model"),
                                status="ok",
                                data={
                                    "fork_id": _fanout.get("fork_id"),
                                    "branches": len(_branches),
                                    "outcomes": [
                                        {
                                            "clone_k": o.get("clone_k"),
                                            "state": o.get("state"),
                                            "deliverable_status": o.get("deliverable_status"),
                                        }
                                        for o in (_fanout.get("outcomes") or [])
                                    ],
                                },
                            )
                            # Collapse: the aggregated merge becomes the next
                            # cycle's audit_report input for the researcher.
                            results["audit_report"] = _fanout["aggregated_report"]
                            results["fanout_divergence_table"] = (
                                _fanout.get("divergence_table") or ""
                            )
                            results["work_output"] = (
                                f"[fan-out collapsed: fork "
                                f"{_fanout['fork_id']} with "
                                f"{len(_branches)} branches — see audit_report "
                                f"for aggregated merge]"
                            )
                            # Credit as a non-low-output cycle so exhaustion
                            # detector does not mistake fan-out cycles for
                            # null work.
                            cycle_forced_substantive = True
                            # Arm post-merge mode for the NEXT cycle: worker
                            # only, reading the merge as its research_brief.
                            # Researcher and auditor sessions stay at their
                            # current state and only resume in cycle N+2, when
                            # they have fresh context (integration output) to
                            # work from — avoids the "one cycle stale" issue.
                            post_merge_pending = True
                            # Skip the remaining flow (worker, auditor) — the
                            # fan-out substitutes for them in this cycle.
                            print(
                                "[long-exposure] Fan-out replaces worker/auditor "
                                "for this cycle; post-merge worker-only "
                                "scheduled for next cycle.",
                                flush=True,
                            )
                            break
                else:
                    # --- Failure handling ---
                    consecutive_failures[agent_name] += 1
                    cycle_ok = False
                    err = result.get("error", "unknown")
                    print(
                        f"[long-exposure]   {agent_name}: FAILED — {err}",
                        flush=True,
                    )
                    print(
                        f"[long-exposure]   (consecutive: "
                        f"{consecutive_failures[agent_name]})",
                        flush=True,
                    )

                    if i == 0:
                        # Research failed — skip entire cycle
                        print(
                            "[long-exposure]   Skipping rest of cycle.",
                            flush=True,
                        )
                        break
                    elif i == len(flow_this_cycle) - 1:
                        # Audit failed — use fallback and store it
                        results["audit_report"] = FALLBACK_AUDIT
                        last_session_id = _store_agent_output(
                            conn, agent_name, agent_def, FALLBACK_AUDIT,
                            cycle, last_session_id,
                        )
                        cycle_session_log.append({
                            "cycle": cycle, "agent": agent_name,
                            "session_id": last_session_id,
                        })
                    else:
                        # Middle agent failed — pass failure marker
                        for out_name in agent_def.get("outputs", []):
                            results[out_name] = (
                                f"[AGENT FAILED: {agent_name}] {err}\n\n"
                                f"The {agent_name} agent was unable to "
                                f"produce output this cycle."
                            )

                    # Brief pause on 3 consecutive failures for this agent.
                    if consecutive_failures[agent_name] >= 3:
                        print(
                            f"\n[long-exposure] WARNING: {agent_name} "
                            f"failed {consecutive_failures[agent_name]}x. "
                            f"Pausing 10s.",
                            flush=True,
                        )
                        _sleep_interruptible(10, data_dir)

            # --- Rotation handling ---
            if not rotation_triggered:
                break  # cycle body completed (success or agent-level failure)

            if _stop_requested:
                # Honor stop signal over rotation retry.
                cycle_ok = False
                break

            rotation_attempts += 1

            # Stage 1: pool-aware rotation takes precedence when the pool is
            # configured. The legacy is_forced short-circuit that used to
            # live at the top of this block silently bypassed the pool —
            # CLAUDE_FORCE_ACCOUNT is set by the pool itself (root pinned
            # to primary, clones pinned to assigned dirs), so checking
            # is_forced first meant the pool branches never ran. The
            # regression (clones inherit parent's primary, all
            # land on acct4, retry indefinitely) was caused by exactly that
            # ordering. Pool path first; legacy is the fallback only.
            if unified_pool.is_unified_active() and not _is_clone():
                old_provider = _provider.current_provider()
                preference = [
                    prv for prv in unified_pool.SUPPORTED_PROVIDERS_FOR_POOL
                    if prv != old_provider
                ] + [old_provider]
                holder = _rotate_unified_root_after_rate_limit(preference)
                if holder:
                    print(
                        f"[long-exposure] Unified pool: {old_provider} "
                        f"rate-limited; promoted to "
                        f"{holder.provider}/{Path(holder.account_dir).name}. "
                        f"Clearing sessions and restarting cycle from top.",
                        flush=True,
                    )
                    print(f"[long-exposure] {unified_pool.format_unified_summary()}", flush=True)
                else:
                    print(
                        "[long-exposure] Unified pool: all accounts cooling; "
                        f"falling back to adaptive cooldown ({rate_limit_reason})",
                        flush=True,
                    )
                    cycle_ok = False
                    break
            elif pool.is_active():
                if _is_clone():
                    # §6.2: a pinned clone whose account rate-limits cannot
                    # rotate. Mark cooling, release slot, exit cleanly so
                    # the parent's barrier observes a clean failure and
                    # records the branch as failed. Next cycle's researcher
                    # sees the failure and decides whether to retry the
                    # branch.
                    pinned = os.environ.get(_provider.force_account_env(), "").strip()
                    pinned_label = Path(pinned).name if pinned else "(unknown)"
                    if pinned:
                        pool.mark_rate_limited(pinned)
                    # Release by (fork_id, clone_k) — robust to PID changes
                    # (the slot was acquired by the parent before Popen and
                    # re-tagged via update_slot_pid post-Popen; releasing by
                    # branch survives even if that re-tag failed).
                    fork_id = _get_fork_id()
                    clone_k = _get_clone_k()
                    if fork_id is not None and clone_k is not None:
                        pool.release_slot_by_branch(fork_id, clone_k)
                    else:
                        # Defensive: should never happen for a clone process.
                        pool.release_slot(os.getpid())
                    print(
                        f"[long-exposure] Clone: pinned account {pinned_label} "
                        f"rate-limited; marking cooling + exiting cycle.",
                        flush=True,
                    )
                    cycle_ok = False
                    _stop_requested = True  # exit the cycle loop cleanly
                    break
                # Root path: mark old primary cooling, promote the freshest
                # available account, hot-swap CLAUDE_FORCE_ACCOUNT.
                force_env = _provider.force_account_env()
                old_primary = os.environ.get(force_env, "").strip()
                old_label = Path(old_primary).name if old_primary else "(unknown)"
                if old_primary:
                    pool.mark_rate_limited(old_primary)
                new_primary = pool.promote_fresh()
                if new_primary:
                    os.environ[force_env] = new_primary
                    print(
                        f"[long-exposure] Pool: primary {old_label} "
                        f"rate-limited; promoted to {Path(new_primary).name}. "
                        f"Clearing sessions and restarting cycle from top.",
                        flush=True,
                    )
                    print(f"[long-exposure] {pool.format_pool_summary()}", flush=True)
                else:
                    print(
                        "[long-exposure] Pool: all accounts cooling; falling "
                        f"back to adaptive cooldown ({rate_limit_reason})",
                        flush=True,
                    )
                    cycle_ok = False
                    break
            else:
                # Legacy rotation (no pool active): rotate via the global
                # ~/.claude-accounts-state.json active_index. Single-account
                # or debug-pinned (CLAUDE_FORCE_ACCOUNT alone) modes hit
                # this path and surface "all accounts rate-limited" so the
                # outer adaptive_cooldown takes over.
                accounts = _parse_accounts()
                is_forced, _pin = _resolve_force_account(accounts)
                if (is_forced
                        or len(accounts) <= 1
                        or rotation_attempts >= len(accounts)):
                    print(
                        f"[long-exposure] All Claude accounts rate-limited this "
                        f"cycle; falling back to adaptive cooldown "
                        f"({rate_limit_reason})",
                        flush=True,
                    )
                    cycle_ok = False
                    break
                prev, new, new_dir = rotate_to_next_account(
                    stale_index=stale_acct_idx,
                )
                label = new_dir or "(default)"
                if prev == stale_acct_idx and new != stale_acct_idx:
                    print(
                        f"[long-exposure] Rate limit on account #{prev}; rotated to "
                        f"#{new} ({label}). Clearing sessions and restarting cycle "
                        f"from top.",
                        flush=True,
                    )
                else:
                    # Peer already rotated past our stale account.
                    print(
                        f"[long-exposure] Rate limit on account #{stale_acct_idx}; "
                        f"peer already rotated to #{new} ({label}). "
                        f"Clearing sessions and restarting cycle from top.",
                        flush=True,
                    )
            # Fresh session UUIDs on the new account. Gems (sessions.db),
            # workspace files, tool permissions, and restored-context
            # summaries are shared state and transfer automatically.
            agent_sessions.clear()
            # Roll back per-cycle accumulators so the retry truly starts
            # from scratch (first agent in flow).
            cycle_topic = None
            cycle_output_tokens = 0
            cycle_forced_substantive = False
            agent_signaled_complete = False
            cycle_ok = True
            # Restore pre-cycle snapshots: `results` mutations from the
            # abandoned attempt, the `last_session_id` parent pointer, and
            # per-agent failure streaks. Without this the retry would see
            # stale upstream outputs and the sessions.db chain would anchor
            # on an abandoned row.
            results.clear()
            results.update(_pre_cycle_results)
            last_session_id = _pre_cycle_last_session_id
            consecutive_failures.clear()
            consecutive_failures.update(_pre_cycle_consecutive_failures)
            reanchor_emitted.clear()
            reanchor_emitted.update(_pre_cycle_reanchor_emitted)
            agent_context_tokens.clear()
            agent_context_tokens.update(_pre_cycle_agent_context_tokens)
            # Discard any partial session log entries from this attempt.
            cycle_session_log = [
                entry for entry in cycle_session_log
                if entry.get("cycle") != cycle
            ]

        # --- End of cycle bookkeeping ---
        cycles_since_last_report += 1

        if _stop_requested:
            break

        if cycle_ok:
            total_failure_streak = 0
        else:
            total_failure_streak += 1

        # Brief pause on 3 consecutive all-failure cycles
        # (adaptive cooldown at cycle end handles the real backoff)
        if total_failure_streak >= 3:
            print(
                f"\n[long-exposure] WARNING: {total_failure_streak} consecutive "
                f"cycles with failures. Pausing 10s.",
                flush=True,
            )
            _sleep_interruptible(10, data_dir)

        # --- Exhaustion detection ---
        # Only count cycles where agents succeeded but produced little output.
        # Failed cycles (rate limits, timeouts) are NOT exhaustion — they are
        # handled by the failure streak / adaptive cooldown logic above.
        # Post-merge cycles credit themselves as meaningful work regardless
        # of worker output size — integration is substantive even when its
        # token output is small (same stance as fan-out cycles).
        if cycle_ok and in_post_merge_cycle:
            cycle_forced_substantive = True
        if cycle_ok:
            # Calibrate the low-output floor to the run's own peak output.
            peak_cycle_output = max(peak_cycle_output, cycle_output_tokens)
            low_threshold = max(
                LOW_OUTPUT_ABS_FLOOR,
                int(LOW_OUTPUT_FRACTION * peak_cycle_output),
            )
            if not cycle_forced_substantive and cycle_output_tokens < low_threshold:
                low_output_streak += 1
                print(
                    f"[long-exposure] Low output: {cycle_output_tokens} tokens "
                    f"(< {low_threshold}; streak: {low_output_streak}/"
                    f"{LOW_OUTPUT_CLOSURE_COUNT})",
                    flush=True,
                )
            else:
                low_output_streak = 0

        if cycle_ok:
            # Post-merge completion: promote the worker's integration output
            # into audit_report so the next cycle's researcher sees a clean
            # summary of what happened (the raw merge is also in sessions.db
            # and can be re-searched if needed). Clear the flag so this is
            # a one-shot; rate-limited retries leave it set. Exhaustion
            # credit is applied earlier (before the low-output check).
            if in_post_merge_cycle:
                results["audit_report"] = (
                    "[POST-MERGE INTEGRATION COMPLETE]\n\n"
                    + results.get("work_output", "(no work output)")
                )
                post_merge_pending = False
                print(
                    "[long-exposure] Post-merge cycle complete; audit_report "
                    "promoted from integration output.",
                    flush=True,
                )

        # Status file + state
        update_status_file(output_dir, cycle, "running", consecutive_failures)
        save_state(state_path, cycle, results, consecutive_failures,
                   last_session_id, agent_sessions, agent_summaries,
                   post_merge_pending=post_merge_pending, task=task,
                   run_id=run_id,
                   last_daily_sync_at=last_daily_sync_at,
                   daily_sync_count=daily_sync_count,
                   reanchor_emitted=reanchor_emitted,
                   agent_context_tokens=agent_context_tokens,
                   peak_cycle_output=peak_cycle_output,
                   low_output_streak=low_output_streak,
                   usage_basis=_usage_basis_arg)

        elapsed = time.monotonic() - cycle_start
        telemetry.emit(
            "cycle_end",
            phase="cycle",
            cycle=cycle,
            provider=config.get("llm_provider"),
            model=config.get("model"),
            status="ok" if cycle_ok else "error",
            data={
                "duration_seconds": round(elapsed, 3),
                "cycle_output_tokens": cycle_output_tokens,
                "low_output_streak": low_output_streak,
                "total_failure_streak": total_failure_streak,
                "post_merge_pending": post_merge_pending,
                "failures": consecutive_failures,
            },
        )
        print(f"[long-exposure] Cycle {cycle} done ({elapsed:.0f}s)", flush=True)

        # ---- Stage 3: Daily sync trigger ----
        # Root-only, after fan-out has fully collapsed (post_merge_pending
        # gates this — we never sync mid-fan-out). Failure isolation:
        # daily-sync agent crashes do NOT increment cycle's failure counter.
        _daily_interval = float(loop_cfg.get("daily_sync_interval_hours", 24))
        if (not _is_clone()
                and not post_merge_pending
                and not _stop_requested
                and _daily_sync_due(last_daily_sync_at, _daily_interval)):
            # Persist in-progress flag so a crash mid-sync is recoverable
            # (load_state clears the flag on resume; see §5.3).
            save_state(state_path, cycle, results, consecutive_failures,
                       last_session_id, agent_sessions, agent_summaries,
                       post_merge_pending=post_merge_pending, task=task,
                       run_id=run_id,
                       last_daily_sync_at=last_daily_sync_at,
                       daily_sync_count=daily_sync_count,
                       daily_sync_in_progress=True,
                       reanchor_emitted=reanchor_emitted,
                       agent_context_tokens=agent_context_tokens,
                       peak_cycle_output=peak_cycle_output,
                       low_output_streak=low_output_streak,
                       usage_basis=_usage_basis_arg)
            try:
                last_session_id = _run_daily_sync(
                    agents=agents,
                    task=task,
                    config=config,
                    results=results,
                    score_inputs=score_inputs,
                    conn=conn,
                    cycle=cycle,
                    last_session_id=last_session_id,
                    context_window=context_window,
                    compact_at=compact_at,
                    data_dir=data_dir,
                    agent_sessions=agent_sessions,
                    agent_summaries=agent_summaries,
                )
            finally:
                # Always advance — failure mode is "next sync 24h later",
                # never "retry every cycle until success."
                last_daily_sync_at = datetime.now(timezone.utc).isoformat()
                daily_sync_count += 1
                save_state(state_path, cycle, results, consecutive_failures,
                           last_session_id, agent_sessions, agent_summaries,
                           post_merge_pending=post_merge_pending, task=task,
                           run_id=run_id,
                           last_daily_sync_at=last_daily_sync_at,
                           daily_sync_count=daily_sync_count,
                           daily_sync_in_progress=False,
                           reanchor_emitted=reanchor_emitted,
                           agent_context_tokens=agent_context_tokens,
                           peak_cycle_output=peak_cycle_output,
                           low_output_streak=low_output_streak,
                           usage_basis=_usage_basis_arg)

                # Plan B: planned 24h rotation. Fires AFTER the
                # daily-sync agents complete, but only when no rotation has
                # happened in the last 24h. Pre-emptively spreads usage on
                # multi-account pools that may not hit a rate-limit within
                # the window. Gates mirror the daily-sync block above plus
                # `pool.is_active()`.
                #
                # The rotation must propagate via TWO state changes (mirrors
                # the rate-limit rotation path at lines ~2954–2967, ~3017):
                #   1. `os.environ["CLAUDE_FORCE_ACCOUNT"] = new_primary` so
                #      the parent's next API call lands on the new primary
                #      (without this, _active_account_dir() reads the env
                #      var first and stays pinned to the old primary).
                #   2. `agent_sessions.clear()` so the next cycle's agents
                #      get fresh UUIDs on the new account (Claude session
                #      IDs are per-account; resuming an old UUID on a new
                #      account fails with "session not found").
                # Without either step, the rotation is observable in pool
                # state but invisible to the running agents.
                # Interactive mode skips this too: the pool was never
                # initialized there, and the transport ignores the
                # CLAUDE_FORCE_ACCOUNT repin — the rotation would only
                # mutate ledger state and clear sessions for nothing.
                if (pool.is_active()
                        and not _is_clone()
                        and not _interactive_mode
                        and not post_merge_pending
                        and not _stop_requested):
                    _planned_rotation_threshold = float(
                        loop_cfg.get(
                            "planned_rotation_min_age_hours",
                            _daily_interval,
                        )
                    )
                    _age_h = pool.last_rotation_age_hours()
                    if _age_h is None or _age_h >= _planned_rotation_threshold:
                        _old_primary = pool.primary_dir()
                        _new_primary = pool.promote_fresh()
                        if _new_primary:
                            # (1) Hot-swap env var so the new primary actually
                            #     receives the next API call.
                            os.environ[_provider.force_account_env()] = _new_primary
                            # (2) Clear cycle-agent session UUIDs (stale on
                            #     the new account). Compaction summaries in
                            #     agent_summaries stay; sessions.db gems are
                            #     account-portable and stay.
                            agent_sessions.clear()
                            _age_str = (
                                f"{_age_h:.1f}h"
                                if _age_h is not None else "first"
                            )
                            _old_label = (
                                Path(_old_primary).name
                                if _old_primary else "(none)"
                            )
                            _new_label = Path(_new_primary).name
                            print(
                                f"[long-exposure] Planned rotation: "
                                f"{_old_label} -> {_new_label} "
                                f"(no rotations in last {_age_str})",
                                flush=True,
                            )
                            try:
                                from long_exposure import health_events as _he
                                _he.append_event(
                                    "planned_rotation",
                                    detail=(
                                        f"old={_old_primary} "
                                        f"new={_new_primary} "
                                        f"age_h={_age_h}"
                                    ),
                                )
                            except Exception:
                                pass
                        else:
                            try:
                                from long_exposure import health_events as _he
                                _he.append_event(
                                    "planned_rotation_skipped",
                                    detail="all accounts cooling",
                                )
                            except Exception:
                                pass

        # --- Topic exhaustion: explicit agent signal (primary) or N
        # consecutive low-output cycles (backstop). Both funnel into the same
        # exit so the proven clone merge-handoff path is reused. ---
        if agent_signaled_complete or low_output_streak >= LOW_OUTPUT_CLOSURE_COUNT:
            reason = (
                "auditor signaled branch complete"
                if agent_signaled_complete
                else f"{low_output_streak} consecutive low-output cycles"
            )
            print(
                f"\n[long-exposure] Topic exhausted: {reason}.",
                flush=True,
            )
            topic_exhausted = True
            break

        # --- Reporter check (every report_interval cycles) ---
        if (reporter_def and cycles_since_last_report >= report_interval
                and not _stop_requested):
            range_start = cycle - cycles_since_last_report + 1
            last_session_id = _run_reporter(
                reporter_def, task, config, results, score_inputs,
                agent_sessions, agent_summaries,
                conn, cycle, last_session_id,
                range_start, cycle,
                cycle_session_log,
                context_window, compact_at,
            )
            cycles_since_last_report = 0
            cycle_session_log = []
            save_state(state_path, cycle, results, consecutive_failures,
                       last_session_id, agent_sessions, agent_summaries,
                       post_merge_pending=post_merge_pending, task=task,
                       run_id=run_id,
                       last_daily_sync_at=last_daily_sync_at,
                       daily_sync_count=daily_sync_count,
                       reanchor_emitted=reanchor_emitted,
                       agent_context_tokens=agent_context_tokens,
                       peak_cycle_output=peak_cycle_output,
                       low_output_streak=low_output_streak,
                       usage_basis=_usage_basis_arg)

        # Cooldown
        cooldown = adaptive_cooldown(base_cooldown, total_failure_streak)
        if cooldown > 0:
            print(f"[long-exposure] Cooldown: {cooldown}s", flush=True)
            _sleep_interruptible(cooldown, data_dir)

    # --- Stopped or Cleared ---
    if _clear_requested:
        # Archive old state before clearing
        _archive_state(state_path)
        _archive_local_session_logs(state_path.parent)
        # Clear: save empty state (sessions.db records preserved). Stamp
        # last_daily_sync_at to now so a subsequent resume from this
        # cleared state doesn't fire the daily sync on cycle 1 (with
        # last_daily_sync_at=None, _daily_sync_due returns True).
        save_state(state_path, 0, {}, {name: 0 for name in agents},
                   None, {}, {},
                   last_daily_sync_at=datetime.now(timezone.utc).isoformat(),
                   daily_sync_count=0,
                   daily_sync_in_progress=False)
        update_status_file(output_dir, cycle, "cleared", consecutive_failures)
        telemetry.emit(
            "run_end",
            phase="run",
            cycle=cycle,
            provider=config.get("llm_provider"),
            model=config.get("model"),
            status="cleared",
            data={
                "topic_exhausted": topic_exhausted,
                "stop_requested": _stop_requested,
                "clear_requested": _clear_requested,
                "failures": consecutive_failures,
            },
        )
        print(f"\n[long-exposure] Cleared after {cycle} cycles.", flush=True)
        print("[long-exposure] Context reset. Sessions.db history preserved.", flush=True)
        # Clone robustness: the root conductor's barrier is polling for
        # merge_report.md. Even on clear, write a placeholder so the barrier
        # does not block indefinitely on a cleared clone.
        if _is_clone():
            try:
                _write_merge_report(
                    _merge_report_path(state_path.parent),
                    f"# Merge Report (clone cleared)\n\n"
                    f"Clone was cleared after {cycle} cycles.\n",
                    config,
                    f"cycles 1-{cycle} [cleared]",
                    verdict="halted",
                )
            except OSError:
                pass
    else:
        # Run standard report if there are unreported cycles
        if reporter_def and cycles_since_last_report > 0:
            range_start = cycle - cycles_since_last_report + 1
            last_session_id = _run_reporter(
                reporter_def, task, config, results, score_inputs,
                agent_sessions, agent_summaries,
                conn, cycle, last_session_id,
                range_start, cycle,
                cycle_session_log,
                context_window, compact_at,
            )

        # Save state after standard reporter, before final reporter
        save_state(state_path, cycle, results, consecutive_failures,
                   last_session_id, agent_sessions, agent_summaries,
                   post_merge_pending=post_merge_pending, task=task,
                   run_id=run_id,
                   last_daily_sync_at=last_daily_sync_at,
                   daily_sync_count=daily_sync_count,
                   reanchor_emitted=reanchor_emitted,
                   agent_context_tokens=agent_context_tokens,
                   peak_cycle_output=peak_cycle_output,
                   low_output_streak=low_output_streak,
                   usage_basis=_usage_basis_arg)

        # Clone exit path: skip final_reporter and curator. Run reporter
        # in merge mode to produce the merge_report.md the root conductor's
        # barrier is watching for. This is the load-bearing invariant — it
        # must fire on ANY clone exit path (exhaustion, stop, timeout, etc.)
        # so the barrier never blocks forever.
        if _is_clone():
            # Provenance: enumerate workspace files this clone touched (mtime
            # >= start). Written BEFORE merge_report so downstream aggregators
            # see a complete picture. Best-effort — zero count on any failure.
            try:
                _start_ts = float(os.environ.get("AGENT_CLONE_START_TS", "0"))
            except ValueError:
                _start_ts = 0.0
            _ws = config.get("working_directory")
            _write_files_touched(
                state_path.parent,
                Path(_ws) if _ws else None,
                _start_ts,
            )
            # Plan H: per-clone authorship from shadow ledger.
            # Returns 0 silently when the shadow ledger is missing
            # (non-fanout runs, or clones that crashed before any ledger
            # activity). Curator falls back to the fork-scoped file.
            _write_clone_artifacts(state_path.parent)

            if reporter_def:
                range_start = max(1, cycle - cycles_since_last_report)
                merge_path = _merge_report_path(state_path.parent)
                try:
                    last_session_id = _run_reporter(
                        reporter_def, task, config, results, score_inputs,
                        agent_sessions, agent_summaries,
                        conn, cycle, last_session_id,
                        range_start, max(cycle, range_start),
                        cycle_session_log,
                        context_window, compact_at,
                        reporter_mode="merge",
                        merge_report_path=merge_path,
                    )
                except Exception as _merge_err:
                    # Best-effort placeholder so the barrier still observes
                    # a file — unblocks the root conductor.
                    print(
                        f"[long-exposure] merge reporter crashed: "
                        f"{_merge_err}; writing placeholder.",
                        flush=True,
                    )
                    try:
                        _write_merge_report(
                            _merge_report_path(state_path.parent),
                            f"# Merge Report (reporter crashed)\n\n"
                            f"{_merge_err}\n",
                            config,
                            f"cycles 1-{cycle} [crashed]",
                            verdict="halted",
                        )
                    except OSError:
                        pass
            else:
                # No reporter defined in score — still unblock the barrier.
                try:
                    _write_merge_report(
                        _merge_report_path(state_path.parent),
                        "# Merge Report\n\n"
                        "(No reporter agent defined in score.)\n",
                        config,
                        f"cycles 1-{cycle} [no-reporter]",
                        verdict="unknown",
                    )
                except OSError:
                    pass
        else:
            # Root path — final auditor + final synthesis + curator. The
            # _should_run_final_synthesis predicate is shared so the auditor
            # and reporter never desynchronize (docs/end-of-run-pipeline.md).
            operator_stop_requested = _stop_requested
            operator_clear_requested = _clear_requested
            should_run_final = _should_run_final_synthesis(
                topic_exhausted=topic_exhausted,
                max_cycles_reached=max_cycles_reached,
                stop_requested=operator_stop_requested,
                clear_requested=operator_clear_requested,
            )
            stop_suppressed_for_final = _clear_stop_flag_for_final_synthesis(
                should_run_final=should_run_final,
                stop_requested=operator_stop_requested,
                clear_requested=operator_clear_requested,
            )
            if stop_suppressed_for_final:
                print(
                    "[long-exposure] Stop acknowledged; running final "
                    "auditor/reporter/curator before exit.",
                    flush=True,
                )

            # 1. Final auditor (if defined) — runs BEFORE the reporter so the
            #    reporter can ingest final_audit_summary.json structurally.
            #    Graceful absence: missing agent definition skips this stage.
            final_auditor_def = agents.get("final_auditor")
            if should_run_final and final_auditor_def:
                try:
                    from long_exposure.auditing import _run_final_auditor
                    last_session_id = _run_final_auditor(
                        final_auditor_def, task, config, results, score_inputs,
                        conn, cycle, last_session_id,
                        context_window, compact_at,
                        data_dir=data_dir,
                        agent_sessions=agent_sessions,
                        agent_summaries=agent_summaries,
                    )
                except Exception as _aud_err:
                    consecutive_failures["final_auditor"] = (
                        consecutive_failures.get("final_auditor", 0) + 1
                    )
                    # Final auditor failure must not block the reporter or
                    # curator — the run still ships a final report. Surface
                    # the error and continue.
                    print(
                        f"[long-exposure] Final auditor crashed: {_aud_err!r} — "
                        f"continuing to final reporter without audit summary.",
                        flush=True,
                    )

            final_reporter_def = agents.get("final_reporter")
            if should_run_final and final_reporter_def:
                try:
                    last_session_id = _run_final_reporter(
                        final_reporter_def, task, config, results, score_inputs,
                        conn, cycle, last_session_id,
                        context_window, compact_at,
                        data_dir=data_dir,
                        agent_sessions=agent_sessions,
                        agent_summaries=agent_summaries,
                    )
                except Exception as _rep_err:
                    consecutive_failures["final_reporter"] = (
                        consecutive_failures.get("final_reporter", 0) + 1
                    )
                    print(
                        f"[long-exposure] Final reporter crashed: {_rep_err!r} — "
                        f"continuing to curator with available artifacts.",
                        flush=True,
                    )

            curator_def = agents.get("curator")
            if should_run_final and curator_def:
                try:
                    last_session_id = _run_curator(
                        curator_def, task, config, results, score_inputs,
                        conn, cycle, last_session_id,
                        agent_sessions=agent_sessions,
                        agent_summaries=agent_summaries,
                    )
                except Exception as _cur_err:
                    consecutive_failures["curator"] = (
                        consecutive_failures.get("curator", 0) + 1
                    )
                    print(
                        f"[long-exposure] Curator crashed: {_cur_err!r} — "
                        f"saving state and final-stage failure counters.",
                        flush=True,
                    )

        # Stop: save current state for resume
        save_state(state_path, cycle, results, consecutive_failures,
                   last_session_id, agent_sessions, agent_summaries,
                   post_merge_pending=post_merge_pending, task=task,
                   run_id=run_id,
                   last_daily_sync_at=last_daily_sync_at,
                   daily_sync_count=daily_sync_count,
                   reanchor_emitted=reanchor_emitted,
                   agent_context_tokens=agent_context_tokens,
                   peak_cycle_output=peak_cycle_output,
                   low_output_streak=low_output_streak,
                   usage_basis=_usage_basis_arg)
        final_status = (
            "cleared" if _clear_requested
            else "completed" if (
                "should_run_final" in locals() and should_run_final
            )
            else "stopped"
        )
        update_status_file(output_dir, cycle, final_status, consecutive_failures)
        telemetry.emit(
            "run_end",
            phase="run",
            cycle=cycle,
            provider=config.get("llm_provider"),
            model=config.get("model"),
            status=final_status,
            data={
                "topic_exhausted": topic_exhausted,
                "max_cycles_reached": (
                    max_cycles_reached
                    if "max_cycles_reached" in locals()
                    else False
                ),
                "stop_requested": (
                    operator_stop_requested
                    if "operator_stop_requested" in locals()
                    else _stop_requested
                ),
                "clear_requested": _clear_requested,
                "final_synthesis_requested": (
                    should_run_final if "should_run_final" in locals() else False
                ),
                "failures": consecutive_failures,
            },
        )
        if final_status == "completed":
            print(
                f"\n[long-exposure] Completed after {cycle} cycles.",
                flush=True,
            )
            print("[long-exposure] Final artifacts written.", flush=True)
        else:
            print(f"\n[long-exposure] Stopped after {cycle} cycles.", flush=True)
            print("[long-exposure] State preserved. Run again to resume.", flush=True)

    conn.close()
    print(f"[long-exposure] State: {state_path}", flush=True)


# ---------------------------------------------------------------------------
# CLI — lightweight command wrappers
# ---------------------------------------------------------------------------

DEFAULT_SCORE_PATH = str(SCRIPT_DIR / "exploration-score.yaml")


def _cmd_start(args):
    """Start exploration, optionally with a task override."""
    task_override = " ".join(args.task) if args.task else None
    instance_dir = resolve_instance_dir(args.instance_dir)

    # Clear existing state if starting fresh with a new task
    if task_override:
        sp = _resolve_state_path(args.state, instance_dir)
        if sp.exists():
            _archive_state(sp)
            sp.unlink()

    run_exploration(
        score_path=args.score,
        config_path=args.config,
        output_dir=_resolve_output_dir(args.output, instance_dir),
        state_path=_resolve_state_path(args.state, instance_dir),
        task_override=task_override,
        instance_dir=instance_dir,
    )


def _cmd_stop(args):
    """Create stop signal file."""
    instance_dir = resolve_instance_dir(args.instance_dir)
    data_dir = _resolve_state_path(args.state, instance_dir).parent
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "long-exposure.stop").write_text("")
    print("[long-exposure] Stop signal sent.", flush=True)


def _cmd_clear(args):
    """Create clear signal file (or clear state directly if not running)."""
    instance_dir = resolve_instance_dir(args.instance_dir)
    sp = _resolve_state_path(args.state, instance_dir)
    data_dir = sp.parent
    data_dir.mkdir(parents=True, exist_ok=True)

    # If exploration is running, send signal file
    # If not running, archive and clear directly
    if sp.exists():
        _archive_state(sp)
        _archive_local_session_logs(sp.parent)
        sp.unlink()
        print("[long-exposure] State archived and cleared.", flush=True)
    else:
        _archive_local_session_logs(sp.parent)

    (data_dir / "long-exposure.clear").write_text("")
    print("[long-exposure] Clear signal sent.", flush=True)


def _cmd_resume(args):
    """Resume exploration.

    Accepts an optional positional argument in the same form as ``start``:

        long-exposure resume                       # continue with saved directive
        long-exposure resume "new directive"       # continue, redirected to new task
        long-exposure resume --from-archive FILE   # restore archived state, then resume

    Back-compat: if the single positional arg looks like an existing .json
    file, it's treated as the old state_file positional (deprecated).
    """
    instance_dir = resolve_instance_dir(args.instance_dir)

    # Back-compat: detect the pre-change usage `resume path/to/archived.json`.
    # If task tokens collapse to exactly one arg and it points to an existing
    # .json file, route it to --from-archive and log a deprecation note.
    task_tokens = list(args.task or [])
    from_archive = args.from_archive
    if from_archive is None and len(task_tokens) == 1:
        _maybe_path = Path(task_tokens[0])
        if _maybe_path.suffix == ".json" and _maybe_path.exists():
            print(
                "[long-exposure] Note: positional state-file form is deprecated; "
                "use --from-archive to restore an archived state file.",
                flush=True,
            )
            from_archive = str(_maybe_path)
            task_tokens = []

    if from_archive:
        resume_path = Path(from_archive)
        if not resume_path.exists():
            print(f"[long-exposure] State file not found: {resume_path}", flush=True)
            return
        active_state = _resolve_state_path(args.state, instance_dir)
        active_state.parent.mkdir(parents=True, exist_ok=True)
        active_state.write_text(resume_path.read_text())
        print(f"[long-exposure] Restored state from: {resume_path.name}", flush=True)

    task_override = " ".join(task_tokens) if task_tokens else None

    run_exploration(
        score_path=args.score,
        config_path=args.config,
        output_dir=_resolve_output_dir(args.output, instance_dir),
        state_path=_resolve_state_path(args.state, instance_dir),
        task_override=task_override,
        instance_dir=instance_dir,
    )


def main():
    parser = argparse.ArgumentParser(
        prog="exploration",
        description="Continuous autonomous exploration conductor",
    )
    parser.add_argument("--score", default=DEFAULT_SCORE_PATH,
                        help="Path to exploration score YAML")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--output", default=None, help="Output directory")
    parser.add_argument("--state", default=None, help="State file path")
    parser.add_argument(
        "--instance-dir",
        default=None,
        help=(
            "Per-session workspace directory. Required to run multiple "
            "concurrent explorations. When set, state file defaults to "
            "<instance-dir>/exploration_state.json, output to "
            "<instance-dir>/output, and MCP config to "
            "<instance-dir>/mcp_config.json. Explicit --state/--output "
            "still override these defaults. Can also be set via the "
            "AGENT_INSTANCE_DIR env var. Omit to use the legacy "
            "single-session default paths (preserves existing resume "
            "behavior)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # /start [task description]
    p_start = sub.add_parser("start", help="Start exploration")
    p_start.add_argument("task", nargs="*", help="Task description (overrides score YAML)")

    # /stop
    sub.add_parser("stop", help="Send stop signal to running exploration")

    # /clear
    sub.add_parser("clear", help="Stop + archive state + clear context")

    # /resume [task...] [--from-archive FILE]
    p_resume = sub.add_parser("resume", help="Resume exploration")
    p_resume.add_argument(
        "task", nargs="*",
        help=(
            "Optional new directive. Same form as `start`: pass a task "
            "string to redirect the exploration on resume. Omit to continue "
            "with the directive saved when the exploration was stopped."
        ),
    )
    p_resume.add_argument(
        "--from-archive", default=None, metavar="FILE",
        help=(
            "Restore from an archived state file, then resume. The archive "
            "is copied into the active state path before the run starts."
        ),
    )

    args = parser.parse_args()

    if args.command == "start":
        _cmd_start(args)
    elif args.command == "stop":
        _cmd_stop(args)
    elif args.command == "clear":
        _cmd_clear(args)
    elif args.command == "resume":
        _cmd_resume(args)


# ---------------------------------------------------------------------------
# Re-exports from extracted modules
# ---------------------------------------------------------------------------
# The curator section moved to long_exposure.curator (file-organization split, no
# behavior change). Re-exported here so existing callers — including
# run_final_reporter.py, the in-loop dispatch in run_exploration, and any
# downstream test that imports these symbols from long_exposure.exploration —
# continue to resolve them at long_exposure.exploration.<name>. Placed at the end
# so that curator.py's `from long_exposure.exploration import _call_agent_with_rotation,
# _store_agent_output, _total_context_tokens` resolves cleanly: by the time
# this line runs, those symbols are already defined.
from long_exposure.curator import (  # noqa: E402
    _PACKAGE_HARD_EXCLUDE_NAMES,
    _PACKAGE_HARD_EXCLUDE_PATTERNS,
    _PACKAGE_HARD_EXCLUDE_SUFFIXES,
    _collect_clone_artifacts,
    _create_package_zip,
    _format_clone_artifacts,
    _is_package_hard_excluded,
    _minimal_safety_curation,
    _package_slug,
    _parse_curation_manifest,
    _render_package_readme,
    _run_curator,
)
# The final-reporter section moved to long_exposure.reporting (narrow split — only
# end-of-exploration helpers; periodic / merge-mode reporter machinery
# stays here because it is shared with the cycle loop and fan-out
# conductor). See reporting.py module docstring.
from long_exposure.reporting import (  # noqa: E402
    _estimate_prior_report_tokens,
    _extract_report_content,
    _final_report_expected_file,
    _render_final_pdf,
    _rescue_stage_file,
    _run_final_reporter,
)
# The fan-out subsystem moved to long_exposure.fanout (both the fork-id helpers /
# XML parser / constants and the conductor / barrier). Broad extraction —
# many names are referenced from the cycle loop below. Re-exported here so
# the existing call sites (and any downstream importer) continue to resolve
# these at long_exposure.exploration.<name> without change.
from long_exposure.fanout import (  # noqa: E402
    FANOUT_CAP_SECONDS,
    FANOUT_GUIDANCE,
    FANOUT_MAX_BRANCHES,
    POST_MERGE_BRIEF_TEMPLATE,
    get_fanout_guidance,
    _FANOUT_BLOCK_RE,
    _FANOUT_BRANCH_RE,
    _FANOUT_FIELD_RE,
    _append_fork_manifest_outcomes,
    _clone_instance_dir,
    _extract_fork_metadata,
    _fork_dir,
    _get_clone_k,
    _get_fork_id,
    _is_clone,
    _merge_report_path,
    _parse_fanout_block,
    _resolve_python_exe,
    _run_fanout_conductor,
    _seed_clone_state,
    _spawn_clone,
    _write_fanout_assignment,
    _write_fork_manifest,
)


if __name__ == "__main__":
    main()
