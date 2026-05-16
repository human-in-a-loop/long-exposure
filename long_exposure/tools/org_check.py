#!/usr/bin/env python3
"""org_check — verify workspace organization against STRUCTURE.md conventions.

Stdlib-only. Sibling validator to promise_check. See docs/workspace-conventions.md.

Exit codes:
  0  — green
  1  — integrity violations (errors)
  2  — bad invocation

Usage:
    python -m long_exposure.tools.org_check /path/to/workspace
    python -m long_exposure.tools.org_check /path/to/workspace --json

Design principle (matching the plans): SURFACE, never enforce. No automatic
moves. The auditor incorporates findings into audit_report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


STANDARD_FOLDERS = (
    "reports",
    "audits",
    "scripts",
    "tests",
    "data",
    "docs",
    "tools",
    "stale",
)

ALLOWED_AT_ROOT_FILES = {
    # Plan + organization artifacts
    "plan_of_record.md",
    "promise_ledger.jsonl",
    "STRUCTURE.md",
    "MANIFEST.md",
    "LESSONS.md",
    "REFERENCES.md",
    # Project / packaging defaults
    "README.md",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "uv.lock",
    "requirements.txt",
    ".gitignore",
}

LEGACY_ROOT_STAGE_PATTERNS = (
    "final_report.md",
    "final_report.pdf",
    "final_report.committed",
    "final_audit_report.md",
    "final_audit_report.pdf",
    "final_audit_report.committed",
    "final_audit_summary.json",
    "final_report_outline.md",
    "final_report_draft.md",
    "final_audit_explore.md",
    "final_audit_findings.jsonl",
    "final_audit_lessons.jsonl",
)

ALLOWED_AT_ROOT_DIRS = {
    *STANDARD_FOLDERS,
    ".venv",
    "venv",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
}

# Filenames that suggest periodic reports — they should live in reports/cycles/.
# `final_report*` and `final_audit_report*` are NOT in this set: canonical
# final-stage outputs live under reports/final/ and audits/final/.
REPORT_FILENAME_PATTERNS = ("report_cycles_",)

# Suffixes that suggest scripts/data — they should not live at root.
SCRIPT_SUFFIXES = (".py", ".sh", ".wls", ".m", ".java")
LARGE_BINARY_SUFFIXES = (".mph", ".bin", ".dat", ".npz", ".h5", ".hdf5", ".mat")
# Plan 06 §4.8: figures should land co-located with the script + data that
# produced them (in domain folders or scripts/ / data/), NOT at workspace
# root and NOT under tools/ or docs/. Image suffixes that should trigger an
# orphan-figure warning when found in unconventional locations. PDFs are
# excluded — final_report.pdf and final_audit_report.pdf legitimately live
# under managed final-stage folders.
IMAGE_SUFFIXES = (".png", ".svg", ".jpg", ".jpeg", ".gif")


class Findings:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)


def _is_domain_folder(name: str) -> bool:
    """Heuristic: 'benchmark-XX-...', 'subproject-...', etc. are domain folders.

    A directory is a domain folder if it's not in ALLOWED_AT_ROOT_DIRS, doesn't
    start with a dot, and contains at least one hyphen or underscore (suggesting
    a multi-word name) — or is otherwise a non-trivial named subdirectory.
    """
    if name in ALLOWED_AT_ROOT_DIRS:
        return False
    if name.startswith("."):
        return False
    return True  # treat any non-system root dir as domain-specific


def _scan(workspace: Path) -> tuple[list[Path], list[Path]]:
    """Return (root files, root directories), excluding hidden top-level entries."""
    root_files: list[Path] = []
    root_dirs: list[Path] = []
    try:
        for entry in sorted(workspace.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_file():
                root_files.append(entry)
            elif entry.is_dir():
                root_dirs.append(entry)
    except OSError:
        pass
    return root_files, root_dirs


def run(workspace: Path) -> Findings:
    findings = Findings()
    structure_path = workspace / "STRUCTURE.md"
    if not structure_path.exists():
        findings.warn("STRUCTURE.md missing — workspace not bootstrapped")

    root_files, root_dirs = _scan(workspace)

    # Standard folder presence (warning-level — graceful absence is OK)
    present = {d.name for d in root_dirs}
    missing = [f for f in STANDARD_FOLDERS if f not in present]
    if missing:
        findings.note(f"standard folders missing: {missing}")

    # Files at root that aren't in the allowed set
    for f in root_files:
        if f.name in ALLOWED_AT_ROOT_FILES:
            continue
        if (
            f.name in LEGACY_ROOT_STAGE_PATTERNS
            or f.name.startswith("final_audit_verify_")
            or f.name.startswith("final_audit_test_")
        ):
            findings.note(
                f"legacy root stage artifact: {f.name} "
                "(new runs write final-stage artifacts under reports/final/ "
                "or audits/final/)"
            )
            continue
        # Reports at root → should be in reports/
        if any(f.name.startswith(p) for p in REPORT_FILENAME_PATTERNS):
            findings.err(
                f"report at workspace root: {f.name} (should live in reports/cycles/)"
            )
            continue
        # Scripts at root
        if f.suffix in SCRIPT_SUFFIXES:
            findings.warn(
                f"script at workspace root: {f.name} (should live in scripts/, "
                f"tools/, or a domain folder)"
            )
            continue
        # Large binaries at root
        if f.suffix in LARGE_BINARY_SUFFIXES:
            findings.warn(
                f"binary at workspace root: {f.name} (should live in data/ or a "
                f"domain folder)"
            )
            continue
        # Plan 06 §4.8: figures at root → should be co-located with their
        # source script/data in a domain folder or under scripts/.
        if f.suffix.lower() in IMAGE_SUFFIXES:
            findings.warn(
                f"figure at workspace root: {f.name} (figures should be "
                f"co-located with the script + data that produced them, "
                f"in scripts/, data/, or a domain folder)"
            )
            continue
        # Anything else at root that isn't whitelisted
        findings.warn(f"file at workspace root not in allowed-set: {f.name}")

    # Plan 06 §4.8: figures under tools/ or docs/ are unconventional — those
    # folders are for harness validators and methodology notes, not data
    # outputs. Walk both folders and warn on any image-suffix file.
    for folder_name in ("tools", "docs"):
        folder = workspace / folder_name
        if not folder.exists():
            continue
        for img in folder.rglob("*"):
            if img.is_file() and img.suffix.lower() in IMAGE_SUFFIXES:
                findings.warn(
                    f"figure in {folder_name}/: "
                    f"{img.relative_to(workspace).as_posix()} "
                    f"(figures should be co-located with their source "
                    f"script + data, not under {folder_name}/)"
                )

    # Reports outside reports/ — checked recursively in standard folders only
    docs_dir = workspace / "docs"
    if docs_dir.exists():
        for p in docs_dir.rglob("report_cycles_*.md"):
            findings.warn(
                f"periodic report under docs/: {p.relative_to(workspace).as_posix()} "
                f"(future runs should write to reports/cycles/)"
            )
    # final_report under docs/ is a misplacement.
    for p in (workspace / "docs").rglob("final_report.md") if (workspace / "docs").exists() else []:
        findings.warn(f"final_report.md under docs/: {p.relative_to(workspace).as_posix()}")

    findings.note(
        f"root files: {len(root_files)}, root dirs: {len(root_dirs)}; "
        f"standard folders present: {sorted(present & set(STANDARD_FOLDERS))}"
    )
    return findings


def format_text(findings: Findings) -> str:
    out: list[str] = []
    for n in findings.notes:
        out.append(f"  {n}")
    for w in findings.warnings:
        out.append(f"! WARNING: {w}")
    for e in findings.errors:
        out.append(f"x ERROR:   {e}")
    if not findings.errors and not findings.warnings:
        out.append("OK: org_check green.")
    return "\n".join(out) + "\n"


def format_json(findings: Findings) -> str:
    return json.dumps(
        {
            "errors": findings.errors,
            "warnings": findings.warnings,
            "notes": findings.notes,
            "ok": not findings.errors,
        },
        indent=2,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify workspace organization against STRUCTURE.md conventions."
    )
    parser.add_argument("workspace", help="Workspace root.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args(argv)

    ws = Path(args.workspace).resolve()
    if not ws.is_dir():
        print(f"org_check: not a directory: {ws}", file=sys.stderr)
        return 2

    findings = run(ws)
    if args.json:
        print(format_json(findings))
    else:
        print(format_text(findings))

    return 1 if findings.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
