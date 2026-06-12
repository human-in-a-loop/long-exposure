import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from long_exposure.orchestrator import load_config


def _load(text: str, env: dict | None = None) -> dict:
    """Write `text` to a temp config.yaml and load it with a clean env."""
    base_env = {
        "LONG_EXPOSURE_LLM_PROVIDER": "claude",
        "LONG_EXPOSURE_CLAUDE_TRANSPORT": "",
    }
    base_env.update(env or {})
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "config.yaml"
        path.write_text(text)
        with patch.dict(os.environ, base_env, clear=False):
            return load_config(path)


class ConfigValidationTests(unittest.TestCase):
    def test_empty_config_yaml_falls_back_to_defaults(self):
        config = _load("")
        self.assertEqual(config["model"], "opus")
        self.assertEqual(config["claude_transport"], "headless")

    def test_comments_only_config_yaml_falls_back_to_defaults(self):
        config = _load("# nothing but comments\n# here\n")
        self.assertEqual(config["claude_transport"], "headless")
        self.assertEqual(config["interactive_permission_mode"], "skip")

    def test_unknown_claude_transport_forced_to_headless(self):
        config = _load("claude_transport: tmux\n")
        self.assertEqual(config["claude_transport"], "headless")

    def test_valid_claude_transport_values_pass_through(self):
        self.assertEqual(
            _load("claude_transport: interactive\n")["claude_transport"],
            "interactive",
        )
        self.assertEqual(
            _load("claude_transport: HEADLESS\n")["claude_transport"],
            "headless",
        )

    def test_unknown_interactive_permission_mode_forced_to_scoped(self):
        # Fail toward the RESTRICTIVE mode: a typo must never silently
        # select permission-skipping.
        config = _load("interactive_permission_mode: skipp\n")
        self.assertEqual(config["interactive_permission_mode"], "scoped")

    def test_valid_interactive_permission_modes_pass_through(self):
        self.assertEqual(
            _load("interactive_permission_mode: skip\n")["interactive_permission_mode"],
            "skip",
        )
        self.assertEqual(
            _load("interactive_permission_mode: scoped\n")["interactive_permission_mode"],
            "scoped",
        )

    def test_env_transport_override_is_validated_too(self):
        config = _load(
            "claude_transport: headless\n",
            env={"LONG_EXPOSURE_CLAUDE_TRANSPORT": "tmux"},
        )
        self.assertEqual(config["claude_transport"], "headless")

    def test_env_transport_override_valid_value_applies(self):
        config = _load(
            "claude_transport: headless\n",
            env={"LONG_EXPOSURE_CLAUDE_TRANSPORT": "interactive"},
        )
        self.assertEqual(config["claude_transport"], "interactive")


if __name__ == "__main__":
    unittest.main()
