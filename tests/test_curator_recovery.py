"""Regression tests for curator CURATION.yaml recovery.

Covers the failure where the curator agent produced a valid CURATION.yaml but
wrote it into a project subdirectory (its effective cwd) instead of the
workspace root, so the packager saw "no manifest" and shipped the report-only
safety package — discarding the curator's work.

  1. ``_locate_curation_file`` prefers the canonical workspace-root path, but
     recovers a manifest misplaced into a subdirectory, while ignoring
     audit-trail copies (under ``report/``) and packaging staging scratch.
  2. ``_create_package_zip`` builds a real (non-fallback) package from a
     recovered manifest, resolving each ``src`` against the workspace root and,
     as a fallback, against the directory the manifest was found in.
  3. With no CURATION.yaml anywhere, the minimal safety fallback still applies.
"""

import os
import time
import zipfile

import yaml

from long_exposure.curator import (
    _locate_curation_file,
    _create_package_zip,
    _is_package_hard_excluded,
)


def test_finals_not_hard_excluded_scratch_is():
    # The canonical deliverables under reports/final/ must ship.
    assert not _is_package_hard_excluded("reports/final/final_report.md")
    assert not _is_package_hard_excluded("reports/final/final_report.pdf")
    # Scratch artifacts under reports/final/ must be dropped.
    assert _is_package_hard_excluded("reports/final/outline.md")
    assert _is_package_hard_excluded("reports/final/draft.md")
    assert _is_package_hard_excluded("reports/final/run_mode.json")
    assert _is_package_hard_excluded("reports/final/final_report.committed")


def _read_effective_curation(zip_path):
    """Return the audit-trail CURATION.yaml dict embedded in a package zip."""
    with zipfile.ZipFile(zip_path) as zf:
        name = next(n for n in zf.namelist() if n.endswith("report/CURATION.yaml"))
        return yaml.safe_load(zf.read(name))


def test_locate_prefers_canonical(tmp_path):
    (tmp_path / "CURATION.yaml").write_text("include: []\n")
    sub = tmp_path / "proj"
    sub.mkdir()
    (sub / "CURATION.yaml").write_text("include: []\n")
    assert _locate_curation_file(tmp_path) == tmp_path / "CURATION.yaml"


def test_locate_recovers_misplaced(tmp_path):
    sub = tmp_path / "proj"
    sub.mkdir()
    misplaced = sub / "CURATION.yaml"
    misplaced.write_text("include: []\n")
    assert _locate_curation_file(tmp_path) == misplaced


def test_locate_ignores_audit_trail_and_staging(tmp_path):
    # Audit-trail copy inside a report/ subtree (e.g. an extracted prior pkg).
    audit = tmp_path / "old_pkg" / "report"
    audit.mkdir(parents=True)
    (audit / "CURATION.yaml").write_text("include: []\n")
    # Packaging staging scratch.
    staging = tmp_path / ".package_staging_deadbeef" / "pkg" / "report"
    staging.mkdir(parents=True)
    (staging / "CURATION.yaml").write_text("include: []\n")
    assert _locate_curation_file(tmp_path) is None


def test_locate_none_when_absent(tmp_path):
    assert _locate_curation_file(tmp_path) is None


def test_locate_picks_most_recent(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    old = a / "CURATION.yaml"
    new = b / "CURATION.yaml"
    old.write_text("include: []\n")
    new.write_text("include: []\n")
    import os
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    assert _locate_curation_file(tmp_path) == new


def test_package_built_from_misplaced_manifest(tmp_path):
    """A manifest in a subdir with workspace-relative src yields a real pkg."""
    (tmp_path / "reports" / "final").mkdir(parents=True)
    (tmp_path / "reports" / "final" / "final_report.md").write_text("# report\n")
    code_dir = tmp_path / "proj" / "src"
    code_dir.mkdir(parents=True)
    (code_dir / "model.py").write_text("print('x')\n")

    manifest = {
        "package_name": "demo",
        "curation_complete": True,
        "include": [
            {"src": "reports/final/final_report.md", "dest": "report/final_report.md", "role": "report"},
            {"src": "proj/src/model.py", "dest": "code/model.py", "role": "code",
             "justification": "produces §3 results"},
        ],
    }
    # Written into the subdir, NOT the workspace root.
    (tmp_path / "proj" / "CURATION.yaml").write_text(yaml.safe_dump(manifest))

    zip_name = _create_package_zip(tmp_path, "demo task")
    assert zip_name is not None
    eff = _read_effective_curation(tmp_path / zip_name)
    assert eff["fallback_used"] is False
    dests = {e["dest"] for e in eff["include"]}
    assert "report/final_report.md" in dests
    assert "code/model.py" in dests


def test_src_resolved_against_manifest_dir(tmp_path):
    """A subdir manifest with subdir-relative src resolves via curation_base."""
    (tmp_path / "reports" / "final").mkdir(parents=True)
    (tmp_path / "reports" / "final" / "final_report.md").write_text("# report\n")
    sub = tmp_path / "proj"
    (sub / "data").mkdir(parents=True)
    (sub / "data" / "result.csv").write_text("a,b\n1,2\n")

    manifest = {
        "package_name": "demo",
        "curation_complete": True,
        "include": [
            # workspace-relative (resolves via working_dir)
            {"src": "reports/final/final_report.md", "dest": "report/final_report.md", "role": "report"},
            # subdir-relative (only resolvable via the manifest's directory)
            {"src": "data/result.csv", "dest": "data/result.csv", "role": "data",
             "justification": "raw results"},
        ],
    }
    (sub / "CURATION.yaml").write_text(yaml.safe_dump(manifest))

    zip_name = _create_package_zip(tmp_path, "demo task")
    assert zip_name is not None
    eff = _read_effective_curation(tmp_path / zip_name)
    dests = {e["dest"] for e in eff["include"]}
    assert "data/result.csv" in dests
    assert not eff["missing"]


def test_no_manifest_falls_back_to_safety(tmp_path):
    """No CURATION.yaml anywhere and no report docs → nothing to package."""
    zip_name = _create_package_zip(tmp_path, "demo task")
    # No report files exist either, so the safety curation is empty → None.
    assert zip_name is None


# ---------------------------------------------------------------------------
# Recency-bounded recovery (not_before freshness floor)
# ---------------------------------------------------------------------------


def test_locate_recency_floor_ignores_stale(tmp_path):
    sub = tmp_path / "proj"
    sub.mkdir()
    stale = sub / "CURATION.yaml"
    stale.write_text("include: []\n")
    os.utime(stale, (1_000_000, 1_000_000))
    # Without a floor the stale manifest is recovered (backward-compat).
    assert _locate_curation_file(tmp_path) == stale
    # With a floor newer than its mtime, it is ignored.
    assert _locate_curation_file(tmp_path, not_before=2_000_000.0) is None


def test_locate_stale_shallow_does_not_shadow_fresh_deeper(tmp_path):
    shallow = tmp_path / "a"
    shallow.mkdir()
    stale = shallow / "CURATION.yaml"
    stale.write_text("include: []\n")
    os.utime(stale, (1_000_000, 1_000_000))
    deep_dir = tmp_path / "b" / "proj"
    deep_dir.mkdir(parents=True)
    fresh = deep_dir / "CURATION.yaml"
    fresh.write_text("include: []\n")
    # Floor sits between the two mtimes: the stale depth-1 hit must not
    # shadow the fresh depth-2 manifest.
    assert _locate_curation_file(tmp_path, not_before=2_000_000.0) == fresh


def test_locate_canonical_root_exempt_from_floor(tmp_path):
    canonical = tmp_path / "CURATION.yaml"
    canonical.write_text("include: []\n")
    os.utime(canonical, (1_000_000, 1_000_000))
    assert _locate_curation_file(tmp_path, not_before=time.time()) == canonical


def test_stale_manifest_falls_back_to_safety_package(tmp_path):
    """A recovered manifest older than the floor must not ship."""
    (tmp_path / "reports" / "final").mkdir(parents=True)
    (tmp_path / "reports" / "final" / "final_report.md").write_text("# report\n")
    sub = tmp_path / "proj"
    sub.mkdir()
    (sub / "old.py").write_text("print('stale')\n")
    manifest = {
        "package_name": "stale_pkg",
        "curation_complete": True,
        "include": [
            {"src": "proj/old.py", "dest": "code/old.py", "role": "code"},
        ],
    }
    stale = sub / "CURATION.yaml"
    stale.write_text(yaml.safe_dump(manifest))
    os.utime(stale, (1_000_000, 1_000_000))

    zip_name = _create_package_zip(tmp_path, "demo task", not_before=time.time())
    assert zip_name is not None
    eff = _read_effective_curation(tmp_path / zip_name)
    assert eff["fallback_used"] is True
    dests = {e["dest"] for e in eff["include"]}
    assert "code/old.py" not in dests
    assert "report/final_report.md" in dests


# ---------------------------------------------------------------------------
# audits/ hard-exclude narrowing
# ---------------------------------------------------------------------------


def test_audit_deliverables_not_hard_excluded_scratch_is():
    # Canonical audit deliverables must ship from agent manifests.
    assert not _is_package_hard_excluded("audits/final/final_audit_report.md")
    assert not _is_package_hard_excluded("audits/final/final_audit_report.pdf")
    assert not _is_package_hard_excluded("audits/final/final_audit_summary.json")
    # Audit scratch must be dropped.
    assert _is_package_hard_excluded("audits/final/explore.md")
    assert _is_package_hard_excluded("audits/final/stages/verify-1-1.md")
    assert _is_package_hard_excluded("audits/final/findings.jsonl")
    assert _is_package_hard_excluded("audits/final/lessons.jsonl")
    assert _is_package_hard_excluded("audits/final/run_mode.json")
    assert _is_package_hard_excluded("audits/final/final_audit_report.committed")
    # Non-final audit subtrees stay excluded wholesale.
    assert _is_package_hard_excluded("audits/cycles/audit_cycle_7.md")
    assert _is_package_hard_excluded("audits")


def test_package_ships_audit_deliverables_from_manifest(tmp_path):
    audit_dir = tmp_path / "audits" / "final"
    audit_dir.mkdir(parents=True)
    (audit_dir / "final_audit_report.md").write_text("# audit\n")
    (audit_dir / "findings.jsonl").write_text('{"x":1}\n')
    manifest = {
        "package_name": "demo",
        "curation_complete": True,
        "include": [
            {"src": "audits/final/final_audit_report.md",
             "dest": "report/final_audit_report.md", "role": "report"},
            {"src": "audits/final/findings.jsonl",
             "dest": "report/findings.jsonl", "role": "report"},
        ],
    }
    (tmp_path / "CURATION.yaml").write_text(yaml.safe_dump(manifest))

    zip_name = _create_package_zip(tmp_path, "demo task")
    assert zip_name is not None
    eff = _read_effective_curation(tmp_path / zip_name)
    assert eff["fallback_used"] is False
    dests = {e["dest"] for e in eff["include"]}
    assert "report/final_audit_report.md" in dests
    assert "report/findings.jsonl" not in dests  # scratch still excluded


# ---------------------------------------------------------------------------
# Symlink containment + dest-collision tolerance
# ---------------------------------------------------------------------------


def test_symlink_escape_counts_as_missing(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ok.py").write_text("print('ok')\n")
    (ws / "leak.txt").symlink_to(outside)
    manifest = {
        "package_name": "demo",
        "curation_complete": True,
        "include": [
            {"src": "ok.py", "dest": "code/ok.py", "role": "code"},
            {"src": "leak.txt", "dest": "data/leak.txt", "role": "data"},
        ],
    }
    (ws / "CURATION.yaml").write_text(yaml.safe_dump(manifest))

    zip_name = _create_package_zip(ws, "demo task")
    assert zip_name is not None
    eff = _read_effective_curation(ws / zip_name)
    dests = {e["dest"] for e in eff["include"]}
    assert "code/ok.py" in dests
    assert "data/leak.txt" not in dests
    assert "leak.txt" in eff["missing"]


def test_internal_symlink_still_ships(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    real = ws / "real.csv"
    real.write_text("a,b\n")
    (ws / "alias.csv").symlink_to(real)
    manifest = {
        "package_name": "demo",
        "curation_complete": True,
        "include": [
            {"src": "alias.csv", "dest": "data/alias.csv", "role": "data"},
        ],
    }
    (ws / "CURATION.yaml").write_text(yaml.safe_dump(manifest))

    zip_name = _create_package_zip(ws, "demo task")
    assert zip_name is not None
    eff = _read_effective_curation(ws / zip_name)
    assert {e["dest"] for e in eff["include"]} == {"data/alias.csv"}


def test_dest_collision_skips_entry_not_package(tmp_path):
    (tmp_path / "a.py").write_text("a\n")
    (tmp_path / "b.py").write_text("b\n")
    manifest = {
        "package_name": "demo",
        "curation_complete": True,
        "include": [
            # First entry creates a FILE at code/tool …
            {"src": "a.py", "dest": "code/tool", "role": "code"},
            # … second entry needs code/tool/ as a DIRECTORY → OSError.
            {"src": "b.py", "dest": "code/tool/inner.py", "role": "code"},
        ],
    }
    (tmp_path / "CURATION.yaml").write_text(yaml.safe_dump(manifest))

    zip_name = _create_package_zip(tmp_path, "demo task")
    assert zip_name is not None  # the package still ships
    eff = _read_effective_curation(tmp_path / zip_name)
    dests = {e["dest"] for e in eff["include"]}
    assert "code/tool" in dests
    assert "code/tool/inner.py" not in dests
    assert "b.py" in eff["missing"]
