"""Pre-cycle conflict forecast against a shared branch. Read-only.

## Why this exists

Across 33,596 agent-authored pull requests in 2,807 repositories, cross-agent
PR pairs conflict at **41.7%**, against 19.8% for pairs from the same agent.
40.2% of repositories had agent PR pairs overlapping in time, and those pairs
were 79.4% of all agent PRs. Of the conflicts, 57.6% are content, 26.8%
modify/delete and 15.1% add/add — so roughly **42% are structural**, the kind
no merge driver resolves because they are a disagreement about whether a
component should exist.

Those are a published study's measurements, NOT this repository's: "When
Agents Collide", codex.danielvaughan.com, 2026-07-28. Cited in full in
docs/git-federation.md §4.3. Every other number in this package was measured
here, so the distinction is worth making explicitly.

Everything in that picture is discovered at merge time, which is the most
expensive moment to discover it. This module moves the discovery to the start
of a cycle, where the researcher can still choose differently.

## The two signals, and why one is not enough

`git merge-tree --write-tree` predicts a three-way merge without touching the
working tree, the index or HEAD. It is the obvious instrument, and on its own
it is nearly useless *here*:

    merge-tree HEAD origin/main   ->  CLEAN

...even when the overlap is real, because long-exposure does not commit. The
harness has no git layer at all (see `docs/git-federation.md`), so HEAD sits
wherever a human last left it while all of the cycle's work is uncommitted.
Verified: with the remote having moved `data/spectral.py` and the local
workspace holding an uncommitted edit to the same file, merge-tree reports
clean.

So there are two signals and the second is the one that earns its keep today:

1. **`merge_tree`** — would the *committed* local state conflict with the
   shared branch? Correct for a repo whose agent commits, and forward-looking
   for when the sync layer of `git-federation.md` §8 lands.
2. **`dirty_overlap`** — which paths has the shared branch changed since the
   merge base that are *also* locally modified or untracked? This is the one
   that fires on an uncommitted workspace, which is every long-exposure run
   today.

Neither writes anything. The strongest statement this module makes is a
`git fetch`, which updates remote-tracking refs and no working file, and even
that is skippable.

## Failure posture

Every git call in the harness goes through `_git` in this module — it is the
only place in the package that invokes git at all, which is pinned by
`tests/test_git_federation_doc.py::GitIsReadOnlyTests`. One chokepoint, one
timeout, one never-raises contract.

Every git failure degrades to "no opinion" and the cycle proceeds. A forecast
that stopped a run would be worse than the conflicts it predicts: this is
advisory input for a researcher, not a gate. Absent git, a detached HEAD, an
unresolvable remote ref, a network timeout and a repository that is not a
repository all produce the same thing — a `Radar` with `available=False` and a
one-line reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from xml.sax.saxutils import escape as _xml_escape

from long_exposure import federation as _federation
from long_exposure import gitcmd as _gitcmd

def _rejects_as_option(value: str) -> bool:
    """See `gitcmd.rejects_as_option`. Shared with git_sync, which puts the same
    values — and an operator name — into argv."""
    return _gitcmd.rejects_as_option(value)


# Paths in the block come from ANOTHER operator's repository, so they are
# untrusted text on its way into a prompt. Escaped the same way
# `anti_patterns.build_block` escapes ledger narratives — this is house style,
# not a new safety layer. Found live: a real file can be named
# `data/</shared_branch_overlap>.py`, whose path string contains the block's own
# closing tag and ends it early, so everything after it escapes the block.
_CONTROL = {c: " " for c in range(0x20)}
_CONTROL[0x7F] = " "


def _safe_path(path: str, limit: int = 200) -> str:
    """One line, XML-escaped, bounded. Never structural."""
    text = str(path or "").translate(_CONTROL)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return _xml_escape(text)


# A cycle boundary is not a place to hang. The local commands are fast, so this
# is a ceiling rather than a budget — but `timeout_seconds` LOWERS it too, so
# configuring a tight budget tightens the whole scan and not just the fetch.
# An adversarial rig with a `git` that slept 60s and `timeout_seconds: 2` cost
# 20s here, because the knob only covered fetch while up to six local calls
# each waited the fixed ceiling. `scan` now passes min(ceiling, configured).
LOCAL_TIMEOUT = 20

# `git status --porcelain` on a huge dirty tree is the one local command that
# can be slow, and the output is bounded by this rather than by the tree.
MAX_STATUS_LINES = 5000


@dataclass
class Radar:
    """The forecast. `available=False` means "no opinion", never "no risk"."""

    available: bool = False
    reason: str = ""
    shared_ref: str = ""
    fetched: bool = False
    stale: bool = False
    merge_tree_conflicts: list[str] = field(default_factory=list)
    dirty_overlap: list[str] = field(default_factory=list)
    remote_changed: int = 0
    truncated: bool = False

    @property
    def any_overlap(self) -> bool:
        return bool(self.merge_tree_conflicts or self.dirty_overlap)

    def slices(self) -> list[str]:
        """The overlapping paths grouped into readable regions.

        A researcher acts on "the overlap is in data-spectral", not on forty
        file paths. Uses the same canonicaliser the future claims registry
        will need (`git-federation.md` §6.2).
        """
        seen: dict[str, None] = {}
        for path in [*self.dirty_overlap, *self.merge_tree_conflicts]:
            name = _federation.slice_for_path(path)
            if name:
                seen.setdefault(name, None)
        return list(seen)


def _git(args: list[str], cwd: Path, timeout: int = LOCAL_TIMEOUT
         ) -> tuple[int, str, str]:
    """Run a READ-ONLY git command. See `long_exposure.gitcmd`."""
    return _gitcmd.run(args, cwd, timeout)


def _is_repo(workspace: Path, timeout: int = LOCAL_TIMEOUT) -> bool:
    code, out, _ = _git(["rev-parse", "--is-inside-work-tree"], workspace, timeout)
    return code == 0 and out.strip() == "true"


def _resolve(ref: str, workspace: Path, timeout: int = LOCAL_TIMEOUT) -> str:
    code, out, _ = _git(["rev-parse", "--verify", "--quiet", ref], workspace, timeout)
    return out.strip() if code == 0 else ""


def _dirty_paths(workspace: Path, timeout: int = LOCAL_TIMEOUT
                 ) -> tuple[set[str], bool]:
    """Workspace-relative paths that are modified, staged or untracked.

    `--porcelain=v1 -z` so a path with a space or a quote is one record and
    needs no unquoting. A rename record carries two paths; both count as
    touched.
    """
    code, out, _ = _git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"], workspace,
        timeout,
    )
    if code != 0:
        return set(), False
    records = [r for r in out.split("\0") if r]
    truncated = len(records) > MAX_STATUS_LINES
    paths: set[str] = set()
    pending_rename = False
    for record in records[:MAX_STATUS_LINES]:
        if pending_rename:
            # The record right after an R/C entry is its source path.
            paths.add(record)
            pending_rename = False
            continue
        if len(record) < 4:
            continue
        status, path = record[:2], record[3:]
        if path:
            paths.add(path)
        if "R" in status or "C" in status:
            pending_rename = True
    return paths, truncated


def scan(workspace: Path, config: dict | None = None) -> Radar:
    """Forecast overlap between this workspace and the shared branch.

    Read-only apart from an optional `git fetch`, which updates
    remote-tracking refs and no working file.
    """
    cfg = _federation.settings(config).get("conflict_radar") or {}
    # Shared with git_sync so the two can never point at different branches.
    remote, branch = _federation.remote_and_branch(config)
    timeout = _positive_int(cfg.get("timeout_seconds"), 30)
    max_paths = _positive_int(cfg.get("max_paths"), 20)
    want_fetch = bool(cfg.get("fetch", True))
    # Every local call gets at most the configured budget, so a tight
    # `timeout_seconds` bounds the whole scan rather than the fetch alone.
    local = min(LOCAL_TIMEOUT, timeout)

    for label, value in (("remote", remote), ("shared_branch", branch)):
        if _rejects_as_option(value):
            return Radar(
                reason=f"federation.{label} starts with '-', which git would "
                       f"read as an option: {value!r}"
            )

    workspace = Path(workspace)
    if not workspace.is_dir():
        return Radar(reason=f"workspace is not a directory: {workspace}")
    if not _is_repo(workspace, local):
        return Radar(reason="workspace is not a git repository")

    radar = Radar()
    if want_fetch:
        code, _, err = _git(["fetch", "--quiet", remote, branch],
                            workspace, timeout=timeout)
        radar.fetched = code == 0
        if code != 0:
            # Offline, no remote, no such branch. Carry on against whatever
            # ref is already local and say it may be stale — a forecast from
            # yesterday's refs still beats no forecast.
            radar.stale = True
            radar.reason = f"fetch failed ({err.strip()[:120]})"

    shared_ref = ""
    for candidate in (f"{remote}/{branch}", f"refs/remotes/{remote}/{branch}",
                      branch):
        if _resolve(candidate, workspace, local):
            shared_ref = candidate
            break
    if not shared_ref:
        return Radar(
            reason=f"no such ref: {remote}/{branch}",
            fetched=radar.fetched, stale=radar.stale,
        )
    radar.shared_ref = shared_ref

    head = _resolve("HEAD", workspace, local)
    if not head:
        return Radar(reason="HEAD does not resolve (empty repository?)",
                     shared_ref=shared_ref, fetched=radar.fetched,
                     stale=radar.stale)

    code, base_out, _ = _git(["merge-base", "HEAD", shared_ref], workspace, local)
    base = base_out.strip() if code == 0 else ""
    if not base:
        # Unrelated histories. Nothing meaningful to diff against.
        return Radar(reason=f"no merge base with {shared_ref}",
                     shared_ref=shared_ref, fetched=radar.fetched,
                     stale=radar.stale)

    # --- signal 1: would the committed state conflict? ---
    code, out, _ = _git(
        ["merge-tree", "--write-tree", "--name-only", "HEAD", shared_ref],
        workspace, local,
    )
    if code not in (0, 1):
        # merge-tree needs git >= 2.38 for --write-tree. Older git exits with
        # a usage error, which is a missing signal, not a broken cycle.
        radar.reason = (radar.reason or
                        "merge-tree unavailable (needs git 2.38+)")
    elif code == 1:
        radar.merge_tree_conflicts = _conflict_paths(out)

    # --- signal 2: has the shared branch moved under uncommitted work? ---
    #
    # `-z` is load-bearing, not tidiness. Without it `git diff --name-only`
    # C-QUOTES any path with a non-ASCII or special character
    # (`"data/h\303\251llo.py"`) while `git status -z` reports the raw bytes,
    # so the two sets spell the same path differently and the intersection is
    # empty. The radar then reported NO overlap on exactly the paths most
    # likely to be interesting — a silent false negative, found by an
    # adversarial rig because every test here had used ASCII names.
    code, diff_out, _ = _git(
        ["diff", "--name-only", "-z", f"{base}..{shared_ref}"], workspace, local
    )
    remote_changed = {p for p in diff_out.split("\0") if p.strip()} if code == 0 else set()
    radar.remote_changed = len(remote_changed)
    dirty, truncated = _dirty_paths(workspace, local)
    radar.truncated = truncated
    radar.dirty_overlap = sorted(remote_changed & dirty)[:max_paths]
    radar.merge_tree_conflicts = radar.merge_tree_conflicts[:max_paths]

    radar.available = True
    return radar


def _conflict_paths(merge_tree_output: str) -> list[str]:
    """Paths named in `merge-tree --name-only` conflict output.

    The format is a tree oid, then a NUL-or-newline separated conflicted-file
    list, then informational messages. `--name-only` gives bare paths, so take
    the lines that are neither the leading oid nor a message.
    """
    paths: list[str] = []
    for raw in merge_tree_output.replace("\0", "\n").splitlines():
        line = raw.strip()
        if not line:
            continue
        if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
            continue          # the written tree's oid
        if line.startswith(("CONFLICT", "Auto-merging", "warning:", "error:")):
            continue
        paths.append(line)
    seen: dict[str, None] = {}
    for p in paths:
        seen.setdefault(p, None)
    return list(seen)


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def build_block(workspace: Path, config: dict | None = None) -> str | None:
    """The `<shared_branch_overlap>` prompt block, or None when there is
    nothing worth saying.

    Returns None when the radar has no opinion AND when it has a clean
    forecast. A block saying "no overlap" every cycle is prompt weight that
    teaches the researcher to skip the tag.
    """
    radar = scan(workspace, config)
    if not radar.available or not radar.any_overlap:
        return None

    lines = [
        "<shared_branch_overlap>",
        "  Another operator has changed work you are also touching. This is a",
        f"  FORECAST against {_safe_path(radar.shared_ref)}, not an error, and",
        "  nothing is blocked. Treat it as a reason to choose differently this",
        "  cycle, or to reconcile deliberately rather than at merge time.",
    ]
    if radar.stale:
        lines.append(
            "  NOTE: the remote could not be reached, so this is based on "
            "refs that may be out of date."
        )
    if radar.slices():
        lines.append(f"  regions: {', '.join(_safe_path(s) for s in radar.slices())}")
    if radar.dirty_overlap:
        lines.append(
            "  the shared branch changed these, and you have uncommitted "
            "changes in them:"
        )
        lines += [f"    {_safe_path(p)}" for p in radar.dirty_overlap]
    if radar.merge_tree_conflicts:
        lines.append("  a merge of committed state would conflict in:")
        lines += [f"    {_safe_path(p)}" for p in radar.merge_tree_conflicts]
    if radar.truncated:
        lines.append(
            "  (the working tree has more changes than were scanned; this "
            "list may be incomplete)"
        )
    lines.append("</shared_branch_overlap>")
    return "\n".join(lines)


def describe(config: dict | None = None) -> str:
    cfg = _federation.settings(config).get("conflict_radar") or {}
    if not cfg.get("enabled"):
        return "conflict radar: off"
    remote, branch = _federation.remote_and_branch(config)
    return (
        f"conflict radar: on ({remote}/{branch}, "
        f"fetch={'yes' if cfg.get('fetch', True) else 'no'})"
    )
