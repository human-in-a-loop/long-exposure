"""Two operators, one real git remote, real harness cycles. Nothing mocked but
the provider.

This is the test that found the split-identity defect
(`tests/test_federation_adversarial.py::SplitIdentityTests`), which every unit
test passed straight through: the identity fix was correct in isolation and
wrong in assembly, because `federation.operator` in config reached agent
subprocesses but not the harness's own appends.

The loop it exercises, in order:

    bootstrap -> real cycle -> commit -> push (one REJECTED) -> rebase -> read

git is real, the ledger is real, `run_exploration` is the real cycle loop, and
the shared branch is a real bare repository. Only `_call_exploration_agent` is
stubbed, because the point is the plumbing around an agent turn, not the turn.
"""

import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from long_exposure import telemetry
from long_exposure.federation import slugify as fed_slug
from long_exposure import workspace_bootstrap as wb
from long_exposure.exploration import run_exploration


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True)


def _agent_stub(captured, finding):
    """Records what each agent was actually handed."""
    def fake(agent_name, agent_def, **kwargs):
        results = kwargs.get("results") or {}
        captured.append({
            "agent": agent_name,
            "live_guidance": results.get("live_guidance", ""),
            "ledger_summary": results.get("promise_ledger_summary", ""),
        })
        out = agent_def["outputs"][0]
        return {"agent": agent_name,
                "outputs": {out: f"{agent_name}: {finding} " + "x" * 2100},
                "usage": {"input_tokens": 100, "output_tokens": 2100},
                "duration_ms": 10, "status": "ok", "error": None,
                "cost_usd": 0.0, "num_turns": 1, "tool_calls": 1}
    return fake


class TwoOperatorsOneRepoTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.T = Path(self._tmp.name)
        self._saved_op = os.environ.get("LONG_EXPOSURE_OPERATOR")
        os.environ.pop("LONG_EXPOSURE_OPERATOR", None)

        git("init", "-q", "--bare", str(self.T / "bare"), cwd=self.T)
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.T / "bare")

        # Alice clones, bootstraps a real workspace, declares the union merge.
        self.alice = self._clone("alice")
        os.environ["LONG_EXPOSURE_OPERATOR"] = "alice"
        try:
            wb.bootstrap_workspace(self.alice, "study the spectral bound",
                                   "run-alice", 1)
        finally:
            os.environ.pop("LONG_EXPOSURE_OPERATOR", None)
        (self.alice / ".gitattributes").write_text(
            "promise_ledger.jsonl merge=union\n")
        git("add", "-A", cwd=self.alice)
        git("commit", "-qm", "bootstrap", cwd=self.alice)
        git("push", "-q", "origin", "main", cwd=self.alice)

        # Bob clones the bootstrapped workspace.
        self.bob = self._clone("bob")

    def tearDown(self):
        telemetry.configure({"telemetry": {"enabled": False}}, None, None)
        if self._saved_op is None:
            os.environ.pop("LONG_EXPOSURE_OPERATOR", None)
        else:
            os.environ["LONG_EXPOSURE_OPERATOR"] = self._saved_op
        self._tmp.cleanup()

    def _clone(self, name):
        git("clone", "-q", str(self.T / "bare"), str(self.T / name), cwd=self.T)
        path = self.T / name
        git("config", "user.email", f"{name}@x", cwd=path)
        git("config", "user.name", name, cwd=path)
        git("checkout", "-q", "-b", "main", cwd=path)
        return path

    def _run_cycle(self, workspace, operator, finding, *, radar=False):
        root = self.T / f"{operator}-run-{uuid.uuid4().hex[:6]}"
        root.mkdir()
        inst = root / "instance"
        inst.mkdir()
        (root / "score.yaml").write_text(
            "task: study the spectral bound\n"
            "loop:\n  max_cycles: 1\n  cycle_cooldown_seconds: 0\n"
            "  report_interval: 100\n  daily_sync_interval_hours: 0\n"
            "agents:\n"
            "  researcher:\n"
            "    inputs: [directive, audit_report, live_guidance,"
            " promise_ledger_summary]\n"
            "    outputs: [research_brief]\n    role: researcher\n"
            "  worker:\n    inputs: [directive, research_brief]\n"
            "    outputs: [work_output]\n    role: worker\n"
            "  auditor:\n    inputs: [directive, work_output]\n"
            "    outputs: [audit_report]\n    role: auditor\n"
            "flow: [researcher, worker, auditor]\n")
        (root / "config.yaml").write_text(
            "llm_provider: local\nmodel: test\nlocal_model: test\n"
            "local_context_window: 32768\ncontext_window: 32768\n"
            "compact_threshold: 0.9\n"
            f"compact_db: {inst / 'sessions.db'}\n"
            f"working_directory: {workspace}\n"
            "checkpoint_format: standard\nrequire_checkpoint_first: false\n"
            "user_gate_approval: false\nanti_patterns_enabled: true\n"
            "telemetry:\n  enabled: false\n"
            f"federation:\n  operator: \"{operator}\"\n"
            "  conflict_radar:\n"
            f"    enabled: {'true' if radar else 'false'}\n"
            "    shared_branch: main\n    fetch: true\n    timeout_seconds: 20\n")
        captured = []
        # A real run resolves identity from config; nothing pre-set.
        os.environ.pop("LONG_EXPOSURE_OPERATOR", None)
        try:
            with patch("long_exposure.exploration._call_exploration_agent",
                       _agent_stub(captured, finding)):
                run_exploration(
                    score_path=str(root / "score.yaml"),
                    config_path=str(root / "config.yaml"),
                    output_dir=inst / "output",
                    state_path=inst / "exploration_state.json",
                    task_override=None, instance_dir=inst)
        finally:
            os.environ.pop("LONG_EXPOSURE_OPERATOR", None)
            telemetry.configure({"telemetry": {"enabled": False}}, None, None)
        return captured

    @staticmethod
    def _settle(workspace, operator, milestone, narrative, ts):
        """An auditor-shaped settled finding — the shape that used to be lost."""
        wb.append_ledger_event(workspace, {
            "event_id": str(uuid.uuid4()), "ts": ts,
            "run_id": f"run-{operator}", "cycle": 1, "agent": "auditor",
            "milestone_id": milestone, "operator": operator,
            "status": "validated",
            "confidence": {"level": "high", "rationale": narrative,
                           "assessor": "auditor"},
            "narrative": narrative})

    def _rows(self, workspace):
        return [json.loads(l) for l in
                (workspace / "promise_ledger.jsonl").read_text().splitlines()
                if l.strip()]

    # -- the test ----------------------------------------------------------

    def test_the_whole_loop(self):
        # 1. Bob inherited Alice's bootstrapped workspace and the merge rule.
        self.assertIn("merge=union", (self.bob / ".gitattributes").read_text())
        self.assertTrue((self.bob / "promise_ledger.jsonl").exists())

        # 2. Both run a real cycle. Identity must come from config alone.
        seen_a = self._run_cycle(self.alice, "alice", "bound looks like 3.2")
        seen_b = self._run_cycle(self.bob, "bob", "bound looks like 7.9")
        self.assertEqual([c["agent"] for c in seen_a],
                         ["researcher", "worker", "auditor"])
        self.assertEqual([c["agent"] for c in seen_b],
                         ["researcher", "worker", "auditor"])

        # THE defect this rig found: one machine must write under one name.
        # Before the fix, Alice's bootstrap event was stamped with her HOSTNAME
        # while config said `alice`, so this set was {"alice", "vm"}.
        import socket
        hostname = fed_slug(socket.gethostname().split(".")[0])
        for who, repo in (("alice", self.alice), ("bob", self.bob)):
            names = {r["operator"] for r in self._rows(repo)}
            self.assertNotIn(hostname, names,
                             f"{who}: hostname leaked past config — split identity")
            self.assertTrue(names <= {"alice", "bob"}, f"{who}: {names}")
        self.assertEqual({r["operator"] for r in self._rows(self.alice)}, {"alice"})
        # Bob's clone carries only Alice's bootstrap event so far: his cycle's
        # agents are stubs that append nothing, and bootstrap is a no-op on an
        # already-bootstrapped workspace. His own event arrives at step 3.
        self.assertEqual({r["operator"] for r in self._rows(self.bob)}, {"alice"})

        # 3. Contradicting settled findings on the same milestone.
        self._settle(self.alice, "alice", "spectral/bound",
                     "ALICE: the bound is 3.2", "2026-01-02T00:00:00Z")
        self._settle(self.bob, "bob", "spectral/bound",
                     "BOB: the bound is 7.9", "2026-01-03T00:00:00Z")

        # 4. Commit and race the push. git is the lock.
        for repo, msg in ((self.alice, "alice cycle 1"), (self.bob, "bob cycle 1")):
            git("add", "-A", cwd=repo)
            git("commit", "-qm", msg, cwd=repo)
        self.assertEqual(git("push", "origin", "main", cwd=self.alice).returncode, 0)
        rejected = git("push", "origin", "main", cwd=self.bob)
        self.assertNotEqual(rejected.returncode, 0,
                            "the second of two conflicting pushes must be rejected")

        # 5. Bob rebases. merge=union must make the ledger converge cleanly.
        git("fetch", "-q", "origin", cwd=self.bob)
        rebase = git("rebase", "origin/main", cwd=self.bob)
        self.assertEqual(rebase.returncode, 0,
                         f"rebase conflicted: {rebase.stdout}{rebase.stderr}")
        self.assertEqual(git("push", "origin", "main", cwd=self.bob).returncode, 0)

        # 6. The payoff: read the merged ledger.
        git("fetch", "-q", "origin", cwd=self.alice)
        git("rebase", "-q", "origin/main", cwd=self.alice)
        merged = self._rows(self.alice)
        ids = [r["event_id"] for r in merged]
        self.assertEqual(len(ids), len(set(ids)), "an event was duplicated")
        self.assertEqual({r["operator"] for r in merged}, {"alice", "bob"})

        summary = wb.summarize_ledger(self.alice)
        self.assertIn("ALICE: the bound is 3.2", summary)
        self.assertIn("BOB: the bound is 7.9", summary)
        self.assertIn("Operators on this ledger: alice, bob", summary)
        rows = [l for l in summary.splitlines()
                if l.lstrip().startswith("- [spectral/bound]")]
        self.assertEqual(len(rows), 2, "one row per operator on a shared milestone")

    def test_the_radar_reaches_the_researcher_in_a_real_cycle(self):
        # Bob moves data/ on the shared branch and pushes.
        (self.bob / "data").mkdir(exist_ok=True)
        (self.bob / "data" / "spectral.py").write_text("bob's version\n")
        git("add", "-A", cwd=self.bob)
        git("commit", "-qm", "bob moves spectral", cwd=self.bob)
        git("push", "-q", "origin", "main", cwd=self.bob)

        # Alice has UNCOMMITTED work on the same file — the normal state.
        (self.alice / "data").mkdir(exist_ok=True)
        (self.alice / "data" / "spectral.py").write_text("alice's UNCOMMITTED\n")

        seen = self._run_cycle(self.alice, "alice", "second pass", radar=True)
        guidance = seen[0]["live_guidance"]
        self.assertIn("<shared_branch_overlap>", guidance)
        self.assertIn("data/spectral.py", guidance)
        self.assertIn("FORECAST", guidance)
        self.assertIn("nothing is blocked", guidance)
        self.assertEqual(guidance.count("</shared_branch_overlap>"), 1)

        # And it left Alice's work alone.
        self.assertEqual((self.alice / "data" / "spectral.py").read_text(),
                         "alice's UNCOMMITTED\n")
        self.assertEqual(git("branch", "--show-current", cwd=self.alice).stdout.strip(),
                         "main")

    def test_the_radar_stays_silent_when_work_is_disjoint(self):
        (self.bob / "data").mkdir(exist_ok=True)
        (self.bob / "data" / "spectral.py").write_text("bob's version\n")
        git("add", "-A", cwd=self.bob)
        git("commit", "-qm", "bob moves spectral", cwd=self.bob)
        git("push", "-q", "origin", "main", cwd=self.bob)

        (self.alice / "reports").mkdir(exist_ok=True)
        (self.alice / "reports" / "mine.md").write_text("alice's own report\n")

        seen = self._run_cycle(self.alice, "alice", "disjoint", radar=True)
        self.assertNotIn("<shared_branch_overlap>", seen[0]["live_guidance"])


if __name__ == "__main__":
    unittest.main()
