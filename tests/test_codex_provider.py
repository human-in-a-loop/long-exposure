import os
import tempfile
import unittest
from unittest.mock import patch

from long_exposure import provider
from long_exposure.exploration import _call_exploration_agent
from long_exposure.orchestrator import (
    _codex_permission_flags,
    _extract_codex_envelope,
    _format_cli_failure_context,
    load_config,
)


class CodexProviderTests(unittest.TestCase):
    def test_normalizes_codex_aliases(self):
        self.assertEqual(provider.normalize_provider("codex"), "codex")
        self.assertEqual(provider.normalize_provider("openai"), "codex")
        self.assertEqual(provider.normalize_provider("codex-cli"), "codex")

    def test_codex_envelope_extracts_thread_usage_and_final_text(self):
        stdout = "\n".join([
            '{"msg":{"type":"thread.started","thread_id":"thread-1"}}',
            '{"msg":{"type":"turn.completed","usage":{"input_tokens":4,"output_tokens":5}}}',
        ])
        env = _extract_codex_envelope(stdout, "final answer", 9)
        self.assertEqual(env["result"], "final answer")
        self.assertEqual(env["session_id"], "thread-1")
        self.assertEqual(env["usage"]["input_tokens"], 4)
        self.assertEqual(env["usage"]["output_tokens"], 5)

    def test_codex_permission_flags_disable_tools_omits_yolo(self):
        flags = _codex_permission_flags({"codex_yolo": True}, disable_tools=True)
        self.assertNotIn("--yolo", flags)
        self.assertIn("-s", flags)
        self.assertIn("read-only", flags)

    def test_cli_failure_context_includes_all_available_channels(self):
        text = _format_cli_failure_context(
            stderr="stderr detail",
            stdout='{"type":"error"}',
            envelope={"result": "structured detail"},
        )
        self.assertIn("stderr detail", text)
        self.assertIn("structured detail", text)
        self.assertIn('{"type":"error"}', text)

    def test_exploration_agent_builds_codex_fresh_command_and_records_thread(self):
        captured = {}

        def fake_invoke(cmd, stdin_text, **kwargs):
            captured["cmd"] = cmd
            captured["stdin"] = stdin_text
            return {
                "result": "[OUTPUT: out]done[END OUTPUT]",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "duration_ms": 1,
                "session_id": "thread-returned",
            }

        with tempfile.TemporaryDirectory() as td:
            config = load_config()
            config.update({
                "llm_provider": "codex",
                "model": "gpt-5.5",
                "codex_model": "gpt-5.5",
                "working_directory": td,
            })
            sessions = {}
            with patch.dict(os.environ, {"LONG_EXPOSURE_LLM_PROVIDER": "codex"}, clear=False):
                with patch("long_exposure.exploration._invoke_claude", fake_invoke):
                    result = _call_exploration_agent(
                        agent_name="tester",
                        agent_def={
                            "role": "You are a tester.",
                            "inputs": ["directive"],
                            "outputs": ["out"],
                        },
                        task="test task",
                        config=config,
                        results={"directive": "test task"},
                        score_inputs={"directive": "test task"},
                        agent_sessions=sessions,
                        agent_summaries={},
                    )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(sessions["tester"], "thread-returned")
        self.assertIn("codex", captured["cmd"][0])
        self.assertIn("exec", captured["cmd"])
        self.assertIn("--json", captured["cmd"])
        self.assertIn("-o", captured["cmd"])
        self.assertIn("[SYSTEM PROMPT]", captured["stdin"])


if __name__ == "__main__":
    unittest.main()
