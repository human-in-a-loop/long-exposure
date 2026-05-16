import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from long_exposure.fanout import _run_fanout_conductor


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


if __name__ == "__main__":
    unittest.main()
