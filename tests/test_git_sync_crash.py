"""A REAL crash: SIGKILL a running harness mid-worker-turn, then resume it.

The property git_sync exists for, tested the only honest way. A subprocess runs
real `run_exploration` cycles with a stub provider; its cycle-2 worker writes a
partial edit and hangs, and the test SIGKILLs the whole process group — no
signal handler, no `finally`, no cleanup, exactly like a machine losing power
or an OOM kill. A fresh process then resumes the run.

What must hold: the dead turn's partial edits are stashed rather than lost or
committed, the resumed researcher is told where they went, the redone cycle is
committed, the run state was never swept into the stash, and the shared branch
was never touched.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

DRIVER = Path(__file__).resolve().parent / "_crash_driver.py"
REPO = Path(__file__).resolve().parent.parent


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True)


@unittest.skipIf(sys.platform == "win32", "needs POSIX process groups")
class RealCrashTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        T = cls.T = Path(cls._tmp.name)
        git("init", "-q", "--bare", str(T / "bare"), cwd=T)
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=T / "bare")
        git("clone", "-q", str(T / "bare"), str(T / "ws"), cwd=T)
        ws = cls.ws = T / "ws"
        git("config", "user.email", "alice@x", cwd=ws)
        git("config", "user.name", "alice", cwd=ws)
        git("checkout", "-q", "-b", "main", cwd=ws)
        (ws / "README").write_text("seed\n")
        git("add", "-A", cwd=ws)
        git("commit", "-qm", "seed", cwd=ws)
        git("push", "-q", "origin", "main", cwd=ws)
        cls.inst = T / "inst"
        cls.inst.mkdir()
        (T / "config.yaml").write_text(
            "llm_provider: local\nmodel: test\nlocal_model: test\n"
            "local_context_window: 32768\ncontext_window: 32768\n"
            "compact_threshold: 0.9\n"
            f"compact_db: {cls.inst / 'sessions.db'}\nworking_directory: {ws}\n"
            "checkpoint_format: standard\nrequire_checkpoint_first: false\n"
            "user_gate_approval: false\ntelemetry:\n  enabled: false\n"
            "federation:\n  operator: alice\n  git_sync:\n    enabled: true\n")
        env = {**os.environ, "PYTHONPATH": str(REPO)}
        env.pop("LONG_EXPOSURE_OPERATOR", None)

        # --- run until cycle 2's worker is mid-turn, then kill it ---
        cls._write_score(5)
        with open(T / "crash.log", "w") as log:
            proc = subprocess.Popen(
                [sys.executable, str(DRIVER), str(T), "crash"], env=env,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + 90
        while (not (T / "worker_mid_turn").exists() and time.time() < deadline
               and proc.poll() is None):
            time.sleep(0.05)
        cls.reached_mid_turn = (T / "worker_mid_turn").exists()
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        cls.kill_code = proc.returncode
        cls.state_after_crash = json.loads(
            (cls.inst / "exploration_state.json").read_text())
        cls.log_after_crash = git("log", "--format=%s", cwd=ws).stdout.splitlines()
        cls.partial_after_crash = (ws / "data/result.py").read_text()
        cls.marker_after_crash = (cls.inst / "git_sync_in_progress.json").exists()

        # --- resume in a fresh process for one more cycle ---
        cls._write_score(2)
        cls.resume = subprocess.run(
            [sys.executable, str(DRIVER), str(T), "resume"], env=env,
            capture_output=True, text=True, timeout=180)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @classmethod
    def _write_score(cls, max_cycles):
        (cls.T / "score.yaml").write_text(
            "task: study the spectral bound\n"
            f"loop:\n  max_cycles: {max_cycles}\n  cycle_cooldown_seconds: 0\n"
            "  report_interval: 100\n  daily_sync_interval_hours: 0\n"
            "agents:\n"
            "  researcher:\n    inputs: [directive, audit_report, live_guidance]\n"
            "    outputs: [research_brief]\n    role: r\n"
            "  worker:\n    inputs: [directive, research_brief]\n"
            "    outputs: [work_output]\n    role: w\n"
            "  auditor:\n    inputs: [directive, work_output]\n"
            "    outputs: [audit_report]\n    role: a\n"
            "flow: [researcher, worker, auditor]\n")

    # -- the crash itself: premises, so nothing below passes vacuously --

    def test_premise_the_kill_really_happened_mid_turn(self):
        self.assertTrue(self.reached_mid_turn,
                        (self.T / "crash.log").read_text()[-800:])
        self.assertEqual(self.kill_code, -signal.SIGKILL)
        self.assertEqual(self.state_after_crash.get("cycle"), 1,
                         "cycle 1 completed, cycle 2 did not")
        self.assertIn("HALF-WRITTEN", self.partial_after_crash)
        self.assertTrue(self.marker_after_crash, "the marker must survive a kill")

    def test_cycle_1_was_committed_before_the_crash(self):
        self.assertTrue(any(s.startswith("long-exposure cycle 1")
                            for s in self.log_after_crash), self.log_after_crash)

    # -- the recovery --

    def test_the_resume_succeeds(self):
        self.assertEqual(self.resume.returncode, 0,
                         self.resume.stdout[-600:] + self.resume.stderr[-600:])

    def test_the_partial_edits_are_stashed_not_lost(self):
        stash = git("stash", "list", cwd=self.ws).stdout
        self.assertIn("crashed cycle 2", stash)
        patch = git("stash", "show", "-p", "--include-untracked", "stash@{0}",
                    cwd=self.ws).stdout
        self.assertIn("HALF-WRITTEN BY A DYING TURN", patch)
        names = git("stash", "show", "--include-untracked", "--name-only",
                    "stash@{0}", cwd=self.ws).stdout
        self.assertIn("scratch.tmp", names, "untracked partial files too")

    def test_the_run_state_was_never_stashed(self):
        patch = git("stash", "show", "-p", "--include-untracked", "stash@{0}",
                    cwd=self.ws).stdout
        self.assertNotIn("exploration_state", patch)

    def test_the_researcher_is_told_where_the_edits_went(self):
        seen = json.loads((self.T / "seen_resume.json").read_text())
        guidance = next(s["live_guidance"] for s in seen
                        if s["agent"] == "researcher")
        self.assertIn("<git_sync>", guidance)
        self.assertIn("STASHED, not discarded", guidance)
        self.assertIn("git stash pop", guidance)

    def test_the_redone_cycle_is_committed_and_complete(self):
        log = git("log", "--format=%s", cwd=self.ws).stdout.splitlines()
        self.assertTrue(any(s.startswith("long-exposure cycle 2") for s in log), log)
        self.assertIn("complete work", (self.ws / "data/result.py").read_text())

    def test_the_partial_edit_never_reached_a_commit(self):
        history = git("log", "-p", "HEAD", cwd=self.ws).stdout
        self.assertNotIn("HALF-WRITTEN", history)

    def test_the_shared_branch_was_never_touched(self):
        main = git("log", "--format=%s", "main", cwd=self.T / "bare").stdout
        self.assertEqual(main.split(), ["seed"])
        heads = git("ls-remote", "--heads", str(self.T / "bare"), cwd=self.T).stdout
        self.assertIn("refs/heads/long-exposure/alice/run-", heads)


if __name__ == "__main__":
    unittest.main()
