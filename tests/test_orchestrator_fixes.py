import os
import tempfile
import unittest
from unittest.mock import patch

from long_exposure import orchestrator


class ParseAccountsGeminiGuardTests(unittest.TestCase):
    def test_gemini_account_pool_env_is_ignored(self):
        # provider.parse_pool_env deliberately returns [] for gemini (OAuth
        # pooling not live-validated); the orchestrator's duplicate parser
        # must honor the same guard.
        with patch.dict(
            os.environ,
            {
                "LONG_EXPOSURE_LLM_PROVIDER": "gemini",
                "GEMINI_ACCOUNT_POOL": "/tmp/a,/tmp/b",
                "GEMINI_HOMES": "/tmp/c",
            },
            clear=False,
        ):
            self.assertEqual(orchestrator._parse_accounts(), [None])

    def test_claude_account_pool_env_still_parses(self):
        with patch.dict(
            os.environ,
            {
                "LONG_EXPOSURE_LLM_PROVIDER": "claude",
                "CLAUDE_ACCOUNT_POOL": "/tmp/a,/tmp/b",
            },
            clear=False,
        ):
            self.assertEqual(orchestrator._parse_accounts(), ["/tmp/a", "/tmp/b"])


class CallClaudeIdleWiringTests(unittest.TestCase):
    _clean_env = {
        "LONG_EXPOSURE_LLM_PROVIDER": "claude",
        "CLAUDE_ACCOUNT_POOL": "",
        "CLAUDE_ACCOUNTS": "",
        "CLAUDE_FORCE_ACCOUNT": "",
    }

    def test_explicit_idle_args_reach_invoke_claude(self):
        captured = {}

        def fake_invoke(cmd, stdin_text, **kwargs):
            captured.update(kwargs)
            return {"result": "ok", "usage": {}}

        with patch.dict(os.environ, self._clean_env, clear=False):
            with patch("long_exposure.orchestrator._invoke_claude", fake_invoke):
                orchestrator.call_claude(
                    "prompt", "system", model="opus", timeout=0,
                    disable_tools=True, idle_timeout=123, idle_poll=7,
                )
        self.assertEqual(captured["idle_timeout"], 123)
        self.assertEqual(captured["idle_poll"], 7)

    def test_idle_args_default_to_module_constants_without_load_config(self):
        # Omitted idle kwargs must fall back to the module-level defaults —
        # NOT to a per-call load_config(), which would read
        # DEFAULT_CONFIG_PATH (ignoring the run's --config overrides) and
        # re-run YAML parsing + configure_provider side effects per call.
        captured = {}

        def fake_invoke(cmd, stdin_text, **kwargs):
            captured.update(kwargs)
            return {"result": "ok", "usage": {}}

        def fail_load_config(*args, **kwargs):
            raise AssertionError("call_claude must not call load_config()")

        with patch.dict(os.environ, self._clean_env, clear=False):
            with patch("long_exposure.orchestrator._invoke_claude", fake_invoke):
                with patch("long_exposure.orchestrator.load_config", fail_load_config):
                    orchestrator.call_claude(
                        "prompt", "system", model="opus", timeout=0,
                        disable_tools=True,
                    )
        self.assertEqual(
            captured["idle_timeout"],
            orchestrator.DEFAULT_PROVIDER_IDLE_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            captured["idle_poll"],
            orchestrator.DEFAULT_PROVIDER_IDLE_POLL_SECONDS,
        )

    def test_module_idle_defaults_match_load_config_defaults(self):
        # The kwarg fallback constants and load_config()'s defaults dict are
        # the same objects by construction; assert the contract anyway so a
        # future hardcoded edit to either side fails loudly.
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
            f.write("{}\n")
            f.flush()
            cfg = orchestrator.load_config(path=f.name)
        self.assertEqual(
            cfg["provider_idle_timeout_seconds"],
            orchestrator.DEFAULT_PROVIDER_IDLE_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            cfg["provider_idle_poll_seconds"],
            orchestrator.DEFAULT_PROVIDER_IDLE_POLL_SECONDS,
        )


if __name__ == "__main__":
    unittest.main()
