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


class ProviderToolCountTests(unittest.TestCase):
    """Tool-call counts per provider envelope and the clone seed guard."""

    def test_codex_envelope_counts_completed_tool_items(self):
        from long_exposure.orchestrator import _extract_codex_envelope
        stdout = "\n".join([
            '{"type":"thread.started","thread_id":"t1"}',
            '{"type":"item.started","item":{"type":"command_execution","id":"c1"}}',
            '{"type":"item.completed","item":{"type":"command_execution","id":"c1"}}',
            '{"type":"item.completed","item":{"type":"reasoning","id":"r1"}}',
            '{"type":"item.completed","item":{"type":"agent_message","id":"m1"}}',
            '{"msg":{"type":"item.completed","item":{"type":"mcp_tool_call","id":"x1"}}}',
            '{"type":"item.completed","item":{"type":"file_change","id":"f1"}}',
            '{"type":"turn.completed","usage":{"input_tokens":4,"output_tokens":5}}',
        ])
        env = _extract_codex_envelope(stdout, "done", 9)
        self.assertEqual(env["tool_calls"], 3)
        # An `error` item is not a tool; a stream with no item events at all
        # reports an unknown count (None), not a confident zero.
        with_error = stdout + '\n{"type":"item.completed","item":{"type":"error","id":"e1"}}'
        self.assertEqual(_extract_codex_envelope(with_error, "done", 9)["tool_calls"], 3)
        no_items = "\n".join([
            '{"msg":{"type":"thread.started","thread_id":"t1"}}',
            '{"msg":{"type":"turn.completed","usage":{"input_tokens":4,"output_tokens":5}}}',
        ])
        self.assertIsNone(_extract_codex_envelope(no_items, "done", 9)["tool_calls"])

    def test_gemini_envelope_reads_tool_stats(self):
        from long_exposure.orchestrator import _extract_gemini_envelope
        stdout = json.dumps({
            "response": "ok",
            "stats": {"models": {"m": {"tokens": {"input": 1, "candidates": 2}}},
                      "tools": {"totalCalls": 6, "totalSuccess": 5}},
        })
        self.assertEqual(_extract_gemini_envelope(stdout, 1)["tool_calls"], 6)
        no_tools = json.dumps({"response": "ok", "stats": {"models": {}}})
        self.assertIsNone(_extract_gemini_envelope(no_tools, 1)["tool_calls"])

    def test_claude_transcript_counts_current_turn_tool_uses_only(self):
        from long_exposure.conductor import _session_turn_tool_calls
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = root / "projects" / "p"
            proj.mkdir(parents=True)
            sid = "abc-123"
            lines = [
                {"type": "user", "message": {"content": "turn 1"}},
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "a"}, {"type": "text", "text": "x"}]}},
                # tool_result echo must not reset the turn
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a"}]}},
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "b"}]}},
                {"type": "user", "message": {"content": "turn 2"}},
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "c"}, {"type": "tool_use", "id": "d"}]}},
                # sidechain (subagent) activity is not the agent's own
                {"type": "assistant", "isSidechain": True,
                 "message": {"content": [{"type": "tool_use", "id": "e"}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "c"}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "final"}]}},
            ]
            (proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n")
            with patch("long_exposure.exploration._claude_config_dir", lambda: root):
                self.assertEqual(_session_turn_tool_calls(sid), 2)
                self.assertIsNone(_session_turn_tool_calls("missing"))
                self.assertIsNone(_session_turn_tool_calls(None))

    def test_clone_seed_state_starts_with_empty_usage(self):
        from long_exposure.fanout import _seed_clone_state
        # Put spend in the root process ledger, then seed a clone.
        exploration._usage.load({})
        exploration._usage.record(
            "worker", {"status": "ok", "usage": {"output_tokens": 5}, "cost_usd": 9.0},
            provider="claude", model="opus", config={},
        )
        try:
            with tempfile.TemporaryDirectory() as td:
                clone_dir = Path(td) / "clone-0"
                clone_dir.mkdir()
                _seed_clone_state(clone_dir, {"directive": "x"}, {}, {}, parent_run_id="r")
                seeded = json.loads((clone_dir / "exploration_state.json").read_text())
            self.assertEqual(seeded["usage_totals"], {})
            # The root ledger itself is untouched by seeding.
            self.assertEqual(exploration._usage.rows()["worker"]["cost_usd"], 9.0)
        finally:
            exploration._usage.load({})

    def test_save_state_default_uses_process_ledger(self):
        from long_exposure.exploration import save_state
        exploration._usage.load({})
        exploration._usage.record(
            "auditor", {"status": "ok", "usage": {"output_tokens": 1}, "cost_usd": 2.5},
            provider="claude", model="opus", config={},
        )
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "state.json"
                save_state(p, 3, {}, {})
                self.assertEqual(json.loads(p.read_text())["usage_totals"]["auditor"]["cost_usd"], 2.5)
                save_state(p, 3, {}, {}, usage_totals={"x": {"calls": 1}})
                self.assertEqual(json.loads(p.read_text())["usage_totals"], {"x": {"calls": 1}})
        finally:
            exploration._usage.load({})


def _reset_signal_globals():
    # The stop/clear flags are module globals meant for one run per process;
    # tests that exercise the clear path must reset them for later tests.
    exploration._stop_requested = False
    exploration._clear_requested = False
    if hasattr(exploration, "_graceful_stop_requested"):
        exploration._graceful_stop_requested = False


class LedgerRobustnessTests(unittest.TestCase):
    """Review findings: clear resets the ledger, per-agent provider
    attribution, failed calls keep their usage, estimated usage is unpriced."""

    def setUp(self):
        _reset_signal_globals()

    def tearDown(self):
        telemetry.configure({"telemetry": {"enabled": False}}, None, None)
        exploration._usage.load({})
        _reset_signal_globals()

    def test_clear_resets_ledger_and_next_run_starts_at_zero(self):
        seen = []
        with tempfile.TemporaryDirectory() as td:
            score, config, inst = _write_files(Path(td), loop_extra="  max_cost_usd: 20\n")
            fake = _fake_agent_factory(seen, cost_usd=6.0)

            def clearing_agent(agent_name, agent_def, **kwargs):
                result = fake(agent_name, agent_def, **kwargs)
                if agent_name == "auditor":
                    # Operator clears mid-run; honoured at the cycle boundary.
                    (inst / "long-exposure.clear").touch()
                return result

            with patch("long_exposure.exploration._call_exploration_agent", clearing_agent):
                _run(score, config, inst)
            cleared = json.loads((inst / "exploration_state.json").read_text())
            self.assertEqual(cleared["cycle"], 0)
            self.assertEqual(cleared["usage_totals"], {})
            self.assertEqual(exploration._usage.totals()["calls"], 0)

            # Next run resumes from the cleared state: $18 of prior spend must
            # not count toward the $20 cap, so it completes its full cycle.
            seen.clear()
            _reset_signal_globals()
            with patch("long_exposure.exploration._call_exploration_agent", fake):
                _run(score, config, inst)
            state = json.loads((inst / "exploration_state.json").read_text())
        self.assertEqual(state["cycle"], 1)
        self.assertEqual(state["usage_totals"]["worker"]["cost_usd"], 6.0)
        self.assertEqual([s["agent"] for s in seen], ["researcher", "worker", "auditor"])

    def test_per_agent_pinned_provider_is_used_for_pricing(self):
        seen = []
        with tempfile.TemporaryDirectory() as td:
            score, config, inst = _write_files(Path(td))
            config.write_text(
                config.read_text()
                + "agent_models:\n"
                + "  worker: {provider: codex, model: gpt-5.5}\n"
                + "pricing:\n"
                + "  codex:\n"
                + "    gpt-5.5: {input: 1000000, output: 0}\n"
            )
            with patch("long_exposure.exploration._call_exploration_agent", _fake_agent_factory(seen)):
                _run(score, config, inst)
            totals = json.loads((inst / "exploration_state.json").read_text())["usage_totals"]
        # worker: 100 input tokens at $1M per 1M tokens = $100, estimated
        # from the codex table even though the run's global provider is local.
        self.assertEqual(totals["worker"]["cost_estimated_usd"], 100.0)
        self.assertEqual(totals["worker"]["cost_sources"], ["estimated"])
        self.assertEqual(totals["researcher"]["cost_estimated_usd"], 0.0)
        self.assertEqual(totals["researcher"]["cost_sources"], [])

    def test_cli_error_keeps_failed_turn_usage(self):
        from long_exposure.orchestrator import ClaudeCliError
        from long_exposure.exploration import _error_result_from_cli_error
        agent_def = {"outputs": ["work_output"]}
        bare = _error_result_from_cli_error("worker", agent_def, ClaudeCliError("boom"))
        self.assertEqual(bare["status"], "error")
        self.assertEqual(bare["usage"], {})
        self.assertNotIn("cost_usd", bare)
        exc = ClaudeCliError(
            "Claude CLI API error: overloaded",
            envelope={"usage": {"input_tokens": 50, "output_tokens": 7},
                      "duration_ms": 1234, "total_cost_usd": 0.75, "num_turns": 9},
        )
        rich = _error_result_from_cli_error("worker", agent_def, exc)
        self.assertEqual(rich["status"], "error")
        self.assertEqual(rich["usage"]["output_tokens"], 7)
        self.assertEqual(rich["cost_usd"], 0.75)
        self.assertEqual(rich["num_turns"], 9)
        self.assertEqual(rich["duration_ms"], 1234)
        # And the ledger counts it as a call, not an ok call.
        exploration._usage.load({})
        exploration._record_usage("worker", rich, config={}, model="opus", provider="claude")
        row = exploration._usage.rows()["worker"]
        self.assertEqual((row["calls"], row["ok_calls"], row["cost_usd"]), (1, 0, 0.75))
        # ClaudeCliError still works with the bare message form everywhere.
        self.assertIsNone(ClaudeCliError("x").envelope)
        self.assertIsNone(ClaudeCliError("x", envelope="not a dict").envelope)

    def test_estimated_usage_is_recorded_but_not_priced(self):
        exploration._usage.load({})
        pricing = {"pricing": {"claude": {"opus": {"input": 5, "output": 25}}}}
        result = {"status": "ok", "usage": {"output_tokens": 4000}, "usage_estimated": True}
        exploration._record_usage("worker", result, config=pricing, model="opus", provider="claude")
        row = exploration._usage.rows()["worker"]
        self.assertEqual(row["output_tokens"], 4000)
        self.assertEqual(row["cost_estimated_usd"], 0.0)
        self.assertEqual(row["cost_sources"], [])
        self.assertEqual(result["cost_source"], "unavailable")
        # The same usage without the flag is priced.
        exploration._record_usage("worker", {"status": "ok", "usage": {"output_tokens": 4000}},
                                  config=pricing, model="opus", provider="claude")
        self.assertEqual(exploration._usage.rows()["worker"]["cost_estimated_usd"], 0.1)


class TelemetryCostRollupTests(unittest.TestCase):
    def tearDown(self):
        telemetry.configure({"telemetry": {"enabled": False}}, None, None)

    def test_summarize_totals_cost_from_usage_recorded_events(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            telemetry.configure({"telemetry": {"enabled": True}}, root, "run-x")
            telemetry.emit("usage_recorded", phase="usage", agent="worker", status="ok",
                           data={"cost_usd": 1.5, "cost_estimated_usd": 0.0, "tool_calls": 3, "turns": 2})
            telemetry.emit("usage_recorded", phase="usage", agent="worker#compaction", status="ok",
                           data={"cost_usd": 0.25, "cost_estimated_usd": 0.5, "tool_calls": 0, "turns": 1})
            # agent_call_end is informational; it must not be double-counted.
            telemetry.emit("agent_call_end", phase="agent", agent="worker", status="ok",
                           data={"cost_usd": 99.0, "tool_calls": 99, "num_turns": 99})
            summary = telemetry.summarize(root)
        self.assertEqual(summary["cost"]["cost_usd"], 1.75)
        self.assertEqual(summary["cost"]["cost_estimated_usd"], 0.5)
        self.assertEqual(summary["cost"]["tool_calls"], 3)
        self.assertEqual(summary["cost"]["num_turns"], 3)


class FinalStageCountTests(unittest.TestCase):
    """One stage per ~100k input tokens; the auditor floors at 1 and caps at
    5 (4..12 stages), the reporter floors at 1 and stays uncapped."""

    def test_auditor_stage_count_is_floored_and_capped(self):
        from long_exposure.auditing import _final_auditor_stage_count, _N_MAX
        from long_exposure.limits import FINAL_STAGE_TOKEN_THRESHOLD
        self.assertEqual((FINAL_STAGE_TOKEN_THRESHOLD, _N_MAX), (100_000, 5))
        # Floor: a small workspace still gets one verify and one test pass,
        # never zero (explore + document alone would skip verification).
        self.assertEqual(_final_auditor_stage_count(0), (1, 4))
        self.assertEqual(_final_auditor_stage_count(99_999), (1, 4))
        self.assertEqual(_final_auditor_stage_count(120_000), (1, 4))
        self.assertEqual(_final_auditor_stage_count(300_000), (3, 8))
        self.assertEqual(_final_auditor_stage_count(500_000), (5, 12))
        # Cap.
        self.assertEqual(_final_auditor_stage_count(1_000_000), (5, 12))
        self.assertEqual(_final_auditor_stage_count(10**9), (5, 12))

    def test_reporter_stage_count_is_floored_and_uncapped(self):
        """Exercises reporting.py itself — re-implementing the formula in
        the test made this pass no matter what the reporter did, and the
        auditor-caps/reporter-does-not asymmetry is the whole point."""
        from long_exposure.reporting import _final_report_stage_count as body

        self.assertEqual(body(0), (1, 3))
        self.assertEqual(body(99_999), (1, 3))
        self.assertEqual(body(100_000), (1, 3))
        self.assertEqual(body(1_000_000), (10, 12))
        # Uncapped, unlike the auditor's _N_MAX=5.
        self.assertEqual(body(10_000_000), (100, 102))


class HealthEventsRoutingTests(unittest.TestCase):
    """Root runs must write health_events.jsonl; only clones used to."""

    def tearDown(self):
        from long_exposure import health_events
        health_events.configure(None)

    def test_configure_directs_events_without_the_clone_env_var(self):
        from long_exposure import health_events
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            health_events.configure(None)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AGENT_INSTANCE_DIR", None)
                # Unconfigured and no env var: nothing is written (old
                # behaviour for a bare tool invocation).
                health_events.append_event("pdf_render_failed", detail="x")
                self.assertFalse((root / "health_events.jsonl").exists())
                # Configured: events land next to the state file.
                health_events.configure(root)
                health_events.append_event("pdf_render_failed", detail="y", cycle=3)
                lines = (root / "health_events.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["kind"], "pdf_render_failed")
        self.assertEqual(record["cycle"], 3)

    def test_explicit_data_dir_wins_over_configured_and_env(self):
        from long_exposure import health_events
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "cfg").mkdir()
            (root / "arg").mkdir()
            (root / "env").mkdir()
            health_events.configure(root / "cfg")
            with patch.dict(os.environ, {"AGENT_INSTANCE_DIR": str(root / "env")}):
                health_events.append_event("k", data_dir=root / "arg")
                health_events.append_event("k")
            self.assertTrue((root / "arg" / "health_events.jsonl").exists())
            self.assertTrue((root / "cfg" / "health_events.jsonl").exists())
            self.assertFalse((root / "env" / "health_events.jsonl").exists())

    def test_clone_env_var_still_works_when_unconfigured(self):
        from long_exposure import health_events
        health_events.configure(None)
        with tempfile.TemporaryDirectory() as td:
            clone = Path(td)
            with patch.dict(os.environ, {"AGENT_INSTANCE_DIR": str(clone)}):
                health_events.append_event("clone_event")
            self.assertTrue((clone / "health_events.jsonl").exists())

    def test_run_exploration_configures_health_events(self):
        from long_exposure import health_events
        seen = []
        with tempfile.TemporaryDirectory() as td:
            score, config, inst = _write_files(Path(td))
            health_events.configure(None)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AGENT_INSTANCE_DIR", None)
                with patch("long_exposure.exploration._call_exploration_agent",
                           _fake_agent_factory(seen)):
                    _run(score, config, inst)
                health_events.append_event("post_run_probe")
            self.assertTrue((inst / "health_events.jsonl").exists())


class McpAdvertisingTests(unittest.TestCase):
    """Only advertise the session-search tools to turns that get them.

    The [AVAILABLE TOOLS] layer is Claude-only, so these tests pin the
    process-global provider (other tests leave it on `local`).
    """

    def setUp(self):
        from long_exposure import provider as _prov
        self._prev_provider = _prov.current_provider()
        _prov.configure_provider({"llm_provider": "claude"})

    def tearDown(self):
        from long_exposure import provider as _prov
        _prov.configure_provider({"llm_provider": self._prev_provider})

    def _prompt(self, **kw):
        from long_exposure.orchestrator import assemble_system_prompt, load_config
        cfg = load_config()
        cfg["working_directory"] = "/ws"
        cfg["compact_db"] = "/tmp/sessions.db"
        return assemble_system_prompt(cfg, role="r", **kw)

    def test_gate(self):
        on = self._prompt(mcp_enabled=True)
        self.assertIn("[AVAILABLE TOOLS]", on)
        self.assertIn("search_sessions(", on)
        off = self._prompt(mcp_enabled=False)
        self.assertNotIn("[AVAILABLE TOOLS]", off)
        self.assertNotIn("search_sessions(", off)
        # None preserves the legacy behaviour for untouched callers.
        self.assertIn("[AVAILABLE TOOLS]", self._prompt())

    def test_cycle_agent_prompt_gates_on_the_mcp_flag(self):
        """Drives the real _call_exploration_agent with the CLI patched, so
        this covers the harness's own mcp_active computation."""
        from long_exposure.exploration import _call_exploration_agent

        captured = {}

        def fake_invoke(cmd, stdin_text, **kwargs):
            # Fresh sessions pass the system prompt as a --system-prompt arg.
            sp = cmd[cmd.index("--system-prompt") + 1] if "--system-prompt" in cmd else ""
            captured[kwargs.get("_name", "last")] = {"prompt": sp, "cmd": list(cmd)}
            return {"result": "[OUTPUT: out]\nbody\n[END OUTPUT: out]",
                    "usage": {"output_tokens": 5}, "duration_ms": 1, "session_id": None}

        with tempfile.TemporaryDirectory() as td:
            inst = Path(td)
            base = {
                "llm_provider": "claude", "model": "opus",
                "context_window": 200000, "compact_threshold": 0.9,
                "compact_db": str(inst / "sessions.db"),
                "instance_dir": str(inst),
                "working_directory": str(inst),
                "philosophy": "efficient", "framework": "staged",
                "checkpoint_format": "standard", "require_checkpoint_first": False,
                "user_gate_approval": False, "anti_patterns_enabled": True,
                "allowed_tools": ["Read"], "wolfram_path": "",
                "model_tier": "opus", "max_summary_pct": 0.15,
                "depth_compression": "gentle",
            }
            results = {}
            for name, mcp in (("with_mcp", True), ("without_mcp", False)):
                agent_def = {
                    "role": "r", "inputs": [], "outputs": ["out"], "mcp": mcp,
                }
                with patch("long_exposure.exploration._invoke_claude",
                           lambda cmd, stdin_text, **kw: fake_invoke(
                               cmd, stdin_text, _name=name, **kw)):
                    _call_exploration_agent(
                        agent_name=name, agent_def=agent_def, task="t",
                        config=dict(base), results={}, score_inputs={},
                        agent_sessions={}, agent_summaries={},
                    )
                results[name] = captured[name]

        self.assertIn("[AVAILABLE TOOLS]", results["with_mcp"]["prompt"])
        self.assertIn("--mcp-config", results["with_mcp"]["cmd"])
        self.assertNotIn("[AVAILABLE TOOLS]", results["without_mcp"]["prompt"])
        self.assertNotIn("--mcp-config", results["without_mcp"]["cmd"])


class StageIoSharedHelperTests(unittest.TestCase):
    """reporting.py and auditing.py must use one implementation."""

    def test_aliases_point_at_the_shared_module(self):
        """Each module's private alias must BE the shared function, never a
        re-implementation. Only aliases with live call sites are kept, so
        this also fails if someone reintroduces a local copy."""
        from long_exposure import auditing, reporting, stage_io
        shared_by_name = {
            "_file_signature": stage_io.file_signature,
            "_atomic_write_text": stage_io.atomic_write_text,
            "_committed_baseline": stage_io.committed_baseline,
            "_write_run_mode": stage_io.write_run_mode,
        }
        for mod, names in (
            (reporting, ("_file_signature", "_atomic_write_text",
                         "_committed_baseline", "_write_run_mode")),
            (auditing, ("_file_signature", "_committed_baseline",
                        "_write_run_mode")),
        ):
            for name in names:
                self.assertIs(getattr(mod, name), shared_by_name[name],
                              f"{mod.__name__}.{name}")
        # Neither module may define its own copy of a stage_io primitive.
        for mod in (reporting, auditing):
            for name in ("file_signature", "atomic_write_text",
                         "marker_metadata", "committed_baseline"):
                local = getattr(mod, name, None)
                if local is not None:
                    self.assertIs(local, getattr(stage_io, name),
                                  f"{mod.__name__}.{name}")

    def test_atomic_write_is_unique_per_process_and_cleans_up(self):
        from long_exposure import stage_io
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "reports" / "final" / "final_report.md"
            stage_io.atomic_write_text(target, "body")
            self.assertEqual(target.read_text(), "body")
            stage_io.atomic_write_text(target, "replaced")
            self.assertEqual(target.read_text(), "replaced")
            self.assertEqual(list(Path(td).rglob("*.tmp")), [])

    def test_crashed_write_leaves_a_temp_the_curator_excludes(self):
        """The temp name must END in .tmp: the curator's hard-exclude keys
        on `Path(name).suffix`, so the older `.<name>.tmp.<pid>.<ms>` shape
        would have shipped a half-written artifact inside the package. An
        assertion that no .tmp survives is satisfied by ANY naming scheme,
        so pin the name itself."""
        from long_exposure import stage_io
        from long_exposure.curator import _is_package_hard_excluded
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "reports" / "final" / "final_report.md"
            # Simulate a crash between write and rename: the temp survives.
            with patch("long_exposure.stage_io.os.replace",
                       side_effect=OSError("crash")):
                with self.assertRaises(OSError):
                    stage_io.atomic_write_text(target, "half-written")
            # The failure path removes its own temp...
            self.assertEqual(list(Path(td).rglob("*.tmp")), [])
            # ...so capture the name it would use and check the curator
            # would exclude a temp orphaned by a hard kill.
            names = []
            real_replace = os.replace

            def capture(src, dst):
                names.append(Path(src).name)
                return real_replace(src, dst)

            with patch("long_exposure.stage_io.os.replace", side_effect=capture):
                stage_io.atomic_write_text(target, "body")
            self.assertEqual(len(names), 1)
            self.assertTrue(names[0].startswith(".final_report.md."), names[0])
            self.assertEqual(Path(names[0]).suffix, ".tmp", names[0])
            self.assertTrue(
                _is_package_hard_excluded(f"reports/final/{names[0]}"), names[0]
            )

    def test_concurrent_atomic_writes_never_publish_a_partial_file(self):
        """A shared temp name let one writer's os.replace publish another
        writer's half-flushed bytes. Every observed value must be whole."""
        import threading as _threading
        from long_exposure import stage_io
        bodies = [f"payload-{i}-" + str(i) * 20000 for i in range(8)]
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "out" / "usage_summary.json"
            seen, errors = [], []

            def writer(body):
                try:
                    for _ in range(12):
                        stage_io.atomic_write_text(target, body)
                        seen.append(target.read_text())
                except Exception as exc:  # pragma: no cover - failure path
                    errors.append(exc)

            threads = [_threading.Thread(target=writer, args=(b,)) for b in bodies]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertTrue(seen)
            for text in seen:
                self.assertIn(text, bodies)
            self.assertEqual(list(Path(td).rglob("*.tmp")), [])

    def test_commit_marker_and_baseline_round_trip(self):
        from long_exposure import stage_io
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            report, marker = root / "r.md", root / "r.committed"
            self.assertEqual(stage_io.committed_baseline(report, marker), (False, "none", None))
            report.write_text("x" * 2000)
            delta, source, _ = stage_io.committed_baseline(report, marker)
            self.assertEqual((delta, source), (True, "legacy_size"))
            stage_io.write_commit_marker(marker, run_id="r1", mode="fresh", token_count=42)
            delta, source, boundary = stage_io.committed_baseline(report, marker)
            self.assertEqual((delta, source), (True, "marker"))
            self.assertIsNotNone(boundary)
            self.assertEqual(stage_io.marker_metadata(marker)["input_tokens"], 42)
            # Unparsable marker still counts as a baseline ({} not None).
            marker.write_text("not json")
            self.assertEqual(stage_io.marker_metadata(marker), {})
            self.assertEqual(stage_io.committed_baseline(report, marker)[1], "marker")

    def test_delta_token_estimate_counts_only_newer_files(self):
        import os as _os
        from long_exposure import stage_io
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old, new = root / "old.md", root / "new.md"
            old.write_text("a" * 400)
            new.write_text("b" * 800)
            _os.utime(old, (1_000_000, 1_000_000))
            _os.utime(new, (2_000_000, 2_000_000))
            self.assertEqual(stage_io.estimate_delta_tokens([old, new], 1_500_000), 200)
            self.assertEqual(stage_io.estimate_delta_tokens([old, new], None), 0)
            self.assertEqual(stage_io.estimate_delta_tokens([root / "gone.md"], 1), 0)
