"""Deterministic workspace artifact routing for long-exposure.

All reserved harness paths should be derived here so writers, readers,
validators, and prompts do not drift.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


# Final-stage invariant set (DO NOT MOVE without coordinated updates):
#   reports/final/final_report.md
#   reports/final/final_report.pdf
#   reports/final/final_report.committed
#   audits/final/final_audit_report.md
#   audits/final/final_audit_report.pdf
#   audits/final/final_audit_report.committed
#   audits/final/final_audit_summary.json
# Delta-mode detection, final-stage file gates, PDF rendering, and curator
# package collection should derive paths from this module rather than
# hardcoding workspace-root filenames.


def workspace_root(config_or_workspace) -> Path:
    """Return the effective workspace root for a config dict or Path-like."""
    if isinstance(config_or_workspace, dict):
        value = config_or_workspace.get("working_directory") or "."
        return Path(value).expanduser().resolve()
    return Path(config_or_workspace or ".").expanduser().resolve()


def reports_dir(config_or_workspace) -> Path:
    return workspace_root(config_or_workspace) / "reports"


def cycle_reports_dir(config_or_workspace) -> Path:
    return reports_dir(config_or_workspace) / "cycles"


def cycle_report_md(config_or_workspace, basename: str) -> Path:
    """Route a caller-supplied report basename into the cycle report tree."""
    return cycle_reports_dir(config_or_workspace) / f"{basename}.md"


def cycle_report_pdf(config_or_workspace, basename: str) -> Path:
    return cycle_reports_dir(config_or_workspace) / f"{basename}.pdf"


def fanout_cycle_dir(config_or_workspace, cycle: int) -> Path:
    return cycle_reports_dir(config_or_workspace) / f"cycle{cycle}"


def cycle_report_globs(config_or_workspace) -> tuple[str, ...]:
    """Workspace-relative report globs, newest layout first, legacy included."""
    return (
        "reports/cycles/report_cycles_*.md",
        "reports/cycles/cycle*/*.md",
        "reports/cycle*/*.md",
        "reports/report_cycles_*.md",
        "report_cycles_*.md",
    )


def cycle_reports_glob(config_or_workspace) -> str:
    return " OR ".join(cycle_report_globs(config_or_workspace))


def iter_cycle_report_paths(config_or_workspace) -> Iterable[Path]:
    root = workspace_root(config_or_workspace)
    seen: set[str] = set()
    for pattern in cycle_report_globs(root):
        for path in sorted(root.glob(pattern)):
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen:
                continue
            seen.add(key)
            yield path


def final_report_scratch_dir(config_or_workspace) -> Path:
    return reports_dir(config_or_workspace) / "final"


def final_report_outline_path(config_or_workspace) -> Path:
    return final_report_scratch_dir(config_or_workspace) / "outline.md"


def final_report_draft_path(config_or_workspace) -> Path:
    return final_report_scratch_dir(config_or_workspace) / "draft.md"


def final_report_run_mode_path(config_or_workspace) -> Path:
    return final_report_scratch_dir(config_or_workspace) / "run_mode.json"


def final_report_path(config_or_workspace) -> Path:
    return final_report_scratch_dir(config_or_workspace) / "final_report.md"


def final_report_pdf_path(config_or_workspace) -> Path:
    return final_report_scratch_dir(config_or_workspace) / "final_report.pdf"


def final_report_commit_marker_path(config_or_workspace) -> Path:
    return final_report_scratch_dir(config_or_workspace) / "final_report.committed"


def audits_dir(config_or_workspace) -> Path:
    return workspace_root(config_or_workspace) / "audits"


def final_audit_dir(config_or_workspace) -> Path:
    return audits_dir(config_or_workspace) / "final"


def final_audit_stages_dir(config_or_workspace) -> Path:
    return final_audit_dir(config_or_workspace) / "stages"


def final_audit_explore_path(config_or_workspace) -> Path:
    return final_audit_dir(config_or_workspace) / "explore.md"


def final_audit_stage_path(config_or_workspace, stage_label: str) -> Path:
    safe = stage_label.replace("(", "").replace(")", "").replace(" ", "_").replace("/", "of")
    return final_audit_stages_dir(config_or_workspace) / f"{safe}.md"


def findings_path(config_or_workspace) -> Path:
    return final_audit_dir(config_or_workspace) / "findings.jsonl"


def lessons_path(config_or_workspace) -> Path:
    return final_audit_dir(config_or_workspace) / "lessons.jsonl"


def final_audit_run_mode_path(config_or_workspace) -> Path:
    return final_audit_dir(config_or_workspace) / "run_mode.json"


def final_audit_report_path(config_or_workspace) -> Path:
    return final_audit_dir(config_or_workspace) / "final_audit_report.md"


def final_audit_pdf_path(config_or_workspace) -> Path:
    return final_audit_dir(config_or_workspace) / "final_audit_report.pdf"


def final_audit_commit_marker_path(config_or_workspace) -> Path:
    return final_audit_dir(config_or_workspace) / "final_audit_report.committed"


def final_audit_summary_path(config_or_workspace) -> Path:
    return final_audit_dir(config_or_workspace) / "final_audit_summary.json"


def ensure_layout(config_or_workspace) -> None:
    """Create managed routing directories; safe on fresh and resumed runs."""
    for directory in (
        reports_dir(config_or_workspace),
        cycle_reports_dir(config_or_workspace),
        final_report_scratch_dir(config_or_workspace),
        audits_dir(config_or_workspace),
        final_audit_dir(config_or_workspace),
        final_audit_stages_dir(config_or_workspace),
    ):
        directory.mkdir(parents=True, exist_ok=True)
