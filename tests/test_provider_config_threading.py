"""Provider calls must honour the run's --config, not the packaged one.

`_invoke_claude` and `call_claude` used to call `load_config()` with no
path for the Gemini auth selector and the Codex/Gemini/local settings,
which reads the packaged `long_exposure/config.yaml` and silently ignores
the run's `--config` file. Both now take an optional `config=` and fall
back to the old behaviour only when it is omitted.
"""

import unittest
from unittest.mock import patch

from long_exposure import provider as _provider
from long_exposure.orchestrator import (
    _configure_gemini_auth_env,
    call_claude,
    load_config,
)


class CodexConfigThreadingTests(unittest.TestCase):
    def _run(self, config):
        seen = {}

        def fake_invoke(cmd, stdin_text, **kwargs):
            seen["cmd"] = cmd
            seen["config"] = kwargs.get("config")
            return {"result": "ok", "usage": {}, "duration_ms": 1}

        with patch("long_exposure.orchestrator._provider.is_local", return_value=False), \
                patch("long_exposure.orchestrator._provider.is_codex", return_value=True), \
                patch("long_exposure.orchestrator._provider.is_gemini", return_value=False), \
                patch("long_exposure.orchestrator._invoke_claude", fake_invoke), \
                patch("long_exposure.orchestrator._parse_accounts", return_value=["a"]):
            call_claude(prompt="p", system_prompt="s", model="gpt-5.5", config=config)
        return seen

    def test_run_config_controls_codex_yolo(self):
        # The packaged config.yaml ships codex_yolo: true. A run whose
        # --config disables it must not get --yolo.
        self.assertTrue(load_config().get("codex_yolo"))
        seen = self._run({"codex_yolo": False})
        self.assertNotIn("--yolo", seen["cmd"])
        self.assertIn("-s", seen["cmd"])
        # And the config is threaded down to _invoke_claude.
        self.assertEqual(seen["config"], {"codex_yolo": False})

    def test_omitted_config_falls_back_to_packaged(self):
        seen = self._run(None)
        self.assertIn("--yolo", seen["cmd"])
        self.assertIsNone(seen["config"])

    def test_run_config_controls_codex_subagent_caps(self):
        seen = self._run({
            "codex_yolo": True,
            "codex_subagents": {"max_threads": 7, "max_depth": 1},
        })
        joined = " ".join(seen["cmd"])
        self.assertIn("agents.max_threads=7", joined)


class GeminiConfigThreadingTests(unittest.TestCase):
    def test_gemini_branch_does_not_mutate_the_run_config(self):
        run_config = {"gemini_yolo": False, "working_directory": "/original"}
        captured = {}

        def fake_settings(cfg):
            captured["cfg"] = dict(cfg)
            return None

        def fake_invoke(cmd, stdin_text, **kwargs):
            captured["cmd"] = cmd
            captured["config"] = kwargs.get("config")
            return {"result": "ok", "usage": {}, "duration_ms": 1}

        with patch("long_exposure.orchestrator._provider.is_local", return_value=False), \
                patch("long_exposure.orchestrator._provider.is_codex", return_value=False), \
                patch("long_exposure.orchestrator._provider.is_gemini", return_value=True), \
                patch("long_exposure.orchestrator.generate_gemini_project_settings", fake_settings), \
                patch("long_exposure.orchestrator._invoke_claude", fake_invoke), \
                patch("long_exposure.orchestrator._parse_accounts", return_value=["a"]):
            call_claude(
                prompt="p", system_prompt="s", model="gemini-3-flash-preview",
                cwd="/work", effort="high", config=run_config,
            )

        # The branch sets working_directory/effort on its own copy only.
        self.assertEqual(run_config, {"gemini_yolo": False, "working_directory": "/original"})
        self.assertEqual(captured["cfg"]["working_directory"], "/work")
        self.assertEqual(captured["cfg"]["effort"], "high")
        # gemini_yolo: false from the run config means no --yolo.
        self.assertNotIn("--yolo", captured["cmd"])
        self.assertIs(captured["config"], run_config)

    def test_auth_env_selector_comes_from_the_passed_config(self):
        env = {}
        _configure_gemini_auth_env(env, {"gemini_auth_env": "MY_SELECTOR", "gemini_auth_value": "yes"})
        self.assertEqual(env, {"MY_SELECTOR": "yes"})
        # Already-authenticated environments are left alone.
        preset = {"GEMINI_API_KEY": "k"}
        _configure_gemini_auth_env(preset, {"gemini_auth_env": "MY_SELECTOR"})
        self.assertEqual(preset, {"GEMINI_API_KEY": "k"})


class LocalConfigThreadingTests(unittest.TestCase):
    def test_local_branch_receives_the_run_config(self):
        seen = {}

        def fake_local(**kwargs):
            seen.update(kwargs)
            return {"result": "ok", "usage": {}, "duration_ms": 1}

        run_config = {"local_base_url": "http://127.0.0.1:9/v1", "local_max_tokens": 123}
        with patch("long_exposure.orchestrator._provider.is_local", return_value=True), \
                patch("long_exposure.orchestrator.call_local_llm", fake_local):
            call_claude(prompt="p", system_prompt="s", model="m", config=run_config)
        self.assertIs(seen["config"], run_config)

        with patch("long_exposure.orchestrator._provider.is_local", return_value=True), \
                patch("long_exposure.orchestrator.call_local_llm", fake_local):
            call_claude(prompt="p", system_prompt="s", model="m")
        # Fallback path still yields a usable config dict.
        self.assertIn("local_base_url", seen["config"])


if __name__ == "__main__":
    unittest.main()
