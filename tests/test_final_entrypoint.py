"""run_final_reporter.py (standalone end-of-run pipeline) and usage
attribution after a provider rotation. Offline: every provider call is
patched.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import long_exposure.exploration as exploration
from long_exposure import provider as _provider
from long_exposure import telemetry

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_final_reporter  # noqa: E402

from test_run_switches import _write_files  # noqa: E402


class ServedAttributionTests(unittest.TestCase):
    """The ledger must price a call against the provider that served it."""

    def setUp(self):
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_live_env_wins_over_stale_config_after_rotation(self):
        # A unified-pool rotation repins the process via the env and leaves
        # config["llm_provider"] at its pre-rotation value.
        config = {
            "llm_provider": "claude",
            "model": "opus",
            "codex_model": "gpt-5.5",
        }
        os.environ["LONG_EXPOSURE_LLM_PROVIDER"] = "codex"
        prov, model = exploration._served_attribution({}, config)
        self.assertEqual(prov, "codex")
        self.assertEqual(model, "gpt-5.5")

    def test_no_rotation_matches_config(self):
        config = {"llm_provider": "claude", "model": "opus", "codex_model": "gpt-5.5"}
        os.environ["LONG_EXPOSURE_LLM_PROVIDER"] = "claude"
        self.assertEqual(
            exploration._served_attribution({}, config), ("claude", "opus")
        )

    def test_explicit_agent_model_wins(self):
        config = {"llm_provider": "codex", "model": "opus", "codex_model": "gpt-5.5"}
        os.environ["LONG_EXPOSURE_LLM_PROVIDER"] = "codex"
        prov, model = exploration._served_attribution({"model": "gpt-5.5-codex"}, config)
        self.assertEqual((prov, model), ("codex", "gpt-5.5-codex"))

    def test_per_agent_pinned_resolves_from_agent_def(self):
        # Pinned runs unwind the env swap around the call, so the agent
        # definition — not the env — is the truth at ledger time.
        config = {
            "llm_provider": "claude",
            "model": "opus",
            "gemini_model": "gemini-3-flash-preview",
            "_per_agent_pinned": True,
        }
        os.environ["LONG_EXPOSURE_LLM_PROVIDER"] = "claude"
        prov, model = exploration._served_attribution({"provider": "gemini"}, config)
        self.assertEqual(prov, "gemini")
        self.assertEqual(model, "gemini-3-flash-preview")

    def test_unknown_provider_falls_back_to_config_model(self):
        config = {"llm_provider": "claude", "model": "opus"}
        os.environ["LONG_EXPOSURE_LLM_PROVIDER"] = "claude"
        self.assertEqual(
            exploration._served_attribution(None, config), ("claude", "opus")
        )


class StandalonePipelineTests(unittest.TestCase):
    """`python run_final_reporter.py` must not damage the state file."""

    def setUp(self):
        self._env = dict(os.environ)
        self._prev_usage = exploration._usage.to_dict()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        exploration._usage.load(self._prev_usage)
        telemetry.configure({"telemetry": {"enabled": False}}, None, None)
        _provider.configure_provider({"llm_provider": "claude"})

    def _seed_state(self, inst: Path) -> Path:
        state_path = inst / "exploration_state.json"
        state_path.write_text(json.dumps({
            "cycle": 7,
            "results": {"directive": "saved task", "run_id": "run-FROM-RESULTS"},
            "failures": {"worker": 1},
            "last_session_id": "sess-1",
            "agent_sessions": {"worker": "sess-w"},
            "agent_summaries": {},
            "task": "saved task",
            "run_id": "run-FROM-STATE",
            "last_daily_sync_at": "2026-09-01T00:00:00+00:00",
            "daily_sync_count": 3,
            "_reanchor_emitted": {"worker": True},
            "agent_context_tokens": {"worker": 4242},
            "peak_cycle_output": 9999,
            "low_output_streak": 2,
            "usage_basis": "local",
            # Flat ledger shape: {agent_name: row, "_clones": {...}}.
            "usage_totals": {
                "worker": {"calls": 5, "cost_usd": 4.0, "output_tokens": 100},
                "_clones": {"forks": 1, "clones": 2},
            },
        }))
        return state_path

    def _run_main(self, inst: Path, score: Path, config: Path, *, extra=()):
        argv = [
            "run_final_reporter.py",
            "--score", str(score),
            "--config", str(config),
            "--instance-dir", str(inst),
            *extra,
        ]
        auditor = MagicMock(return_value="sess-audit")
        reporter = MagicMock(return_value="sess-report")
        curator = MagicMock(return_value="sess-curate")
        with patch.object(sys, "argv", argv), \
                patch("run_final_reporter._run_final_auditor", auditor), \
                patch("run_final_reporter._run_final_reporter", reporter), \
                patch("run_final_reporter._run_curator", curator), \
                patch("run_final_reporter._render_final_pdf", MagicMock()):
            run_final_reporter.main()
        return auditor, reporter, curator

    def test_state_fields_survive_and_usage_extends(self):
        with tempfile.TemporaryDirectory() as td:
            score, config, inst = _write_files(Path(td), final_agents=True)
            state_path = self._seed_state(inst)
            auditor, reporter, curator = self._run_main(inst, score, config)

            auditor.assert_called_once()
            reporter.assert_called_once()
            curator.assert_called_once()
            # run_id is threaded explicitly into the auditor, which keys its
            # reconciliation event ids on it.
            self.assertEqual(auditor.call_args.kwargs["run_id"], "run-FROM-STATE")

            saved = json.loads(state_path.read_text())
            self.assertEqual(saved["run_id"], "run-FROM-STATE")
            self.assertEqual(saved["task"], "saved task")
            self.assertEqual(saved["last_daily_sync_at"], "2026-09-01T00:00:00+00:00")
            self.assertEqual(saved["daily_sync_count"], 3)
            self.assertEqual(saved["_reanchor_emitted"], {"worker": True})
            self.assertEqual(saved["agent_context_tokens"], {"worker": 4242})
            self.assertEqual(saved["peak_cycle_output"], 9999)
            self.assertEqual(saved["low_output_streak"], 2)
            self.assertEqual(saved["usage_basis"], "local")
            self.assertEqual(saved["failures"], {"worker": 1})
            # The run's prior spend is carried, not overwritten.
            self.assertEqual(saved["usage_totals"]["worker"]["calls"], 5)
            self.assertAlmostEqual(saved["usage_totals"]["worker"]["cost_usd"], 4.0)
            self.assertEqual(saved["usage_totals"]["_clones"], {"forks": 1, "clones": 2})

    def test_run_id_falls_back_to_results_then_synthesizes(self):
        with tempfile.TemporaryDirectory() as td:
            score, config, inst = _write_files(Path(td), final_agents=True)
            state_path = self._seed_state(inst)
            state = json.loads(state_path.read_text())
            del state["run_id"]  # pre-dates run_id persistence
            state_path.write_text(json.dumps(state))
            auditor, _, _ = self._run_main(inst, score, config)
            self.assertEqual(auditor.call_args.kwargs["run_id"], "run-FROM-RESULTS")

        with tempfile.TemporaryDirectory() as td:
            score, config, inst = _write_files(Path(td), final_agents=True)
            state_path = self._seed_state(inst)
            state = json.loads(state_path.read_text())
            del state["run_id"]
            del state["results"]["run_id"]
            state_path.write_text(json.dumps(state))
            auditor, _, _ = self._run_main(inst, score, config)
            self.assertTrue(
                auditor.call_args.kwargs["run_id"].startswith("run-"),
                auditor.call_args.kwargs["run_id"],
            )

    def test_observability_sinks_are_configured(self):
        with tempfile.TemporaryDirectory() as td:
            score, config, inst = _write_files(Path(td), final_agents=True)
            self._seed_state(inst)
            self._run_main(inst, score, config)
            # health_events routes to the state dir, not to the default
            # AGENT_INSTANCE_DIR-only path.
            from long_exposure import health_events
            self.assertEqual(
                health_events._resolve_log_path(),
                inst / "health_events.jsonl",
            )
            self.assertTrue(telemetry.is_enabled())

    def test_skip_auditor_leaves_the_audit_alone(self):
        with tempfile.TemporaryDirectory() as td:
            score, config, inst = _write_files(Path(td), final_agents=True)
            self._seed_state(inst)
            auditor, reporter, curator = self._run_main(
                inst, score, config, extra=("--skip-auditor",)
            )
            auditor.assert_not_called()
            reporter.assert_called_once()
            curator.assert_called_once()

    def test_end_of_run_switch_off_skips_every_stage(self):
        with tempfile.TemporaryDirectory() as td:
            score, config, inst = _write_files(
                Path(td), loop_extra="  end_of_run: false\n", final_agents=True
            )
            self._seed_state(inst)
            auditor, reporter, curator = self._run_main(inst, score, config)
            auditor.assert_not_called()
            reporter.assert_not_called()
            curator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
