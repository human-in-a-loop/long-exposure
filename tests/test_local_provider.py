import os
import tempfile
import unittest
from unittest.mock import patch

from long_exposure import provider
from long_exposure.exploration import (
    _append_local_session_turn,
    _format_local_recent_log,
    _read_local_session_turns,
)
from long_exposure.orchestrator import assemble_system_prompt, load_config


class LocalProviderTests(unittest.TestCase):
    def test_normalizes_generic_local_aliases(self):
        self.assertEqual(provider.normalize_provider("openai-compatible"), "local")
        self.assertEqual(provider.normalize_provider("custom"), "local")
        self.assertEqual(provider.normalize_provider("local"), "local")

    def test_load_config_applies_custom_local_model_and_context(self):
        with patch.dict(os.environ, {"LONG_EXPOSURE_LLM_PROVIDER": "local"}, clear=False):
            config = load_config()
        self.assertEqual(config["llm_provider"], "local")
        self.assertEqual(config["model"], config["local_model"])
        self.assertEqual(config["local_model"], "custom-local-model")
        self.assertEqual(config["context_window"], config["local_context_window"])
        self.assertEqual(config["context_window"], 32768)

    def test_local_prompt_does_not_advertise_mcp_session_tools(self):
        with patch.dict(os.environ, {"LONG_EXPOSURE_LLM_PROVIDER": "local"}, clear=False):
            config = load_config()
            prompt = assemble_system_prompt(config)
        self.assertNotIn("[AVAILABLE TOOLS]", prompt)
        self.assertNotIn("search_sessions(", prompt)

    def test_claude_prompt_still_advertises_mcp_session_tools(self):
        with patch.dict(os.environ, {"LONG_EXPOSURE_LLM_PROVIDER": "claude"}, clear=False):
            config = load_config()
            prompt = assemble_system_prompt(config)
        self.assertIn("[AVAILABLE TOOLS]", prompt)
        self.assertIn("search_sessions(query, limit)", prompt)

    def test_local_session_log_round_trip_and_recent_injection(self):
        with tempfile.TemporaryDirectory() as td:
            config = {
                "instance_dir": td,
                "context_window": 32768,
                "local_recent_log_pct": 0.25,
            }
            _append_local_session_turn(
                config,
                "session-1",
                "researcher",
                "user prompt",
                "assistant response",
                {"input_tokens": 3, "output_tokens": 2},
            )
            turns = _read_local_session_turns(config, "session-1")
            self.assertEqual(len(turns), 1)
            self.assertEqual(turns[0]["agent"], "researcher")
            recent = _format_local_recent_log(config, "session-1")
            self.assertIn("[RECENT LOCAL SESSION LOG]", recent)
            self.assertIn("assistant response", recent)


if __name__ == "__main__":
    unittest.main()
