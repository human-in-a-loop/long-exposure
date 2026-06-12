"""Regression tests for end-of-run pipeline gates (reporting.py / auditing.py).

Covers three audited bugs:

  1. Rescue plausibility floor — a finalize/outline-stage file-gate rescue of
     a short status receipt (no ``#`` heading → whole text returned) must not
     overwrite a plausible existing artifact, and must not be committed as
     the canonical final report.
  2. Stale-PDF re-render decision — every PDF gate must re-render when the
     markdown is newer than the PDF (delta/daily-sync passes previously
     shipped sync-1's .pdf with sync-2's .md), degrading to render on stat
     errors.
  3. Commit-marker withholding — a pass that ends via stop signal / draft
     promotion must NOT write the ``.committed`` marker; otherwise the next
     pass runs delta mode against a partial baseline with a near-zero budget
     and the missing sections are never written.
"""

import os
from pathlib import Path

from long_exposure import paths, reporting
from long_exposure.auditing import _rescue_audit_stage_file
from long_exposure.reporting import (
    _pdf_needs_render,
    _rescue_stage_file,
    _run_final_reporter,
)

RECEIPT = "Done. The report was written to disk as requested. See file."
PLAUSIBLE = "# Real Report\n\n" + ("Substantive paragraph with findings. " * 20)


# ---------------------------------------------------------------------------
# 1. Rescue plausibility floor
# ---------------------------------------------------------------------------


def test_finalize_rescue_refuses_receipt_over_plausible_file(tmp_path):
    target = tmp_path / "final_report.md"
    target.write_text(PLAUSIBLE)
    before = target.read_text()
    assert _rescue_stage_file(3, 3, target, RECEIPT) is False
    assert target.read_text() == before


def test_finalize_rescue_refuses_receipt_even_without_existing(tmp_path):
    target = tmp_path / "final_report.md"
    assert _rescue_stage_file(3, 3, target, RECEIPT) is False
    assert not target.exists()


def test_outline_rescue_refuses_receipt_over_plausible_outline(tmp_path):
    """Stage-1 'unchanged outline on restart' case: receipt must not clobber."""
    target = tmp_path / "outline.md"
    target.write_text(PLAUSIBLE)
    assert _rescue_stage_file(1, 3, target, RECEIPT) is False
    assert target.read_text() == PLAUSIBLE


def test_finalize_rescue_refuses_when_existing_substantially_larger(tmp_path):
    target = tmp_path / "final_report.md"
    big = "# Big Report\n\n" + ("Long-form section content here. " * 200)
    target.write_text(big)
    # >200 chars but dwarfed by the existing file → refuse.
    medium = "# Short revision\n\n" + ("note " * 50)
    assert len(medium) > 200
    assert len(big) > 2 * len(medium)
    assert _rescue_stage_file(3, 3, target, medium) is False
    assert target.read_text() == big


def test_finalize_rescue_writes_plausible_content(tmp_path):
    target = tmp_path / "final_report.md"
    assert _rescue_stage_file(3, 3, target, PLAUSIBLE) is True
    assert target.read_text().startswith("# Real Report")


def test_finalize_rescue_overwrites_smaller_existing_with_real_content(tmp_path):
    target = tmp_path / "final_report.md"
    target.write_text("# Stub outline\n\n- a\n- b\n")
    assert _rescue_stage_file(3, 3, target, PLAUSIBLE) is True
    assert "Substantive paragraph" in target.read_text()


def test_body_rescue_append_unaffected_by_floor(tmp_path):
    target = tmp_path / "draft.md"
    target.write_text("# Draft\n")
    short_section = "## S2\nbrief"
    assert _rescue_stage_file(2, 4, target, short_section) is True
    assert "## S2" in target.read_text()


def test_audit_document_rescue_refuses_receipt(tmp_path):
    target = tmp_path / "final_audit_report.md"
    assert _rescue_audit_stage_file(target, RECEIPT, min_chars=200) is False
    assert not target.exists()


def test_audit_document_rescue_refuses_receipt_append(tmp_path):
    target = tmp_path / "final_audit_report.md"
    target.write_text(PLAUSIBLE)
    assert _rescue_audit_stage_file(target, RECEIPT, min_chars=200) is False
    assert target.read_text() == PLAUSIBLE


def test_audit_document_rescue_accepts_real_content(tmp_path):
    target = tmp_path / "final_audit_report.md"
    assert _rescue_audit_stage_file(target, PLAUSIBLE, min_chars=200) is True
    assert target.exists()


def test_audit_mid_stage_rescue_keeps_old_behavior(tmp_path):
    target = tmp_path / "verify-1.md"
    assert _rescue_audit_stage_file(target, "short note") is True
    assert target.exists()


# ---------------------------------------------------------------------------
# 2. Stale-PDF re-render decision
# ---------------------------------------------------------------------------


def test_pdf_needs_render_when_missing(tmp_path):
    md = tmp_path / "r.md"
    md.write_text("# r\n")
    assert _pdf_needs_render(md, tmp_path / "r.pdf") is True


def test_pdf_fresh_when_newer_than_md(tmp_path):
    md = tmp_path / "r.md"
    pdf = tmp_path / "r.pdf"
    md.write_text("# r\n")
    pdf.write_bytes(b"%PDF")
    os.utime(md, (1_000_000, 1_000_000))
    os.utime(pdf, (1_000_100, 1_000_100))
    assert _pdf_needs_render(md, pdf) is False


def test_pdf_stale_when_md_newer(tmp_path):
    md = tmp_path / "r.md"
    pdf = tmp_path / "r.pdf"
    md.write_text("# r\n")
    pdf.write_bytes(b"%PDF")
    os.utime(pdf, (1_000_000, 1_000_000))
    os.utime(md, (1_000_100, 1_000_100))
    assert _pdf_needs_render(md, pdf) is True


def test_pdf_degrades_to_render_on_stat_error(tmp_path):
    md = tmp_path / "gone.md"  # never created → md.stat() raises
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF")
    assert _pdf_needs_render(md, pdf) is True


# ---------------------------------------------------------------------------
# 3. Commit marker withheld on stop / draft promotion
# ---------------------------------------------------------------------------


class _FakeReporterAgent:
    """Writes each stage's expected file; optionally raises the stop flag
    after a given stage so the loop breaks before finalize, or skips the
    finalize write entirely (agent emitted a receipt instead)."""

    def __init__(self, stop_after_stage=None, skip_finalize_write=False):
        self.stop = False
        self.stop_after_stage = stop_after_stage
        self.skip_finalize_write = skip_finalize_write
        self.stages_seen = []

    def __call__(self, **kwargs):
        results = kwargs["results"]
        stage = int(results["stage"].split(" of ")[0])
        total = int(results["total_stages"])
        self.stages_seen.append(stage)
        expected = Path(results["expected_file"])
        expected.parent.mkdir(parents=True, exist_ok=True)
        if 1 < stage < total:
            with open(expected, "a") as f:
                f.write(f"\n\n## Section {stage}\n\n" + "body text. " * 60)
        elif stage == total and self.skip_finalize_write:
            pass  # agent only emitted a receipt; file untouched this pass
        else:
            expected.write_text(
                f"# Stage {stage} deliverable\n\n" + "content. " * 100
            )
        if self.stop_after_stage is not None and stage >= self.stop_after_stage:
            self.stop = True
        return {
            "status": "ok",
            "outputs": {"final_report_stage": "wrote the file directly"},
            "usage": {},
            "duration_ms": 0,
        }


def _drive_final_reporter(tmp_path, monkeypatch, fake, pre_seed=None):
    ws = tmp_path / "ws"
    (ws / "reports" / "cycles").mkdir(parents=True, exist_ok=True)
    (ws / "reports" / "cycles" / "report_cycles_001-003.md").write_text(
        "# Cycles 1-3\n\n" + "findings. " * 200
    )
    if pre_seed is not None:
        pre_seed(ws)
    config = {
        "working_directory": str(ws),
        "ledger_graph": {"enabled": False},
    }
    monkeypatch.setattr(reporting, "_call_agent_with_rotation", fake)
    monkeypatch.setattr(reporting, "_is_stop_requested", lambda: fake.stop)
    monkeypatch.setattr(
        reporting, "_store_agent_output", lambda *a, **k: "sess-1"
    )
    monkeypatch.setattr(reporting, "_total_context_tokens", lambda usage: 0)
    monkeypatch.setattr(reporting, "_render_final_pdf", lambda wd: True)
    monkeypatch.setattr(reporting, "normalize_report_file", lambda *a, **k: None)
    _run_final_reporter(
        {}, "test directive", config, {}, {},
        conn=None, cycle=1, last_session_id=None,
        context_window=1_000_000, compact_at=10**12,
    )
    return ws


def test_marker_withheld_on_stop_draft_promotion(tmp_path, monkeypatch):
    # ~2000 tokens of prior reports → 1 body stage → 3 total stages.
    # Stop after the body stage (2) so finalize never runs.
    fake = _FakeReporterAgent(stop_after_stage=2)
    ws = _drive_final_reporter(tmp_path, monkeypatch, fake)
    assert 3 not in fake.stages_seen  # finalize did not run
    # Draft was promoted to final_report.md…
    assert paths.final_report_path(ws).exists()
    # …but the delta-baseline commit marker must NOT exist.
    assert not paths.final_report_commit_marker_path(ws).exists()


def test_marker_written_on_completed_finalize(tmp_path, monkeypatch):
    fake = _FakeReporterAgent(stop_after_stage=None)
    ws = _drive_final_reporter(tmp_path, monkeypatch, fake)
    assert 3 in fake.stages_seen
    assert paths.final_report_path(ws).exists()
    assert paths.final_report_commit_marker_path(ws).exists()


def test_marker_withheld_when_stale_prior_file_satisfies_existence(
    tmp_path, monkeypatch,
):
    """Finalize ran ok but left the file untouched this pass: a
    final_report.md left over from a PRIOR pass must not arm the commit
    marker (the finalize_completed = bare exists() hole)."""

    def pre_seed(ws):
        # Small stale report from a prior pass (below DELTA_DETECT_MIN_BYTES
        # so this pass runs fresh, not delta). No commit marker on disk.
        final_md = paths.final_report_path(ws)
        final_md.parent.mkdir(parents=True, exist_ok=True)
        final_md.write_text("# Stale prior-pass report\n\nshort.\n")

    fake = _FakeReporterAgent(skip_finalize_write=True)
    ws = _drive_final_reporter(tmp_path, monkeypatch, fake, pre_seed=pre_seed)
    # Finalize stage DID run (status ok) ...
    assert max(fake.stages_seen) == 3
    # ... and the stale file still exists (rescue of the receipt refused) ...
    assert paths.final_report_path(ws).exists()
    # ... but the commit marker must be withheld: this pass produced nothing.
    assert not paths.final_report_commit_marker_path(ws).exists()


def test_delta_pass_untouched_finalize_leaves_marker_unchanged(
    tmp_path, monkeypatch,
):
    """Delta pass (marker present) where finalize runs ok but never touches
    final_report.md: the prior commit marker must not be rewritten — that
    would advance the delta boundary past content the pass never produced."""

    def pre_seed(ws):
        final_md = paths.final_report_path(ws)
        final_md.parent.mkdir(parents=True, exist_ok=True)
        final_md.write_text(PLAUSIBLE)
        reporting._write_commit_marker(
            paths.final_report_commit_marker_path(ws),
            run_id="prior-run", mode="fresh", token_count=123,
        )

    fake = _FakeReporterAgent(skip_finalize_write=True)
    ws = tmp_path / "ws"
    marker = paths.final_report_commit_marker_path(ws)
    # Capture the prior marker payload after pre-seeding inside the driver.
    seeded_payload = {}

    def pre_seed_and_capture(ws_):
        pre_seed(ws_)
        seeded_payload["text"] = marker.read_text()

    _drive_final_reporter(
        tmp_path, monkeypatch, fake, pre_seed=pre_seed_and_capture,
    )
    assert max(fake.stages_seen) >= 1
    assert paths.final_report_path(ws).read_text() == PLAUSIBLE
    assert marker.read_text() == seeded_payload["text"]
