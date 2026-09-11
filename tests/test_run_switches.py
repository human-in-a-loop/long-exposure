"""Run-level switches: loop.fanout_enabled, loop.end_of_run, and the usage
ledger / budget gates. Offline: every provider call is patched."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from long_exposure import cli, telemetry
import long_exposure.exploration as exploration
from long_exposure.exploration import (
    _end_of_run_enabled,
    _fanout_enabled,
    run_exploration,
)


def _write_files(root: Path, *, loop_extra: str = "", final_agents: bool = False):
    workspace = root / "workspace"
    instance = root / "instance"
    workspace.mkdir()
    instance.mkdir()
    score = root / "score.yaml"
    agents = (
        "agents:\n"
        "  researcher:\n"
        "    inputs: [directive, audit_report, live_guidance]\n"
        "    outputs: [research_brief]\n"
        "    role: researcher\n"
        "  worker:\n"
        "    inputs: [directive, research_brief]\n"
        "    outputs: [work_output]\n"
        "    role: worker\n"
        "  auditor:\n"
        "    inputs: [directive, work_output]\n"
        "    outputs: [audit_report]\n"
        "    role: auditor\n"
    )
    if final_agents:
        agents += (
            "  final_auditor:\n"
            "    inputs: [directive]\n"
            "    outputs: [final_audit_stage]\n"
            "    role: final auditor\n"
            "  final_reporter:\n"
            "    inputs: [directive]\n"
            "    outputs: [final_report_stage]\n"
            "    role: final reporter\n"
            "  curator:\n"
            "    inputs: [directive]\n"
            "    outputs: [curation_status]\n"
            "    role: curator\n"
        )
    score.write_text(
        "task: test directive\n"
        "loop:\n"
        "  max_cycles: 1\n"
        "  cycle_cooldown_seconds: 0\n"
        "  report_interval: 100\n"
        "  daily_sync_interval_hours: 0\n"
        + loop_extra
        + agents
        + "flow: [researcher, worker, auditor]\n"
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
        "telemetry:\n"
        "  enabled: true\n"
    )
    return score, config, instance


def _fake_agent_factory(seen: list, *, cost_usd=None, tool_calls=2):
    def fake_agent(agent_name, agent_def, **kwargs):
        seen.append({
            "agent": agent_name,
            "live_guidance": (kwargs.get("results") or {}).get("live_guidance", ""),
        })
        output_name = agent_def["outputs"][0]
        return {
            "agent": agent_name,
            "outputs": {output_name: f"{agent_name} output " + ("x" * 2100)},
            "usage": {"input_tokens": 100, "output_tokens": 2100},
            "duration_ms": 10,
            "status": "ok",
            "error": None,
            "cost_usd": cost_usd,
            "num_turns": 1,
            "tool_calls": tool_calls,
        }
    return fake_agent


def _run(score, config, inst):
    run_exploration(
        score_path=str(score),
        config_path=str(config),
        output_dir=inst / "output",
        state_path=inst / "exploration_state.json",
        task_override=None,
        instance_dir=inst,
    )


class SwitchHelperTests(unittest.TestCase):
    def test_fanout_enabled_defaults_and_env_override(self):
        self.assertTrue(_fanout_enabled({}))
        self.assertTrue(_fanout_enabled(None))
        self.assertFalse(_fanout_enabled({"fanout_enabled": False}))
        with patch.dict(os.environ, {"LONG_EXPOSURE_FANOUT": "0"}):
            self.assertFalse(_fanout_enabled({"fanout_enabled": True}))
        with patch.dict(os.environ, {"LONG_EXPOSURE_FANOUT": "1"}):
            self.assertTrue(_fanout_enabled({"fanout_enabled": False}))

    def test_end_of_run_enabled_shapes(self):
        for stage in ("final_auditor", "final_reporter", "curator"):
            self.assertTrue(_end_of_run_enabled({}, stage))
            self.assertFalse(_end_of_run_enabled({"end_of_run": False}, stage))
            self.assertFalse(_end_of_run_enabled({"end_of_run": {"enabled": False}}, stage))
        cfg = {"end_of_run": {"final_auditor": False}}
        self.assertFalse(_end_of_run_enabled(cfg, "final_auditor"))
        self.assertTrue(_end_of_run_enabled(cfg, "final_reporter"))
        self.assertTrue(_end_of_run_enabled(cfg, "curator"))
        with patch.dict(os.environ, {"LONG_EXPOSURE_END_OF_RUN": "off"}):
            self.assertFalse(_end_of_run_enabled({}, "curator"))


class RunSwitchIntegrationTests(unittest.TestCase):
    def tearDown(self):
        telemetry.configure({"telemetry": {"enabled": False}}, None, None)

    def test_fanout_disabled_skips_guidance_and_parser(self):
        seen = []
        parser = MagicMock(return_value=None)
        with tempfile.TemporaryDirectory() as td:
            score, config, inst = _write_files(Path(td), loop_extra="  fanout_enabled: false\n")
            with patch("long_exposure.exploration._call_exploration_agent", _fake_agent_factory(seen)), \
                    patch("long_exposure.exploration._parse_fanout_block", parser):
                _run(score, config, inst)
        parser.assert_not_called()
        researcher = next(s for s in seen if s["agent"] == "researcher")
        self.assertNotIn("parallel_cycle_fanout", researcher["live_guidance"])

    def test_fanout_enabled_by_default_injects_guidance_and_parses(self):
        seen = []
        parser = MagicMock(return_value=None)
        with tempfile.TemporaryDirectory() as td:
            score, config, inst = _write_files(Path(td))
            with patch("long_exposure.exploration._call_exploration_agent", _fake_agent_factory(seen)), \
                    patch("long_exposure.exploration._parse_fanout_block", parser):
                _run(score, config, inst)
        parser.assert_called_once()
        researcher = next(s for s in seen if s["agent"] == "researcher")
        self.assertIn("parallel_cycle_fanout", researcher["live_guidance"])

    def _run_with_final_stages(self, loop_extra: str):
        seen = []
        auditor = MagicMock(return_value=None)
        reporter = MagicMock(return_value=None)
        curator = MagicMock(return_value=None)
        with tempfile.TemporaryDirectory() as td:
            score, config, inst = _write_files(Path(td), loop_extra=loop_extra, final_agents=True)
            with patch("long_exposure.exploration._call_exploration_agent", _fake_agent_factory(seen)), \
                    patch("long_exposure.auditing._run_final_auditor", auditor), \
                    patch("long_exposure.exploration._run_final_reporter", reporter), \
                    patch("long_exposure.exploration._run_curator", curator):
                _run(score, config, inst)
        return auditor, reporter, curator

    def test_end_of_run_all_stages_run_by_default(self):
        auditor, reporter, curator = self._run_with_final_stages("")
        auditor.assert_called_once()
        reporter.assert_called_once()
        curator.assert_called_once()

    def test_end_of_run_per_stage_switches(self):
        auditor, reporter, curator = self._run_with_final_stages(
            "  end_of_run:\n    final_auditor: false\n    final_reporter: false\n"
        )
        auditor.assert_not_called()
        reporter.assert_not_called()
        curator.assert_called_once()

    def test_end_of_run_master_switch_off(self):
        auditor, reporter, curator = self._run_with_final_stages("  end_of_run: false\n")
        auditor.assert_not_called()
        reporter.assert_not_called()
        curator.assert_not_called()

    def test_usage_ledger_persisted_rendered_and_budget_stops_run(self):
        seen = []
        with tempfile.TemporaryDirectory() as td:
            score, config, inst = _write_files(
                Path(td),
                loop_extra="  max_cost_usd: 20\n",
            )
            # 3 agents x $4 = $12/cycle; cap $20 -> boundary check after
            # cycle 2 sees $24 and stops before cycle 3 (max_cycles is 1 in
            # the fixture, so raise it here).
            score.write_text(score.read_text().replace("max_cycles: 1", "max_cycles: 5"))
            with patch(
                "long_exposure.exploration._call_exploration_agent",
                _fake_agent_factory(seen, cost_usd=4.0, tool_calls=3),
            ):
                _run(score, config, inst)
            state = json.loads((inst / "exploration_state.json").read_text())
            status = (inst / "output" / "exploration_status.md").read_text()
            summary = json.loads((inst / "output" / "usage_summary.json").read_text())
            events = [
                json.loads(line)
                for line in (inst / "telemetry" / "events.jsonl").read_text().splitlines()
            ]
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = cli.main(["--instance-dir", str(inst), "usage"])
            with contextlib.redirect_stdout(io.StringIO()) as out_json:
                rc_json = cli.main(["--instance-dir", str(inst), "usage", "--json"])

        self.assertEqual(state["cycle"], 2)
        totals = state["usage_totals"]
        self.assertEqual(totals["researcher"]["calls"], 2)
        self.assertEqual(totals["worker"]["cost_usd"], 8.0)
        self.assertEqual(totals["auditor"]["tool_calls"], 6)
        self.assertEqual(summary["totals"]["cost_usd"], 24.0)
        self.assertEqual(summary["totals"]["tool_calls"], 18)
        self.assertIn("cost budget reached", summary["budget"]["exceeded"])
        self.assertIn("## Usage", status)
        self.assertIn("| **Total** |", status)
        self.assertIn("$24.00 of $20.00", status)
        run_end = next(e for e in events if e["event_type"] == "run_end")
        self.assertTrue(run_end["data"]["budget_exhausted"])
        self.assertEqual(run_end["data"]["usage_totals"]["cost_usd"], 24.0)
        agent_end = next(e for e in events if e["event_type"] == "agent_call_end")
        self.assertEqual(agent_end["data"]["cost_usd"], 4.0)
        self.assertEqual(agent_end["data"]["tool_calls"], 3)
        self.assertEqual(agent_end["data"]["cost_source"], "provider")
        self.assertEqual(rc, 0)
        self.assertIn("| researcher |", out.getvalue())
        self.assertEqual(rc_json, 0)
        self.assertEqual(json.loads(out_json.getvalue())["totals"]["cost_usd"], 24.0)

    def test_usage_totals_survive_resume(self):
        seen = []
        with tempfile.TemporaryDirectory() as td:
            score, config, inst = _write_files(Path(td))
            with patch(
                "long_exposure.exploration._call_exploration_agent",
                _fake_agent_factory(seen, cost_usd=1.0),
            ):
                _run(score, config, inst)
                first = json.loads((inst / "exploration_state.json").read_text())
                # Resume for one more cycle (no task override + existing
                # state = continue): totals must accumulate, not reset.
                score.write_text(score.read_text().replace("max_cycles: 1", "max_cycles: 2"))
                _run(score, config, inst)
                second = json.loads((inst / "exploration_state.json").read_text())
        self.assertEqual(first["usage_totals"]["worker"]["calls"], 1)
        self.assertEqual(second["cycle"], 2)
        self.assertEqual(second["usage_totals"]["worker"]["calls"], 2)
        self.assertEqual(second["usage_totals"]["worker"]["cost_usd"], 2.0)


if __name__ == "__main__":
    unittest.main()
