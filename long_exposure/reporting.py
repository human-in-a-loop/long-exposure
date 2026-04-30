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
import re as _re
import subprocess
import time as _time
from pathlib import Path

from long_exposure.limits import WALL_CAP_SECONDS
from long_exposure.orchestrator import ClaudeRateLimitError


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
    working_dir = Path(working_dir)
    report_paths = sorted(working_dir.rglob("report_cycles_*.md"))
    total_chars = 0
    paths = []
    for p in report_paths:
        try:
            total_chars += len(p.read_text())
            paths.append(str(p))
        except OSError:
            pass
    # Rough token estimate: ~4 chars per token
    return total_chars // 4, paths


def _final_report_expected_file(
    stage: int, total_stages: int, working_dir: str,
) -> Path | None:
    """Return the file path that MUST exist after a final-report stage.

    Stage 1 (Outline)  → final_report_outline.md
    Body stages        → final_report_draft.md
    Final stage        → final_report.md
    """
    wd = Path(working_dir)
    if stage == 1:
        return wd / "final_report_outline.md"
    if stage == total_stages:
        return wd / "final_report.md"
    return wd / "final_report_draft.md"


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
    findings = data.get("findings") or {}
    parts = []
    if distrib:
        ordered = sorted(distrib.items(), key=lambda kv: -int(kv[1] or 0))
        parts.append(", ".join(f"{v} {k}" for k, v in ordered if v))
    if findings:
        sev_str = " ".join(
            f"{k}={findings.get(k, 0)}"
            for k in ("CRITICAL", "MODERATE", "MINOR")
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

    For body stages, appends to final_report_draft.md.
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


_HEADER_TEX = """\
% Unicode glyph coverage. Tectonic uses XeTeX, which can render any
% Unicode codepoint via fontspec, but pandoc's default LaTeX template
% selects Latin Modern (T1) — no Greek, no math symbols in text mode.
% A literal `φ` in the markdown would crash with rc=43 ("Missing
% character: There is no φ (U+03C6) in font [lmroman10-regular]").
% DejaVu covers the full Unicode BMP and ships with every modern Linux.
\\usepackage{fontspec}
\\setmainfont{DejaVu Serif}
\\setsansfont{DejaVu Sans}
\\setmonofont{DejaVu Sans Mono}

% Allow line breaks in URLs
\\usepackage{xurl}

% Microtypographic improvements — character protrusion and font expansion
\\usepackage{microtype}

% Global overflow tolerance
\\tolerance=2000
\\emergencystretch=3em
\\setlength{\\hfuzz}{5pt}

% Tables: smaller font + sloppy line breaking for narrow columns.
\\usepackage{etoolbox}
\\AtBeginEnvironment{longtable}{\\small\\sloppy}
\\AtBeginEnvironment{quote}{\\sloppy}

% Note: a previous version of this header redefined \\texttt to wrap
% its argument in \\seqsplit so long inline code could break across
% lines. That redefinition crashed (rc=43, "Missing number, treated
% as zero") on pandoc-emitted control sequences containing
% backslash-caret combinations inside backtick code — seqsplit's
% per-character splitting can't traverse `\\^` cleanly. The override
% was load-bearing only for pretty-breaking very long identifiers; we
% accept occasional overfull-hbox warnings on those (microtype +
% \\sloppy + \\hfuzz=5pt absorb most cases) in exchange for the
% renderer not crashing on any markdown that contains carets or
% backslashes inside backtick code.
"""


def render_pdf(working_dir: str, stem: str = "final_report") -> bool:
    """Deterministic PDF render — called by the orchestrator, not the agent.

    Renders ``<stem>.md`` to ``<stem>.pdf`` via pandoc + tectonic, with the
    same LaTeX preamble (Unicode coverage, URL breaking, microtype, table
    overflow tolerance) used for the canonical final report. Returns True
    on success. Used for BOTH ``final_report`` and ``final_audit_report``
    so the two synthesis artifacts render identically.
    """
    wd = Path(working_dir)
    md_path = wd / f"{stem}.md"
    pdf_path = wd / f"{stem}.pdf"
    if not md_path.exists():
        print(
            f"[long-exposure]   Cannot render PDF — {md_path.name} missing.",
            flush=True,
        )
        return False

    # Write header.tex for pandoc -H include
    header_path = wd / "header.tex"
    header_existed = header_path.exists()
    if not header_existed:
        header_path.write_text(_HEADER_TEX)

    cmd = [
        "pandoc", str(md_path),
        "-o", str(pdf_path),
        "--pdf-engine=tectonic",
        "--toc",
        "--number-sections",
        "-H", str(header_path),
        "-V", "geometry:margin=1in",
        "-V", "fontsize=11pt",
        "-V", "documentclass=article",
        "-V", "colorlinks=true",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, cwd=working_dir,
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
    finally:
        # Clean up header.tex only if we created it
        if not header_existed and header_path.exists():
            header_path.unlink()


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
    final_report.md and final_report.pdf are always produced when the
    agent generates valid content.
    """
    working_dir = config.get("working_directory", "/tmp")

    total_tokens, report_paths = _estimate_prior_report_tokens(working_dir)
    if not report_paths:
        print("[long-exposure] No prior reports found — skipping final report.", flush=True)
        return last_session_id

    # Stage 3 §4.4: explicit cap removed so multi-day runs with ~1M tokens
    # of prior reports get the stages they need. Wall-cap (WALL_CAP_SECONDS)
    # remains the real ceiling.
    num_body_stages = max(1, total_tokens // 20_000)
    total_stages = num_body_stages + 2  # outline + body stages + finalize
    outline_path = str(Path(working_dir) / "final_report_outline.md")

    # Audit-summary ingestion (Plan 2 Phase 3). The full JSON is injected
    # verbatim (agent can quote/parse) AND we pre-compute a one-line headline
    # the agent can use as a guard rail even if JSON parsing slips. This is
    # the robust + simple fix for gap 1.3 — text remains the substrate, the
    # headline is an additive fallback. If the JSON is malformed, the headline
    # surfaces "(parse failed)" and the reporter narrates conservatively.
    _audit_summary_path = Path(working_dir) / "final_audit_summary.json"
    _audit_summary_text, _audit_headline = _load_audit_summary(_audit_summary_path)

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
    print(f"[long-exposure] Stages: {total_stages} (1 outline + {num_body_stages} body + 1 finalize)", flush=True)

    prior_reports_str = "\n".join(f"  {p}" for p in report_paths)

    # Persistent session — resumes across stages, auto-compacts when needed
    final_sessions: dict = {}
    final_summaries: dict = {}

    # Rescue state carried to the next stage's prompt (Fix 2).
    # Set when the FILE GATE fires, consumed and cleared on the next iteration.
    pending_rescue_warning: str | None = None

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

        stage_results = dict(results)
        stage_results["stage"] = f"{stage} of {total_stages}"
        stage_results["total_stages"] = str(total_stages)
        stage_results["expected_file"] = str(expected_path) if expected_path else "(none)"
        stage_results["rescue_warning"] = pending_rescue_warning or "(none)"
        stage_results["outline_path"] = outline_path
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
    final_md = Path(working_dir) / "final_report.md"
    final_pdf = Path(working_dir) / "final_report.pdf"
    draft_md = Path(working_dir) / "final_report_draft.md"

    if not final_md.exists() and draft_md.exists():
        # Finalize stage failed to assemble — promote draft as final
        print(
            "[long-exposure]   FINAL GATE: final_report.md missing but draft exists "
            "— promoting draft.",
            flush=True,
        )
        draft_md.rename(final_md)

    if final_md.exists():
        size_kb = final_md.stat().st_size // 1024
        print(f"[long-exposure]   Final report: {final_md.name} ({size_kb} KB)", flush=True)

        if not final_pdf.exists():
            print("[long-exposure]   FINAL GATE: PDF missing — rendering now.", flush=True)
            _render_final_pdf(working_dir)
    else:
        print(
            "[long-exposure]   FINAL GATE: final_report.md not produced. "
            "Check agent outputs in sessions.db.",
            flush=True,
        )

    print(f"\n[long-exposure] Final report complete.", flush=True)
    return last_session_id

