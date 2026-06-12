import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from long_exposure.fanout import (
    FANOUT_MAX_BRANCHES,
    _fanout_branch_cap,
    _run_fanout_conductor,
    _spawn_clone,
)


class FakeProc:
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


class FanoutConductorTests(unittest.TestCase):
    def test_conductor_collects_fake_clone_merge_reports(self):
        branches = [
            {
                "objective": "left branch",
                "output_artifact": "left.md",
                "branchial_budget": {"novelty_class": "novel", "novelty_score": 0.9},
            },
            {
                "objective": "right branch",
                "output_artifact": "right.md",
                "branchial_budget": {
                    "novelty_class": "likely-retread",
                    "novelty_score": 0.2,
                    "matched_session_ids": ["s1"],
                },
            },
        ]

        def fake_spawn(cdir, fork_id, clone_k, *args, **kwargs):
            (Path(cdir) / "merge_report.md").write_text(
                f"# Merge Report\n\nclone {clone_k} complete\n"
            )
            return FakeProc(1000 + clone_k)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspace"
            data = root / "data"
            workspace.mkdir()
            data.mkdir()
            with patch("long_exposure.fanout._spawn_clone", fake_spawn):
                with patch("long_exposure.fanout._active_account_dir", return_value=None):
                    result = _run_fanout_conductor(
                        branches=branches,
                        score_path=str(root / "score.yaml"),
                        config_path=None,
                        root_instance_dir=root,
                        data_dir=data,
                        task="test task",
                        parent_results={},
                        parent_agent_sessions={},
                        parent_agent_summaries={},
                        working_directory=str(workspace),
                    )

            fork_dir = root / f"fork-{result['fork_id']}"
            self.assertIn("Fan-out Merge", result["aggregated_report"])
            self.assertIn("clone-0=done", result["aggregated_report"])
            self.assertIn("clone 1 complete", result["aggregated_report"])
            self.assertIn("Branch outcomes", result["divergence_table"])
            self.assertTrue((fork_dir / "fork_manifest.md").exists())
            self.assertTrue((fork_dir / "fanout_merge.md").exists())
            self.assertTrue((fork_dir / "fanout_divergence.md").exists())
            manifest = (fork_dir / "fork_manifest.md").read_text()
            self.assertIn("branchial_budget: likely-retread", manifest)
            self.assertTrue((data / "branchial_budget.jsonl").exists())


class FanoutBranchCapTests(unittest.TestCase):
    def test_pool_inactive_uses_legacy_cap(self):
        with (
            patch("long_exposure.unified_pool.is_unified_active", return_value=False),
            patch("long_exposure.pool.is_active", return_value=False),
        ):
            self.assertEqual(_fanout_branch_cap(), FANOUT_MAX_BRANCHES)

    def test_active_pool_with_zero_cap_returns_one(self):
        # Saturated/cooling pool must NOT fall back to the permissive legacy
        # cap (which invited branches that all PoolExhausted-fallback onto
        # the root's account). 1 makes the parser reject fan-out blocks.
        with (
            patch("long_exposure.unified_pool.is_unified_active", return_value=False),
            patch("long_exposure.pool.is_active", return_value=True),
            patch("long_exposure.pool.fanout_cap", return_value=0),
        ):
            self.assertEqual(_fanout_branch_cap(), 1)

    def test_active_unified_pool_with_zero_cap_returns_one(self):
        with (
            patch("long_exposure.unified_pool.is_unified_active", return_value=True),
            patch("long_exposure.unified_pool.fanout_cap", return_value=0),
        ):
            self.assertEqual(_fanout_branch_cap(), 1)

    def test_active_pool_with_capacity_passes_through(self):
        with (
            patch("long_exposure.unified_pool.is_unified_active", return_value=False),
            patch("long_exposure.pool.is_active", return_value=True),
            patch("long_exposure.pool.fanout_cap", return_value=5),
        ):
            self.assertEqual(_fanout_branch_cap(), 5)


class SpawnCloneEnvTests(unittest.TestCase):
    def test_pinned_clone_gets_pool_fallback_env(self):
        """_spawn_clone pops the primary pool env vars but must stash the
        original value under LONG_EXPOSURE_CLONE_POOL_CONFIG so clone-side
        pool.is_active() / slot lifecycle keep working."""
        captured = {}

        class FakeProc:
            pid = 12345
            stdout = io.BytesIO(b"")

        def fake_popen(cmd, env=None, **kwargs):
            captured["env"] = env
            return FakeProc()

        with tempfile.TemporaryDirectory() as td:
            clone_dir = Path(td) / "clone-0"
            clone_dir.mkdir()
            parent_env = {"HOME": td, "CLAUDE_ACCOUNT_POOL": "/acct-a,/acct-b"}
            with patch.dict(os.environ, parent_env, clear=True):
                with patch(
                    "long_exposure.fanout.subprocess.Popen", side_effect=fake_popen
                ):
                    _spawn_clone(
                        clone_dir, "fk1", 0, "score.yaml", None,
                        pinned_account_dir="/acct-b",
                    )

        env = captured["env"]
        self.assertNotIn("CLAUDE_ACCOUNT_POOL", env)
        self.assertNotIn("CLAUDE_ACCOUNTS", env)
        self.assertEqual(env["LONG_EXPOSURE_CLONE_POOL_CONFIG"], "/acct-a,/acct-b")
        self.assertEqual(env["CLAUDE_FORCE_ACCOUNT"], "/acct-b")

    def test_unpinned_clone_keeps_inherited_pool_env(self):
        # PoolExhausted-fallback path: no pin, no pops, no fallback var.
        captured = {}

        class FakeProc:
            pid = 12346
            stdout = io.BytesIO(b"")

        def fake_popen(cmd, env=None, **kwargs):
            captured["env"] = env
            return FakeProc()

        with tempfile.TemporaryDirectory() as td:
            clone_dir = Path(td) / "clone-0"
            clone_dir.mkdir()
            parent_env = {"HOME": td, "CLAUDE_ACCOUNT_POOL": "/acct-a,/acct-b"}
            with patch.dict(os.environ, parent_env, clear=True):
                with patch(
                    "long_exposure.fanout.subprocess.Popen", side_effect=fake_popen
                ):
                    _spawn_clone(
                        clone_dir, "fk1", 0, "score.yaml", None,
                        pinned_account_dir=None,
                    )

        env = captured["env"]
        self.assertEqual(env["CLAUDE_ACCOUNT_POOL"], "/acct-a,/acct-b")
        self.assertNotIn("LONG_EXPOSURE_CLONE_POOL_CONFIG", env)
        self.assertNotIn("CLAUDE_FORCE_ACCOUNT", env)

    def test_pre_popen_failure_releases_slot(self):
        """A failure BEFORE Popen (interpreter resolution / clone.log open)
        must release the acquired slot — the caller's `except OSError`
        assumes it was."""
        with tempfile.TemporaryDirectory() as td:
            clone_dir = Path(td) / "clone-0"
            clone_dir.mkdir()
            parent_env = {"HOME": td, "CLAUDE_ACCOUNT_POOL": "/acct-a,/acct-b"}
            with patch.dict(os.environ, parent_env, clear=True):
                with (
                    patch(
                        "long_exposure.fanout._resolve_python_exe",
                        side_effect=FileNotFoundError("no usable interpreter"),
                    ),
                    patch(
                        "long_exposure.pool.release_slot_by_branch",
                        return_value=True,
                    ) as release,
                ):
                    with self.assertRaises(OSError):
                        _spawn_clone(
                            clone_dir, "fk1", 2, "score.yaml", None,
                            pinned_account_dir="/acct-b",
                        )
        release.assert_called_once_with("fk1", 2)


if __name__ == "__main__":
    unittest.main()
