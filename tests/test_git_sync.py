"""git_sync against real repositories.

Real git, never mocked: every property that matters here is a property of
git's behaviour. The rule under test throughout is the module's own — nothing
it does can lose work — so most tests end by checking the work is still
somewhere recoverable.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from long_exposure import git_sync as gs
from long_exposure import gitcmd

RUN = "run-2026-09-26T100000Z"


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True)


def cfg(**sync):
    block = {"enabled": True, **sync}
    return {"federation": {"operator": "alice", "git_sync": block}}


class SyncTestCase(unittest.TestCase):
    """A bare remote, one clone as the workspace, an instance dir inside it
    (the awkward case: the marker and state live in the workspace)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.T = Path(self._tmp.name)
        self._saved = os.environ.pop("LONG_EXPOSURE_OPERATOR", None)
        git("init", "-q", "--bare", str(self.T / "bare"), cwd=self.T)
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.T / "bare")
        self.ws = self.T / "ws"
        git("clone", "-q", str(self.T / "bare"), str(self.ws), cwd=self.T)
        git("config", "user.email", "alice@x", cwd=self.ws)
        git("config", "user.name", "alice", cwd=self.ws)
        git("checkout", "-q", "-b", "main", cwd=self.ws)
        (self.ws / "data").mkdir()
        (self.ws / "data/f.py").write_text("v1\n")
        git("add", "-A", cwd=self.ws)
        git("commit", "-qm", "base", cwd=self.ws)
        git("push", "-q", "origin", "main", cwd=self.ws)
        self.inst = self.ws / "instances" / "i1"      # inside the workspace
        self.inst.mkdir(parents=True)
        (self.inst / "exploration_state.json").write_text('{"cycle": 0}')

    def tearDown(self):
        if self._saved is not None:
            os.environ["LONG_EXPOSURE_OPERATOR"] = self._saved
        self._tmp.cleanup()

    def begin(self, last=0, config=None, run_id=RUN):
        return gs.begin(self.ws, config or cfg(), run_id=run_id,
                        marker_dir=self.inst, last_completed_cycle=last,
                        exclude_paths=[self.inst])

    def branch(self):
        return git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.ws).stdout.strip()

    def log(self):
        return git("log", "--format=%s", cwd=self.ws).stdout.splitlines()

    def stashes(self):
        return git("stash", "list", cwd=self.ws).stdout.splitlines()


class OffAndRefusalTests(SyncTestCase):
    def test_off_by_default(self):
        with mock.patch.object(gitcmd, "run") as run:
            self.assertIsNone(gs.begin(self.ws, {}, run_id=RUN,
                                       marker_dir=self.inst,
                                       last_completed_cycle=0))
        run.assert_not_called()

    def test_a_quoted_false_is_off(self):
        self.assertIsNone(self.begin(config=cfg(enabled="false")))

    def test_not_a_repository(self):
        plain = self.T / "plain"
        plain.mkdir()
        self.assertIsNone(gs.begin(plain, cfg(), run_id=RUN, marker_dir=plain,
                                   last_completed_cycle=0))

    def test_a_subdirectory_of_a_repository_is_refused(self):
        """Switching branches there would move files outside the workspace."""
        self.assertIsNone(gs.begin(self.ws / "data", cfg(), run_id=RUN,
                                   marker_dir=self.inst, last_completed_cycle=0))
        self.assertEqual(self.branch(), "main", "nothing was switched")

    def test_option_shaped_values_are_refused(self):
        for key, value in (("remote", "--upload-pack=touch /tmp/x"),
                           ("shared_branch", "-x")):
            c = cfg()
            c["federation"][key] = value
            self.assertIsNone(self.begin(config=c), key)
        self.assertIsNone(self.begin(run_id="--exec=bad"))

    def test_an_invalid_branch_name_is_refused(self):
        self.assertIsNone(self.begin(run_id="run..bad"))
        self.assertEqual(self.branch(), "main")


class FreshStartTests(SyncTestCase):
    def test_switches_to_the_run_branch(self):
        state = self.begin()
        self.assertIsNotNone(state)
        self.assertEqual(self.branch(), f"long-exposure/alice/{RUN}")

    def test_uncommitted_work_at_start_becomes_its_own_commit(self):
        (self.ws / "plan_of_record.md").write_text("bootstrap\n")
        self.begin()
        self.assertEqual(self.log()[0], "long-exposure: workspace state at run start")
        self.assertIn("plan_of_record.md",
                      git("show", "--name-only", "HEAD", cwd=self.ws).stdout)

    def test_a_clean_start_makes_no_commit(self):
        self.begin()
        self.assertEqual(self.log(), ["base"])

    def test_the_instance_dir_is_never_committed(self):
        (self.inst / "exploration_state.json").write_text('{"cycle": 3}')
        (self.ws / "data/g.py").write_text("x\n")
        state = self.begin()
        gs.after_cycle(state, 1, "t")
        tracked = git("ls-files", cwd=self.ws).stdout
        self.assertNotIn("instances/", tracked)
        self.assertIn("data/g.py", tracked)


class CycleCommitTests(SyncTestCase):
    def test_each_cycle_is_one_commit_with_trailers(self):
        state = self.begin()
        gs.before_cycle(state, 1)
        (self.ws / "data/f.py").write_text("v2\n")
        self.assertTrue(gs.after_cycle(state, 1, "study the bound"))
        self.assertEqual(self.log()[0], "long-exposure cycle 1: study the bound")
        body = git("log", "-1", "--format=%b", cwd=self.ws).stdout
        self.assertIn(f"Long-Exposure-Run: {RUN}", body)
        self.assertIn("Long-Exposure-Operator: alice", body)
        self.assertIn("Long-Exposure-Harness:", body)

    def test_a_cycle_with_no_changes_makes_no_commit(self):
        state = self.begin()
        gs.before_cycle(state, 1)
        self.assertFalse(gs.after_cycle(state, 1, "nothing"))
        self.assertEqual(self.log(), ["base"])

    def test_the_run_branch_is_pushed_and_main_is_untouched(self):
        state = self.begin()
        gs.before_cycle(state, 1)
        (self.ws / "data/f.py").write_text("v2\n")
        gs.after_cycle(state, 1, "t")
        remote = git("ls-remote", "--heads", str(self.T / "bare"), cwd=self.T).stdout
        self.assertIn(f"refs/heads/long-exposure/alice/{RUN}", remote)
        main = git("log", "--format=%s", "main", cwd=self.T / "bare").stdout
        self.assertEqual(main.split(), ["base"], "main must never be pushed to")

    def test_push_false_commits_locally_only(self):
        state = self.begin(config=cfg(push=False))
        gs.before_cycle(state, 1)
        (self.ws / "data/f.py").write_text("v2\n")
        gs.after_cycle(state, 1, "t")
        self.assertEqual(self.log()[0], "long-exposure cycle 1: t")
        remote = git("ls-remote", "--heads", str(self.T / "bare"), cwd=self.T).stdout
        self.assertNotIn("long-exposure/", remote)

    def test_the_marker_is_cleared_even_when_the_commit_fails(self):
        """A completed cycle must never later look like a crash."""
        state = self.begin()
        gs.before_cycle(state, 1)
        self.assertTrue((self.inst / gs.MARKER_FILENAME).exists())
        with mock.patch.object(gs, "_commit", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                gs.after_cycle(state, 1, "t")
        self.assertFalse((self.inst / gs.MARKER_FILENAME).exists())

    def test_finish_commits_what_the_run_wrote_after_its_last_cycle(self):
        state = self.begin()
        (self.ws / "reports").mkdir()
        (self.ws / "reports/final.md").write_text("closing report\n")
        self.assertTrue(gs.finish(state))
        self.assertEqual(self.log()[0], "long-exposure: end of run")


class CrashRecoveryTests(SyncTestCase):
    """The marker and the last completed cycle together say what happened."""

    def _crash(self, marker_cycle, run_id=RUN):
        """A previous process got as far as `marker_cycle` and died, leaving
        edits behind. It had switched to the run branch first."""
        state = self.begin(run_id=run_id)
        gs.before_cycle(state, marker_cycle)       # writes the marker
        (self.ws / "data/f.py").write_text("PARTIAL EDIT FROM A DEAD TURN\n")
        (self.ws / "data/new.py").write_text("half-written new file\n")
        # the process dies here: no after_cycle, the marker stays

    def test_a_mid_cycle_crash_stashes_the_partial_edits(self):
        self._crash(marker_cycle=3)
        state = self.begin(last=2)                 # cycle 3 never completed
        self.assertEqual((self.ws / "data/f.py").read_text(), "v1\n",
                         "the workspace must match the last completed cycle")
        self.assertFalse((self.ws / "data/new.py").exists())
        self.assertEqual(len(self.stashes()), 1)
        self.assertIn("crashed cycle 3", self.stashes()[0])
        block = gs.before_cycle(state, 3)
        self.assertIn("STASHED, not discarded", block)
        self.assertIn("git stash pop", block)

    def test_the_stashed_edits_are_fully_recoverable(self):
        self._crash(marker_cycle=3)
        self.begin(last=2)
        self.assertEqual(git("stash", "pop", cwd=self.ws).returncode, 0)
        self.assertEqual((self.ws / "data/f.py").read_text(),
                         "PARTIAL EDIT FROM A DEAD TURN\n")
        self.assertEqual((self.ws / "data/new.py").read_text(),
                         "half-written new file\n")

    def test_recovery_never_stashes_the_state_it_is_resuming_from(self):
        self._crash(marker_cycle=3)
        (self.inst / "exploration_state.json").write_text('{"cycle": 2}')
        self.begin(last=2)
        self.assertEqual((self.inst / "exploration_state.json").read_text(),
                         '{"cycle": 2}')

    def test_dying_after_the_save_but_before_the_commit_commits_the_work(self):
        """The state already counts cycle 3, so its edits are COMPLETED work."""
        self._crash(marker_cycle=3)
        self.begin(last=3)
        self.assertEqual(self.stashes(), [], "completed work must not be stashed")
        self.assertIn("recovered commit", self.log()[0])
        self.assertEqual((self.ws / "data/f.py").read_text(),
                         "PARTIAL EDIT FROM A DEAD TURN\n")

    def test_a_marker_from_another_run_is_ignored(self):
        """Honouring it would stash the new run's own bootstrap files.

        The first version of this test passed with the run_id check deleted:
        its setup cleared the old run's residue with `git stash -u`, which also
        stashed the untracked instance dir — marker included — so begin() never
        saw a marker at all. Found by mutation testing. The marker is now
        placed directly, and its presence is asserted before begin() runs.
        """
        (self.inst / gs.MARKER_FILENAME).write_text(json.dumps(
            {"run_id": "run-OLD", "cycle": 3, "branch": "x"}))
        (self.ws / "plan_of_record.md").write_text("new run bootstrap\n")
        self.assertTrue((self.inst / gs.MARKER_FILENAME).exists(),
                        "premise: an old run's marker is present")
        self.begin(last=0)                         # a NEW run, fresh start
        self.assertEqual(self.stashes(), [], "nothing may be stashed")
        self.assertTrue((self.ws / "plan_of_record.md").exists())
        self.assertEqual(self.log()[0], "long-exposure: workspace state at run start")
        self.assertFalse((self.inst / gs.MARKER_FILENAME).exists(),
                         "the stale marker is cleared")

    def test_a_corrupt_marker_is_treated_as_no_marker(self):
        for junk in ("not json", "[]", '{"cycle": "three"}', ""):
            (self.inst / gs.MARKER_FILENAME).write_text(junk)
            (self.ws / "data/f.py").write_text(f"edit {junk!r}\n")
            state = self.begin(last=0)
            self.assertIsNotNone(state)
            self.assertEqual(self.stashes(), [], junk)


class IntegrationTests(SyncTestCase):
    def _other_operator_pushes(self, path, body):
        bob = self.T / "bob"
        if not bob.exists():
            git("clone", "-q", str(self.T / "bare"), str(bob), cwd=self.T)
            git("config", "user.email", "bob@x", cwd=bob)
            git("config", "user.name", "bob", cwd=bob)
        git("checkout", "-q", "main", cwd=bob)
        git("pull", "-q", "origin", "main", cwd=bob)
        (bob / path).parent.mkdir(parents=True, exist_ok=True)
        (bob / path).write_text(body)
        git("add", "-A", cwd=bob)
        git("commit", "-qm", f"bob changes {path}", cwd=bob)
        git("push", "-q", "origin", "main", cwd=bob)

    def test_non_conflicting_shared_work_is_merged_in(self):
        state = self.begin()
        self._other_operator_pushes("data/bob.py", "bob's file\n")
        block = gs.before_cycle(state, 1)
        self.assertTrue((self.ws / "data/bob.py").exists())
        self.assertIn("Merged new work", block)

    def test_a_conflict_is_aborted_and_handed_to_the_researcher(self):
        state = self.begin()
        gs.before_cycle(state, 1)
        (self.ws / "data/f.py").write_text("alice's version\n")
        gs.after_cycle(state, 1, "t")
        self._other_operator_pushes("data/f.py", "bob's version\n")
        block = gs.before_cycle(state, 2)
        self.assertIn("CONFLICTS", block)
        self.assertIn("data/f.py", block)
        self.assertIn("not auto-resolved", block)
        text = (self.ws / "data/f.py").read_text()
        self.assertEqual(text, "alice's version\n", "no conflict markers left")
        self.assertFalse((self.ws / ".git" / "MERGE_HEAD").exists(),
                         "no merge left in progress")

    def test_an_unreachable_remote_is_a_notice_not_a_failure(self):
        state = self.begin()
        git("remote", "set-url", "origin", "https://127.0.0.1:1/nope.git",
            cwd=self.ws)
        state.timeout = 5
        block = gs.before_cycle(state, 1)
        self.assertIn("Could not fetch", block)
        (self.ws / "data/f.py").write_text("v2\n")
        self.assertTrue(gs.after_cycle(state, 1, "t"), "commit still happens")
        self.assertIn("failed", gs.before_cycle(state, 2), "push failure noted")

    def test_integrate_false_never_fetches(self):
        state = self.begin(config=cfg(integrate=False))
        with mock.patch.object(gitcmd, "run", wraps=gitcmd.run) as run:
            gs.before_cycle(state, 1)
        subs = [c.args[0][0] for c in run.call_args_list]
        self.assertNotIn("fetch", subs)
        self.assertNotIn("merge", subs)


class NeverDestroyTests(SyncTestCase):
    def test_a_rejected_push_is_never_forced(self):
        state = self.begin()
        gs.before_cycle(state, 1)
        (self.ws / "data/f.py").write_text("v2\n")
        gs.after_cycle(state, 1, "t")
        # Something outside the harness moves this run's remote branch.
        other = self.T / "other"
        git("clone", "-q", "-b", f"long-exposure/alice/{RUN}",
            str(self.T / "bare"), str(other), cwd=self.T)
        git("config", "user.email", "o@x", cwd=other)
        git("config", "user.name", "o", cwd=other)
        (other / "data/f.py").write_text("someone else\n")
        git("commit", "-qam", "outside change", cwd=other)
        git("push", "-q", "origin", f"long-exposure/alice/{RUN}", cwd=other)
        gs.before_cycle(state, 2)
        (self.ws / "data/f.py").write_text("v3\n")
        gs.after_cycle(state, 2, "t")
        remote_tip = git("log", "-1", "--format=%s",
                         f"long-exposure/alice/{RUN}", cwd=self.T / "bare").stdout
        self.assertEqual(remote_tip.strip(), "outside change",
                         "the outside commit must survive; nothing was forced")
        self.assertIn("failed", gs.before_cycle(state, 3))

    def test_a_failing_pre_commit_hook_is_respected_not_bypassed(self):
        hook = self.ws / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho 'operator policy says no' >&2\nexit 1\n")
        hook.chmod(0o755)
        state = self.begin()
        gs.before_cycle(state, 1)
        (self.ws / "data/f.py").write_text("v2\n")
        self.assertFalse(gs.after_cycle(state, 1, "t"))
        self.assertEqual(self.log(), ["base"], "the hook blocked the commit")
        self.assertEqual((self.ws / "data/f.py").read_text(), "v2\n",
                         "the work is still there")
        self.assertIn("operator policy says no", gs.before_cycle(state, 2))

    def test_no_configured_identity_uses_an_obviously_fake_one(self):
        git("config", "--unset", "user.email", cwd=self.ws)
        git("config", "--unset", "user.name", cwd=self.ws)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GIT_AUTHOR", "GIT_COMMITTER", "EMAIL"))}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.dict(os.environ, {"HOME": str(self.T),
                                          "GIT_CONFIG_NOSYSTEM": "1"}):
            state = self.begin()
            gs.before_cycle(state, 1)
            (self.ws / "data/f.py").write_text("v2\n")
            self.assertTrue(gs.after_cycle(state, 1, "t"))
        author = git("log", "-1", "--format=%ae", cwd=self.ws).stdout.strip()
        self.assertTrue(author.endswith("@long-exposure.invalid"), author)

    def test_every_git_call_in_a_full_scenario_obeys_the_policy(self):
        """Runtime counterpart to the static GitPolicyTests: record every git
        subcommand ACTUALLY executed through a crash, a recovery, a conflict,
        a merge and a push, and check none is destructive or forced."""
        from tests.test_git_federation_doc import DESTRUCTIVE_GIT, FORCE_FLAGS

        calls = []
        real = gitcmd.run

        def spy(args, cwd, timeout=20):
            calls.append(list(args))
            return real(args, cwd, timeout)

        with mock.patch.object(gitcmd, "run", side_effect=spy):
            state = self.begin()
            gs.before_cycle(state, 1)
            (self.ws / "data/f.py").write_text("alice\n")
            gs.after_cycle(state, 1, "t")
            gs.before_cycle(state, 2)
            (self.ws / "data/f.py").write_text("partial\n")
            state = self.begin(last=1)             # crash + recovery
            IntegrationTests._other_operator_pushes(self, "data/f.py", "bob\n")
            gs.before_cycle(state, 2)              # conflict path
            gs.finish(state)
        subs = [next((a for a in c if not a.startswith("-") and "=" not in a), "")
                for c in calls]
        self.assertTrue({"stash", "commit", "push", "merge"} & set(subs), subs)
        self.assertEqual([s for s in subs if s in DESTRUCTIVE_GIT], [])
        self.assertEqual([a for c in calls for a in c if a in FORCE_FLAGS], [])


if __name__ == "__main__":
    unittest.main()
