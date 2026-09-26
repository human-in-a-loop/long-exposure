"""Commit the workspace at every cycle boundary. Off by default.

## Why

Two problems, one mechanism.

**Crash consistency, for a single operator.** The harness saves its state at
cycle boundaries, not per agent turn, and never snapshotted the workspace. A
process that died twelve minutes into a thirteen-minute worker turn lost the
cycle AND left that dead turn's half-finished edits in a workspace that no
longer matched the saved state. A commit per cycle is a snapshot per cycle, so
a resumed run can put the dead turn's edits aside and start from a workspace
that matches its state.

**Federation, for several.** `docs/git-federation.md` §4: each run commits to
its own branch, `long-exposure/<operator>/<run_id>`, and merges the shared
branch in before each cycle. Runs never contend for a branch, so a push is
never forced, and convergence to the shared branch stays a pull request a
person reviews.

## Never destroy, always recoverable

This module writes to the operator's repository, so it is held to one rule:
nothing it does can lose work. Concretely —

- a crashed cycle's partial edits are STASHED, never reset or cleaned; the
  researcher is told the stash exists and how to restore it;
- it never force-pushes, never rebases, never checks out paths, and never
  passes `--no-verify` (the operator's own hooks apply to its commits);
- a merge that conflicts is aborted and the conflict is handed to the next
  researcher as an input. It is never auto-resolved: conflicts in generated
  artifacts are usually "keep both", conflicts in source are a disagreement
  between two runs, and neither is a strategy flag's call;
- every failure — no remote, a rejected push, a failed commit, a hook that
  blocks — is a notice to the next cycle, and the run continues.

`tests/test_git_federation_doc.py::GitPolicyTests` fails the build if a
destructive subcommand appears here.

## Scope

Root process only. Fan-out clones share the root's workspace, so letting each
clone commit would be N processes writing one repository; the root commits the
combined result once the barrier collapses.

The workspace must be the repository's top level. Switching a branch changes
the whole working tree, and doing that to a monorepo whose research lives in a
subdirectory would move files the harness has no business touching.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from long_exposure import federation as _federation
from long_exposure import flags as _flags
from long_exposure import gitcmd as _gitcmd

MARKER_FILENAME = "git_sync_in_progress.json"
BRANCH_PREFIX = "long-exposure"

# Local operations are fast; fetch/push/merge get the configured budget.
LOCAL_TIMEOUT = 20


@dataclass
class SyncState:
    workspace: Path
    branch: str
    remote: str
    shared_branch: str
    push: bool
    integrate: bool
    timeout: int
    operator: str
    run_id: str
    marker_dir: Path
    excludes: list[str] = field(default_factory=list)
    harness_commit: str = "unknown"
    # Things to tell the next researcher. Drained by `before_cycle`.
    notices: list[str] = field(default_factory=list)


def _git(state_or_ws, args: list[str], timeout: int = LOCAL_TIMEOUT):
    ws = state_or_ws.workspace if isinstance(state_or_ws, SyncState) else state_or_ws
    return _gitcmd.run(args, ws, timeout)


def _say(message: str) -> None:
    print(f"[git-sync] {message}", flush=True)


def settings(config: dict | None) -> dict:
    return _federation.settings(config).get("git_sync") or {}


def enabled(config: dict | None) -> bool:
    return _flags.truthy(settings(config).get("enabled"), False,
                         name="federation.git_sync.enabled")


def branch_name(operator: str, run_id: str) -> str:
    return f"{BRANCH_PREFIX}/{operator}/{run_id}"


# ---------------------------------------------------------------------------
# Run start
# ---------------------------------------------------------------------------

def begin(
    workspace: Path,
    config: dict | None,
    *,
    run_id: str,
    marker_dir: Path,
    last_completed_cycle: int,
    exclude_paths: list[Path] | None = None,
) -> SyncState | None:
    """Put the workspace on this run's branch, recovering from a crash first.

    Returns None — sync off for this run, with the reason printed — whenever a
    precondition fails. Refusing is always safe; guessing is not.
    """
    if not enabled(config):
        return None
    cfg = settings(config)
    workspace = Path(workspace).resolve()
    remote, shared = _federation.remote_and_branch(config)
    operator = _federation.operator_name(config)

    for label, value in (("remote", remote), ("shared_branch", shared),
                         ("operator", operator), ("run_id", run_id)):
        if _gitcmd.rejects_as_option(value):
            _say(f"off: {label} {value!r} would be read by git as an option")
            return None

    code, top, _ = _git(workspace, ["rev-parse", "--show-toplevel"])
    if code != 0:
        _say(f"off: {workspace} is not a git repository")
        return None
    if Path(top.strip()).resolve() != workspace:
        _say(f"off: the workspace must be the repository's top level "
             f"({top.strip()}), so switching branches cannot move files "
             "outside it")
        return None

    branch = branch_name(operator, run_id)
    code, _, err = _git(workspace, ["check-ref-format", "--branch", branch])
    if code != 0:
        _say(f"off: {branch!r} is not a valid branch name ({err.strip()[:80]})")
        return None

    state = SyncState(
        workspace=workspace, branch=branch, remote=remote,
        shared_branch=shared, push=_flags.truthy(cfg.get("push"), True),
        integrate=_flags.truthy(cfg.get("integrate"), True),
        timeout=_positive_int(cfg.get("timeout_seconds"), 60),
        operator=operator, run_id=run_id, marker_dir=Path(marker_dir),
        excludes=_exclude_specs(workspace, exclude_paths or []),
        harness_commit=_harness_commit(),
    )

    marker = _read_marker(state)
    if marker is not None and marker.get("run_id") != run_id:
        # A marker from a DIFFERENT run, left in a reused instance dir. It
        # says nothing about this run's workspace, and honouring it would
        # stash the bootstrap files this run just wrote. Discard it.
        marker = None
    if marker is not None and marker.get("cycle", 0) > last_completed_cycle:
        # Died DURING a cycle: its edits are partial. Put them aside before
        # anything else — a stash also leaves a clean tree to switch from.
        _stash_crashed_cycle(state, marker.get("cycle"))
        marker = None

    if not _ensure_branch(state):
        return None

    if marker is not None:
        # Died after the cycle's state was saved but before its commit. The
        # edits are that cycle's COMPLETED work, and the state already counts
        # it — so commit it, do not stash it.
        if _commit(state, _subject(marker.get("cycle"), "recovered commit")):
            state.notices.append(
                f"Cycle {marker.get('cycle')} completed but the process died "
                "before committing it; its work was committed on resume.")
    elif _is_dirty(state):
        # Uncommitted work at run start that no crash explains: an operator's
        # edits between runs, a fresh workspace's bootstrap files, or both.
        # Committed separately so every cycle commit holds only that cycle.
        _commit(state, "long-exposure: workspace state at run start")

    _clear_marker(state)
    _say(f"on: committing each cycle to {branch}"
         + (f", pushing to {remote}" if state.push else " (local only)"))
    return state


def _ensure_branch(state: SyncState) -> bool:
    code, current, _ = _git(state, ["rev-parse", "--abbrev-ref", "HEAD"])
    if code == 0 and current.strip() == state.branch:
        return True
    exists = _git(state, ["rev-parse", "--verify", "--quiet",
                          f"refs/heads/{state.branch}"])[0] == 0
    args = ["switch", state.branch] if exists else ["switch", "-c", state.branch]
    code, _, err = _git(state, args)
    if code == 0:
        return True
    # A switch to an existing branch can refuse over dirty files. Put them
    # aside (recoverably) and try once more rather than giving up.
    if exists and _is_dirty(state):
        _stash(state, "uncommitted work blocking the switch to the run branch")
        code, _, err = _git(state, args)
        if code == 0:
            return True
    _say(f"off: could not switch to {state.branch} ({err.strip()[:120]})")
    return False


def _stash_crashed_cycle(state: SyncState, cycle) -> None:
    if not _is_dirty(state):
        return
    label = f"crashed cycle {cycle} of {state.run_id}"
    if _stash(state, label):
        state.notices.append(
            f"A previous process died during cycle {cycle}. Its partial edits "
            f"were STASHED, not discarded (git stash message: 'long-exposure: "
            f"{label}'). Review with `git stash list` / `git stash show -p`, "
            "and restore with `git stash pop` if they were worth keeping.")


def _stash(state: SyncState, label: str) -> bool:
    code, _, err = _git(state, ["stash", "push", "--include-untracked",
                                "-m", f"long-exposure: {label}",
                                "--", ".", *state.excludes])
    if code != 0:
        _say(f"could not stash ({err.strip()[:120]}); leaving the edits in place")
        return False
    _say(f"stashed: {label}")
    return True


# ---------------------------------------------------------------------------
# Each cycle
# ---------------------------------------------------------------------------

def before_cycle(state: SyncState | None, cycle: int) -> str | None:
    """Mark the cycle in progress, merge the shared branch in, report notices.

    Returns a `<git_sync>` block for the researcher, or None when there is
    nothing to say.
    """
    if state is None:
        return None
    _write_marker(state, cycle)
    if state.integrate:
        _integrate(state)
    return _drain_block(state)


def _integrate(state: SyncState) -> None:
    code, _, err = _git(state, ["fetch", "--quiet", state.remote,
                                state.shared_branch], state.timeout)
    if code != 0:
        state.notices.append(
            f"Could not fetch {state.remote}/{state.shared_branch} "
            f"({err.strip()[:100]}); this cycle starts from local history "
            "and may be missing other operators' work.")
        return
    ref = f"refs/remotes/{state.remote}/{state.shared_branch}"
    if _git(state, ["rev-parse", "--verify", "--quiet", ref])[0] != 0:
        return
    if _is_dirty(state):
        # Only reachable if something edited the workspace between cycles.
        state.notices.append(
            f"Skipped merging {state.remote}/{state.shared_branch}: the "
            "workspace had uncommitted changes at cycle start.")
        return
    code, out, err = _git(state, [*_identity(state), "merge", "--no-edit",
                                  ref], state.timeout)
    if code == 0:
        if "Already up to date" not in out:
            state.notices.append(
                f"Merged new work from {state.remote}/{state.shared_branch} "
                "into this run before the cycle started.")
        return
    conflicted = [p for p in _git(state, ["diff", "--name-only", "-z",
                                          "--diff-filter=U"])[1].split("\0") if p]
    _git(state, ["merge", "--abort"])
    if conflicted:
        state.notices.append(
            f"Merging {state.remote}/{state.shared_branch} CONFLICTS with this "
            "run, so it was aborted and not auto-resolved. Conflicting files: "
            + ", ".join(conflicted[:20])
            + ". Another run changed the same files differently — decide "
              "whether to reconcile them this cycle or work elsewhere.")
    else:
        state.notices.append(
            f"Could not merge {state.remote}/{state.shared_branch} "
            f"({(err or out).strip()[:120]}); continuing without it.")


def after_cycle(state: SyncState | None, cycle: int,
                topic: str | None = None) -> bool:
    """Commit the cycle's work and push the run branch. Returns True if a
    commit was made.

    Runs AFTER the cycle's state is saved, so a commit never records a cycle
    the state does not know about.
    """
    if state is None:
        return False
    try:
        committed = _commit(state, _subject(cycle, topic))
        if committed:
            _push(state)
        return committed
    finally:
        # The cycle COMPLETED, whatever happened to its commit. Leaving the
        # marker would make the next start treat finished work as a crash.
        _clear_marker(state)


def finish(state: SyncState | None) -> bool:
    """Commit what the run wrote after its last cycle, and push.

    The closing report and the end-of-run pipeline write into the workspace
    after the final cycle's commit. Without this they would sit uncommitted
    and be swept into the NEXT run's "workspace state at run start" commit,
    attributed to the wrong run.
    """
    if state is None:
        return False
    committed = _commit(state, "long-exposure: end of run")
    if committed:
        _push(state)
    return committed


def _push(state: SyncState) -> None:
    if not state.push:
        return
    # An explicit refspec to this run's own branch, never forced: runs never
    # share a branch, so a rejection means something outside the harness moved
    # it, and overwriting that would be the harness destroying work.
    code, _, err = _git(state, ["push", "--quiet", state.remote,
                                f"HEAD:refs/heads/{state.branch}"],
                        state.timeout)
    if code != 0:
        state.notices.append(
            f"Pushing {state.branch} to {state.remote} failed "
            f"({err.strip()[:120]}); the commit is safe locally and the next "
            "successful push will carry it.")


def _commit(state: SyncState, subject: str) -> bool:
    code, _, err = _git(state, ["add", "-A", "--", ".", *state.excludes])
    if code != 0:
        state.notices.append(f"git add failed ({err.strip()[:120]}).")
        return False
    if _git(state, ["diff", "--cached", "--quiet"])[0] == 0:
        return False                      # nothing to commit
    body = (f"Long-Exposure-Run: {state.run_id}\n"
            f"Long-Exposure-Operator: {state.operator}\n"
            f"Long-Exposure-Harness: {state.harness_commit}")
    code, _, err = _git(state, [*_identity(state), "commit", "--quiet",
                                "-m", subject, "-m", body])
    if code != 0:
        # Often the operator's own pre-commit hook. Respected, not bypassed:
        # the work stays staged and rides along with the next commit.
        state.notices.append(
            f"The commit for '{subject}' failed ({err.strip()[:160]}); the "
            "work is still in the workspace and will be included next cycle.")
        return False
    return True


def _subject(cycle, topic) -> str:
    clean = " ".join(str(topic or "").split())[:72]
    return f"long-exposure cycle {cycle}" + (f": {clean}" if clean else "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _identity(state: SyncState) -> list[str]:
    """A committer identity, only when the repository has none.

    The operator's own identity is used whenever it is configured, so commits
    are attributable to them. Without one, git refuses to commit at all; the
    fallback uses the reserved `.invalid` TLD so it can never be mistaken for a
    real address.
    """
    if _git(state, ["config", "user.email"])[0] == 0:
        return []
    return ["-c", f"user.name=long-exposure ({state.operator})",
            "-c", f"user.email={state.operator}@long-exposure.invalid"]


def _is_dirty(state: SyncState) -> bool:
    code, out, _ = _git(state, ["status", "--porcelain", "--untracked-files=all",
                                "--", ".", *state.excludes])
    return code == 0 and bool(out.strip())


def _exclude_specs(workspace: Path, paths: list[Path]) -> list[str]:
    """Pathspecs keeping harness-private files out of commits and stashes.

    The instance dir holds this module's own marker and the run state. If it
    sits inside the workspace, stashing it on crash recovery would stash the
    very state the resume is reading.
    """
    specs = []
    for p in paths:
        try:
            rel = Path(p).resolve().relative_to(workspace)
        except (ValueError, OSError):
            continue                     # outside the workspace: nothing to do
        if str(rel) not in ("", "."):
            specs.append(f":(exclude){rel.as_posix()}")
    return specs


def _harness_commit() -> str:
    """The harness's own commit, for version-drift visibility across operators."""
    root = Path(__file__).resolve().parent.parent
    code, out, _ = _gitcmd.run(["rev-parse", "--short=12", "HEAD"], root, 5)
    return out.strip() if code == 0 and out.strip() else "unknown"


def _marker_path(state: SyncState) -> Path:
    return state.marker_dir / MARKER_FILENAME


def _write_marker(state: SyncState, cycle: int) -> None:
    try:
        state.marker_dir.mkdir(parents=True, exist_ok=True)
        _marker_path(state).write_text(json.dumps({
            "run_id": state.run_id, "cycle": cycle, "branch": state.branch,
            "started_at": time.time()}))
    except OSError:
        pass


def _read_marker(state: SyncState) -> dict | None:
    try:
        data = json.loads(_marker_path(state).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        data["cycle"] = int(data.get("cycle", 0))
    except (TypeError, ValueError):
        return None
    return data


def _clear_marker(state: SyncState) -> None:
    try:
        _marker_path(state).unlink(missing_ok=True)
    except OSError:
        pass


def _drain_block(state: SyncState) -> str | None:
    if not state.notices:
        return None
    from long_exposure.conflict_radar import _safe_path as _safe

    notices, state.notices = state.notices, []
    lines = ["<git_sync>",
             f"  This run commits each cycle to {_safe(state.branch)}. Notes "
             "from the harness since the last cycle:"]
    lines += [f"  - {_safe(n, limit=600)}" for n in notices]
    lines.append("</git_sync>")
    return "\n".join(lines)


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
