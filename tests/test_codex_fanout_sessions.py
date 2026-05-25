import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from long_exposure.fanout import _seed_clone_state


class CodexFanoutSessionTests(unittest.TestCase):
    def test_codex_clone_seed_drops_parent_native_sessions(self):
        with tempfile.TemporaryDirectory() as td:
            clone_dir = Path(td) / "clone"
            with patch.dict(os.environ, {"LONG_EXPOSURE_LLM_PROVIDER": "codex"}, clear=False):
                _seed_clone_state(
                    clone_dir,
                    parent_results={"directive": "task", "live_guidance": "omit"},
                    parent_agent_sessions={"researcher": "parent-thread"},
                    parent_agent_summaries={"researcher": "summary"},
                    parent_run_id="run-test",
                    parent_account_dir="/same",
                    pinned_account_dir="/same",
                    clone_k=0,
                )

            state = json.loads((clone_dir / "exploration_state.json").read_text())

        self.assertEqual(state["agent_sessions"], {})
        self.assertEqual(state["agent_summaries"], {"researcher": "summary"})
        self.assertNotIn("live_guidance", state["results"])
        self.assertEqual(state["run_id"], "run-test")

    def test_claude_clone_seed_preserves_parent_sessions_when_account_matches(self):
        with tempfile.TemporaryDirectory() as td:
            clone_dir = Path(td) / "clone"
            with patch.dict(os.environ, {"LONG_EXPOSURE_LLM_PROVIDER": "claude"}, clear=False):
                _seed_clone_state(
                    clone_dir,
                    parent_results={"directive": "task"},
                    parent_agent_sessions={"researcher": "parent-session"},
                    parent_agent_summaries={},
                    parent_run_id="run-test",
                    parent_account_dir="/same",
                    pinned_account_dir="/same",
                    clone_k=0,
                )

            state = json.loads((clone_dir / "exploration_state.json").read_text())

        self.assertEqual(state["agent_sessions"], {"researcher": "parent-session"})


if __name__ == "__main__":
    unittest.main()
