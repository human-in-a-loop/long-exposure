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
its own branch, `long-exposure/<operator>/<run_id>`, and nothing ever merges
it. A separate integrator (`long_exposure/integrator.py`) projects each
operator's published paths onto the shared branch under `operators/<op>/`, and
before each cycle this module mirrors every OTHER operator's projection,
read-only, into the workspace's `peers/`. Ownership is by path, so operators
never conflict, and no push is ever forced.

## Never destroy, always recoverable

This module writes to the operator's repository, so it is held to one rule:
nothing it does can lose work. Concretely —

- a crashed cycle's partial edits are STASHED, never reset or cleaned; the
  researcher is told the stash exists and how to restore it;
- it never force-pushes, never rebases, never checks out paths, and never
  passes `--no-verify` (the operator's own hooks apply to its commits);
- it never merges another operator's work into this one. An earlier version
  merged the shared branch in, and with two operators that silently replaced
  one operator's MEMOIR.md with the other's (see `_integrate`);
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
        harness_commit=_harness_commit(),
    )
    if not _ignore_privately(state, _private_patterns(workspace, exclude_paths or [])):
        _say("off: could not record harness-private paths in .git/info/exclude, "
             "so they could end up committed")
        return None

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
                                "--", "."])
    if code != 0:
        _say(f"could not stash ({err.strip()[:120]}); leaving the edits in place")
        return False
    _say(f"stashed: {label}")
    return True


# ---------------------------------------------------------------------------
# Each cycle
# ---------------------------------------------------------------------------

def before_cycle(state: SyncState | None, cycle: int) -> str | None:
    """Mark the cycle in progress, mirror peers' published work, report notices.

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
    """Bring other operators' published work in, as read-only mirrors.

    Reads the shared branch's `operators/<peer>/` trees — projected there by the
    integrator — and mirrors each peer's into the workspace's `peers/<peer>/`,
    which git_sync never commits.

    This REPLACED a design that merged the shared branch into the run branch.
    That design was unsafe with more than one operator: every operator's harness
    keeps per-operator state at the same paths (`MEMOIR.md`,
    `plan_of_record.md`, `reports/...`), and once the shared branch held another
    operator's history, merging it back silently replaced this operator's memoir
    with theirs — no conflict, because relative to the merge base only one side
    had changed the file. Found by testing against real repositories before any
    live run. A mirror never touches this operator's own files, so it cannot do
    that.
    """
    code, _, err = _git(state, ["fetch", "--quiet", state.remote,
                                state.shared_branch], state.timeout)
    if code != 0:
        state.notices.append(
            f"Could not fetch {state.remote}/{state.shared_branch} "
            f"({err.strip()[:100]}); peers' work was not refreshed this cycle.")
        return
    ref = f"refs/remotes/{state.remote}/{state.shared_branch}"
    if _git(state, ["rev-parse", "--verify", "--quiet", ref])[0] != 0:
        return
    code, out, _ = _git(state, ["ls-tree", "-r", "-z", ref, "--",
                                _federation.OPERATORS_DIR + "/"])
    if code != 0:
        return
    peers: dict[str, dict[str, str]] = {}
    for rec in out.split("\0"):
        if not rec or "\t" not in rec:
            continue
        meta, path = rec.split("\t", 1)
        mode, kind, sha = meta.split()
        if kind != "blob" or mode not in ("100644", "100755"):
            continue
        parts = path.split("/")
        if len(parts) < 3:
            continue
        peer, rel = parts[1], "/".join(parts[2:])
        if peer == state.operator or peer != _federation.slugify(peer):
            continue
        if not _safe_rel(rel):
            continue
        peers.setdefault(peer, {})[rel] = sha

    # Files every operator got from the shared branch itself (the directive,
    # a README contract, an empty table with its header) are not duplicated
    # work, and listing them buried the real overlap in the first rehearsal.
    code, tout, _ = _git(state, ["ls-tree", "-r", "-z", "--name-only", ref])
    template = {p for p in tout.split("\0")
                if p and not p.startswith(_federation.OPERATORS_DIR + "/")} if code == 0 else set()
    root = state.workspace / _federation.PEERS_DIR
    lines = []
    for peer in sorted(peers):
        changed = _mirror(state, root / peer, peers[peer])
        overlap = sorted(rel for rel in peers[peer]
                         if rel not in _BOOKKEEPING and rel not in template
                         and (state.workspace / rel).is_file())
        if changed or overlap:
            line = f"{peer}: {len(peers[peer])} files"
            if changed:
                line += f", {changed} updated since last cycle"
            if overlap:
                shown = ", ".join(overlap[:12]) + (" …" if len(overlap) > 12 else "")
                line += f"; you both have {shown}"
            lines.append(line)
    for stale in (sorted(p.name for p in root.iterdir() if p.is_dir())
                  if root.is_dir() else []):
        if stale not in peers and stale == _federation.slugify(stale):
            _remove_tree(root / stale, root)
    if lines:
        state.notices.append(
            "Other operators' published work is mirrored read-only under "
            f"{_federation.PEERS_DIR}/, one folder per operator — read it before "
            "duplicating their work, and never edit it (it is overwritten each "
            "cycle). "
            + " | ".join(lines))


# Every operator publishes these harness files, so "you both have them" is
# always true and says nothing about duplicated research.
_BOOKKEEPING = {"promise_ledger.jsonl", "MEMOIR.md", "plan_of_record.md", "STRUCTURE.md"}


def _safe_rel(rel: str) -> bool:
    """A path from ANOTHER operator's tree, about to be written to disk here."""
    parts = rel.split("/")
    return bool(rel) and not rel.startswith("/") and all(
        p not in ("", ".", "..", ".git") for p in parts)


def _mirror(state: SyncState, dest: Path, files: dict[str, str]) -> int:
    """Make `dest` hold exactly `files` (rel -> blob id). Returns files changed.

    Writes only regular files, only inside `dest`, and never through a symlink:
    `target.resolve()` follows any link an agent may have left in the mirror,
    so a resolved path outside `dest` is skipped rather than written.
    """
    dest_real = dest.resolve()
    changed = 0
    for rel, sha in files.items():
        target = dest / rel
        try:
            target.resolve().relative_to(dest_real)
        except (ValueError, OSError):
            continue
        code, blob, _ = _gitcmd.run(["cat-file", "blob", sha], state.workspace,
                                    LOCAL_TIMEOUT, binary=True)
        if code != 0:
            continue
        try:
            if target.is_file() and not target.is_symlink() and target.read_bytes() == blob:
                continue
            if target.is_symlink():
                target.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
            changed += 1
        except OSError:
            continue
    if dest.is_dir():
        for existing in sorted(dest.rglob("*"), key=lambda q: len(q.parts), reverse=True):
            rel = existing.relative_to(dest).as_posix()
            try:
                if existing.is_symlink() or (existing.is_file() and rel not in files):
                    if rel not in files or existing.is_symlink():
                        existing.unlink()
                        changed += 1
                elif existing.is_dir() and not any(existing.iterdir()):
                    existing.rmdir()
            except OSError:
                continue
    return changed


def _remove_tree(path: Path, root: Path) -> None:
    """Delete a peer mirror that no longer exists upstream. Only ever inside
    `peers/`, which holds nothing but copies of the shared branch."""
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return
    for p in sorted(path.rglob("*"), key=lambda q: len(q.parts), reverse=True):
        try:
            (p.unlink if p.is_symlink() or p.is_file() else p.rmdir)()
        except OSError:
            continue
    try:
        path.rmdir()
    except OSError:
        pass


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
    code, _, err = _git(state, ["add", "-A", "--", "."])
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
                                "--", "."])
    return code == 0 and bool(out.strip())


def _private_patterns(workspace: Path, paths: list[Path]) -> list[str]:
    """Ignore patterns for the paths the harness owns inside the workspace.

    `peers/` always: it is a mirror of the shared branch. And any of `paths`
    — the instance dir, output dir, session DB — that sits inside the
    workspace: the instance dir holds this module's own marker and the run
    state, and stashing it on crash recovery would stash the very state the
    resume is reading.
    """
    out = [f"/{_federation.PEERS_DIR}/"]
    for p in paths:
        try:
            rel = Path(p).resolve().relative_to(workspace)
        except (ValueError, OSError):
            continue                     # outside the workspace: nothing to do
        if str(rel) in ("", "."):
            continue
        pat = "/" + rel.as_posix() + ("/" if Path(p).is_dir() else "")
        if pat not in out:
            out.append(pat)
    return out


_EXCLUDE_HEADER = "# long-exposure git_sync: harness-private paths (safe to delete)"


def _ignore_privately(state: SyncState, patterns: list[str]) -> bool:
    """Record `patterns` in the repository's `.git/info/exclude`.

    git's own mechanism for machine-local ignores: never committed, never
    shared, and honoured by add, status and stash alike. It REPLACED passing
    `:(exclude)` pathspecs, which broke in a way only a rehearsal found. When a
    pathspec names a path the repository's .gitignore already ignores, `git
    add` stages everything else and then exits 1 ("The following paths are
    ignored by one of your .gitignore files"). The live-test repo ignores
    `peers/`, so from the first peer import onward every commit was reported
    as a failed add and skipped — commits, and therefore publishing, silently
    stopped at cycle 3. An ignore rule cannot collide with another ignore rule.
    """
    code, out, _ = _git(state, ["rev-parse", "--git-path", "info/exclude"])
    if code != 0 or not out.strip():
        return False
    path = Path(out.strip())
    if not path.is_absolute():
        path = state.workspace / path
    try:
        existing = path.read_text().splitlines() if path.is_file() else []
        missing = [p for p in patterns if p not in existing]
        if missing:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as fh:
                if existing and existing[-1].strip():
                    fh.write("\n")
                if _EXCLUDE_HEADER not in existing:
                    fh.write(_EXCLUDE_HEADER + "\n")
                fh.write("\n".join(missing) + "\n")
        return True
    except OSError:
        return False


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
