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


class CompactionAndCheckpointConfigTests(unittest.TestCase):
    """The config= fix stopped short of the orchestrator's own summary calls.

    `compact_with_conditioning` and `checkpoint_without_compaction` built
    their `call_claude_pool_aware` kwargs without `config=`, so inside
    `call_claude` those paths took the `load_config()` fallback and read the
    PACKAGED config.yaml. A run started with `--config myrun.yaml` setting
    `local_base_url` therefore had ordinary turns honour it while
    compaction and checkpoint went to the packaged endpoint — summarizing
    on a different model, or failing outright.
    """

    class _Sentinel(Exception):
        pass

    def _captured_kwargs(self, call):
        seen = {}

        def fake_pool_aware(**kwargs):
            seen.update(kwargs)
            raise self._Sentinel()

        with patch("long_exposure.orchestrator.call_claude_pool_aware",
                   fake_pool_aware):
            with self.assertRaises(self._Sentinel):
                call()
        return seen

    def _config(self):
        config = load_config()
        config["model"] = "test-model"
        config["local_base_url"] = "http://run-specific:9000/v1"
        return config

    def test_compaction_passes_the_run_config(self):
        from long_exposure.orchestrator import compact_with_conditioning
        config = self._config()
        seen = self._captured_kwargs(lambda: compact_with_conditioning(
            config, None, [{"role": "user", "content": "hi"}], 0, None, 1000,
        ))
        self.assertIs(seen.get("config"), config)

    def test_checkpoint_passes_the_run_config(self):
        from long_exposure.orchestrator import checkpoint_without_compaction
        config = self._config()
        seen = self._captured_kwargs(lambda: checkpoint_without_compaction(
            config, None, [{"role": "user", "content": "hi"}], 0, None, 1000,
        ))
        self.assertIs(seen.get("config"), config)


if __name__ == "__main__":
    unittest.main()
