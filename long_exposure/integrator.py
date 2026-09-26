"""Publish every operator's shared work to the shared branch. Separate from
the cycle harness; one runs beside each operator.

## Why a projection, not a merge

The first design merged each run branch into the shared branch. Tested against
real repositories before any live run, it failed twice:

1. Every operator's harness keeps per-operator state at the SAME paths —
   `MEMOIR.md`, `plan_of_record.md`, `reports/cycles/...`. Once the shared
   branch holds operator B's history, git sees operator A's copy of those files
   as a newer revision of B's, and merging the shared branch back into B's run
   replaced B's memoir with A's — silently, with no conflict. A per-path
   "keep mine" merge driver does not prevent it: drivers run only when BOTH
   sides changed a file, and here only one side had, relative to the merge base.
2. The `union` driver, the obvious way to combine two operators' survey files,
   works line by line and de-duplicates shared lines, so two independently
   written entries came out interleaved with one entry's body missing.

So nothing is merged. The shared branch is a PROJECTION: the files an operator
publishes (`federation.publish_paths`), copied from that operator's latest run
branch to `operators/<operator>/<path>`, plus the shared branch's own non-
projected files unchanged. Ownership is by path — only this code writes under
`operators/<x>/`, and only from operator x's branch — so two operators can never
conflict. Duplicated effort still shows, as two operators' copies side by side,
which is the measurement the multi-operator experiment wants.

## Why deterministic

Building the projection is mechanical and has nothing to resolve. An LLM here
would add nondeterminism to the one component whose value is being exactly
repeatable. The cycle harness keeps every judgement call; this only moves bytes.

## How it stays safe

- Git plumbing against a TEMPORARY index in a private bare clone: no working
  tree, no real index, nothing the operator or the cycle harness is using.
- The projection is a pure function of the run-branch tips, so two integrators
  on two machines compute the same tree from the same tips. When both push, the
  remote accepts one and rejects the other; the loser fetches, re-derives on
  the new tip, and either finds nothing left to publish or pushes the newer
  state. git's push rejection is the whole coordination protocol.
- Never forces a push. Never deletes a run branch. A run branch's blobs are
  referenced, not copied, so a projected file is byte-identical to its source.
"""

from __future__ import annotations

import json
import os
import random
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from long_exposure import federation as _federation
from long_exposure import gitcmd as _gitcmd

RUN_PREFIX = "refs/heads/long-exposure/"
TIMEOUT = 60
MAX_PUSH_ATTEMPTS = 6

IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "long-exposure integrator",
    "GIT_AUTHOR_EMAIL": "integrator@long-exposure.invalid",
    "GIT_COMMITTER_NAME": "long-exposure integrator",
    "GIT_COMMITTER_EMAIL": "integrator@long-exposure.invalid",
}


@dataclass
class Round:
    published: bool = False
    reason: str = ""
    commit: str = ""
    push_attempts: int = 0
    rejected: int = 0
    operators: dict = field(default_factory=dict)   # operator -> run tip used

    def as_dict(self) -> dict:
        return {"published": self.published, "reason": self.reason,
                "commit": self.commit, "push_attempts": self.push_attempts,
                "rejected": self.rejected, "operators": self.operators}


def _git(repo: Path, args, **kw):
    kw.setdefault("timeout", TIMEOUT)
    return _gitcmd.run(args, repo, **kw)


def ensure_clone(url: str, clone_dir: Path) -> None:
    """A private bare clone. Bare, so there is no working tree to disturb."""
    clone_dir = Path(clone_dir)
    if (clone_dir / "HEAD").exists():
        return
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    code, _, err = _gitcmd.run(["clone", "--bare", "--quiet", url, str(clone_dir)],
                               clone_dir.parent, TIMEOUT * 5)
    if code != 0:
        raise RuntimeError(f"could not clone {url}: {err.strip()[:200]}")


def latest_runs(repo: Path) -> dict[str, tuple[str, str]]:
    """operator -> (branch, tip), the most recently committed run per operator.

    Run branches are `long-exposure/<operator>/<run_id>`. An operator that has
    run more than once keeps every branch; the newest is its current work.
    """
    # Primary key: newest commit (the LAST --sort is primary in git). Tie-break
    # on the branch name, descending: run_ids are UTC timestamps, so a newer run
    # sorts later. Committer dates have one-second resolution, and a mutation
    # test surfaced two runs committed in the same second making "newest"
    # ambiguous — flaky in a test, and wrong in principle.
    code, out, _ = _git(repo, ["for-each-ref", "--sort=-refname",
                               "--sort=-committerdate",
                               "--format=%(refname) %(objectname)", RUN_PREFIX])
    runs: dict[str, tuple[str, str]] = {}
    if code != 0:
        return runs
    for line in out.splitlines():
        ref, _, sha = line.partition(" ")
        parts = ref[len(RUN_PREFIX):].split("/")
        if len(parts) != 2:
            continue
        op = parts[0]
        if op != _federation.slugify(op):   # not a name the harness would write
            continue
        runs.setdefault(op, (ref[len("refs/heads/"):], sha.strip()))
    return runs


def build_tree(repo: Path, base: str, runs: dict[str, tuple[str, str]],
               publish: list[str]) -> str:
    """The projected tree: `base` minus operators/, plus each operator's
    published paths at operators/<op>/. Returns the tree id."""
    ops_prefix = _federation.OPERATORS_DIR + "/"
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        code, out, err = _git(repo, ["ls-tree", "-r", "-z", base])
        if code != 0:
            raise RuntimeError(f"ls-tree {base}: {err.strip()[:200]}")
        keep = [e for e in out.split("\0") if e and
                not e.split("\t", 1)[-1].startswith(ops_prefix)]
        entries = list(keep)
        for op, (_, tip) in sorted(runs.items()):
            code, out, _ = _git(repo, ["ls-tree", "-r", "-z", tip, "--", *publish])
            if code != 0:
                continue
            for rec in out.split("\0"):
                if not rec or "\t" not in rec:
                    continue
                meta, path = rec.split("\t", 1)
                mode, kind, _sha = meta.split()
                if kind != "blob" or mode not in ("100644", "100755"):
                    continue                   # no symlinks or submodules
                entries.append(f"{meta}\t{ops_prefix}{op}/{path}")
        _git(repo, ["read-tree", "--empty"], env=env)
        code, _, err = _git(repo, ["update-index", "-z", "--index-info"],
                            env=env, input="\0".join(entries) + ("\0" if entries else ""))
        if code != 0:
            raise RuntimeError(f"update-index: {err.strip()[:200]}")
        code, tree, err = _git(repo, ["write-tree"], env=env)
        if code != 0:
            raise RuntimeError(f"write-tree: {err.strip()[:200]}")
        return tree.strip()


def publish_once(repo: Path, config: dict | None) -> Round:
    """Fetch, project, push. Retries a rejected push against the new tip."""
    remote, shared = _federation.remote_and_branch(config)
    publish = _federation.publish_paths(config)
    for value in (remote, shared):
        if _gitcmd.rejects_as_option(value):
            return Round(reason=f"{value!r} would be read by git as an option")
    rnd = Round()
    for attempt in range(1, MAX_PUSH_ATTEMPTS + 1):
        # A bare clone maps remote heads onto local heads; `+` because this
        # clone holds nothing of its own — it is a cache of the remote.
        code, _, err = _git(repo, ["fetch", "--quiet", "--prune", remote,
                                   "+refs/heads/*:refs/heads/*"])
        if code != 0:
            rnd.reason = f"fetch failed: {err.strip()[:160]}"
            return rnd
        code, base, _ = _git(repo, ["rev-parse", "--verify", "--quiet",
                                    f"refs/heads/{shared}"])
        if code != 0:
            rnd.reason = f"no shared branch {shared!r} on {remote}"
            return rnd
        base = base.strip()
        runs = latest_runs(repo)
        rnd.operators = {op: tip for op, (_, tip) in runs.items()}
        if not runs:
            rnd.reason = "no run branches yet"
            return rnd
        tree = build_tree(repo, base, runs, publish)
        code, base_tree, _ = _git(repo, ["rev-parse", f"{base}^{{tree}}"])
        if tree == base_tree.strip():
            rnd.reason = "up to date"
            return rnd
        summary = ", ".join(f"{op}@{tip[:8]}" for op, (_, tip) in sorted(runs.items()))
        code, commit, err = _git(repo, ["commit-tree", tree, "-p", base, "-m",
                                        f"integrator: publish {summary}"],
                                 env=IDENTITY_ENV)
        if code != 0:
            rnd.reason = f"commit-tree failed: {err.strip()[:160]}"
            return rnd
        rnd.push_attempts = attempt
        code, _, err = _git(repo, ["push", "--quiet", remote,
                                   f"{commit.strip()}:refs/heads/{shared}"])
        if code == 0:
            rnd.published, rnd.commit, rnd.reason = True, commit.strip(), "published"
            return rnd
        # Rejected: another integrator moved the shared branch first. Back off
        # a little (both may be retrying) and re-derive on the new tip.
        rnd.rejected += 1
        time.sleep(random.uniform(0.2, 1.5) * attempt)
    rnd.reason = f"push rejected {rnd.rejected} times; will retry next round"
    return rnd


def run_loop(config: dict | None, *, url: str, clone_dir: Path, log_path: Path,
             interval: float = 300, until_pid: int | None = None,
             stop_file: Path | None = None, max_rounds: int | None = None) -> int:
    """Publish every `interval` seconds until the watched process exits (then
    one final round), a stop file appears, or `max_rounds` is reached."""
    ensure_clone(url, clone_dir)
    rounds = 0
    final = False
    while True:
        rounds += 1
        try:
            rnd = publish_once(Path(clone_dir), config)
        except Exception as exc:                      # a round must never kill the loop
            rnd = Round(reason=f"error: {exc!r}"[:300])
        record = {"ts": time.time(), "round": rounds, **rnd.as_dict()}
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError:
            pass
        print(f"[integrator] round {rounds}: {rnd.reason}"
              + (f" ({rnd.rejected} push rejection(s))" if rnd.rejected else ""),
              flush=True)
        if final or (max_rounds and rounds >= max_rounds):
            return 0
        if stop_file and Path(stop_file).exists():
            return 0
        if until_pid and not _alive(until_pid):
            final = True          # publish once more: the run's last commits
            continue
        time.sleep(interval)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
