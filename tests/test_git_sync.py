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


class PeerImportTests(SyncTestCase):
    """Integration is a read-only mirror of peers' published work.

    It replaced merging the shared branch into the run branch, which with two
    operators silently replaced one operator's MEMOIR.md with the other's.
    """

    def _publish_as(self, operator, files: dict[str, str]):
        """Put `operators/<operator>/...` on the shared branch, as the
        integrator would."""
        pub = self.T / f"pub-{operator}"
        if not pub.exists():
            git("clone", "-q", str(self.T / "bare"), str(pub), cwd=self.T)
            git("config", "user.email", "i@x", cwd=pub)
            git("config", "user.name", "integrator", cwd=pub)
        git("checkout", "-q", "main", cwd=pub)
        git("pull", "-q", "origin", "main", cwd=pub)
        base = pub / "operators" / operator
        if base.exists():
            import shutil
            shutil.rmtree(base)
        for rel, body in files.items():
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            (base / rel).write_text(body)
        git("add", "-A", cwd=pub)
        git("commit", "-qm", f"publish {operator}", cwd=pub)
        git("push", "-q", "origin", "main", cwd=pub)

    def test_a_peers_published_work_is_mirrored_read_only(self):
        state = self.begin()
        self._publish_as("bob", {"MEMOIR.md": "bob's memoir\n",
                                 "literature-survey/families/cuprates.md": "## YBCO (bob)\n"})
        block = gs.before_cycle(state, 1)
        self.assertEqual((self.ws / "peers/bob/MEMOIR.md").read_text(), "bob's memoir\n")
        self.assertTrue((self.ws / "peers/bob/literature-survey/families/cuprates.md").exists())
        self.assertIn("mirrored read-only under peers/, one folder per operator", block)
        self.assertIn("never edit it", block)

    def test_this_operators_own_files_are_never_touched(self):
        """THE regression for the defect that forced the redesign."""
        (self.ws / "MEMOIR.md").write_text("alice's memoir\n")
        state = self.begin()
        self._publish_as("bob", {"MEMOIR.md": "bob's memoir\n"})
        gs.before_cycle(state, 1)
        self.assertEqual((self.ws / "MEMOIR.md").read_text(), "alice's memoir\n")

    def test_this_operators_own_projection_is_not_imported(self):
        state = self.begin()
        self._publish_as("alice", {"MEMOIR.md": "an older copy of me\n"})
        gs.before_cycle(state, 1)
        self.assertFalse((self.ws / "peers/alice").exists())

    def test_the_mirror_follows_upstream_deletions(self):
        state = self.begin()
        self._publish_as("bob", {"a.md": "1\n", "b.md": "2\n"})
        gs.before_cycle(state, 1)
        self._publish_as("bob", {"a.md": "1 revised\n"})
        gs.before_cycle(state, 2)
        self.assertEqual((self.ws / "peers/bob/a.md").read_text(), "1 revised\n")
        self.assertFalse((self.ws / "peers/bob/b.md").exists())

    def test_peers_are_never_committed_stashed_or_counted_dirty(self):
        state = self.begin()
        self._publish_as("bob", {"MEMOIR.md": "bob\n"})
        gs.before_cycle(state, 1)
        self.assertFalse(gs._is_dirty(state), "peers/ must not make the tree dirty")
        (self.ws / "data/f.py").write_text("alice's cycle work\n")
        gs.after_cycle(state, 1, "t")
        tracked = git("ls-files", cwd=self.ws).stdout
        self.assertNotIn("peers/", tracked)
        self.assertIn("data/f.py", tracked)

    def test_duplicated_work_is_pointed_out(self):
        # Premise: this operator HAS a MEMOIR.md too, or the bookkeeping filter
        # would have nothing to filter (a mutation test found exactly that).
        (self.ws / "MEMOIR.md").write_text("alice's memoir\n")
        (self.ws / "literature-survey/families").mkdir(parents=True)
        (self.ws / "literature-survey/families/cuprates.md").write_text("mine\n")
        state = self.begin()
        self._publish_as("bob", {"literature-survey/families/cuprates.md": "bob's\n",
                                 "MEMOIR.md": "bob\n"})
        block = gs.before_cycle(state, 1)
        self.assertIn("you both have literature-survey/families/cuprates.md", block)
        self.assertNotIn("you both have MEMOIR.md", block,
                         "harness bookkeeping always overlaps; it is not duplication")

    def test_a_symlink_left_in_the_mirror_cannot_redirect_a_write(self):
        state = self.begin()
        self._publish_as("bob", {"notes/x.md": "v1\n"})
        gs.before_cycle(state, 1)
        outside = self.T / "outside.txt"
        outside.write_text("must stay\n")
        target = self.ws / "peers/bob/notes/x.md"
        target.unlink()
        target.symlink_to(outside)
        self._publish_as("bob", {"notes/x.md": "v2\n"})
        gs.before_cycle(state, 2)
        self.assertEqual(outside.read_text(), "must stay\n")

    def test_a_symlinked_directory_in_the_mirror_cannot_redirect_a_write(self):
        """The case the containment check exists for. A symlinked FILE is also
        handled by unlinking it before writing, which hid this gap until a
        mutation test removed the check and nothing failed."""
        state = self.begin()
        self._publish_as("bob", {"notes/x.md": "v1\n"})
        gs.before_cycle(state, 1)
        outside = self.T / "outside_dir"
        outside.mkdir()
        notes = self.ws / "peers/bob/notes"
        import shutil
        shutil.rmtree(notes)
        notes.symlink_to(outside, target_is_directory=True)
        self._publish_as("bob", {"notes/x.md": "v2\n", "notes/new.md": "new\n"})
        gs.before_cycle(state, 2)
        self.assertEqual(list(outside.iterdir()), [],
                         "nothing may be written through the symlinked directory")

    def test_a_repo_that_already_ignores_peers_keeps_committing(self):
        """THE rehearsal defect. The live-test repo's .gitignore lists peers/
        and instances/. git_sync also excluded them with `:(exclude)` pathspecs,
        and `git add` exits 1 when a pathspec names an ignored path — so from
        the first peer import on, every commit was skipped as a failed add."""
        (self.ws / ".gitignore").write_text("peers/\ninstances/\n")
        git("add", ".gitignore", cwd=self.ws)
        git("commit", "-qm", "ignore rules like the live-test repo", cwd=self.ws)
        state = self.begin()
        for cycle in (1, 2, 3):
            self._publish_as("bob", {"notes.md": f"bob cycle {cycle}\n"})
            block = gs.before_cycle(state, cycle) or ""
            self.assertTrue((self.ws / "peers/bob/notes.md").exists())
            (self.ws / "data/f.py").write_text(f"alice cycle {cycle}\n")
            self.assertTrue(gs.after_cycle(state, cycle, f"c{cycle}"),
                            f"cycle {cycle} did not commit")
            self.assertNotIn("git add failed", block)
        log = self.log()
        self.assertEqual([l for l in log if l.startswith("long-exposure cycle")],
                         ["long-exposure cycle 3: c3", "long-exposure cycle 2: c2",
                          "long-exposure cycle 1: c1"])
        self.assertNotIn("peers/", git("ls-files", cwd=self.ws).stdout)

    def test_private_ignores_are_recorded_once(self):
        self.begin()
        self.begin()
        text = (self.ws / ".git/info/exclude").read_text()
        self.assertEqual(text.count("/peers/"), 1)
        self.assertEqual(text.count("/instances/i1/"), 1)
        self.assertEqual(text.count("long-exposure git_sync"), 1)

    def test_template_files_are_not_reported_as_duplicated_work(self):
        """The first rehearsal listed README, exclusions.md, the empty CSV and
        .gitkeep — files every operator got from the shared branch — ahead of
        the one real duplicate."""
        (self.ws / "literature-survey").mkdir()
        (self.ws / "literature-survey/README.md").write_text("contract\n")
        git("add", "-A", cwd=self.ws)
        git("commit", "-qm", "template", cwd=self.ws)
        git("push", "-q", "origin", "main", cwd=self.ws)
        (self.ws / "literature-survey/cuprates.md").write_text("mine\n")
        state = self.begin()
        self._publish_as("bob", {"literature-survey/README.md": "contract\n",
                                 "literature-survey/cuprates.md": "bob's\n"})
        block = gs.before_cycle(state, 1)
        self.assertIn("you both have literature-survey/cuprates.md", block)
        self.assertNotIn("README.md", block.split("you both have", 1)[1])

    def test_unsafe_relative_paths_are_rejected(self):
        for bad in ("../x", "a/../../x", "/etc/hosts", ".git/config", "a//b", "./a"):
            self.assertFalse(gs._safe_rel(bad), bad)
        for ok in ("a", "literature-survey/families/cuprates.md", "a.b/c"):
            self.assertTrue(gs._safe_rel(ok), ok)

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

        def spy(args, cwd, timeout=20, **kw):
            calls.append(list(args))
            return real(args, cwd, timeout, **kw)

        with mock.patch.object(gitcmd, "run", side_effect=spy):
            state = self.begin()
            gs.before_cycle(state, 1)
            (self.ws / "data/f.py").write_text("alice\n")
            gs.after_cycle(state, 1, "t")
            gs.before_cycle(state, 2)
            (self.ws / "data/f.py").write_text("partial\n")
            state = self.begin(last=1)             # crash + recovery
            PeerImportTests._publish_as(self, "bob", {"MEMOIR.md": "bob\n"})
            gs.before_cycle(state, 2)              # peer import path
            gs.finish(state)
        subs = [next((a for a in c if not a.startswith("-") and "=" not in a), "")
                for c in calls]
        self.assertTrue({"stash", "commit", "push", "cat-file"} <= set(subs), subs)
        self.assertNotIn("merge", subs, "integration no longer merges")
        self.assertEqual([s for s in subs if s in DESTRUCTIVE_GIT], [])
        self.assertEqual([a for c in calls for a in c if a in FORCE_FLAGS], [])


if __name__ == "__main__":
    unittest.main()
