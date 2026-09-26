"""The integrator, against real repositories: projection, idempotence, the
push race between two integrators, and the full round trip back into a peer's
workspace.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from long_exposure import integrator as ig
from long_exposure import git_sync as gs
from long_exposure import workspace_bootstrap as wb

PUBLISH = ["promise_ledger.jsonl", "MEMOIR.md", "plan_of_record.md", "literature-survey"]
CFG = {"federation": {"publish_paths": PUBLISH}}


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True)


def ev(op, narrative):
    import uuid
    return json.dumps({"event_id": str(uuid.uuid4()), "ts": "2026-01-02T00:00:00Z",
                       "run_id": "r", "cycle": 1, "agent": "auditor",
                       "milestone_id": "families/cuprates", "operator": op,
                       "status": "in-progress", "narrative": narrative,
                       "confidence": {"level": "medium", "rationale": "r",
                                      "assessor": "auditor"}}) + "\n"


class IntegratorTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.T = Path(self._tmp.name)
        self.bare = self.T / "bare"
        git("init", "-q", "--bare", str(self.bare), cwd=self.T)
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.bare)
        seed = self.clone("seed")
        (seed / "DIRECTIVE.md").write_text("survey superconductors\n")
        (seed / "literature-survey").mkdir()
        (seed / "literature-survey/README.md").write_text("contract\n")
        git("add", "-A", cwd=seed)
        git("commit", "-qm", "seed", cwd=seed)
        git("push", "-q", "origin", "HEAD:main", cwd=seed)

    def tearDown(self):
        self._tmp.cleanup()

    def clone(self, name):
        path = self.T / name
        git("clone", "-q", str(self.bare), str(path), cwd=self.T)
        git("config", "user.email", f"{name}@x", cwd=path)
        git("config", "user.name", name, cwd=path)
        git("checkout", "-q", "-B", "main", cwd=path)
        return path

    def operator_run(self, op, run="run-1", files=None, *, from_main=True):
        """Commit `files` on long-exposure/<op>/<run> and push it."""
        repo = self.T / f"op-{op}"
        if not repo.exists():
            repo = self.clone(f"op-{op}")
        git("fetch", "-q", "origin", cwd=repo)
        branch = f"long-exposure/{op}/{run}"
        exists = git("rev-parse", "--verify", "-q", f"refs/heads/{branch}", cwd=repo).returncode == 0
        if exists:
            git("switch", "-q", branch, cwd=repo)
        else:
            git("switch", "-q", "-c", branch, "origin/main" if from_main else "HEAD", cwd=repo)
        for rel, body in (files or {}).items():
            p = repo / rel
            if body is None:
                p.unlink(missing_ok=True)
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        git("add", "-A", cwd=repo)
        git("commit", "-qm", f"{op} {run}", cwd=repo)
        git("push", "-q", "origin", branch, cwd=repo)
        return repo

    def integrator(self, name="ig1"):
        path = self.T / name
        ig.ensure_clone(str(self.bare), path)
        return path

    def main_files(self):
        out = git("ls-tree", "-r", "--name-only", "main", cwd=self.bare).stdout
        return sorted(out.split())

    def main_show(self, path):
        return git("show", f"main:{path}", cwd=self.bare).stdout


class ProjectionTests(IntegratorTestCase):
    def setUp(self):
        super().setUp()
        self.operator_run("alice", files={
            "MEMOIR.md": "alice memoir\n",
            "literature-survey/families/cuprates.md": "## YBCO (alice)\n",
            "reports/cycles/r1.md": "alice's report — not published\n",
            "promise_ledger.jsonl": ev("alice", "A"),
        })
        self.operator_run("bob", files={
            "MEMOIR.md": "bob memoir\n",
            "literature-survey/families/cuprates.md": "## YBCO (bob)\n",
        })

    def test_each_operators_published_paths_land_under_their_own_name(self):
        rnd = ig.publish_once(self.integrator(), CFG)
        self.assertTrue(rnd.published, rnd.reason)
        files = self.main_files()
        for f in ("operators/alice/MEMOIR.md",
                  "operators/alice/literature-survey/families/cuprates.md",
                  "operators/alice/promise_ledger.jsonl",
                  "operators/bob/MEMOIR.md",
                  "operators/bob/literature-survey/families/cuprates.md"):
            self.assertIn(f, files)
        self.assertEqual(self.main_show("operators/alice/MEMOIR.md"), "alice memoir\n")
        self.assertEqual(self.main_show("operators/bob/MEMOIR.md"), "bob memoir\n")

    def test_two_operators_can_never_conflict(self):
        """Both wrote cuprates.md; both copies survive, side by side, intact —
        the case where the union merge driver interleaved them."""
        ig.publish_once(self.integrator(), CFG)
        self.assertEqual(
            self.main_show("operators/alice/literature-survey/families/cuprates.md"),
            "## YBCO (alice)\n")
        self.assertEqual(
            self.main_show("operators/bob/literature-survey/families/cuprates.md"),
            "## YBCO (bob)\n")

    def test_unpublished_paths_stay_private(self):
        ig.publish_once(self.integrator(), CFG)
        self.assertFalse([f for f in self.main_files() if "reports/" in f])

    def test_the_shared_branchs_own_files_are_kept(self):
        ig.publish_once(self.integrator(), CFG)
        files = self.main_files()
        self.assertIn("DIRECTIVE.md", files)
        self.assertIn("literature-survey/README.md", files)
        self.assertNotIn("MEMOIR.md", files, "no operator's memoir at the root")

    def test_projected_blobs_are_byte_identical_to_the_source(self):
        ig.publish_once(self.integrator(), CFG)
        src = git("rev-parse", "long-exposure/alice/run-1:MEMOIR.md", cwd=self.bare).stdout
        dst = git("rev-parse", "main:operators/alice/MEMOIR.md", cwd=self.bare).stdout
        self.assertEqual(src, dst)

    def test_a_second_round_with_nothing_new_is_a_no_op(self):
        path = self.integrator()
        ig.publish_once(path, CFG)
        tip = git("rev-parse", "main", cwd=self.bare).stdout
        rnd = ig.publish_once(path, CFG)
        self.assertFalse(rnd.published)
        self.assertEqual(rnd.reason, "up to date")
        self.assertEqual(git("rev-parse", "main", cwd=self.bare).stdout, tip)

    def test_the_newest_run_per_operator_is_projected(self):
        ig.publish_once(self.integrator(), CFG)
        self.operator_run("alice", run="run-2", files={"MEMOIR.md": "alice run 2\n"})
        ig.publish_once(self.integrator(), CFG)
        self.assertEqual(self.main_show("operators/alice/MEMOIR.md"), "alice run 2\n")

    def test_an_upstream_deletion_propagates(self):
        path = self.integrator()
        ig.publish_once(path, CFG)
        self.operator_run("alice", files={
            "literature-survey/families/cuprates.md": None})
        ig.publish_once(path, CFG)
        self.assertNotIn("operators/alice/literature-survey/families/cuprates.md",
                         self.main_files())

    def test_a_branch_name_the_harness_would_not_write_is_ignored(self):
        self.operator_run("Not_A_Slug", files={"MEMOIR.md": "x\n"})
        ig.publish_once(self.integrator(), CFG)
        self.assertFalse([f for f in self.main_files() if "Not_A_Slug" in f])

    def test_symlinks_are_never_projected(self):
        """A symlink in a run branch could point anywhere on a peer's machine
        once mirrored. Only regular files are published."""
        repo = self.T / "op-alice"
        link = repo / "literature-survey" / "escape"
        git("switch", "-q", "long-exposure/alice/run-1", cwd=repo)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to("/etc/passwd")
        git("add", "-A", cwd=repo)
        git("commit", "-qm", "a symlink", cwd=repo)
        git("push", "-q", "origin", "long-exposure/alice/run-1", cwd=repo)
        self.assertIn("120000", git("ls-tree", "-r", "long-exposure/alice/run-1",
                                    cwd=self.bare).stdout, "premise: the link is committed")
        ig.publish_once(self.integrator(), CFG)
        self.assertNotIn("operators/alice/literature-survey/escape", self.main_files())

    def test_run_branches_are_never_modified_or_deleted(self):
        before = git("for-each-ref", "refs/heads/long-exposure/", cwd=self.bare).stdout
        ig.publish_once(self.integrator(), CFG)
        after = git("for-each-ref", "refs/heads/long-exposure/", cwd=self.bare).stdout
        self.assertEqual(before, after)


class RaceTests(IntegratorTestCase):
    def test_two_integrators_converge_through_a_rejected_push(self):
        """Integrator 2 is interrupted right before its push while integrator 1
        publishes. The remote rejects 2; it re-derives on the new tip."""
        self.operator_run("alice", files={"MEMOIR.md": "alice\n"})
        self.operator_run("bob", files={"MEMOIR.md": "bob\n"})
        ig1, ig2 = self.integrator("ig1"), self.integrator("ig2")
        real = ig._git
        fired = {"done": False}

        def interleave(repo, args, **kw):
            if (Path(repo) == ig2 and args and args[0] == "push"
                    and not fired["done"]):
                fired["done"] = True
                # Bob commits more, and the OTHER machine's integrator sees it
                # and publishes first — so integrator 2's pending commit is now
                # both stale and not a fast-forward.
                self.operator_run("bob", files={"MEMOIR.md": "bob, newer\n"})
                ig.publish_once(ig1, CFG)
            return real(repo, args, **kw)

        with mock.patch.object(ig, "_git", side_effect=interleave), \
             mock.patch.object(ig.time, "sleep"):
            rnd = ig.publish_once(ig2, CFG)
        self.assertTrue(fired["done"])
        self.assertGreaterEqual(rnd.rejected, 1, "the push must have been rejected")
        # After the rejection it re-derived on the new tip and found the other
        # integrator had already published exactly what it would have.
        self.assertIn(rnd.reason, ("published", "up to date"))
        self.assertEqual(self.main_show("operators/bob/MEMOIR.md"), "bob, newer\n")
        self.assertEqual(self.main_show("operators/alice/MEMOIR.md"), "alice\n")

    def test_identical_views_make_identical_commits(self):
        """Why a same-second race between two integrators with the same view
        often produces no rejection at all: commit-tree over the same tree,
        parent, message, identity and timestamp yields the same commit, so the
        second push is a no-op. Found when the first version of the race test
        failed to provoke the rejection it expected."""
        self.operator_run("alice", files={"MEMOIR.md": "alice\n"})
        ig1, ig2 = self.integrator("ig1"), self.integrator("ig2")
        env = {**ig.IDENTITY_ENV, "GIT_AUTHOR_DATE": "1700000000 +0000",
               "GIT_COMMITTER_DATE": "1700000000 +0000"}
        with mock.patch.object(ig, "IDENTITY_ENV", env):
            ig.publish_once(ig1, CFG)
            tip1 = git("rev-parse", "main", cwd=self.bare).stdout
            rnd = ig.publish_once(ig2, CFG)
        self.assertEqual(rnd.reason, "up to date")
        self.assertEqual(git("rev-parse", "main", cwd=self.bare).stdout, tip1)

    def test_two_integrators_with_the_same_view_agree(self):
        """A pure function of the run tips: the second finds nothing to do."""
        self.operator_run("alice", files={"MEMOIR.md": "alice\n"})
        ig.publish_once(self.integrator("ig1"), CFG)
        rnd = ig.publish_once(self.integrator("ig2"), CFG)
        self.assertEqual(rnd.reason, "up to date")


class LoopTests(IntegratorTestCase):
    def test_it_stops_after_the_watched_process_exits_with_one_final_round(self):
        self.operator_run("alice", files={"MEMOIR.md": "alice\n"})
        log = self.T / "log.jsonl"
        dead = subprocess.Popen(["true"])
        dead.wait()
        ig.run_loop(CFG, url=str(self.bare), clone_dir=self.T / "ig", log_path=log,
                    interval=0, until_pid=dead.pid)
        rows = [json.loads(l) for l in log.read_text().splitlines()]
        self.assertEqual(len(rows), 2, "one round, then one final round")
        self.assertTrue(rows[0]["published"])
        self.assertEqual(rows[1]["reason"], "up to date")

    def test_a_failing_round_does_not_kill_the_loop(self):
        log = self.T / "log.jsonl"
        with mock.patch.object(ig, "publish_once", side_effect=RuntimeError("boom")):
            ig.run_loop(CFG, url=str(self.bare), clone_dir=self.T / "ig",
                        log_path=log, interval=0, max_rounds=3)
        rows = [json.loads(l) for l in log.read_text().splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertIn("boom", rows[0]["reason"])


class RoundTripTests(IntegratorTestCase):
    """The whole loop: operator commits -> integrator projects -> the OTHER
    operator's next cycle mirrors it, and its own files are untouched."""

    def test_alices_work_reaches_bobs_workspace_and_ledger(self):
        saved = os.environ.pop("LONG_EXPOSURE_OPERATOR", None)
        try:
            bob = self.clone("bob-ws")
            (bob / "MEMOIR.md").write_text("bob's own memoir\n")
            # Bob has findings of his own, so the summary has two operators and
            # labels each line (one operator shows no column, by design).
            (bob / "promise_ledger.jsonl").write_text(ev("bob", "BOB is on hydrides"))
            cfg_b = {"federation": {"operator": "bob", "publish_paths": PUBLISH,
                                    "git_sync": {"enabled": True}}}
            sync = gs.begin(bob, cfg_b, run_id="run-b", marker_dir=self.T / "ib",
                            last_completed_cycle=0)
            self.assertIsNotNone(sync)
            self.operator_run("alice", files={
                "MEMOIR.md": "alice's memoir\n",
                "literature-survey/families/hydrides.md": "## H3S (alice)\n",
                "promise_ledger.jsonl": ev("alice", "ALICE is on cuprates"),
            })
            ig.publish_once(self.integrator(), CFG)
            block = gs.before_cycle(sync, 1)
            self.assertEqual((bob / "MEMOIR.md").read_text(), "bob's own memoir\n")
            self.assertEqual(
                (bob / "peers/alice/literature-survey/families/hydrides.md").read_text(),
                "## H3S (alice)\n")
            self.assertIn("alice", block)
            summary = wb.summarize_ledger(bob)
            self.assertIn("ALICE is on cuprates", summary)
            self.assertIn("BOB is on hydrides", summary)
            self.assertIn("Operators on this ledger: alice, bob", summary)
        finally:
            if saved is not None:
                os.environ["LONG_EXPOSURE_OPERATOR"] = saved


if __name__ == "__main__":
    unittest.main()
