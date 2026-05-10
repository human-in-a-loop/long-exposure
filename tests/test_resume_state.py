import json
import tempfile
import unittest
from pathlib import Path

from long_exposure.exploration import run_exploration


class ResumeStateTests(unittest.TestCase):
    def test_provider_mismatch_clears_native_sessions_but_keeps_summaries(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspace"
            instance = root / "instance"
            workspace.mkdir()
            instance.mkdir()
            state_path = instance / "exploration_state.json"
            state_path.write_text(json.dumps({
                "cycle": 3,
                "results": {"directive": "saved task", "audit_report": "prior"},
                "failures": {"researcher": 0},
                "agent_sessions": {"researcher": "codex-thread"},
                "agent_sessions_provider": "codex",
                "agent_summaries": {"researcher": "summary survives"},
                "task": "saved task",
                "run_id": "run-test",
            }))
            score = root / "score.yaml"
            score.write_text(
                "task: yaml task\n"
                "loop:\n"
                "  max_cycles: 3\n"
                "  cycle_cooldown_seconds: 0\n"
                "agents:\n"
                "  researcher:\n"
                "    inputs: [directive, audit_report]\n"
                "    outputs: [research_brief]\n"
                "    role: researcher\n"
                "flow: [researcher]\n"
            )
            config = root / "config.yaml"
            config.write_text(
                "llm_provider: local\n"
                "model: test\n"
                "local_model: test\n"
                "local_context_window: 32768\n"
                "context_window: 32768\n"
                "compact_threshold: 0.9\n"
                f"compact_db: {instance / 'sessions.db'}\n"
                f"working_directory: {workspace}\n"
                "checkpoint_format: standard\n"
                "require_checkpoint_first: false\n"
                "user_gate_approval: false\n"
                "anti_patterns_enabled: true\n"
            )

            run_exploration(
                score_path=str(score),
                config_path=str(config),
                output_dir=instance / "output",
                state_path=state_path,
                instance_dir=instance,
            )

            state = json.loads(state_path.read_text())

        self.assertEqual(state["agent_sessions"], {})
        self.assertEqual(state["agent_summaries"]["researcher"], "summary survives")
        self.assertEqual(state["task"], "saved task")


if __name__ == "__main__":
    unittest.main()
