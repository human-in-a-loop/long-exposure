import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from long_exposure import provider
from long_exposure.exploration import _call_exploration_agent, load_state, save_state
from long_exposure.orchestrator import (
    _extract_gemini_envelope,
    _gemini_permission_flags,
    assemble_system_prompt,
    generate_gemini_project_settings,
    load_config,
)


class GeminiProviderTests(unittest.TestCase):
    def test_normalizes_gemini_aliases(self):
        self.assertEqual(provider.normalize_provider("gemini"), "gemini")
        self.assertEqual(provider.normalize_provider("google"), "gemini")
        self.assertEqual(provider.normalize_provider("gemini-cli"), "gemini")

    def test_gemini_provider_env_names(self):
        self.assertEqual(provider.force_account_env("gemini"), "GEMINI_FORCE_ACCOUNT")
        self.assertEqual(provider.child_config_env("gemini"), "GEMINI_CLI_HOME")
        self.assertEqual(
            provider.account_pool_envs("gemini"),
            ("GEMINI_ACCOUNT_POOL", "GEMINI_HOMES"),
        )
        self.assertEqual(provider.pool_state_path("gemini").name, ".gemini-pool-state.json")
        with patch.dict(os.environ, {"GEMINI_ACCOUNT_POOL": "/tmp/a,/tmp/b"}, clear=False):
            self.assertEqual(provider.parse_pool_env("gemini"), [])

    def test_load_config_applies_gemini_model_and_context(self):
        with patch.dict(os.environ, {"LONG_EXPOSURE_LLM_PROVIDER": "gemini"}, clear=False):
            config = load_config()
        self.assertEqual(config["llm_provider"], "gemini")
        self.assertEqual(config["model"], config["gemini_model"])
        self.assertEqual(config["context_window"], config["gemini_context_window"])
        self.assertEqual(config["context_window"], 1_000_000)

    def test_gemini_prompt_uses_conservative_parallelism_guidance(self):
        with patch.dict(os.environ, {"LONG_EXPOSURE_LLM_PROVIDER": "gemini"}, clear=False):
            config = load_config()
            config["agent_teams"] = True
            prompt = assemble_system_prompt(config)
        self.assertIn("<gemini-parallelism>", prompt)
        self.assertIn("native subagents are NOT enabled", prompt)

    def test_gemini_project_settings_include_tools_and_mcp(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = {
                "working_directory": td,
                "compact_db": f"{td}/sessions.db",
                "allowed_tools": ["Read", "Edit", "Bash(python *)", "WebSearch"],
            }
            path = generate_gemini_project_settings(cfg)
            self.assertTrue(path.endswith(".gemini/settings.json"))
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn('"read_file"', text)
            self.assertIn('"replace"', text)
            self.assertIn('"run_shell_command(python)"', text)
            self.assertIn('"google_web_search"', text)
            self.assertIn('"mcpServers"', text)
            self.assertIn('"sessions"', text)

    def test_extract_gemini_envelope_normalizes_response_and_usage(self):
        stdout = """
        {
          "session_id": "abc",
          "response": "ok",
          "stats": {
            "models": {
              "gemini-3-flash-preview": {
                "tokens": {
                  "input": 10,
                  "prompt": 12,
                  "candidates": 3,
                  "cached": 4
                }
              }
            }
          }
        }
        """
        envelope = _extract_gemini_envelope(stdout, 123)
        self.assertEqual(envelope["result"], "ok")
        self.assertEqual(envelope["session_id"], "abc")
        self.assertEqual(envelope["duration_ms"], 123)
        self.assertEqual(envelope["usage"]["input_tokens"], 10)
        self.assertEqual(envelope["usage"]["output_tokens"], 3)
        self.assertEqual(envelope["usage"]["cache_read_input_tokens"], 4)

    def test_gemini_permission_flags(self):
        self.assertIn("--yolo", _gemini_permission_flags({"gemini_yolo": True}))
        self.assertEqual(
            _gemini_permission_flags({"gemini_yolo": True}, disable_tools=True),
            ["--skip-trust", "--approval-mode", "plan"],
        )

    def test_exploration_agent_builds_gemini_session_command(self):
        captured = {}

        def fake_invoke(cmd, stdin_text, **kwargs):
            captured["cmd"] = cmd
            captured["stdin"] = stdin_text
            return {
                "result": "[OUTPUT: out]done[END OUTPUT]",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "duration_ms": 1,
                "session_id": "returned",
            }

        with tempfile.TemporaryDirectory() as td:
            config = load_config()
            config.update({
                "llm_provider": "gemini",
                "model": "gemini-3-flash-preview",
                "gemini_model": "gemini-3-flash-preview",
                "context_window": 1_000_000,
                "working_directory": td,
            })
            agent_sessions = {}
            with patch.dict(os.environ, {"LONG_EXPOSURE_LLM_PROVIDER": "gemini"}, clear=False):
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
                        agent_sessions=agent_sessions,
                        agent_summaries={},
                    )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["outputs"]["out"], "done")
        self.assertIn("--session-id", captured["cmd"])
        self.assertIn("--skip-trust", captured["cmd"])
        self.assertIn("--output-format", captured["cmd"])
        self.assertIn("--yolo", captured["cmd"])
        self.assertIn("[SYSTEM PROMPT]", captured["stdin"])
        self.assertIn("[USER PROMPT]", captured["stdin"])

    def test_saved_state_records_session_provider(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "exploration_state.json")
            with patch.dict(os.environ, {"LONG_EXPOSURE_LLM_PROVIDER": "gemini"}, clear=False):
                provider.configure_provider({"llm_provider": "gemini"})
                save_state(
                    path=Path(path),
                    cycle=1,
                    results={"directive": "x"},
                    failures={},
                    agent_sessions={"researcher": "abc"},
                )
            state = load_state(Path(path))
        self.assertEqual(state["agent_sessions_provider"], "gemini")


if __name__ == "__main__":
    unittest.main()
