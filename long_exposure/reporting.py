"""Final-report orchestration: staged-framework runner + helpers.

Extracted verbatim from long_exposure.exploration. The body of this module
mirrors the prior in-line section (lines 2251-2738 of the previous
exploration.py); only the import header differs.

Scope of this split is intentionally narrow. The periodic / merge-mode
reporter (`_run_reporter`, `_write_merge_report`, frontmatter helpers,
`_install_clone_local_log`, `_atomic_write_text`) remains in
long_exposure.exploration because it is shared infrastructure between the
cycle loop and the fan-out conductor; pulling it out would force a
bidirectional cross-import mesh. The final reporter, by contrast,
runs once at end-of-exploration and is freestanding.
"""

from __future__ import annotations

import json as _json
import os
import re as _re
import time as _time
from datetime import datetime, timezone
from pathlib import Path

from long_exposure import paths
from long_exposure.limits import (
    DELTA_DETECT_MIN_BYTES,
    FINAL_STAGE_TOKEN_THRESHOLD,
    WALL_CAP_SECONDS,
)
from long_exposure.orchestrator import ClaudeRateLimitError
from long_exposure.report_formatting import (
    normalize_report_file,
    render_report_pdf,
)


# Lazy-import delegators for names defined in long_exposure.exploration.
#
# Importing these at module-load time creates a circular import, because
# exploration.py re-exports this module near its tail. The prior design used
# a PEP-562 module `__getattr__` for lazy resolution — but that only handles
# *external* attribute access (`reporting._stop_requested`), NOT bare-name
# `LOAD_GLOBAL` lookups from inside functions defined here. Python's
# global-name resolution consults the module `__dict__` and builtins only;
# it never invokes `__getattr__`. Every bare-name reference below would have
# raised NameError on first execution of `_run_final_reporter`. The fix:
# real module-level wrapper functions. Each does the lazy import at call
# time and delegates. `sys.modules` caches the resolved module, so overhead
# is one dict lookup per call.
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
    # `_stop_requested` is a module-level bool rebound by exploration.py's
    # SIGINT/SIGTERM handler. Read through the module each call to capture
    # the live value — a snapshot import would freeze at False forever.
    import long_exposure.exploration as _exploration
    return bool(_exploration._stop_requested)


def _estimate_prior_report_tokens(working_dir: str | Path) -> tuple[int, list[str]]:
    """Estimate total tokens in prior report .md files. Returns (tokens, paths)."""
    total_chars = 0
    report_paths = []
    for p in paths.iter_cycle_report_paths(working_dir):
        try:
            total_chars += len(p.read_text())
            report_paths.append(str(p))
        except OSError:
            pass
    # Rough token estimate: ~4 chars per token
    return total_chars // 4, report_paths


def _final_report_expected_file(
    stage: int, total_stages: int, working_dir: str,
) -> Path | None:
    """Return the file path that MUST exist after a final-report stage.

    Stage 1 (Outline)  → reports/final/outline.md
    Body stages        → reports/final/draft.md
    Final stage        → reports/final/final_report.md
    """
    if stage == 1:
        return paths.final_report_outline_path(working_dir)
    if stage == total_stages:
        return paths.final_report_path(working_dir)
    return paths.final_report_draft_path(working_dir)


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
        return st.st_size, st.st_mtime_ns
    except OSError:
        return None


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{int(_time.time() * 1000)}")
    tmp.write_text(text)
    os.replace(tmp, path)


def _marker_metadata(marker_path: Path) -> dict | None:
    if not marker_path.exists():
        return None
    try:
        data = _json.loads(marker_path.read_text())
        return data if isinstance(data, dict) else {}
    except (_json.JSONDecodeError, OSError):
        return {}


def _committed_baseline(path: Path, marker_path: Path) -> tuple[bool, str, float | None]:
    """Detect a delta baseline, preferring explicit commit markers."""
    marker = _marker_metadata(marker_path)
    if marker is not None and path.exists():
        ts = marker.get("committed_at")
        try:
            boundary = datetime.fromisoformat(str(ts)).timestamp() if ts else marker_path.stat().st_mtime
        except (OSError, ValueError):
            boundary = None
        return True, "marker", boundary
    try:
        if path.exists() and path.stat().st_size > DELTA_DETECT_MIN_BYTES:
            return True, "legacy_size", None
    except OSError:
        pass
    return False, "none", None


def _write_commit_marker(marker_path: Path, *, run_id: str | None, mode: str, token_count: int) -> None:
    payload = {
        "committed_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "mode": mode,
        "input_tokens": int(token_count),
    }
    try:
        _atomic_write_text(marker_path, _json.dumps(payload, indent=2) + "\n")
    except OSError as e:
        print(f"[long-exposure]   Commit marker write skipped: {e}", flush=True)


def _write_run_mode(path: Path, payload: dict) -> None:
    try:
        _atomic_write_text(path, _json.dumps(payload, indent=2) + "\n")
    except OSError:
        pass


def _estimate_delta_report_tokens(report_paths: list[str], boundary_ts: float | None) -> int:
    if boundary_ts is None:
        return 0
    chars = 0
    for raw in report_paths:
        p = Path(raw)
        try:
            if p.stat().st_mtime > boundary_ts:
                chars += len(p.read_text())
        except OSError:
            continue
    return chars // 4


def _load_audit_summary(path: Path) -> tuple[str, str]:
    """Read final_audit_summary.json and return (full_json_text, headline).

    The headline is a one-line, human-and-LLM-readable digest of the structured
    summary that the reporter can quote directly. It is computed deterministically
    from parsed JSON; on any parse failure or absent file, returns
    ``("(no final audit summary available)", "(no audit summary)")`` so the
    reporter degrades gracefully (gap 1.3).
    """
    if not path.exists():
        return ("(no final audit summary available)", "(no audit summary)")
    try:
        text = path.read_text()
    except OSError:
        return ("(no final audit summary available)", "(no audit summary)")

    try:
        data = _json.loads(text)
    except _json.JSONDecodeError:
        return (text, "(audit summary present but JSON parse failed)")

    if not isinstance(data, dict):
        return (text, "(audit summary not a JSON object)")

    distrib = data.get("milestone_status_distribution") or {}
    if not isinstance(distrib, dict):
        distrib = {}
    findings = _coerce_findings_counts(data)
    parts = []
    if distrib:
        ordered = sorted(distrib.items(), key=lambda kv: -_coerce_int(kv[1]))
        parts.append(", ".join(f"{v} {k}" for k, v in ordered if v))
    if findings:
        sev_str = " ".join(
            f"{k}={findings.get(k, 0)}"
            for k in ("CRITICAL", "MODERATE", "MINOR", "OTHER")
            if findings.get(k) is not None
        )
        if sev_str:
            parts.append(f"findings {sev_str}")
    promise_status = data.get("promise_check_status")
    if promise_status:
        parts.append(f"promise_check={promise_status}")
    if data.get("wall_cap_exceeded"):
        parts.append("wall_cap_exceeded")
    headline = " · ".join(parts) if parts else "(audit summary present but empty)"
    return (text, headline)


def _coerce_int(value) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _coerce_findings_counts(data: dict) -> dict:
    counts = {"CRITICAL": 0, "MODERATE": 0, "MINOR": 0, "OTHER": 0}
    source = data.get("findings")
    if isinstance(source, dict):
        for key, value in source.items():
            bucket = key if key in counts else "OTHER"
            counts[bucket] += _coerce_int(value)
    elif isinstance(source, list):
        for item in source:
            if not isinstance(item, dict):
                continue
            sev = item.get("severity")
            bucket = sev if sev in counts else "OTHER"
            counts[bucket] += 1
    elif isinstance(data.get("severity_breakdown"), dict):
        for key, value in data["severity_breakdown"].items():
            bucket = key if key in counts else "OTHER"
            counts[bucket] += _coerce_int(value)
    return {key: value for key, value in counts.items() if value}


def _extract_report_content(
    output_text: str,
    marker: str = "final_report_stage",
) -> str:
    """Extract markdown report content from agent output.

    The output_text has already been through parse_outputs(), so
    [OUTPUT]...[END OUTPUT] markers are stripped.  The text may
    contain agent preamble before the first markdown heading.
    We strip everything before the first ``# `` heading, since
    report content always starts with a heading.  If no heading
    is found, return the full text (better than nothing).

    `marker` parametrizes the [OUTPUT:<marker>] regex so other callers
    (Stage 2: merge synthesis) can rescue from their own output blocks
    without colliding with final_report_stage. Default preserves prior
    behavior verbatim.
    """
    # Try [OUTPUT] regex first (in case raw response leaks through)
    m = _re.search(
        rf"\[OUTPUT:\s*{_re.escape(marker)}\]\s*\n(.*?)(?:\[END OUTPUT)",
        output_text,
        _re.DOTALL,
    )
    text = m.group(1).strip() if m else output_text.strip()

    # Strip agent preamble before first heading
    heading = _re.search(r"^(#+ )", text, _re.MULTILINE)
    if heading:
        text = text[heading.start():]
    return text


def _rescue_stage_file(
    stage: int,
    total_stages: int,
    expected_path: Path,
    output_text: str,
) -> bool:
    """If the agent failed to write the expected file, write it from output.

    For body stages, appends to reports/final/draft.md.
    For outline/finalize, writes/overwrites the target file.
    Returns True if a rescue write was performed.
    """
    content = _extract_report_content(output_text)
    if not content:
        return False

    is_body = 1 < stage < total_stages
    try:
        if is_body and expected_path.exists():
            # Append this stage's sections to the draft
            with open(expected_path, "a") as f:
                f.write("\n\n" + content + "\n")
        else:
            expected_path.write_text(content + "\n")
        try:
            from long_exposure import health_events as _he
            _he.append_event(
                "file_gate_rescue",
                detail=f"stage={stage}/{total_stages} target={expected_path.name} bytes={len(content)}",
            )
        except Exception:
            pass
        return True
    except OSError as e:
        print(f"[long-exposure]   Rescue write failed: {e}", flush=True)
        try:
            from long_exposure import health_events as _he
            _he.append_event(
                "file_gate_rescue_failed",
                detail=f"stage={stage}/{total_stages} target={expected_path.name} err={type(e).__name__}: {e}",
            )
        except Exception:
            pass
        return False


def render_pdf(working_dir: str, stem: str = "final_report") -> bool:
    """Deterministic PDF render — called by the orchestrator, not the agent.

    Renders ``<stem>.md`` to ``<stem>.pdf`` via pandoc + tectonic, with the
    same LaTeX preamble (Unicode coverage, URL breaking, microtype, table
    overflow tolerance) used for the canonical final report. Returns True
    on success. Used for BOTH ``final_report`` and ``final_audit_report``
    so the two synthesis artifacts render identically.
    """
    wd = Path(working_dir)
    if stem == "final_report":
        md_path = paths.final_report_path(wd)
        pdf_path = paths.final_report_pdf_path(wd)
    elif stem == "final_audit_report":
        md_path = paths.final_audit_report_path(wd)
        pdf_path = paths.final_audit_pdf_path(wd)
    else:
        md_path = wd / f"{stem}.md"
        pdf_path = wd / f"{stem}.pdf"
    if not md_path.exists():
        print(
            f"[long-exposure]   Cannot render PDF — {md_path.name} missing.",
            flush=True,
        )
        return False

    try:
        normalize_report_file(md_path, fallback_title=stem.replace("_", " ").title())
        proc = render_report_pdf(
            md_path,
            pdf_path,
            cwd=working_dir,
            timeout=300,
        )
        if proc.returncode == 0:
            size_kb = pdf_path.stat().st_size // 1024
            print(
                f"[long-exposure]   PDF rendered: {pdf_path.name} ({size_kb} KB)",
                flush=True,
            )
            return True
        else:
            err = (proc.stderr or proc.stdout or "")[:300]
            print(f"[long-exposure]   pandoc failed (rc={proc.returncode}): {err}", flush=True)
            try:
                from long_exposure import health_events as _he
                _he.append_event(
                    "pdf_render_failed",
                    detail=f"stem={stem} rc={proc.returncode} stderr={err!r}",
                )
            except Exception:
                pass
            return False
    except Exception as e:
        print(f"[long-exposure]   PDF render error: {e}", flush=True)
        try:
            from long_exposure import health_events as _he
            _he.append_event(
                "pdf_render_failed",
                detail=f"stem={stem} exception={type(e).__name__}: {e}",
            )
        except Exception:
            pass
        return False


# Backward-compatible alias — the old name is referenced by exploration.py's
# top-of-module re-export list and may be imported by external tools.
def _render_final_pdf(working_dir: str) -> bool:
    return render_pdf(working_dir, "final_report")


def _run_final_reporter(
    final_reporter_def: dict,
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
    """Run the final reporter in multiple stages to produce a synthesis report.

    Stage count is determined by the volume of prior reports:
      - 1 outline stage
      - N body stages (1 per ~20k source tokens)
      - 1 finalize stage

    Uses a persistent session across stages (with auto-compact) so the
    agent builds cumulative understanding of the exploration. Follows
    the same session and compaction patterns as _run_reporter.

    File-gate enforcement: after each stage, the orchestrator verifies
    that the expected file exists on disk. If the agent produced content
    in its [OUTPUT] block but failed to write the file, the orchestrator
    writes it deterministically as a rescue fallback, then re-prompts
    the agent to continue from the rescued state. This guarantees that
    reports/final/final_report.md and reports/final/final_report.pdf are
    always produced when the agent generates valid content.
    """
    working_dir = str(paths.workspace_root(config.get("working_directory") or "/tmp"))
    config["working_directory"] = working_dir
    paths.ensure_layout(config)

    total_tokens, report_paths = _estimate_prior_report_tokens(working_dir)
    if not report_paths:
        print("[long-exposure] No prior reports found — skipping final report.", flush=True)
        return last_session_id

    final_path = paths.final_report_path(config)
    marker_path = paths.final_report_commit_marker_path(config)
    delta_mode, delta_source, boundary_ts = _committed_baseline(final_path, marker_path)
    mode = "delta" if delta_mode else "fresh"
    delta_tokens = _estimate_delta_report_tokens(report_paths, boundary_ts) if delta_mode else 0
    budget_tokens = max(delta_tokens, 1) if delta_mode and boundary_ts is not None else total_tokens

    # Stage count scales with the relevant artifact set: whole workspace for
    # fresh runs, files newer than the prior committed baseline for deltas.
    num_body_stages = max(1, budget_tokens // FINAL_STAGE_TOKEN_THRESHOLD)
    total_stages = num_body_stages + 2  # outline + body stages + finalize
    outline_path = str(paths.final_report_outline_path(config))
    _write_run_mode(paths.final_report_run_mode_path(config), {
        "agent": "final_reporter",
        "mode": mode,
        "detection_source": delta_source,
        "canonical_path": str(final_path),
        "commit_marker": str(marker_path),
        "baseline_boundary_ts": boundary_ts,
        "total_report_tokens": total_tokens,
        "budget_tokens": budget_tokens,
        "stages": total_stages,
    })

    # Audit-summary ingestion (Plan 2 Phase 3). The full JSON is injected
    # verbatim (agent can quote/parse) AND we pre-compute a one-line headline
    # the agent can use as a guard rail even if JSON parsing slips. This is
    # the robust + simple fix for gap 1.3 — text remains the substrate, the
    # headline is an additive fallback. If the JSON is malformed, the headline
    # surfaces "(parse failed)" and the reporter narrates conservatively.
    _audit_summary_path = paths.final_audit_summary_path(config)
    _audit_summary_text, _audit_headline = _load_audit_summary(_audit_summary_path)
    try:
        if config.get("ledger_graph", {}).get("enabled", True):
            from long_exposure.tools import ledger_graph as _ledger_graph
            results["ledger_causal_summary"] = _ledger_graph.render_summary(
                _ledger_graph.build(Path(working_dir))
            )
        else:
            results["ledger_causal_summary"] = ""
    except Exception as _ledger_err:
        print(f"[final-reporter] ledger_causal_summary skipped: {_ledger_err!r}", flush=True)
        results["ledger_causal_summary"] = ""

    # Wall-cap (Plan 2 §7.7) — shared with the final auditor. Document/finalize
    # stage always runs even when the cap is hit.
    _final_report_start_ts = _time.monotonic()
    _wall_cap_hit = False

    print(f"\n{'='*60}", flush=True)
    print("[long-exposure] === Final Report ===", flush=True)
    print(
        f"[long-exposure] Prior reports: {len(report_paths)} files, "
        f"~{total_tokens:,} tokens",
        flush=True,
    )
    if delta_mode:
        print(
            f"[long-exposure] Delta final-report mode via {delta_source}; "
            f"budget ~{budget_tokens:,} tokens",
            flush=True,
        )
    print(f"[long-exposure] Stages: {total_stages} (1 outline + {num_body_stages} body + 1 finalize)", flush=True)

    prior_reports_str = "\n".join(f"  {p}" for p in report_paths)
    if delta_mode:
        prior_reports_str = (
            "DELTA MODE: A committed baseline final_report.md already exists at "
            f"{final_path}. Preserve unchanged sections unless the listed cycle "
            "reports explicitly revise them. Stage 1 should identify changed "
            "sections; body stages should update only those sections; finalize "
            "must emit the full revised final_report.md with frontmatter, "
            "abstract framing, section order, and figure references preserved.\n\n"
            + prior_reports_str
        )

    # Persistent session — resumes across stages, auto-compacts when needed
    final_sessions: dict = {}
    final_summaries: dict = {}

    # Rescue state carried to the next stage's prompt (Fix 2).
    # Set when the FILE GATE fires, consumed and cleared on the next iteration.
    pending_rescue_warning: str | None = None
    final_stage_touched = False

    for stage in range(1, total_stages + 1):
        # Check for stop signal between stages
        if data_dir:
            _check_signal_files(data_dir)
        if _is_stop_requested():
            print("[long-exposure] Stop signal — ending final report early.", flush=True)
            break

        # Wall-cap pre-check. The finalize stage (= total_stages) always runs;
        # it is the commit step that produces final_report.md.
        #
        # IMPLEMENTATION NOTE: when the cap fires we re-bind the loop
        # variable to `total_stages` and let the rest of the iteration run
        # the finalize body. The `if _wall_cap_hit and stage == total_stages:
        # break` at the bottom of the loop ensures we exit *before* the
        # for-loop assigns the next iteration value (which would re-run a
        # mid-stage and overwrite final_report.md). The `break` is
        # load-bearing — do not delete it. A while-loop refactor was
        # considered and rejected as not worth the churn.
        if (stage < total_stages
                and (_time.monotonic() - _final_report_start_ts) > WALL_CAP_SECONDS):
            _wall_cap_hit = True
            print(
                f"[long-exposure]   FINAL REPORT WALL CAP exceeded "
                f"({WALL_CAP_SECONDS}s) — skipping to finalize stage.",
                flush=True,
            )
            stage = total_stages  # advance the loop variable to finalize

        expected_path = _final_report_expected_file(stage, total_stages, working_dir)
        expected_before = _file_signature(expected_path) if expected_path else None

        stage_results = dict(results)
        stage_results["stage"] = f"{stage} of {total_stages}"
        stage_results["total_stages"] = str(total_stages)
        stage_results["expected_file"] = str(expected_path) if expected_path else "(none)"
        stage_results["rescue_warning"] = pending_rescue_warning or "(none)"
        stage_results["outline_path"] = outline_path
        stage_results["draft_path"] = str(paths.final_report_draft_path(config))
        stage_results["final_report_path"] = str(paths.final_report_path(config))
        stage_results["report_glob"] = paths.cycle_reports_glob(config)
        stage_results["final_report_dir"] = str(paths.final_report_scratch_dir(config))
        stage_results["prior_reports"] = prior_reports_str
        stage_results["working_dir"] = working_dir
        stage_results["final_audit_summary"] = _audit_summary_text
        stage_results["final_audit_headline"] = _audit_headline
        stage_results["wall_cap_hit"] = "true" if _wall_cap_hit else "false"

        # Consume the rescue warning — it only applies to the stage right after rescue.
        pending_rescue_warning = None

        stage_label = (
            "Outline" if stage == 1
            else "Finalize" if stage == total_stages
            else f"Body {stage - 1}/{num_body_stages}"
        )
        is_resume = "final_reporter" in final_sessions

        print(
            f"\n[long-exposure] --- Final Report Stage {stage}/{total_stages} "
            f"({stage_label}) {'(resume)' if is_resume else ''} ---",
            flush=True,
        )

        result = _call_agent_with_rotation(
            agent_name="final_reporter",
            agent_def=final_reporter_def,
            sessions_dict=final_sessions,
            task=task,
            config=config,
            results=stage_results,
            score_inputs=score_inputs,
            agent_summaries=final_summaries,
        )

        if result["status"] == "ok":
            usage = result.get("usage", {})
            dur = result.get("duration_ms", 0) / 1000
            total_ctx = _total_context_tokens(usage)
            print(
                f"[long-exposure]   final_reporter: ok "
                f"({dur:.1f}s, ctx:{total_ctx:,}tok, "
                f"out:{usage.get('output_tokens', 0)}tok)",
                flush=True,
            )

            output_text = "\n\n".join(result["outputs"].values())
            last_session_id = _store_agent_output(
                conn, "final_reporter", final_reporter_def, output_text,
                cycle, last_session_id,
                current_topic="Final Report",
            )

            # --- FILE GATE: verify expected file exists on disk ---
            expected = expected_path
            if expected is not None:
                if not expected.exists():
                    print(
                        f"[long-exposure]   FILE GATE: {expected.name} missing "
                        f"— rescuing from [OUTPUT] content.",
                        flush=True,
                    )
                    if _rescue_stage_file(stage, total_stages, expected, output_text):
                        print(
                            f"[long-exposure]   FILE GATE: rescued {expected.name} "
                            f"({expected.stat().st_size:,} bytes).",
                            flush=True,
                        )
                    else:
                        print(
                            f"[long-exposure]   FILE GATE: rescue failed — "
                            f"no usable content in [OUTPUT].",
                            flush=True,
                        )
                    pending_rescue_warning = (
                        f"STAGE {stage} FILE GATE FAILED. The expected file "
                        f"{expected} was not written. The orchestrator saved "
                        f"a stub from the [OUTPUT] block in its place, which "
                        f"is almost certainly not the real content. In this "
                        f"stage, overwrite {expected} with the correct "
                        f"stage-{stage} content before writing the current "
                        f"stage's file."
                    )
                elif _file_signature(expected) == expected_before:
                    if stage == total_stages:
                        print(
                            f"[long-exposure]   FILE GATE: {expected.name} "
                            "unchanged during finalize stage.",
                            flush=True,
                        )
                        if (not delta_mode
                                and _rescue_stage_file(stage, total_stages, expected, output_text)):
                            final_stage_touched = True
                            print(
                                f"[long-exposure]   FILE GATE: rescued "
                                f"{expected.name} from output.",
                                flush=True,
                            )
                        pending_rescue_warning = (
                            f"STAGE {stage} FILE GATE FAILED. The expected "
                            f"file {expected} already existed but was not "
                            "changed during the finalize stage. Re-write it "
                            "with the full revised final report."
                        )
                    elif stage == 1:
                        print(
                            f"[long-exposure]   FILE GATE: {expected.name} "
                            "unchanged during outline stage.",
                            flush=True,
                        )
                        _rescue_stage_file(stage, total_stages, expected, output_text)
                # For body stages, also rescue if the file existed before
                # but the agent didn't append (content went to OUTPUT instead)
                elif 1 < stage < total_stages:
                    content = _extract_report_content(output_text)
                    if content and len(content) > 200:
                        # Check if this content is already in the draft
                        existing = expected.read_text()
                        # Use first 80 chars of content as fingerprint
                        fingerprint = content[:80]
                        if fingerprint not in existing:
                            print(
                                f"[long-exposure]   FILE GATE: {expected.name} exists "
                                f"but stage content not found — appending.",
                                flush=True,
                            )
                            _rescue_stage_file(stage, total_stages, expected, output_text)
                            pending_rescue_warning = (
                                f"STAGE {stage} FILE GATE PARTIAL: {expected} "
                                f"existed but your stage-{stage} content was "
                                f"not found in it. The orchestrator appended "
                                f"content extracted from your response. Going "
                                f"forward, always write body-stage content "
                                f"directly to {expected} (create or append) — "
                                f"do not put it in the [OUTPUT] block."
                            )

                if stage == total_stages and _file_signature(expected) != expected_before:
                    final_stage_touched = True

            # Auto-compact — same pattern as standard reporter
            if total_ctx >= compact_at:
                print(
                    f"[long-exposure]   final_reporter context "
                    f"{total_ctx:,}/{context_window:,} "
                    f"({total_ctx/context_window:.0%}) — compacting...",
                    flush=True,
                )
                try:
                    last_session_id = _compact_agent_session(
                        "final_reporter", final_reporter_def, config,
                        final_sessions, final_summaries,
                        conn, cycle, last_session_id,
                    )
                except ClaudeRateLimitError as e:
                    # Stage output is already written to disk and sessions.db;
                    # compaction loss is recoverable on the next stage.
                    print(
                        f"[long-exposure]   final_reporter compaction "
                        f"rate-limited (non-fatal, stage work saved): "
                        f"{str(e)[:200]}",
                        flush=True,
                    )
        else:
            err = result.get("error", "unknown")
            print(f"[long-exposure]   final_reporter: FAILED — {err}", flush=True)

            # Clear session and retry once with a fresh start
            final_sessions.pop("final_reporter", None)
            print(f"[long-exposure]   Cleared session. Retrying stage {stage}...", flush=True)
            retry_before = _file_signature(expected_path) if expected_path else None

            result = _call_agent_with_rotation(
                agent_name="final_reporter",
                agent_def=final_reporter_def,
                sessions_dict=final_sessions,
                task=task,
                config=config,
                results=stage_results,
                score_inputs=score_inputs,
                agent_summaries=final_summaries,
            )
            if result["status"] == "ok":
                usage = result.get("usage", {})
                dur = result.get("duration_ms", 0) / 1000
                total_ctx = _total_context_tokens(usage)
                print(
                    f"[long-exposure]   final_reporter: ok on retry "
                    f"({dur:.1f}s, ctx:{total_ctx:,}tok)",
                    flush=True,
                )
                output_text = "\n\n".join(result["outputs"].values())
                last_session_id = _store_agent_output(
                    conn, "final_reporter", final_reporter_def, output_text,
                    cycle, last_session_id,
                    current_topic="Final Report",
                )
                # Rescue on retry path too
                expected = expected_path
                if expected and not expected.exists():
                    print(
                        f"[long-exposure]   FILE GATE (retry): {expected.name} missing "
                        f"— rescuing.",
                        flush=True,
                    )
                    _rescue_stage_file(stage, total_stages, expected, output_text)
                    pending_rescue_warning = (
                        f"STAGE {stage} FILE GATE FAILED (after retry). The "
                        f"expected file {expected} was not written. The "
                        f"orchestrator saved a stub from the [OUTPUT] block "
                        f"in its place. In this stage, overwrite {expected} "
                        f"with the correct stage-{stage} content before "
                        f"writing the current stage's file."
                    )
                elif expected and stage == total_stages and _file_signature(expected) == retry_before:
                    print(
                        f"[long-exposure]   FILE GATE (retry): {expected.name} "
                        "unchanged during finalize stage.",
                        flush=True,
                    )
                elif expected and stage == total_stages:
                    final_stage_touched = True

                # Auto-compact on retry path too
                if total_ctx >= compact_at:
                    try:
                        last_session_id = _compact_agent_session(
                            "final_reporter", final_reporter_def, config,
                            final_sessions, final_summaries,
                            conn, cycle, last_session_id,
                        )
                    except ClaudeRateLimitError as e:
                        print(
                            f"[long-exposure]   final_reporter compaction "
                            f"rate-limited (non-fatal, stage work saved): "
                            f"{str(e)[:200]}",
                            flush=True,
                        )
            else:
                print(f"[long-exposure]   Stage {stage} failed. Skipping.", flush=True)

        # If the wall-cap forced us into the finalize stage, do not let
        # the for-loop iterate further (which would re-run finalize
        # repeatedly and overwrite final_report.md each time).
        if _wall_cap_hit and stage == total_stages:
            break

    # Merge final reporter sessions back so they're persisted in state
    if agent_sessions is not None:
        agent_sessions.update(final_sessions)
    if agent_summaries is not None:
        agent_summaries.update(final_summaries)

    # --- FINAL GATE: ensure .md and .pdf exist ---
    final_md = paths.final_report_path(config)
    final_pdf = paths.final_report_pdf_path(config)
    draft_md = paths.final_report_draft_path(config)

    if not final_md.exists() and draft_md.exists():
        # Finalize stage failed to assemble — promote draft as final
        print(
            "[long-exposure]   FINAL GATE: final_report.md missing but draft exists "
            "— promoting draft.",
            flush=True,
        )
        try:
            _atomic_write_text(final_md, draft_md.read_text())
        except OSError as e:
            print(f"[long-exposure]   FINAL GATE: draft promotion failed: {e}", flush=True)

    if final_md.exists() and delta_mode and not final_stage_touched:
        print(
            "[long-exposure]   Final report unchanged in delta mode; "
            "leaving prior baseline and commit marker untouched.",
            flush=True,
        )
    elif final_md.exists():
        try:
            normalize_report_file(final_md, fallback_title="Final Report")
        except OSError as e:
            print(
                f"[long-exposure]   Final report normalization skipped: {e}",
                flush=True,
            )
        size_kb = final_md.stat().st_size // 1024
        print(f"[long-exposure]   Final report: {final_md.name} ({size_kb} KB)", flush=True)

        if not final_pdf.exists():
            print("[long-exposure]   FINAL GATE: PDF missing — rendering now.", flush=True)
            _render_final_pdf(working_dir)
        _write_commit_marker(marker_path, run_id=results.get("run_id") or score_inputs.get("run_id"), mode=mode, token_count=budget_tokens)
    else:
        print(
            "[long-exposure]   FINAL GATE: final_report.md not produced. "
            "Check agent outputs in sessions.db.",
            flush=True,
        )

    print(f"\n[long-exposure] Final report complete.", flush=True)
    return last_session_id
