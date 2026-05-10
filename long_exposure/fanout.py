"""Parallel cycle fan-out: fork-id state, XML parser, guidance, conductor.

Extracted verbatim from long_exposure.exploration. Two contiguous regions of
the prior exploration.py are concatenated below in their original
top-to-bottom order:

  - Region A (was lines 151-525): fork-id env helpers, the
    parallel_cycle_fanout XML parser, FANOUT_GUIDANCE/POST_MERGE_BRIEF
    constants, fork manifest writers.
  - Region B (was lines 1770-2249): conductor — assignment JSON, clone
    spawn, barrier wait, 10h cap, stop-signal cascade.

Helpers used from long_exposure.exploration (`_atomic_write_text`,
`_check_signal_files`) are imported below. The cycle loop in
long_exposure.exploration imports these names back from this module via the
end-of-file re-export block in exploration.py.
"""

from __future__ import annotations

import json
import os
import re as _re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from long_exposure import provider as _provider
from long_exposure import telemetry


# Lazy-import delegators for names defined in long_exposure.exploration.
#
# Importing these at module-load time creates a circular import, because
# exploration.py re-exports this module near its tail. The prior design used
# a PEP-562 module `__getattr__` to resolve them lazily — but that only
# handles *external* attribute access (`fanout._atomic_write_text`), NOT
# bare-name `LOAD_GLOBAL` lookups from inside functions defined in this
# module. Python's global-name resolution consults the module `__dict__`
# and builtins only; it never invokes `__getattr__`. The result was a
# latent NameError on every first-time fan-out (crashed at
# `_write_fanout_assignment` ). The fix: real module-level
# wrapper functions. Each does the lazy import at call time and delegates.
# `sys.modules` caches the resolved module, so overhead is one dict lookup.
def _atomic_write_text(*args, **kwargs):
    from long_exposure.exploration import _atomic_write_text as _impl
    return _impl(*args, **kwargs)


def _check_signal_files(*args, **kwargs):
    from long_exposure.exploration import _check_signal_files as _impl
    return _impl(*args, **kwargs)


def save_state(*args, **kwargs):
    from long_exposure.exploration import save_state as _impl
    return _impl(*args, **kwargs)


def _active_account_dir():
    from long_exposure.exploration import _active_account_dir as _impl
    return _impl()


def _is_stop_requested() -> bool:
    # `_stop_requested` is a module-level bool rebound by exploration.py's
    # SIGINT/SIGTERM handler. Read through the module each call to capture
    # the live value — a snapshot import would freeze at False forever.
    import long_exposure.exploration as _exploration
    return bool(_exploration._stop_requested)


# ---------------------------------------------------------------------------
# Parallel cycle fan-out: fork-id state, XML parser, guidance injection.
#
# One piece of derivable state: AGENT_FORK_ID (env). If set, this process is
# a clone spawned by a root exploration; everything else derives from it
# (is_clone, max_parallel_depth=0, reporter-on-exit mode=merge, paths).
#
# Two XML surfaces:
#   1. Guidance block (what the researcher sees in live_guidance) — injected
#      at root only. ~120 tokens per root cycle. Omitted in clones.
#   2. Emission block (what the researcher outputs) — parsed by the conductor
#      at cycle boundary. Hard cap K=3, distinct output_artifact paths.
#
# Runtime enforcement: clones' parser logs a warning and returns None, so
# even if a clone inherits guidance somehow, it cannot recursively fan out.
# ---------------------------------------------------------------------------

FANOUT_MAX_BRANCHES = 3  # legacy cap; superseded by pool fanout_cap when active.
FANOUT_CAP_SECONDS = 10 * 60 * 60  # 10h wall-clock cap per clone


def _fanout_branch_cap() -> int:
    """Maximum branches the researcher may propose this cycle.

    When the account pool (Stage 1) is active, derive from pool capacity
    (`pool.fanout_cap()` = available_slots - 1 reserved for sequential root
    calls). Otherwise fall back to FANOUT_MAX_BRANCHES.
    """
    try:
        from long_exposure import pool as _pool
        if _pool.is_active():
            cap = _pool.fanout_cap()
            return max(1, cap) if cap > 0 else FANOUT_MAX_BRANCHES
    except Exception:
        pass
    return FANOUT_MAX_BRANCHES


def _build_fanout_guidance() -> str:
    """Render the fan-out guidance with the current pool's dynamic cap.

    The cap is dynamic so the researcher learns its real budget every cycle
    (per Stage 1 §5.4). When the pool is inactive the cap is the legacy 3.
    """
    cap = _fanout_branch_cap()
    return (
        "<parallel_cycle_fanout_guidance>\n"
        f"Use when 2-{cap} INDEPENDENT tasks each need their own full\n"
        "Researcher -> Worker -> Auditor loop. Before emitting, self-check all\n"
        "three factors:\n"
        "\n"
        "  (a) Independence -- no branch consumes another branch's output.\n"
        "      If no -> stay linear.\n"
        "  (b) Own audit -- each branch's findings need their own auditor gate\n"
        "      (not covered by one cycle-level auditor validating all work).\n"
        "      If no -> delegate to the worker for intra-cycle agent-teams fan-out.\n"
        "  (c) Iteration -- each branch needs at least one build -> test -> refine\n"
        "      round inside its own loop (not a single-shot operation).\n"
        "      If no -> delegate to the worker.\n"
        "\n"
        "All three must hold. If uncertain on any, stay linear.\n"
        "\n"
        f"Cap: up to {cap} branches (set by current account-pool capacity, "
        "not by you).\n"
        "Strictly non-overlapping scopes, distinct output_artifact paths\n"
        "(enforced mechanically -- duplicates reject the block).\n"
        f"If you have more than {cap} independent sub-problems, propose the\n"
        f"top {cap} highest-priority ones; the rest can run next cycle.\n"
        "\n"
        "Emit at the end of research_brief:\n"
        "  <parallel_cycle_fanout>\n"
        "    <branch>\n"
        "      <objective>one paragraph</objective>\n"
        "      <output_artifact>distinct/relative/path.md</output_artifact>\n"
        "    </branch>\n"
        f"    ... (2 to {cap} branches)\n"
        "  </parallel_cycle_fanout>\n"
        "\n"
        "Clones inherit your gems, context, and workspace.\n"
        "</parallel_cycle_fanout_guidance>"
    )


# Computed lazily when first accessed. Kept as a module-level name for
# back-compat with `from long_exposure.fanout import FANOUT_GUIDANCE`. The
# guidance is rebuilt from the live pool state at every cycle boundary via
# `get_fanout_guidance()`; the constant below is a static-cap fallback.
FANOUT_GUIDANCE = (
    "<parallel_cycle_fanout_guidance>\n"
    f"Use when 2-{FANOUT_MAX_BRANCHES} INDEPENDENT tasks each need their own full\n"
    "Researcher -> Worker -> Auditor loop. Before emitting, self-check all\n"
    "three factors:\n"
    "\n"
    "  (a) Independence -- no branch consumes another branch's output.\n"
    "      If no -> stay linear.\n"
    "  (b) Own audit -- each branch's findings need their own auditor gate\n"
    "      (not covered by one cycle-level auditor validating all work).\n"
    "      If no -> delegate to the worker for intra-cycle agent-teams fan-out.\n"
    "  (c) Iteration -- each branch needs at least one build -> test -> refine\n"
    "      round inside its own loop (not a single-shot operation).\n"
    "      If no -> delegate to the worker.\n"
    "\n"
    "All three must hold. If uncertain on any, stay linear.\n"
    "\n"
    f"Cap: {FANOUT_MAX_BRANCHES} branches, strictly non-overlapping scopes, "
    "distinct output_artifact paths\n"
    "(enforced mechanically -- duplicates reject the block).\n"
    "\n"
    "Emit at the end of research_brief:\n"
    "  <parallel_cycle_fanout>\n"
    "    <branch>\n"
    "      <objective>one paragraph</objective>\n"
    "      <output_artifact>distinct/relative/path.md</output_artifact>\n"
    "    </branch>\n"
    f"    ... (2 or {FANOUT_MAX_BRANCHES} branches)\n"
    "  </parallel_cycle_fanout>\n"
    "\n"
    "Clones inherit your gems, context, and workspace.\n"
    "</parallel_cycle_fanout_guidance>"
)


def get_fanout_guidance() -> str:
    """Live fan-out guidance with the dynamic pool cap. Callers prefer this
    over the FANOUT_GUIDANCE constant when the pool may be active.
    """
    return _build_fanout_guidance()

# Stage 2: hierarchical merge synthesis. When fan-out width >= this
# threshold, the reporter agent is reused at the merge boundary to
# compress N raw merge_reports (often ~100k+ tokens combined) into one
# bounded synthesis (~15-30k tokens). Below the threshold, raw
# concatenation goes through unchanged — the post-merge worker handles
# 3 reports fine. Configurable via config.yaml `merge_synthesis_min_branches`.
MERGE_SYNTHESIS_MIN_BRANCHES = 4

MERGE_SYNTHESIS_PROMPT = (
    "You are synthesizing the outputs of {n_branches} parallel research\n"
    "branches that ran independently from a fan-out point. Their raw\n"
    "merge_reports below total roughly {n_branches}x the size that the\n"
    "post-merge worker can comfortably read in full, so you produce one\n"
    "bounded synthesis as the worker's primary input.\n\n"
    "Input format: per-branch sections, each tagged with branch ID,\n"
    "objective, deliverable status (exists / missing / unchecked), and\n"
    "the branch's merge_report content.\n\n"
    "Produce a single coherent synthesis that:\n"
    "  - Integrates findings across branches.\n"
    "  - Surfaces convergences (agreements across branches).\n"
    "  - Surfaces divergences (where branches contradict or disagree).\n"
    "  - Notes failed branches with what was lost.\n"
    "  - Flags cross-cutting constraints discovered in any branch.\n"
    "  - Stays under 30,000 tokens (target ~15,000).\n\n"
    "The synthesis is the SOLE input for the next cycle's post-merge worker.\n"
    "Be the bridge between parallel work and integrated next steps. Write\n"
    "directly to {output_path} (the file must exist on disk after your\n"
    "response). If you also include the synthesis in an [OUTPUT:\n"
    "merge_synthesis] block, the orchestrator will rescue from there as a\n"
    "fallback.\n\n"
    "Fork: {fork_id}. Branches:\n\n{raw_input}"
)

# Post-merge cycle: the cycle immediately after a fan-out collapse runs
# worker-only (no researcher, no auditor). The worker receives this
# synthesized brief in place of a normal research_brief. The merge content
# is embedded directly so the worker has everything it needs from one
# input (worker's declared inputs do not include audit_report).
POST_MERGE_BRIEF_TEMPLATE = (
    "# POST-MERGE INTEGRATION CYCLE\n\n"
    "A parallel_cycle_fanout just collapsed. {k} sub-cycles produced outputs\n"
    "that now need integration into the main workspace. This cycle runs\n"
    "worker-only -- researcher and auditor are skipped.\n\n"
    "## Your task\n"
    "1. Read the merge content below.\n"
    "2. Integrate the sub-cycle outputs into the main workspace. Reconcile\n"
    "   any overlap or conflict between branches.\n"
    "3. Run integration tests / consistency checks that span branch outputs.\n\n"
    "## Do not\n"
    "- Start new research directions. The researcher resumes next cycle.\n"
    "- Perform audit-level validation of the sub-cycles. They audited\n"
    "  themselves via their own R/W/A loops.\n\n"
    "## Fan-out merge content (fork {fork_id})\n\n"
    "{merge}\n"
)


def _extract_fork_metadata(audit_report: str) -> tuple[str, int]:
    """Parse the fork_id and branch count out of the aggregated merge header.

    The conductor writes the merge with a known prefix:
        # Fan-out Merge (fork <id>)
        Branches: <k>. ...
    Returns (fork_id, k_branches). Falls back to ("unknown", 0) when the
    header is malformed or missing — a post-merge cycle still runs, just
    with degraded provenance in the brief.
    """
    fid = "unknown"
    k = 0
    if not audit_report:
        return fid, k
    m = _re.search(r"# Fan-out Merge \(fork ([a-f0-9]+)\)", audit_report)
    if m:
        fid = m.group(1)
    m = _re.search(r"Branches:\s*(\d+)\b", audit_report)
    if m:
        try:
            k = int(m.group(1))
        except ValueError:
            pass
    return fid, k


def _get_fork_id() -> str | None:
    """Return this process's fork-id if it is a spawned clone, else None."""
    fid = os.environ.get("AGENT_FORK_ID", "").strip()
    return fid or None


def _get_clone_k() -> int | None:
    """Return this clone's zero-based index within its fork, or None for root."""
    raw = os.environ.get("AGENT_FORK_CLONE_K", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _is_clone() -> bool:
    """True iff this process is a fan-out clone."""
    return _get_fork_id() is not None


_FANOUT_BLOCK_RE = _re.compile(
    r"<parallel_cycle_fanout>(.*?)</parallel_cycle_fanout>",
    _re.DOTALL | _re.IGNORECASE,
)
_FANOUT_BRANCH_RE = _re.compile(
    r"<branch>(.*?)</branch>", _re.DOTALL | _re.IGNORECASE,
)
_FANOUT_FIELD_RE = {
    "objective": _re.compile(
        r"<objective>(.*?)</objective>", _re.DOTALL | _re.IGNORECASE,
    ),
    "output_artifact": _re.compile(
        r"<output_artifact>(.*?)</output_artifact>", _re.DOTALL | _re.IGNORECASE,
    ),
}


def _parse_fanout_block(text: str | None) -> list[dict] | None:
    """Parse a <parallel_cycle_fanout> block from researcher output.

    Returns a list of {objective, output_artifact} dicts on success, or None
    if the block is absent, malformed, fails validation, or this process is
    a clone (1-level depth cap enforced at runtime).

    Validation:
      - 2 <= K <= FANOUT_MAX_BRANCHES
      - Each branch has non-empty objective and output_artifact
      - output_artifact paths are distinct across branches
    """
    if not text:
        return None
    if _is_clone():
        if _FANOUT_BLOCK_RE.search(text):
            print(
                "[long-exposure] parallel_cycle_fanout block from clone ignored "
                "(1-level depth cap).",
                flush=True,
            )
        return None

    m = _FANOUT_BLOCK_RE.search(text)
    if not m:
        return None
    body = m.group(1)
    branches_raw = _FANOUT_BRANCH_RE.findall(body)
    if not branches_raw:
        print(
            "[long-exposure] parallel_cycle_fanout block has no <branch> entries; "
            "ignoring.",
            flush=True,
        )
        return None

    if len(branches_raw) < 2:
        print(
            "[long-exposure] parallel_cycle_fanout requires >= 2 branches; "
            f"got {len(branches_raw)}. Ignoring.",
            flush=True,
        )
        return None
    # Stage 1 §5.3: clamp K to the live pool cap. If the pool can't host >=2
    # branches right now (e.g., all overflow accounts cooling), reject the
    # whole block — the researcher will retry next cycle when slots free up.
    dynamic_cap = _fanout_branch_cap()
    if dynamic_cap < 2:
        print(
            f"[long-exposure] parallel_cycle_fanout: pool cap is "
            f"{dynamic_cap}; insufficient capacity for fan-out. Ignoring.",
            flush=True,
        )
        return None
    if len(branches_raw) > dynamic_cap:
        print(
            f"[long-exposure] parallel_cycle_fanout: clamping {len(branches_raw)} "
            f"branches to pool cap {dynamic_cap}; tail-dropped branches will be "
            "available for the next cycle's researcher.",
            flush=True,
        )
        branches_raw = branches_raw[:dynamic_cap]

    branches: list[dict] = []
    for i, raw in enumerate(branches_raw):
        branch = {}
        for name, rx in _FANOUT_FIELD_RE.items():
            fm = rx.search(raw)
            branch[name] = (fm.group(1).strip() if fm else "")
        if not branch["objective"] or not branch["output_artifact"]:
            print(
                f"[long-exposure] parallel_cycle_fanout branch #{i} missing "
                "objective or output_artifact; ignoring whole block.",
                flush=True,
            )
            return None
        branches.append(branch)

    # Path safety: reject absolute paths and any `..` segments so the
    # deliverable always lands under the workspace root. Keeps downstream
    # consumers honest and prevents accidental writes outside the workspace.
    for i, br in enumerate(branches):
        p = Path(br["output_artifact"])
        if p.is_absolute() or ".." in p.parts:
            print(
                f"[long-exposure] parallel_cycle_fanout branch #{i} unsafe "
                f"output_artifact ({br['output_artifact']!r}): must be a "
                "relative workspace path with no '..'. Ignoring block.",
                flush=True,
            )
            return None

    # Strong path-collision check (replaces simple exact-string dedup).
    #
    # The fan-out design lets clones share the workspace freely; the only
    # contract is that no two clones write to the same file. This check
    # rejects the block at parse time if any two declared output_artifact
    # paths collide under any of three relations:
    #
    #   1. Identical after `os.path.normpath` (catches `./a.md` vs `a.md`,
    #      `dir/file` vs `dir//file`, trailing slashes, redundant `.`).
    #   2. One is an ancestor of the other (catches `dir/file.md` vs `dir`
    #      where one clone would create a directory the other tries to
    #      treat as a file, or vice versa).
    #
    # Why pre-emptive instead of post-hoc: the existing manifest annotation
    # in `_append_fork_manifest_outcomes` reports collisions AFTER both
    # clones have written and corrupted each other (last-writer-wins). A
    # parse-time reject saves the cycle and forces the researcher to
    # propose a non-overlapping plan.
    norm_paths: list[tuple[int, str, list[str]]] = []
    for i, br in enumerate(branches):
        normalised = os.path.normpath(br["output_artifact"]).replace(os.sep, "/")
        # normpath('') -> '.'; normpath('/x') -> '/x'. Already rejected
        # absolute paths above; '.' would be empty original which the
        # required-field check above already rejected. Empty parts list is
        # impossible at this point.
        parts = [p for p in normalised.split("/") if p]
        norm_paths.append((i, normalised, parts))

    for a in range(len(norm_paths)):
        for b in range(a + 1, len(norm_paths)):
            ai, ap, aparts = norm_paths[a]
            bi, bp, bparts = norm_paths[b]
            if ap == bp:
                print(
                    "[long-exposure] parallel_cycle_fanout branches share the same "
                    f"output_artifact ({branches[ai]['output_artifact']!r} == "
                    f"{branches[bi]['output_artifact']!r} after normalisation); "
                    "ignoring block.",
                    flush=True,
                )
                return None
            # Ancestor/descendant: one's parts are a strict prefix of the
            # other's. Equal length is impossible here (would have matched
            # above), so we only test prefix on the shorter side.
            if len(aparts) < len(bparts) and bparts[:len(aparts)] == aparts:
                print(
                    "[long-exposure] parallel_cycle_fanout branches collide on "
                    f"path tree: {branches[ai]['output_artifact']!r} is an "
                    f"ancestor of {branches[bi]['output_artifact']!r}. "
                    "Ignoring block.",
                    flush=True,
                )
                return None
            if len(bparts) < len(aparts) and aparts[:len(bparts)] == bparts:
                print(
                    "[long-exposure] parallel_cycle_fanout branches collide on "
                    f"path tree: {branches[bi]['output_artifact']!r} is an "
                    f"ancestor of {branches[ai]['output_artifact']!r}. "
                    "Ignoring block.",
                    flush=True,
                )
                return None

    return branches


def _fork_dir(root_workspace: Path, fork_id: str) -> Path:
    """Directory that holds all clones of a single fan-out."""
    return Path(root_workspace) / f"fork-{fork_id}"


def _clone_instance_dir(root_workspace: Path, fork_id: str, clone_k: int) -> Path:
    """Per-clone instance directory (passed as --instance-dir to the clone)."""
    return _fork_dir(root_workspace, fork_id) / f"clone-{clone_k}"


def _write_fork_manifest(
    fork_dir: Path,
    fork_id: str,
    branches: list[dict],
    procs: list,
    starts_iso: list[str],
) -> None:
    """Write fork_manifest.md at fan-out start — one-stop summary of the run.

    Records: fork id, start time, per-clone (output_artifact, pid, start,
    objective first-sentence). Downstream consumers see the whole fan-out
    in one file instead of trawling three clone dirs.
    """
    lines = [
        f"# Fork Manifest — {fork_id}",
        "",
        f"- Created: {datetime.now(timezone.utc).isoformat()}",
        f"- Branches: {len(branches)}",
        "",
    ]
    for k, br in enumerate(branches):
        p = procs[k] if k < len(procs) else None
        pid = p.pid if p is not None else "SPAWN_FAILED"
        first_sentence = br["objective"].split(". ", 1)[0][:240]
        lines += [
            f"## clone-{k}",
            f"- output_artifact: `{br['output_artifact']}`",
            f"- pid: {pid}",
            f"- started: {starts_iso[k]}",
            f"- objective: {first_sentence}",
            "",
        ]
    try:
        _atomic_write_text(fork_dir / "fork_manifest.md", "\n".join(lines))
    except OSError:
        pass


def _append_fork_manifest_outcomes(
    fork_dir: Path,
    outcomes: list[dict],
    clone_dirs: list[Path] | None = None,
) -> None:
    """Append concluded-outcomes + per-clone artifacts + fork-wide diff.

    Plan H: two provenance sections, distinct sources:

      - "Per-clone artifacts (from ledger)": derived from each clone's
        `clone_artifacts.json` (shadow-ledger-tagged, accurate per-clone
        authorship). Falls back to `fork_files_touched.txt` with a `(scope:
        fork)` qualifier for clones that produced no ledger events.

      - "Files modified during fan-out": fork-scoped mtime diff
        (`fork_files_touched.txt`). All clones produce identical content
        since they all start within milliseconds — read one, dedupe, render.

    The previous "Cross-clone collisions" section is removed: it was always
    meaningless under fork-scoped data (every clone listed every file).
    Real cross-clone artifact collisions are detectable from the merged
    ledger if needed; not in scope here.
    """
    path = fork_dir / "fork_manifest.md"
    try:
        existing = path.read_text() if path.exists() else ""
    except OSError:
        existing = ""
    tail = [
        "",
        "---",
        "",
        f"## Concluded — {datetime.now(timezone.utc).isoformat()}",
        "",
    ]
    for o in outcomes:
        tail.append(
            f"- clone-{o['clone_k']}: "
            f"state={o['state']}, "
            f"deliverable={o.get('deliverable_status', 'unchecked')} "
            f"({o.get('deliverable_path', '')})"
        )

    if clone_dirs:
        # Per-clone artifacts (from shadow ledger).
        per_clone_artifacts: dict[int, tuple[list[str], str]] = {}
        for k, cd in enumerate(clone_dirs):
            cj = Path(cd) / "clone_artifacts.json"
            if cj.is_file():
                try:
                    payload = json.loads(cj.read_text())
                    arts = payload.get("artifacts", []) or []
                    files = [
                        e["path"] for e in arts
                        if isinstance(e, dict) and isinstance(e.get("path"), str)
                    ]
                    per_clone_artifacts[k] = (files, "clone")
                    continue
                except (OSError, json.JSONDecodeError):
                    pass
            # Fallback: fork-scoped diff. Tag scope so the operator can see
            # this clone's list is coarser than its peers'.
            ft = Path(cd) / "fork_files_touched.txt"
            if not ft.is_file():
                # Backward-compat for stale fork dirs from before Plan H.
                ft = Path(cd) / "files_touched.txt"
            if ft.is_file():
                try:
                    lines = [
                        ln.strip() for ln in ft.read_text().splitlines()
                        if ln.strip()
                    ]
                    per_clone_artifacts[k] = (lines, "fork")
                except OSError:
                    pass

        if per_clone_artifacts:
            tail += ["", "### Per-clone artifacts (from ledger)", ""]
            for k in sorted(per_clone_artifacts):
                files, scope = per_clone_artifacts[k]
                qualifier = "" if scope == "clone" else " (scope: fork)"
                tail.append(f"- clone-{k}: {len(files)} files{qualifier}")
                for f in files[:50]:
                    tail.append(f"  - `{f}`")
                if len(files) > 50:
                    tail.append(
                        f"  - ... and {len(files) - 50} more "
                        f"(see clone-{k}/clone_artifacts.json or "
                        f"fork_files_touched.txt)"
                    )

        # Fork-wide diff. Read from any one clone (they all match) and
        # dedupe; if all are missing, skip silently.
        fork_files: list[str] = []
        for cd in clone_dirs:
            ft = Path(cd) / "fork_files_touched.txt"
            if not ft.is_file():
                ft = Path(cd) / "files_touched.txt"
            if ft.is_file():
                try:
                    fork_files = [
                        ln.strip() for ln in ft.read_text().splitlines()
                        if ln.strip()
                    ]
                    break
                except OSError:
                    continue
        if fork_files:
            tail += ["", "### Files modified during fan-out (fork-scoped)", ""]
            tail.append(f"- {len(fork_files)} files modified")
            for f in fork_files[:50]:
                tail.append(f"  - `{f}`")
            if len(fork_files) > 50:
                tail.append(
                    f"  - ... and {len(fork_files) - 50} more "
                    f"(see any clone-*/fork_files_touched.txt)"
                )

    try:
        _atomic_write_text(path, existing + "\n".join(tail) + "\n")
    except OSError:
        pass


def _merge_report_path(clone_instance_dir: Path) -> Path:
    """Where a clone writes its merge report on exit."""
    return Path(clone_instance_dir) / "merge_report.md"


# ---------------------------------------------------------------------------
# Parallel cycle fan-out conductor: fork point, spawn, barrier, 10h cap,
# stop-signal cascade. Called from the main cycle loop after researcher
# emits a valid <parallel_cycle_fanout> block.
# ---------------------------------------------------------------------------


def _write_fanout_assignment(
    clone_dir: Path, fork_id: str, clone_k: int, branch: dict, task: str,
) -> None:
    """Persist a clone's per-branch assignment so it can read it on startup."""
    clone_dir.mkdir(parents=True, exist_ok=True)
    assignment_text = (
        f"FANOUT CLONE ASSIGNMENT (clone {clone_k} of fork {fork_id})\n\n"
        f"Root directive: {task}\n\n"
        f"Your scoped objective for this fan-out branch:\n"
        f"{branch['objective']}\n\n"
        f"Your required output artifact: {branch['output_artifact']}\n\n"
        f"You inherit the parent's gems, auto-compact context, and workspace.\n"
        f"Run until exhaustion (your own low-output detector\n"
        f"will terminate the cycle loop naturally). On exit, a merge report\n"
        f"is written to {clone_dir / 'merge_report.md'} for the root\n"
        f"conductor to pick up.\n"
    )
    payload = {
        "fork_id": fork_id,
        "clone_k": clone_k,
        "objective": branch["objective"],
        "output_artifact": branch["output_artifact"],
        "assignment": assignment_text,
    }
    _atomic_write_text(
        clone_dir / "fanout_assignment.json",
        json.dumps(payload, indent=2),
    )


def _seed_clone_state(
    clone_dir: Path,
    parent_results: dict,
    parent_agent_sessions: dict,
    parent_agent_summaries: dict,
    parent_run_id: str | None = None,
    parent_account_dir: str | None = None,
    pinned_account_dir: str | None = None,
    clone_k: int | None = None,
) -> None:
    """Seed the clone's state file so it resumes with parent's sessions but
    at cycle 0. Clones inherit Claude Code --resume IDs (gems + context),
    but run their own cycle count.

    Propagates parent_run_id so clone-emitted ledger events share the same
    run_id as the parent (Plan 1 §6 + Plan 5 §2.2). Without this, clone
    cycles would not be counted by `_count_total_cycles` and lessons
    cap arithmetic would underestimate the run.

    When parent_account_dir and pinned_account_dir are both known and
    differ, agent_sessions are dropped: Claude Code --resume UUIDs live
    under one account dir's sessions/ and do not roam. Without this drop
    the clone wastes its first cycle on a "No conversation found" failure
    plus the 800s adaptive cooldown that follows. Comparison is by string
    equality of dir paths (indices are broken under pool mode — see Plan D
    Part 1 "Why we cannot reuse the existing index-based field"). Other
    state (agent_summaries, parent_results, run_id) is account-portable
    and preserved verbatim.
    """
    clone_dir.mkdir(parents=True, exist_ok=True)
    # Strip live_guidance (will be regenerated) and prior fanout guidance.
    seeded_results = {
        k: v for k, v in parent_results.items()
        if k not in ("live_guidance",)
    }
    seeded_sessions = dict(parent_agent_sessions or {})
    if (
        seeded_sessions
        and parent_account_dir
        and pinned_account_dir
        and parent_account_dir != pinned_account_dir
    ):
        print(
            f"[long-exposure]   clone-{clone_k}: parent sessions were on "
            f"{Path(parent_account_dir).name} but clone pinned to "
            f"{Path(pinned_account_dir).name}; dropping sessions to avoid "
            f"--resume failures.",
            flush=True,
        )
        try:
            from long_exposure import health_events as _he
            _he.append_event(
                "account_mismatch_drop",
                detail=(
                    f"clone_k={clone_k} parent={Path(parent_account_dir).name} "
                    f"pinned={Path(pinned_account_dir).name}"
                ),
            )
        except Exception:
            pass
        seeded_sessions = {}
    save_state(
        clone_dir / "exploration_state.json",
        cycle=0,
        results=seeded_results,
        failures={},
        last_session_id=None,
        agent_sessions=seeded_sessions,
        agent_summaries=dict(parent_agent_summaries or {}),
        post_merge_pending=False,
        run_id=parent_run_id,
    )


def _resolve_python_exe() -> str:
    """Return an executable Python interpreter path for clone spawn.

    Prefers sys.executable (the parent's own interpreter — same venv, same
    version, guaranteed to have long_exposure.exploration importable). Falls back
    to python3 on PATH only if sys.executable is empty or not executable
    (embedded interpreter, stripped perms, broken venv symlink). Never
    returns bare 'python' — not all systems have it (this box doesn't).

    Raises FileNotFoundError (OSError subclass) if nothing works, so the
    caller's existing `except OSError` handles it and the fan-out collapses
    with a clean per-clone error instead of a root-process traceback.
    """
    exe = sys.executable
    if exe and os.access(exe, os.X_OK):
        return exe
    py3 = shutil.which("python3")
    if py3:
        return py3
    raise FileNotFoundError(
        f"no usable Python interpreter: sys.executable={exe!r} "
        f"(exists={bool(exe) and os.path.exists(exe)}, "
        f"x_ok={bool(exe) and os.access(exe, os.X_OK)}), "
        f"python3 not on PATH"
    )


def _acquire_clone_pool_slot(fork_id: str, clone_k: int) -> str | None:
    """Acquire a pool slot for a clone. Returns the pinned account dir,
    or None when the pool is inactive or PoolExhausted is raised.

    Tags the holder with the parent's PID. The clone's actual PID is
    re-tagged via update_slot_pid in _spawn_clone post-Popen, mirroring
    the prior in-spawn flow.
    """
    try:
        from long_exposure import pool as _pool
        if not _pool.is_active():
            return None
        try:
            return _pool.acquire_slot(
                role="clone", fork_id=fork_id, clone_k=clone_k,
            )
        except _pool.PoolExhausted as _ex:
            print(
                f"[long-exposure]   clone-{clone_k}: pool exhausted "
                f"({_ex}); falling back to inherited account",
                flush=True,
            )
            return None
    except Exception as _err:
        print(
            f"[long-exposure]   clone-{clone_k}: pool pin skipped ({_err})",
            flush=True,
        )
        return None


def _spawn_clone(
    clone_dir: Path,
    fork_id: str,
    clone_k: int,
    score_path: str,
    config_path: str | None,
    pinned_account_dir: str | None = None,
) -> subprocess.Popen:
    """Spawn one clone subprocess. Returns the Popen handle.

    Uses start_new_session=True so the clone has its own process group —
    this lets the conductor send a hard kill to the whole group (including
    any Claude CLI subprocess the clone has spawned) when the 10h cap is
    reached.

    Clone stdout+stderr is piped through a daemon reader thread that tees
    each line to (a) the clone's clone.log for post-hoc inspection and
    (b) the root process's stdout with a "[clone-<k>] " prefix so the
    user can see clone cycle progression in the root terminal — mirrors
    the existing "[long-exposure] ..." print style but lane-tagged per clone.
    """
    env = dict(os.environ)
    env["AGENT_FORK_ID"] = fork_id
    env["AGENT_FORK_CLONE_K"] = str(clone_k)
    # Per-clone instance dir, also set as env var so the shadow-ledger router
    # in workspace_bootstrap.resolve_ledger_path can find it without re-parsing
    # CLI args (Plan 1 §6). The --instance-dir CLI flag is also passed below
    # for the cycle loop's resolve_instance_dir; the env var is the parallel
    # path for harness helpers like ledger_append.
    env["AGENT_INSTANCE_DIR"] = str(clone_dir)
    # Clone-start epoch seconds — used by the clone at exit to enumerate
    # workspace files with mtime >= start as its provenance list.
    env["AGENT_CLONE_START_TS"] = f"{time.time():.6f}"
    # Stage 1: per-clone account pinning. The pool slot is acquired by the
    # conductor BEFORE _seed_clone_state runs (so seed can decide whether
    # parent's session UUIDs are portable to the pinned account — Plan D).
    # When pinned_account_dir is non-None, this function trusts that the
    # caller acquired the slot and pins via CLAUDE_FORCE_ACCOUNT. When
    # None (pool inactive, PoolExhausted, or import failure), the clone
    # inherits CLAUDE_ACCOUNTS + global state as before. The clone reads
    # CLAUDE_FORCE_ACCOUNT on every _invoke_claude call
    # (orchestrator._resolve_force_account) and never rotates away from
    # its pinned account.
    pool_module = None  # captured so post-Popen update can reuse it
    pool_slot_acquired = False
    if pinned_account_dir:
        try:
            from long_exposure import pool as _pool
            pool_module = _pool
            pool_slot_acquired = True
        except Exception as _err:
            print(
                f"[long-exposure]   clone-{clone_k}: pool module unavailable "
                f"post-acquire ({_err}); update_slot_pid will be skipped",
                flush=True,
            )
        env[_provider.force_account_env()] = pinned_account_dir
        # Drop provider pool envs so the clone doesn't see the multi-account
        # retry loop in call_claude — pinned accounts must single-attempt.
        for env_name in _provider.account_pool_envs():
            env.pop(env_name, None)
        print(
            f"[long-exposure]   clone-{clone_k}: pinned to "
            f"{Path(pinned_account_dir).name}",
            flush=True,
        )
    python_exe = _resolve_python_exe()
    cmd = [
        python_exe, "-m", "long_exposure.exploration",
        "--score", str(score_path),
        "--instance-dir", str(clone_dir),
    ]
    if config_path:
        cmd.extend(["--config", str(config_path)])
    cmd.append("resume")
    log_path = clone_dir / "clone.log"
    log_fh = open(log_path, "a")
    log_fh.write(
        f"\n\n=== clone spawn {datetime.now(timezone.utc).isoformat()} ===\n"
        f"python_exe={python_exe}\n"
    )
    log_fh.flush()

    try:
        p = subprocess.Popen(
            cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError:
        # Popen failed before any clone process existed. Release the slot
        # we just acquired so it doesn't leak. Re-raise so the conductor
        # records this branch as spawn_failed.
        if pool_slot_acquired and pool_module is not None:
            try:
                pool_module.release_slot_by_branch(fork_id, clone_k)
            except Exception as _re:
                print(
                    f"[long-exposure]   clone-{clone_k}: slot release after "
                    f"Popen failure also failed ({_re})",
                    flush=True,
                )
        log_fh.close()
        raise

    # Parent-side post-Popen slot re-tag. Idempotent fallback: the clone
    # itself re-tags the slot at startup (exploration._is_clone() bootstrap
    # block PID-race fix), so this call usually finds the
    # slot already tagged with the clone's PID and is a no-op. We keep the
    # parent-side call because there's a small window before the clone's
    # Python interpreter starts during which parent-PID is the only holder
    # tag — re-tagging here closes that window from the parent side as soon
    # as Popen returns, while the clone-side re-tag is the authoritative
    # path that survives parent crashes.
    if pool_slot_acquired and pool_module is not None:
        try:
            pool_module.update_slot_pid(fork_id, clone_k, p.pid)
        except Exception as _ue:
            # Best-effort — clone-side re-tag will close any remaining gap.
            print(
                f"[long-exposure]   clone-{clone_k}: parent-side slot PID "
                f"update failed ({_ue}); clone-side re-tag will recover.",
                flush=True,
            )

    # Reader thread: forwards clone output to both clone.log and root
    # stdout with a per-clone prefix. Lines from different clones may
    # interleave on root stdout, but each line is prefixed so tracing
    # a single clone's cycle progression is straightforward.
    prefix = f"[clone-{clone_k}] "

    def _forward() -> None:
        try:
            for raw in iter(p.stdout.readline, b""):
                try:
                    line = raw.decode("utf-8", errors="replace")
                except Exception:
                    line = repr(raw) + "\n"
                try:
                    log_fh.write(line)
                    log_fh.flush()
                except Exception:
                    pass
                try:
                    sys.stdout.write(prefix + line)
                    sys.stdout.flush()
                except Exception:
                    pass
        finally:
            try:
                p.stdout.close()
            except Exception:
                pass
            try:
                log_fh.close()
            except Exception:
                pass

    t = threading.Thread(
        target=_forward, daemon=True, name=f"clone-{clone_k}-reader",
    )
    t.start()
    return p


def _build_synthesis_input(
    branches: list[dict], outcomes: list[dict]
) -> str:
    """Format per-branch sections for the synthesis prompt's {raw_input} slot."""
    parts: list[str] = []
    for k, (br, o) in enumerate(zip(branches, outcomes)):
        parts.append(
            f"---\n\n## Clone {k} — {br.get('output_artifact', 'unknown')}\n\n"
            f"Objective: {br.get('objective', '(missing)')}\n\n"
            f"Exit state: {o.get('state', 'unknown')}\n"
            f"Deliverable: {o.get('deliverable_status', 'unchecked')} "
            f"({o.get('deliverable_path', '')})\n\n"
            f"### merge_report\n{o.get('merge_report') or '(no report)'}\n"
        )
    return "\n".join(parts)


def _run_merge_synthesis(
    reporter_def: dict,
    config: dict,
    task: str,
    fork_id: str,
    fork_dir: Path,
    branches: list[dict],
    outcomes: list[dict],
) -> str | None:
    """Compress N merge_reports into one bounded synthesis (Stage 2).

    Reuses the reporter agent — no new agent role. One-shot call (no session
    continuity), writes merge_synthesis.md to fork_dir. On failure, returns
    None so the caller falls back to raw concatenation (graceful degradation).

    The reporter is called via _call_agent_with_rotation so a primary
    rate-limit during synthesis still rotates to a fresh account before
    giving up.
    """
    output_path = fork_dir / "merge_synthesis.md"
    raw_input = _build_synthesis_input(branches, outcomes)
    prompt_text = MERGE_SYNTHESIS_PROMPT.format(
        n_branches=len(branches),
        fork_id=fork_id,
        output_path=str(output_path),
        raw_input=raw_input,
    )

    print(
        f"[long-exposure] Merge synthesis: invoking reporter on "
        f"{len(branches)} merge_reports -> {output_path.name}",
        flush=True,
    )

    # Lazy imports to dodge the circular import with exploration.py.
    from long_exposure.exploration import _call_agent_with_rotation as _call
    from long_exposure.reporting import _extract_report_content

    # Stage_results is a flat dict the prompt template can interpolate from
    # the reporter's <inputs>. We override directive/cycle_range/audit_report
    # slots with synthesis-specific content so the existing reporter prompt
    # still resolves cleanly when it asks for those names.
    stage_results = {
        "directive": prompt_text,
        "cycle_range": f"merge-synthesis-{fork_id}",
        "audit_report": "(merge-synthesis: not applicable)",
        "cycle_sessions": "(merge-synthesis: not applicable)",
        "report_basename": "merge_synthesis",
        "working_dir": str(fork_dir),
    }

    try:
        result = _call(
            agent_name="reporter",
            agent_def=reporter_def,
            sessions_dict={},  # one-shot; no continuity
            task=task,
            config=config,
            results=stage_results,
            score_inputs={"directive": task},
            agent_summaries={},
        )
    except Exception as e:
        print(f"[long-exposure] Merge synthesis: agent call failed: {e}", flush=True)
        return None

    if result.get("status") != "ok":
        print(
            f"[long-exposure] Merge synthesis: status="
            f"{result.get('status', 'unknown')}, "
            f"err={result.get('error', '')[:200]}",
            flush=True,
        )
        return None

    # File-gate rescue: if the agent didn't write the file, pull from
    # [OUTPUT: merge_synthesis] in its raw output.
    if not output_path.exists():
        output_text = "\n\n".join(result.get("outputs", {}).values())
        rescued = _extract_report_content(output_text, marker="merge_synthesis")
        if rescued and len(rescued) > 200:
            try:
                output_path.write_text(rescued + "\n")
                print(
                    f"[long-exposure] Merge synthesis: file-gate rescue "
                    f"({len(rescued)} chars) -> {output_path.name}",
                    flush=True,
                )
            except OSError as e:
                print(f"[long-exposure] Merge synthesis: rescue write failed: {e}", flush=True)
                return None
        else:
            print(
                "[long-exposure] Merge synthesis: agent produced no usable "
                "content; falling back to raw concatenation.",
                flush=True,
            )
            return None

    try:
        return output_path.read_text()
    except OSError as e:
        print(f"[long-exposure] Merge synthesis: read failed: {e}", flush=True)
        return None


def _read_clone_cycle_count(clone_dir: Path) -> int:
    """Best-effort read of cycle count from a clone's exploration_state.json.

    Used by the barrier preempt trigger (Stage 9 §2.4) to gauge how much
    work each running clone has produced. Atomic writes via os.replace()
    in save_state mean we either see the old or new cycle value cleanly —
    never a partial. Returns 0 on any error so the trigger conservatively
    waits rather than misfires.
    """
    state_path = clone_dir / "exploration_state.json"
    try:
        return int(json.loads(state_path.read_text()).get("cycle", 0))
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def _should_preempt_barrier(
    barrier_started_monotonic: float,
    clone_dirs: list[Path],
    outcomes: list[dict],
    loop_cfg: dict,
) -> tuple[bool, str]:
    """Stage 9: graceful preemption eligibility check.

    Returns (should_preempt, reason). The reason is a short
    human-readable string for log output explaining which trigger
    branch fired.

    Two trigger branches, both gated on `cold_account_exists`:

      PRIMARY (cycle-based, fires at natural cadence):
        at-least-one-clone-already-exited AND
        every-still-running-clone-done-min-cycles_done

      BACKUP (timer, last-line-of-defense; fires when ALL clones are stuck):
        barrier-elapsed >= barrier_preempt_timeout_seconds

    Both branches require at least one cold (callable) account in the
    pool — without idle capacity to rotate to, preemption gains nothing.
    """
    # Capacity precondition.
    try:
        from long_exposure import pool as _pool
        if not _pool.is_active():
            return (False, "pool inactive")
        state = _pool.pool_state()
        cold = [a for a in state.get("accounts", []) if a.get("state") == "cold"]
        if not cold:
            return (False, "no cold account available")
    except Exception:
        return (False, "pool unavailable")

    running_indices = [k for k, o in enumerate(outcomes) if o["state"] == "running"]
    if not running_indices:
        return (False, "no running clones")

    # PRIMARY trigger: any-exit gate + all-running-clones-have-min-cycles.
    # The any-exit gate is critical: without it, fan-outs would be
    # preempted at cycle 1 every time cold capacity exists, never
    # accumulating per-branch depth (churn pattern, not bottleneck fix).
    min_cycles = int(loop_cfg.get("min_clone_cycles_before_preempt", 1))
    if min_cycles > 0:
        any_exited = len(running_indices) < len(outcomes)
        if any_exited and all(
            _read_clone_cycle_count(clone_dirs[k]) >= min_cycles
            for k in running_indices
        ):
            return (
                True,
                f"primary: {len(outcomes) - len(running_indices)}/{len(outcomes)} "
                f"clones exited, {len(running_indices)} remaining have all done "
                f">= {min_cycles} cycle(s), {len(cold)} cold account(s) available"
            )

    # BACKUP trigger: barrier has been waiting too long regardless. No
    # any-exit gate here — this fires when ALL clones are stuck (a
    # different pathology from the primary case) and unblocks the run
    # so the operator doesn't have to intervene manually.
    timeout_s = float(loop_cfg.get("barrier_preempt_timeout_seconds", 3600))
    if timeout_s > 0:
        elapsed = time.monotonic() - barrier_started_monotonic
        if elapsed >= timeout_s:
            return (
                True,
                f"backup: barrier elapsed {elapsed:.0f}s >= {timeout_s:.0f}s timeout, "
                f"{len(cold)} cold account(s) available"
            )

    return (False, "no trigger fired")


def _run_fanout_conductor(
    branches: list[dict],
    score_path: str,
    config_path: str | None,
    root_instance_dir: Path,
    data_dir: Path,
    task: str,
    parent_results: dict,
    parent_agent_sessions: dict,
    parent_agent_summaries: dict,
    working_directory: str | None = None,
    parent_run_id: str | None = None,
    reporter_def: dict | None = None,
    config: dict | None = None,
    loop_cfg: dict | None = None,
) -> dict:
    """Spawn K clones, barrier-wait for merge reports, return aggregated text.

    Returns dict with:
      - aggregated_report: str (the concatenated merge reports)
      - fork_id: str
      - clone_dirs: list[Path]
      - outcomes: list[dict]  # per-clone exit metadata
    """
    fork_id = uuid.uuid4().hex[:12]
    root_workspace = Path(root_instance_dir)
    fork_dir = _fork_dir(root_workspace, fork_id)
    fork_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"\n[long-exposure] === Fan-out: fork {fork_id}, "
        f"{len(branches)} branches ===",
        flush=True,
    )
    telemetry.emit(
        "fanout_started",
        phase="fanout",
        provider=(config or {}).get("llm_provider") if config else None,
        model=(config or {}).get("model") if config else None,
        status="started",
        data={
            "fork_id": fork_id,
            "branches": len(branches),
            "root_instance_dir": str(root_instance_dir),
        },
    )
    for k, br in enumerate(branches):
        print(
            f"[long-exposure]   clone-{k}: {br['objective'][:80]}... "
            f"-> {br['output_artifact']}",
            flush=True,
        )

    # --- Seed each clone's workspace, assignment, state ---
    # Resolve parent's active account dir ONCE; it does not change across the
    # spawn loop. Used by _seed_clone_state to compare against the per-clone
    # pinned dir and drop stale --resume IDs (Plan D).
    try:
        parent_acct_dir = _active_account_dir()
    except Exception:
        parent_acct_dir = None
    clone_dirs: list[Path] = []
    procs: list[subprocess.Popen | None] = []
    starts: list[float] = []
    starts_iso: list[str] = []
    for k, br in enumerate(branches):
        cdir = _clone_instance_dir(root_workspace, fork_id, k)
        clone_dirs.append(cdir)
        # Acquire the pool slot BEFORE seed_clone_state so the seed step
        # can compare parent's account dir vs. clone's pinned account dir
        # (Plan D). Falls back to None on inactive pool / exhaustion;
        # _seed_clone_state then preserves sessions verbatim.
        pinned_dir = _acquire_clone_pool_slot(fork_id, k)
        # Defensive release: if assignment/seed raises before spawn, the
        # slot we just acquired must not leak. _spawn_clone's own OSError
        # handler covers the Popen-failure case (release_slot_by_branch).
        spawn_succeeded = False
        try:
            _write_fanout_assignment(cdir, fork_id, k, br, task)
            _seed_clone_state(
                cdir,
                parent_results=parent_results,
                parent_agent_sessions=parent_agent_sessions,
                parent_agent_summaries=parent_agent_summaries,
                parent_run_id=parent_run_id,
                parent_account_dir=parent_acct_dir,
                pinned_account_dir=pinned_dir,
                clone_k=k,
            )
            try:
                p = _spawn_clone(
                    cdir, fork_id, k, score_path, config_path,
                    pinned_account_dir=pinned_dir,
                )
                spawn_succeeded = True
                procs.append(p)
                starts.append(time.monotonic())
                starts_iso.append(datetime.now(timezone.utc).isoformat())
                print(
                    f"[long-exposure]   clone-{k} spawned (pid={p.pid}, "
                    f"dir={cdir})",
                    flush=True,
                )
            except OSError as e:
                # _spawn_clone already released the slot on Popen failure;
                # mark spawn_succeeded so the outer finally doesn't double-release.
                spawn_succeeded = True
                procs.append(None)
                starts.append(time.monotonic())
                starts_iso.append(datetime.now(timezone.utc).isoformat())
                print(
                    f"[long-exposure]   clone-{k} spawn FAILED: {e}",
                    flush=True,
                )
        finally:
            if not spawn_succeeded and pinned_dir:
                # Assignment write or seed-state write raised before we
                # reached _spawn_clone; release the slot we acquired so
                # it doesn't leak. Best-effort.
                try:
                    from long_exposure import pool as _pool
                    _pool.release_slot_by_branch(fork_id, k)
                except Exception as _re:
                    print(
                        f"[long-exposure]   clone-{k} pre-spawn release "
                        f"failed ({_re})",
                        flush=True,
                    )

    # One-stop coordination file for downstream consumers — enumerates the
    # fan-out at a glance. Updated with outcomes at the end of the barrier.
    _write_fork_manifest(fork_dir, fork_id, branches, procs, starts_iso)

    # --- Barrier: poll each clone's merge_report.md, enforce 10h cap ---
    outcomes: list[dict] = [
        {"clone_k": k, "state": "running", "merge_report": None}
        for k in range(len(branches))
    ]
    poll_interval = 5.0
    last_status_print = 0.0

    # Stage 9: graceful barrier preemption.
    # `barrier_started_monotonic` is the wall-time anchor for the backup
    # (timer-based) trigger. `preempt_fired` is the one-shot guard so the
    # eligibility check doesn't re-fire every poll. `preempted_clones`
    # records which clones got the graceful-stop signal so when their
    # merge_report eventually lands, we can mark the outcome `done_preempted`
    # (vs. organic `done`) for observability in the aggregated header.
    barrier_started_monotonic = time.monotonic()
    preempt_fired = False
    preempted_clones: set[int] = set()
    _loop_cfg = loop_cfg or {}

    def _all_done() -> bool:
        return all(o["state"] != "running" for o in outcomes)

    while not _all_done():
        now = time.monotonic()

        # Check root stop-signal. Cascade to clones.
        _check_signal_files(data_dir)
        if _is_stop_requested():
            print(
                "[long-exposure] Fan-out: root stop signal observed; "
                "cascading to clones...",
                flush=True,
            )
            for k, cd in enumerate(clone_dirs):
                if outcomes[k]["state"] == "running":
                    try:
                        (cd / "long-exposure.stop").write_text("")
                    except OSError:
                        pass
            # Keep polling to collect partial reports, but shorten the wait.

        for k, (p, cd) in enumerate(zip(procs, clone_dirs)):
            # Opportunistically reap any clone whose state was already marked
            # non-running in a prior iteration (typically because we observed
            # its merge_report.md before its python process actually exited).
            # Without this poll() the Popen handle never collects the child's
            # exit code and the kernel keeps it as a `Zs` zombie until this
            # parent process exits — observed at 4h+ for 3-of-4 clones in
            # fork-e84d2e35d494. poll() is non-blocking; it
            # returns the exit code if the process has finished, None
            # otherwise, and is idempotent on already-reaped handles.
            if p is not None:
                p.poll()
            if outcomes[k]["state"] != "running":
                continue

            # 1. Did the clone write its merge report?
            mrp = _merge_report_path(cd)
            if mrp.exists():
                try:
                    outcomes[k]["merge_report"] = mrp.read_text()
                    # Stage 9: preempted clones get a distinct outcome state
                    # so the aggregated header / post-merge worker can see
                    # which branches were cut short by capacity preemption
                    # (vs. organic completion).
                    outcomes[k]["state"] = (
                        "done_preempted" if k in preempted_clones else "done"
                    )
                    print(
                        f"[long-exposure]   clone-{k}: merge_report received "
                        f"({len(outcomes[k]['merge_report'])} chars, "
                        f"state={outcomes[k]['state']})",
                        flush=True,
                    )
                except OSError as e:
                    outcomes[k]["state"] = "error"
                    outcomes[k]["merge_report"] = (
                        f"# Merge Report Unreadable\n\n{e}\n"
                    )
                continue

            # 2. Did the subprocess exit without writing a report?
            if p is None:
                outcomes[k]["state"] = "spawn_failed"
                outcomes[k]["merge_report"] = (
                    f"# Merge Report (spawn failed)\n\n"
                    f"Clone {k} failed to spawn.\n"
                )
                continue
            rc = p.poll()
            if rc is not None:
                # Process finished. Re-check for the report once more (race
                # between process exit and atomic rename).
                time.sleep(0.5)
                if mrp.exists():
                    try:
                        outcomes[k]["merge_report"] = mrp.read_text()
                        outcomes[k]["state"] = (
                            "done_preempted" if k in preempted_clones else "done"
                        )
                        continue
                    except OSError:
                        pass
                outcomes[k]["state"] = "exited_no_report"
                outcomes[k]["merge_report"] = (
                    f"# Merge Report (clone exited without writing report)\n\n"
                    f"Clone {k} exited with code {rc}.\n"
                )
                print(
                    f"[long-exposure]   clone-{k}: exited rc={rc} without "
                    f"merge_report.md",
                    flush=True,
                )
                continue

            # 3. 10h cap breach?
            if (now - starts[k]) > FANOUT_CAP_SECONDS:
                print(
                    f"[long-exposure]   clone-{k}: 10h cap breached; "
                    f"signalling stop.",
                    flush=True,
                )
                try:
                    (cd / "long-exposure.stop").write_text("")
                except OSError:
                    pass
                # Give the clone 120s to finish its merge reporter.
                for _ in range(24):
                    time.sleep(5.0)
                    if mrp.exists() or p.poll() is not None:
                        break
                if mrp.exists():
                    outcomes[k]["merge_report"] = mrp.read_text()
                    outcomes[k]["state"] = "done_capped"
                else:
                    # Hard-kill the whole process group so any Claude CLI
                    # subprocess the clone is blocked in also dies. The clone
                    # was spawned with start_new_session=True, so its pgid
                    # equals its pid.
                    try:
                        os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                        time.sleep(2.0)
                        if p.poll() is None:
                            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
                    outcomes[k]["state"] = "timeout"
                    outcomes[k]["merge_report"] = (
                        f"# Merge Report (10h cap)\n\n"
                        f"Clone {k} hit the 10h wall-clock cap and did not\n"
                        f"produce a merge report in the 120s grace window.\n"
                        f"Process group killed.\n"
                    )

        # Stage 9: graceful barrier preemption check. Runs AFTER the
        # per-clone state poll above (so we see the freshest outcomes —
        # any-exit gate needs accurate state). One-shot guard: once
        # preemption fires, future polls skip this check until the
        # barrier collapses naturally and the conductor returns.
        if not preempt_fired and not _all_done():
            should_preempt, reason = _should_preempt_barrier(
                barrier_started_monotonic=barrier_started_monotonic,
                clone_dirs=clone_dirs,
                outcomes=outcomes,
                loop_cfg=_loop_cfg,
            )
            if should_preempt:
                preempt_fired = True
                running_now = [
                    k for k, o in enumerate(outcomes) if o["state"] == "running"
                ]
                print(
                    f"[long-exposure] Fan-out: preempting {len(running_now)} "
                    f"running clone(s) (cause: {reason})",
                    flush=True,
                )
                # Write graceful-stop to each running clone's instance dir.
                # Clones see it at their next cycle boundary (Stage 1 §6.4),
                # finish current cycle, enter merge-mode reporter, write
                # merge_report.md, exit cleanly. Same mechanism the
                # rate-limit-driven graceful-stop uses — no new clone-side
                # code path.
                for k in running_now:
                    cd = clone_dirs[k]
                    try:
                        (cd / "long-exposure.graceful-stop").write_text("")
                        preempted_clones.add(k)
                        print(
                            f"[long-exposure]   clone-{k}: graceful-stop "
                            f"signal written -> {cd}",
                            flush=True,
                        )
                    except OSError as _e:
                        print(
                            f"[long-exposure]   clone-{k}: failed to write "
                            f"graceful-stop signal ({_e}); will exit on its "
                            f"own cadence",
                            flush=True,
                        )

        # Heartbeat every 5 min
        if now - last_status_print > 300:
            running = sum(1 for o in outcomes if o["state"] == "running")
            print(
                f"[long-exposure] Fan-out: {running}/{len(branches)} clones "
                f"still running (fork {fork_id})",
                flush=True,
            )
            last_status_print = now

        if not _all_done():
            time.sleep(poll_interval)

    # Final reap pass. After _all_done() flips True, every clone has a
    # non-"running" outcome — but the Popen child may still be in cleanup
    # (db close, atomic rename, NFS flush) when we observed its
    # merge_report.md. Bound the wait so a hung-cleanup clone doesn't block
    # the whole fan-out collapse; the per-clone poll() above already reaped
    # the common case where cleanup is sub-second. Anything still alive at
    # the timeout is recorded with a warning — caller can SIGKILL the proc
    # group if downstream work needs the slot urgently.
    for k, p in enumerate(procs):
        if p is None or p.poll() is not None:
            continue
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print(
                f"[long-exposure]   clone-{k}: still alive 10s after barrier "
                f"collapse (pid {p.pid}); leaving for caller cleanup.",
                flush=True,
            )

    # --- Aggregate ---
    parts: list[str] = [
        f"# Fan-out Merge (fork {fork_id})\n",
        f"Branches: {len(branches)}. Outcomes: "
        + ", ".join(
            f"clone-{o['clone_k']}={o['state']}" for o in outcomes
        ) + "\n",
    ]
    # Deliverable existence check: did each clone's declared output_artifact
    # actually land on disk under the workspace root? Annotate outcomes so
    # downstream consumers see `missing_deliverable` instead of an
    # optimistic path that doesn't resolve (root-cause of the clone-0
    # silent miss in fork cf14789856e6 / 7a1f3baffe75).
    ws = Path(working_directory) if working_directory else None
    for k, br in enumerate(branches):
        if ws is None:
            outcomes[k]["deliverable_status"] = "unchecked"
            outcomes[k]["deliverable_path"] = br["output_artifact"]
            continue
        target = ws / br["output_artifact"]
        outcomes[k]["deliverable_status"] = (
            "exists" if target.is_file() else "missing"
        )
        outcomes[k]["deliverable_path"] = str(target)

    for k, (br, o) in enumerate(zip(branches, outcomes)):
        parts.append(
            f"\n---\n\n## Clone {k} — {br['output_artifact']}\n\n"
            f"Objective: {br['objective']}\n\n"
            f"Exit state: {o['state']}\n\n"
            f"Deliverable status: {o.get('deliverable_status', 'unchecked')}"
            f" ({o.get('deliverable_path', br['output_artifact'])})\n\n"
            f"{o.get('merge_report') or '(no report)'}\n"
        )
    aggregated = "\n".join(parts)

    # Persist the aggregated raw report for observability and as the
    # fallback when synthesis is skipped or fails.
    try:
        _atomic_write_text(fork_dir / "fanout_merge.md", aggregated)
    except OSError:
        pass

    # Stage 2: hierarchical merge synthesis. When fan-out width >= the
    # configured threshold, ask the reporter to compress the raw N-way
    # concat into one bounded synthesis. The synthesis replaces the raw
    # concat as the post-merge worker's input, keeping the worker's
    # context bounded as fan-out widens. On any failure (synthesis disabled,
    # threshold not met, agent error, file-gate rescue empty) we keep the
    # raw concat — graceful degradation, not a hard failure.
    threshold = MERGE_SYNTHESIS_MIN_BRANCHES
    if config:
        threshold = int(config.get("merge_synthesis_min_branches", threshold))
    if reporter_def is not None and len(branches) >= threshold:
        synthesis_text = _run_merge_synthesis(
            reporter_def=reporter_def,
            config=config or {},
            task=task,
            fork_id=fork_id,
            fork_dir=fork_dir,
            branches=branches,
            outcomes=outcomes,
        )
        if synthesis_text:
            # Build a small index for traceability so the post-merge worker
            # can find raw branch reports if the synthesis is unclear, then
            # replace the aggregated text with synthesis + index.
            index_lines = [f"\n## Branch index (raw reports available for traceability)\n"]
            for k, (br, o) in enumerate(zip(branches, outcomes)):
                index_lines.append(
                    f"- clone-{k}: {br.get('objective', '(no objective)')[:120]}"
                    f" -> {o.get('deliverable_status', 'unchecked')}"
                    f" ({o.get('deliverable_path', br.get('output_artifact', ''))})"
                )
            aggregated = (
                f"# Fan-out Merge (fork {fork_id})\n"
                f"Branches: {len(branches)}. "
                f"Outcomes: " + ", ".join(
                    f"clone-{o['clone_k']}={o['state']}" for o in outcomes
                ) + "\n\n"
                f"## Synthesis (compressed from {len(branches)} branches)\n\n"
                f"{synthesis_text}\n"
                + "\n".join(index_lines) + "\n"
            )
            try:
                _atomic_write_text(fork_dir / "fanout_merge_synthesized.md", aggregated)
            except OSError:
                pass
            print(
                f"[long-exposure] Merge synthesis used (n={len(branches)} "
                f">= threshold={threshold}); post-merge worker reads the "
                f"compressed synthesis + branch index.",
                flush=True,
            )
        else:
            print(
                "[long-exposure] Merge synthesis skipped or failed; using "
                "raw concatenation for post-merge worker.",
                flush=True,
            )

    # Finalize the fork manifest with concluded-outcomes block + per-clone
    # file provenance + cross-clone collisions (reads files_touched.txt).
    _append_fork_manifest_outcomes(fork_dir, outcomes, clone_dirs)

    # Concatenate per-clone shadow ledgers into the workspace main ledger
    # (Plan 1 §6). Shadow ledgers eliminate JSONL append contention between
    # concurrent clones; this barrier-time merge is idempotent (UUID event_id
    # dedup) so re-running after a partial collapse never duplicates events.
    if working_directory:
        try:
            from long_exposure.workspace_bootstrap import concat_clone_ledgers
            n_merged = concat_clone_ledgers(Path(working_directory), fork_dir)
            if n_merged:
                print(
                    f"[long-exposure]   Ledger merge: {n_merged} new events "
                    f"from clone shadow ledgers → workspace main ledger",
                    flush=True,
                )
        except Exception as _le:
            # Best-effort: a ledger-merge failure must not block fan-out collapse.
            print(f"[long-exposure]   Ledger merge skipped: {_le!r}", flush=True)

    # Terminate any clones still alive. The barrier marks a clone done the
    # moment it sees merge_report.md, but the clone's main loop may keep
    # cycling afterwards — without this sweep, clones outlive the root,
    # get reparented to init, and silently burn API budget. SIGTERM first,
    # 10s grace, then SIGKILL. Use process groups (clones were spawned with
    # start_new_session=True) so any Claude CLI subprocess inside the clone
    # dies with it. Mirrors the 10h-cap cleanup at lines ~1694-1697.
    for _k, _p in enumerate(procs):
        if _p is None or _p.poll() is not None:
            continue
        try:
            os.killpg(os.getpgid(_p.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            continue
        try:
            _p.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(_p.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        print(
            f"[long-exposure]   clone-{_k}: terminated post-merge (pid={_p.pid})",
            flush=True,
        )

    # Proactively release every clone's pool slot now that all clones are
    # confirmed dead. heartbeat_sweep on the next root cycle boundary would
    # also reclaim them (PID-based liveness check), but doing it here keeps
    # slot accounting accurate immediately at fan-out collapse — important
    # so the next cycle's fanout_cap reflects the freed capacity.
    # release_slot_by_branch is idempotent: if a clone already released its
    # own slot via the rate-limit path, this is a no-op for that branch.
    try:
        from long_exposure import pool as _pool
        if _pool.is_active():
            for _k in range(len(branches)):
                try:
                    _pool.release_slot_by_branch(fork_id, _k)
                except Exception as _re:
                    print(
                        f"[long-exposure]   clone-{_k}: slot release failed "
                        f"({_re}); heartbeat_sweep will reclaim",
                        flush=True,
                    )
    except Exception as _pe:
        # Pool import / lookup failure is never fatal at fan-out collapse.
        print(
            f"[long-exposure]   pool slot cleanup skipped ({_pe})",
            flush=True,
        )

    print(
        f"[long-exposure] Fan-out collapsed: fork {fork_id} "
        f"({sum(1 for o in outcomes if o['state'] == 'done')}/"
        f"{len(branches)} clean completions)",
        flush=True,
    )
    telemetry.emit(
        "fanout_collapsed",
        phase="fanout",
        provider=(config or {}).get("llm_provider") if config else None,
        model=(config or {}).get("model") if config else None,
        status="ok",
        data={
            "fork_id": fork_id,
            "branches": len(branches),
            "outcomes": [
                {
                    "clone_k": o.get("clone_k"),
                    "state": o.get("state"),
                    "deliverable_status": o.get("deliverable_status"),
                    "deliverable_path": o.get("deliverable_path"),
                }
                for o in outcomes
            ],
        },
    )

    return {
        "aggregated_report": aggregated,
        "fork_id": fork_id,
        "fork_dir": str(fork_dir),
        "clone_dirs": [str(p) for p in clone_dirs],
        "outcomes": outcomes,
    }
