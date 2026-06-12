"""Curator: single-stage agent + deterministic curated-zip packager.

Extracted verbatim from long_exposure.exploration. The body of this module is
byte-identical to the prior in-line section (lines 2745-3244 of the
original exploration.py); only the import header differs. Helper
functions used by the curator (account-rotation wrapper, output
storage, context-token math) live in long_exposure.exploration and are
imported below.
"""

from __future__ import annotations

import json
import re as _re
import shutil
import time as _time
import uuid
import zipfile
from pathlib import Path

import yaml

from long_exposure import paths


# Lazy-import delegators for names defined in long_exposure.exploration.
#
# Importing these at module-load time creates a circular import, because
# exploration.py re-exports this module near its tail. The prior design used
# a PEP-562 module `__getattr__` for lazy resolution — but that only handles
# *external* attribute access (`curator._call_agent_with_rotation`), NOT
# bare-name `LOAD_GLOBAL` lookups from inside functions defined here.
# Python's global-name resolution consults the module `__dict__` and
# builtins only; it never invokes `__getattr__`. Every bare-name reference
# below would have raised NameError on first execution of `_run_curator`.
# The fix: real module-level wrapper functions. Each does the lazy import
# at call time and delegates. `sys.modules` caches the resolved module, so
# overhead is one dict lookup per call.
def _call_agent_with_rotation(*args, **kwargs):
    from long_exposure.exploration import _call_agent_with_rotation as _impl
    return _impl(*args, **kwargs)


def _store_agent_output(*args, **kwargs):
    from long_exposure.exploration import _store_agent_output as _impl
    return _impl(*args, **kwargs)


def _total_context_tokens(*args, **kwargs):
    from long_exposure.exploration import _total_context_tokens as _impl
    return _impl(*args, **kwargs)


# Files never included regardless of what CURATION.yaml says.
# These are either process artifacts (per-cycle reports, intermediate drafts),
# infrastructure state (sessions.db, signal files), or previous packaging.
_PACKAGE_HARD_EXCLUDE_NAMES = {
    "sessions.db",
    "exploration.log",
    "exploration_state.json",
    # New (post-rename) signal-file basenames.
    "long-exposure.stop",
    "long-exposure.clear",
    "long-exposure.guide",
    "long-exposure.pause-for-user",
    "manager.lock",
    # Legacy names retained one release for defense-in-depth: workspaces
    # mid-transition may still carry these.
    "exploration.stop",
    "exploration.clear",
    "exploration.guide",
    "SKILL.md",
    "final_report_outline.md",
    "final_report_draft.md",
}
_PACKAGE_HARD_EXCLUDE_SUFFIXES = {".tmp", ".zip", ".db", ".log"}
_PACKAGE_HARD_EXCLUDE_PATTERNS = [
    _re.compile(r"^report_cycles_[^/]+\.md$"),
    _re.compile(r"^report_cycles_[^/]+\.pdf$"),
    # reports/final/ holds BOTH the canonical deliverables (final_report.md,
    # final_report.pdf) and scratch artifacts (outline.md, draft.md,
    # run_mode.json, the .committed marker). Exclude only the scratch — never
    # the finals, which are the package's primary documents. A broader
    # `reports/final/` exclude silently dropped the final report from every
    # non-fallback curated package.
    _re.compile(r"(^|/)reports/final/(outline\.md|draft\.md|run_mode\.json|final_report\.committed)$"),
    # audits/final/ holds BOTH the canonical audit deliverables
    # (final_audit_report.md, final_audit_report.pdf, final_audit_summary.json)
    # and scratch artifacts (explore.md, stages/, findings.jsonl,
    # lessons.jsonl, run_mode.json, the .committed marker). Exclude only the
    # scratch — same bug class as the reports/final fix above: a broad
    # `audits/` exclude silently dropped the audit deliverables from every
    # agent-curated package while the legacy bare-filename spelling shipped.
    _re.compile(
        r"(^|/)audits/final/"
        r"(stages(/|$)|explore\.md$|findings\.jsonl$|lessons\.jsonl$|"
        r"run_mode\.json$|final_audit_report\.committed$)"
    ),
    # Anything under audits/ OUTSIDE final/ stays excluded (cycle-audit
    # scratch and legacy layouts), as does the bare directory itself.
    _re.compile(r"(^|/)audits/(?!final(/|$))"),
    _re.compile(r"(^|/)audits$"),
    _re.compile(r"(^|/)manager_assessments(/|$)"),
]


def _is_package_hard_excluded(rel_path: str) -> bool:
    """True if a workspace-relative path must not ship in the package.

    Enforced in code so agent hallucinations or stale CURATION entries
    can't sneak process artifacts into the user-facing package.
    """
    name = Path(rel_path).name
    if name in _PACKAGE_HARD_EXCLUDE_NAMES:
        return True
    if Path(rel_path).suffix in _PACKAGE_HARD_EXCLUDE_SUFFIXES:
        return True
    for pat in _PACKAGE_HARD_EXCLUDE_PATTERNS:
        if pat.match(name) or pat.search(rel_path):
            return True
    return False


def _package_slug(text: str) -> str:
    """snake_case ASCII slug, max 40 chars. Empty → 'exploration'."""
    s = _re.sub(r"[^a-z0-9]+", "_", text.strip().lower()[:60]).strip("_")[:40]
    return s or "exploration"


def _parse_curation_manifest(path: Path) -> dict | None:
    """Parse CURATION.yaml and sanitize it. Returns None on unrecoverable failure.

    Sanitization:
    - Entries missing src or dest → dropped.
    - Entries whose src would traverse out of the workspace → dropped.
    - Entries whose src is in the hard-exclude list → dropped (with log).
    - Roles normalized to {report, code, test, data}; unknowns → code.
    - Duplicates by dest → first wins.
    """
    if not path.is_file():
        return None
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        print(f"[long-exposure] CURATION.yaml parse error: {e}", flush=True)
        return None
    if not isinstance(raw, dict):
        print("[long-exposure] CURATION.yaml: top-level must be a mapping.", flush=True)
        return None
    include = raw.get("include")
    if not isinstance(include, list):
        print("[long-exposure] CURATION.yaml: 'include' must be a list.", flush=True)
        return None

    # Plan 06 §4.5: figures are first-class artifacts. The curator may tag
    # entries with role:"figure" so the package README renders a dedicated
    # figures section; the file lands under figures/<dest> in the bundle.
    # Unknown roles still fall back to "code" rather than getting dropped.
    allowed_roles = {"report", "code", "test", "data", "figure"}
    cleaned: list[dict] = []
    seen_dest: set[str] = set()
    for entry in include:
        if not isinstance(entry, dict):
            continue
        src = entry.get("src")
        dest = entry.get("dest")
        if not isinstance(src, str) or not isinstance(dest, str):
            continue
        src_norm = src.strip().lstrip("./").lstrip("/")
        dest_norm = dest.strip().lstrip("./").lstrip("/")
        if not src_norm or not dest_norm:
            continue
        if ".." in Path(src_norm).parts or ".." in Path(dest_norm).parts:
            continue
        if _is_package_hard_excluded(src_norm):
            print(
                f"[long-exposure] CURATION: hard-excluded entry dropped: {src}",
                flush=True,
            )
            continue
        if dest_norm in seen_dest:
            continue
        role = entry.get("role", "code")
        if role not in allowed_roles:
            role = "code"
        # Plan 06 §4.5: figures stage under figures/ in the bundle. Normalize
        # so agents that wrote a bare filename (e.g. "fig1.png") still land
        # under figures/fig1.png — the per-role staging convention is
        # enforced here, not pushed back onto the agent.
        if role == "figure" and not dest_norm.startswith("figures/"):
            dest_norm = f"figures/{Path(dest_norm).name}"
        if dest_norm in seen_dest:
            continue
        seen_dest.add(dest_norm)
        item = {"src": src_norm, "dest": dest_norm, "role": role}
        just = entry.get("justification")
        if isinstance(just, str) and just.strip():
            item["justification"] = just.strip()
        # Plan 06 §4.5: figures may carry a `caption` field — preserved when
        # provided so the package README can render it next to the figure.
        caption = entry.get("caption")
        if isinstance(caption, str) and caption.strip():
            item["caption"] = caption.strip()
        cleaned.append(item)

    pkg_name_raw = raw.get("package_name")
    desc_raw = raw.get("description")
    return {
        "package_name": pkg_name_raw if isinstance(pkg_name_raw, str) else None,
        "description": desc_raw.strip() if isinstance(desc_raw, str) else "",
        "curation_complete": bool(raw.get("curation_complete", False)),
        "include": cleaned,
    }


def _minimal_safety_curation(working_dir: Path, task: str) -> dict:
    """Fallback curation when CURATION.yaml is missing or unparseable.

    Ships only the report documents. Never falls back to zipping the
    workspace — a tight minimal package beats a junk drawer.
    """
    include: list[dict] = []
    canonical = {
        "final_report.md": paths.final_report_path(working_dir),
        "final_report.pdf": paths.final_report_pdf_path(working_dir),
        "final_audit_report.md": paths.final_audit_report_path(working_dir),
        "final_audit_report.pdf": paths.final_audit_pdf_path(working_dir),
        "final_audit_summary.json": paths.final_audit_summary_path(working_dir),
        "MANIFEST.md": working_dir / "MANIFEST.md",
        "REFERENCES.md": working_dir / "REFERENCES.md",
    }
    for name, path in canonical.items():
        if path.is_file():
            include.append({
                "src": path.relative_to(working_dir).as_posix(),
                "dest": f"report/{name}",
                "role": "report",
            })
    return {
        "package_name": _package_slug(task),
        "description": (
            "Minimal safety package. The curator agent did not produce a "
            "usable CURATION.yaml, so only the final report documents are "
            "included. The full workspace is preserved in sessions.db."
        ),
        "curation_complete": False,
        "include": include,
    }


def _render_package_readme(curation: dict, task: str) -> str:
    """Build the package README.md deterministically from the curation manifest."""
    pkg = curation.get("package_name") or _package_slug(task)
    desc = (curation.get("description") or "").strip()
    complete = bool(curation.get("curation_complete", False))
    by_role: dict[str, list[dict]] = {}
    for entry in curation.get("include", []):
        by_role.setdefault(entry["role"], []).append(entry)

    lines: list[str] = [f"# {pkg}", ""]
    if desc:
        lines += [desc, ""]
    lines += [f"_Exploration directive:_ {task.strip()}", ""]
    if not complete:
        lines += [
            "> **Note.** This package was produced with `curation_complete: "
            "false`. Either the final reporter did not author a "
            "`## Key Files` section in MANIFEST.md, or the curator was "
            "unable to map every Key File to the workspace. The report "
            "documents above are authoritative; the full workspace is "
            "preserved in `sessions.db`.",
            "",
        ]
    lines += ["## Contents", ""]
    # Plan 06: figures are listed in their own section with captions when
    # available, so an operator can browse the trust-calibration probe set
    # without reading the report first.
    for role, title in [
        ("report", "Report documents"),
        ("figure", "Figures (interrogation primitives)"),
        ("code", "Code (results-producing scripts)"),
        ("test", "Tests (validation scripts)"),
        ("data", "Data"),
    ]:
        entries = by_role.get(role)
        if not entries:
            continue
        lines += [f"### {title}", ""]
        for entry in sorted(entries, key=lambda e: e["dest"]):
            just = entry.get("justification", "").strip()
            caption = entry.get("caption", "").strip()
            row = f"- `{entry['dest']}`"
            # For figures, prefer caption ahead of justification (caption is
            # the data-substantive label; justification is the curation rationale).
            label = caption or just
            if label:
                row += f" — {label}"
            lines.append(row)
        lines.append("")
    lines += [
        "## How to verify the work",
        "",
        "1. Open `report/final_report.pdf` (or `final_report.md`) for the "
        "synthesized findings.",
        "2. Cross-reference specific claims to the bibliography in "
        "`report/REFERENCES.md`.",
        "3. Each code/test entry above has a justification that links it "
        "to the report section it supports. Run the listed scripts to "
        "reproduce the cited results.",
        "4. `report/CURATION.yaml` is the machine-readable inventory "
        "used to build this package (audit trail).",
        "",
    ]
    return "\n".join(lines)


def _canonical_final_src(working_dir: Path, src: str) -> Path | None:
    """Resolve legacy final-artifact manifest paths to canonical locations."""
    by_name = {
        "final_report.md": paths.final_report_path(working_dir),
        "final_report.pdf": paths.final_report_pdf_path(working_dir),
        "final_report.committed": paths.final_report_commit_marker_path(working_dir),
        "final_audit_report.md": paths.final_audit_report_path(working_dir),
        "final_audit_report.pdf": paths.final_audit_pdf_path(working_dir),
        "final_audit_report.committed": paths.final_audit_commit_marker_path(working_dir),
        "final_audit_summary.json": paths.final_audit_summary_path(working_dir),
    }
    return by_name.get(Path(src).name)


def _locate_curation_file(
    working_dir: Path,
    not_before: float | None = None,
) -> Path | None:
    """Find the curator agent's CURATION.yaml, even if misplaced in a subdir.

    The curator is instructed to write CURATION.yaml to the workspace root,
    but when the actual project lives in a subdirectory (the agent's effective
    cwd is e.g. ``<workspace>/<project>/``) it can write the file relative to
    that cwd. Recovering a misplaced manifest preserves the curator's work
    instead of discarding it and shipping the report-only safety package.

    ``not_before`` is a freshness floor (epoch seconds): recovered candidates
    with an older mtime are ignored at every search depth, so a stale manifest
    left behind by a previous run can neither ship nor shadow a fresher,
    deeper one. The canonical root path is exempt — it is the explicitly
    documented location, not a heuristic recovery. ``None`` disables the
    filter (backward-compatible).

    Resolution order:
      1. ``<working_dir>/CURATION.yaml`` — the canonical location.
      2. The most recently modified loose CURATION.yaml elsewhere under
         working_dir, excluding audit-trail copies (a ``report/`` subtree) and
         package staging scratch dirs.

    Returns the resolved path, or None when no CURATION.yaml exists anywhere.
    """
    canonical = working_dir / "CURATION.yaml"
    if canonical.is_file():
        return canonical

    def _eligible(p: Path) -> bool:
        if not p.is_file():
            return False
        rel_parts = p.relative_to(working_dir).parts
        # Skip audit-trail copies (report/CURATION.yaml inside a package
        # tree) and packaging staging scratch.
        if "report" in rel_parts:
            return False
        if any(part.startswith(".package_staging_") for part in rel_parts):
            return False
        if not_before is not None:
            try:
                if p.stat().st_mtime < not_before:
                    return False
            except OSError:
                return False
        return True

    # Search shallow-first and stop at the shallowest depth that has a match.
    # The curator misplaces the manifest at its project-root cwd (1-3 levels
    # deep), so a bounded search is both faster than a full-tree rglob on a
    # large workspace and less likely to pick up a stale manifest buried in a
    # reference subtree. Within a depth tier, prefer the most recent file.
    for depth in (1, 2, 3):
        pattern = "/".join(["*"] * depth) + "/CURATION.yaml"
        try:
            tier = [p for p in working_dir.glob(pattern) if _eligible(p)]
        except OSError:
            return None
        if tier:
            try:
                return max(tier, key=lambda p: p.stat().st_mtime)
            except OSError:
                return tier[0]
    return None


def _create_package_zip(
    working_dir: str | Path,
    task: str,
    timestamp_suffix: str | None = None,
    not_before: float | None = None,
) -> str | None:
    """Build a curated handoff zip from CURATION.yaml.

    Steps:
      1. Parse CURATION.yaml (or fall back to minimal safety curation).
      2. Validate each entry's src exists and isn't hard-excluded.
      3. Stage copies into <pkg>/{report,code,test,data}/ layout.
      4. Write an auto-generated README.md.
      5. Write the effective CURATION.yaml under report/ as an audit trail.
      6. Zip the staged tree to <pkg>_package.zip in the workspace root.

    Stage 3: when ``timestamp_suffix`` is provided (e.g. ``<TIMESTAMP>``),
    the zip name becomes ``<pkg>_package_<suffix>.zip`` and a symlink
    ``<pkg>_package_latest.zip -> <pkg>_package_<suffix>.zip`` is updated
    atomically. Daily-sync invocations pass a suffix so historical packages
    accumulate; one-shot end-of-run invocations omit it (default behavior).

    ``not_before`` is a freshness floor forwarded to ``_locate_curation_file``
    (see there); ``_run_curator`` passes the curator-stage start time so a
    stale recovered manifest from a previous run cannot ship.

    Returns the zip filename (relative to working_dir), or None on failure.
    """
    working_dir = Path(working_dir)
    if not working_dir.is_dir():
        print("[long-exposure] Package: working_dir does not exist.", flush=True)
        return None

    # Locate the curator's CURATION.yaml. Prefer the canonical workspace-root
    # path, but recover a manifest the agent misplaced into a working subdir
    # rather than discarding its work (see _locate_curation_file).
    curation_path = _locate_curation_file(working_dir, not_before=not_before)
    # Base directory for resolving an entry's `src` when it isn't workspace-
    # relative — i.e. the directory the curator was actually writing from.
    curation_base = curation_path.parent if curation_path else working_dir
    if curation_path is not None and curation_path.parent != working_dir:
        print(
            "[long-exposure] Package: recovered CURATION.yaml from a "
            f"non-canonical location ({curation_path.relative_to(working_dir)}); "
            "resolving its sources relative to that directory.",
            flush=True,
        )
    curation = _parse_curation_manifest(curation_path) if curation_path else None
    fallback_used = curation is None or not curation.get("include")
    if fallback_used:
        # Attribute the failure to the specific cause so the user can fix
        # the upstream issue (usually: final reporter did not write the
        # '## Key Files' section in MANIFEST.md, so the curator agent had
        # nothing to select from).
        if curation_path is None:
            reason = "CURATION.yaml was not produced by the curator agent"
        elif curation is None:
            reason = "CURATION.yaml could not be parsed"
        else:
            reason = "CURATION.yaml had an empty include list"
        print(
            f"[long-exposure] WARNING: {reason}. Falling back to the minimal "
            "safety package (report documents only). Check whether the "
            "final reporter wrote a '## Key Files' section in MANIFEST.md.",
            flush=True,
        )
        curation = _minimal_safety_curation(working_dir, task)
        # The safety curation always uses workspace-relative paths.
        curation_base = working_dir
        if not curation["include"]:
            print(
                "[long-exposure] Package: no report documents found either. "
                "Nothing to package.",
                flush=True,
            )
            return None
    elif not curation.get("curation_complete"):
        # Curator produced a valid manifest but flagged it as incomplete —
        # typically because MANIFEST.md had no '## Key Files' section and
        # the curator refused to guess. Package still ships, but coverage
        # may be partial. Same root cause as the fallback, just caught
        # earlier by the agent.
        print(
            "[long-exposure] WARNING: curator marked CURATION.yaml as "
            "curation_complete: false — likely because MANIFEST.md had no "
            "'## Key Files' section. The package ships what the curator "
            "selected, but coverage may be partial.",
            flush=True,
        )

    pkg_name = _package_slug(curation.get("package_name") or task)
    if timestamp_suffix:
        # Sanitize suffix: keep alphanumerics, hyphens, underscores, colons.
        safe_suffix = _re.sub(r"[^A-Za-z0-9_:-]", "", timestamp_suffix)
        zip_name = f"{pkg_name}_package_{safe_suffix}.zip"
    else:
        zip_name = f"{pkg_name}_package.zip"
    zip_path = working_dir / zip_name

    staging = working_dir / f".package_staging_{uuid.uuid4().hex[:8]}"
    staging_root = staging / pkg_name
    copied: list[dict] = []
    missing: list[str] = []
    try:
        staging_root.mkdir(parents=True, exist_ok=True)
        try:
            workspace_resolved = working_dir.resolve()
        except OSError:
            workspace_resolved = working_dir
        for entry in curation["include"]:
            src_abs = working_dir / entry["src"]
            if not src_abs.is_file():
                # Try relative to where CURATION.yaml was found — handles a
                # curator that wrote src paths relative to its working subdir.
                alt = curation_base / entry["src"]
                if curation_base != working_dir and alt.is_file():
                    src_abs = alt
                else:
                    canonical = _canonical_final_src(working_dir, entry["src"])
                    if canonical and canonical.is_file():
                        src_abs = canonical
                    else:
                        missing.append(entry["src"])
                        continue
            # Symlink containment: the manifest parser's lexical ".." check
            # cannot stop an in-workspace symlink that points outside the
            # workspace. Require the resolved source to live under the
            # resolved workspace root; otherwise treat as missing.
            try:
                src_resolved = src_abs.resolve(strict=True)
            except OSError:
                missing.append(entry["src"])
                continue
            if not src_resolved.is_relative_to(workspace_resolved):
                print(
                    f"[long-exposure] Package: entry escapes the workspace "
                    f"via symlink and was skipped: {entry['src']}",
                    flush=True,
                )
                missing.append(entry["src"])
                continue
            dest_abs = staging_root / entry["dest"]
            # Per-entry copy failure (e.g. a file-vs-dir dest collision)
            # skips the entry instead of aborting the whole package.
            try:
                dest_abs.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_abs, dest_abs)
            except OSError as e:
                print(
                    f"[long-exposure] Package: copy failed for "
                    f"{entry['src']} -> {entry['dest']} ({e}); entry skipped.",
                    flush=True,
                )
                missing.append(entry["src"])
                continue
            copied.append(entry)

        if not copied:
            print(
                "[long-exposure] Package: curation produced zero valid files. "
                "Nothing to package.",
                flush=True,
            )
            return None

        # Audit-trail CURATION.yaml — dump the EFFECTIVE curation so what
        # ships reflects actual file copies (post-sanitization and
        # post-fallback), not whatever the agent originally wrote.
        effective_curation = {
            "package_name": pkg_name,
            "description": curation.get("description", ""),
            "curation_complete": curation.get("curation_complete", False),
            "fallback_used": fallback_used,
            "include": [dict(e) for e in copied],
            "missing": missing,
        }
        audit_dir = staging_root / "report"
        audit_dir.mkdir(parents=True, exist_ok=True)
        (audit_dir / "CURATION.yaml").write_text(
            yaml.safe_dump(effective_curation, sort_keys=False)
        )

        # Auto-generated README at package root
        effective_for_readme = dict(curation)
        effective_for_readme["include"] = copied
        effective_for_readme["package_name"] = pkg_name
        (staging_root / "README.md").write_text(
            _render_package_readme(effective_for_readme, task)
        )

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fpath in sorted(staging_root.rglob("*")):
                if not fpath.is_file():
                    continue
                arcname = fpath.relative_to(staging)
                zf.write(fpath, arcname)

        # Stage 3: when a timestamp suffix was used, also (re)point the
        # latest-symlink at this zip. Atomic: write to tmp link then replace.
        # Falls back to copy on filesystems that don't support symlinks.
        if timestamp_suffix:
            latest_path = working_dir / f"{pkg_name}_package_latest.zip"
            tmp_link = working_dir / f"{pkg_name}_package_latest.zip.tmp"
            try:
                if tmp_link.exists() or tmp_link.is_symlink():
                    tmp_link.unlink()
                tmp_link.symlink_to(zip_name)
                import os as _os
                _os.replace(tmp_link, latest_path)
            except (OSError, NotImplementedError):
                # symlink not supported — fall back to copy
                try:
                    shutil.copy2(zip_path, latest_path)
                except OSError as _e:
                    print(
                        f"[long-exposure] Package: latest-symlink fallback "
                        f"copy failed: {_e}",
                        flush=True,
                    )

        tag = " (safety fallback)" if fallback_used else ""
        print(
            f"[long-exposure] Package: {zip_name} — {len(copied)} files{tag}",
            flush=True,
        )
        if missing:
            preview = ", ".join(missing[:5])
            suffix = "…" if len(missing) > 5 else ""
            print(
                f"[long-exposure] Package: {len(missing)} curated path(s) "
                f"missing from workspace and skipped: {preview}{suffix}",
                flush=True,
            )
        return zip_name
    except OSError as e:
        print(f"[long-exposure] Package zip failed: {e}", flush=True)
        return None
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _collect_clone_artifacts(working_dir: str | Path) -> list[dict]:
    """Walk fan-out clone subdirs and return per-clone file lists.

    Layout assumed (written by the fan-out conductor):
        <working_dir>/fork-<uuid>/clone-<k>/clone_artifacts.json (preferred)
        <working_dir>/fork-<uuid>/clone-<k>/fork_files_touched.txt (fallback)
        <working_dir>/fork-<uuid>/clone-<k>/files_touched.txt (legacy)

    Plan H: prefer `clone_artifacts.json`, derived from the
    per-clone shadow ledger by `_write_clone_artifacts`. The ledger tags
    each event with clone identity, so paths returned here are accurate
    by construction. When the JSON is missing (older fork dirs, or a
    clone that produced no ledger events) fall back to the fork-scoped
    mtime walk and tag the entry with `scope: "fork"` so the curator
    knows it's coarser data.

    Returns a list of {fork_id, clone_k, files: [paths], scope, events?}.
    Empty list when no fan-out ran (no fork-*/ subdirs). Best-effort —
    silently skips any unreadable file.
    """
    workspace = Path(working_dir)
    if not workspace.is_dir():
        return []
    entries: list[dict] = []
    try:
        fork_dirs = sorted(p for p in workspace.glob("fork-*") if p.is_dir())
    except OSError:
        return []
    for fd in fork_dirs:
        fork_id = fd.name[len("fork-"):]
        try:
            clone_dirs = sorted(p for p in fd.glob("clone-*") if p.is_dir())
        except OSError:
            continue
        for cd in clone_dirs:
            clone_k = cd.name[len("clone-"):]
            artifacts_json = cd / "clone_artifacts.json"
            if artifacts_json.is_file():
                try:
                    payload = json.loads(artifacts_json.read_text())
                    events = payload.get("artifacts", []) or []
                    files = [
                        e["path"] for e in events
                        if isinstance(e, dict) and isinstance(e.get("path"), str)
                    ]
                    entries.append({
                        "fork_id": fork_id,
                        "clone_k": clone_k,
                        "files": files,
                        "scope": "clone",
                        "events": events,
                    })
                    continue
                except (OSError, json.JSONDecodeError):
                    pass
            # Fallback: fork-scoped mtime walk.
            ft = cd / "fork_files_touched.txt"
            if not ft.is_file():
                # One-cycle backward-compat for stale fork dirs from before
                # the rename.
                ft = cd / "files_touched.txt"
            if not ft.is_file():
                continue
            try:
                lines = ft.read_text().splitlines()
            except OSError:
                continue
            files = [ln.strip() for ln in lines if ln.strip()]
            entries.append({
                "fork_id": fork_id,
                "clone_k": clone_k,
                "files": files,
                "scope": "fork",
            })
    return entries


def _format_clone_artifacts(entries: list[dict]) -> str:
    """Render _collect_clone_artifacts output as a curator-facing string.

    Returns "(none)" when no fan-out ran, so the curator role's
    {clone_artifacts} slot is unambiguous in the linear case.

    Plan H: each entry carries a `scope` qualifier
    (`"clone"` for ledger-derived per-clone authorship; `"fork"` for
    fallback fork-wide mtime walk). Surfaced in the heading so the
    curator knows whether a clone's list is precise or coarse.
    """
    if not entries:
        return "(none)"
    lines: list[str] = []
    for e in entries:
        scope = e.get("scope", "fork")
        lines.append(
            f"## fork-{e['fork_id']} / clone-{e['clone_k']} (scope: {scope})"
        )
        if not e["files"]:
            lines.append("  (no files recorded)")
        else:
            for f in e["files"]:
                lines.append(f"  - {f}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _run_curator(
    curator_def: dict,
    task: str,
    config: dict,
    results: dict,
    score_inputs: dict,
    conn,
    cycle: int,
    last_session_id: str | None,
    agent_sessions: dict | None = None,
    agent_summaries: dict | None = None,
    timestamp_suffix: str | None = None,
) -> str | None:
    """Run the curator agent, then assemble a curated handoff zip.

    1. Agent writes CURATION.yaml based on MANIFEST.md's "## Key Files"
       section + reports/final/final_report.md.
    2. Deterministic code validates the manifest, enforces hard
       exclusions, organizes files into <pkg>/{report,code,...}/, writes
       an auto-generated README and an audit-trail CURATION.yaml, and
       zips the tree to <pkg>_package.zip.
    """
    working_dir = config.get("working_directory") or "/tmp"

    print(f"\n{'='*60}", flush=True)
    print("[long-exposure] === Curator ===", flush=True)

    # Freshness floor for recovered (non-canonical) CURATION.yaml files:
    # only manifests written during/after this curator pass are eligible,
    # so a stale manifest from a previous run can neither ship nor shadow
    # the one the agent writes below. Canonical-root manifests are exempt.
    # Small slack absorbs coarse filesystem mtime granularity — the stale
    # case being guarded against is hours-to-days old, not seconds.
    curator_start_ts = _time.time() - 2.0

    # Build results with working_dir for the agent
    dev_results = dict(results)
    dev_results["working_dir"] = working_dir
    # Surface fan-out clone artifacts so the curator can include any
    # branch outputs the final reporter did not promote into Key Files.
    # Returns "(none)" on root-only runs — preserves linear-case behavior.
    dev_results["clone_artifacts"] = _format_clone_artifacts(
        _collect_clone_artifacts(working_dir)
    )

    # Use provided dicts or create local ones
    dev_sessions = agent_sessions if agent_sessions is not None else {}
    dev_summaries = agent_summaries if agent_summaries is not None else {}

    is_resume = "curator" in dev_sessions
    print(
        f"[long-exposure] {'Resuming' if is_resume else 'Starting'}: curator",
        flush=True,
    )

    result = _call_agent_with_rotation(
        agent_name="curator",
        agent_def=curator_def,
        sessions_dict=dev_sessions,
        task=task,
        config=config,
        results=dev_results,
        score_inputs=score_inputs,
        agent_summaries=dev_summaries,
    )

    if result["status"] == "ok":
        usage = result.get("usage", {})
        dur = result.get("duration_ms", 0) / 1000
        total_ctx = _total_context_tokens(usage)
        print(
            f"[long-exposure]   curator: ok "
            f"({dur:.1f}s, ctx:{total_ctx:,}tok, "
            f"out:{usage.get('output_tokens', 0)}tok)",
            flush=True,
        )

        output_text = "\n\n".join(result["outputs"].values())
        last_session_id = _store_agent_output(
            conn, "curator", curator_def, output_text,
            cycle, last_session_id,
            current_topic="Curation Manifest",
        )
    else:
        err = result.get("error", "unknown")
        print(f"[long-exposure]   curator: FAILED — {err}", flush=True)
        print(
            "[long-exposure]   CURATION.yaml not produced. Falling back to "
            "minimal safety package (report documents only).",
            flush=True,
        )

    # Deterministic step: build the curated zip regardless of agent outcome.
    # On missing/unparseable CURATION.yaml, _create_package_zip falls back to
    # a report-only safety package — never to a whole-workspace dump.
    zip_name = _create_package_zip(
        working_dir, task,
        timestamp_suffix=timestamp_suffix,
        not_before=curator_start_ts,
    )
    if zip_name:
        print(f"[long-exposure] Package ready: {zip_name}", flush=True)
    else:
        print("[long-exposure] Package could not be created.", flush=True)

    print(f"\n[long-exposure] Curation complete.", flush=True)
    return last_session_id
