import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from long_exposure import telemetry
import long_exposure.exploration as exploration
from long_exposure.exploration import (
    _clear_stop_flag_for_final_synthesis,
    _should_run_final_synthesis,
    run_exploration,
)


def write_minimal_files(root: Path):
    workspace = root / "workspace"
    instance = root / "instance"
    workspace.mkdir()
    instance.mkdir()
    score = root / "score.yaml"
    score.write_text(
        "task: test directive\n"
        "loop:\n"
        "  max_cycles: 1\n"
        "  cycle_cooldown_seconds: 0\n"
        "  report_interval: 100\n"
        "agents:\n"
        "  researcher:\n"
        "    inputs: [directive, audit_report]\n"
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
        "flow: [researcher, worker, auditor]\n"
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


class CycleLoopIntegrationTests(unittest.TestCase):
    def tearDown(self):
        telemetry.configure({"telemetry": {"enabled": False}}, None, None)

    def test_one_cycle_writes_state_and_outputs_without_live_provider(self):
        calls = []

        def fake_agent(agent_name, agent_def, **kwargs):
            calls.append(agent_name)
            output_name = agent_def["outputs"][0]
            return {
                "agent": agent_name,
                "outputs": {output_name: f"{agent_name} output " + ("x" * 2100)},
                "usage": {"output_tokens": 2100},
                "duration_ms": 1,
                "status": "ok",
                "error": None,
            }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            score, config, inst = write_minimal_files(root)
            with patch("long_exposure.exploration._call_exploration_agent", fake_agent):
                run_exploration(
                    score_path=str(score),
                    config_path=str(config),
                    output_dir=inst / "output",
                    state_path=inst / "exploration_state.json",
                    task_override=None,
                    instance_dir=inst,
                )

            state = json.loads((inst / "exploration_state.json").read_text())
            status = (inst / "output" / "exploration_status.md").read_text()
            telemetry_events = [
                json.loads(line)
                for line in (inst / "telemetry" / "events.jsonl").read_text().splitlines()
            ]

        self.assertEqual(calls, ["researcher", "worker", "auditor"])
        self.assertEqual(state["cycle"], 1)
        self.assertIn("research_brief", state["results"])
        self.assertIn("work_output", state["results"])
        self.assertIn("audit_report", state["results"])
        self.assertIn("Status:** completed", status)
        event_types = {event["event_type"] for event in telemetry_events}
        self.assertIn("run_start", event_types)
        self.assertIn("cycle_start", event_types)
        self.assertIn("agent_call_end", event_types)
        self.assertIn("cycle_end", event_types)
        self.assertIn("run_end", event_types)

    def test_reanchor_is_injected_before_compaction_threshold(self):
        live_guidance_by_call = []

        def fake_agent(agent_name, agent_def, **kwargs):
            live_guidance_by_call.append(
                (agent_name, kwargs["results"].get("live_guidance", ""))
            )
            output_name = agent_def["outputs"][0]
            # Context must land in the reanchor window: at or above
            # reanchor_at (5/6 of the compact threshold, 24 576 for a 32 768
            # window at 0.9) but BELOW compact_at (29 491). Crossing compact_at
            # correctly compacts the session and resets both the context
            # counter and the reanchor-emitted flag, so reanchor would
            # (correctly) never fire on the next cycle.
            return {
                "agent": agent_name,
                "outputs": {output_name: f"{agent_name} output " + ("x" * 2100)},
                "usage": {"input_tokens": 25000, "output_tokens": 2100},
                "duration_ms": 1,
                "status": "ok",
                "error": None,
            }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            score, config, inst = write_minimal_files(root)
            score.write_text(score.read_text().replace("  max_cycles: 1\n", "  max_cycles: 2\n"))
            with patch("long_exposure.exploration._call_exploration_agent", fake_agent):
                run_exploration(
                    score_path=str(score),
                    config_path=str(config),
                    output_dir=inst / "output",
                    state_path=inst / "exploration_state.json",
                    task_override=None,
                    instance_dir=inst,
                )

            state = json.loads((inst / "exploration_state.json").read_text())

        researcher_guidance = [
            guidance for agent_name, guidance in live_guidance_by_call
            if agent_name == "researcher"
        ]
        self.assertEqual(len(researcher_guidance), 2)
        self.assertNotIn("<reanchor>", researcher_guidance[0])
        self.assertIn("<reanchor>", researcher_guidance[1])
        self.assertTrue(state["_reanchor_emitted"]["researcher"])
        self.assertEqual(state["agent_context_tokens"]["researcher"], 27100)

    def test_final_synthesis_runs_on_stop_or_topic_exhaustion_but_not_clear(self):
        self.assertTrue(_should_run_final_synthesis(
            topic_exhausted=False,
            stop_requested=True,
            clear_requested=False,
        ))
        self.assertTrue(_should_run_final_synthesis(
            topic_exhausted=True,
            stop_requested=False,
            clear_requested=False,
        ))
        self.assertTrue(_should_run_final_synthesis(
            topic_exhausted=False,
            max_cycles_reached=True,
            stop_requested=False,
            clear_requested=False,
        ))
        self.assertFalse(_should_run_final_synthesis(
            topic_exhausted=True,
            max_cycles_reached=True,
            stop_requested=True,
            clear_requested=True,
        ))

    def test_stop_flag_is_cleared_only_for_final_synthesis(self):
        original = exploration._stop_requested
        try:
            exploration._stop_requested = True
            changed = _clear_stop_flag_for_final_synthesis(
                should_run_final=True,
                stop_requested=True,
                clear_requested=False,
            )
            self.assertTrue(changed)
            self.assertFalse(exploration._stop_requested)

            exploration._stop_requested = True
            changed = _clear_stop_flag_for_final_synthesis(
                should_run_final=True,
                stop_requested=True,
                clear_requested=True,
            )
            self.assertFalse(changed)
            self.assertTrue(exploration._stop_requested)
        finally:
            exploration._stop_requested = original

    def test_final_stage_exceptions_are_isolated_and_recorded(self):
        calls = []

        def fake_agent(agent_name, agent_def, **kwargs):
            calls.append(agent_name)
            output_name = agent_def["outputs"][0]
            return {
                "agent": agent_name,
                "outputs": {output_name: f"{agent_name} output " + ("x" * 2100)},
                "usage": {"output_tokens": 2100},
                "duration_ms": 1,
                "status": "ok",
                "error": None,
            }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            score, config, inst = write_minimal_files(root)
            score_text = score.read_text()
            score.write_text(
                score_text.replace(
                    "flow: [researcher, worker, auditor]\n",
                    "  final_auditor:\n"
                    "    inputs: [directive]\n"
                    "    outputs: [final_audit_report]\n"
                    "    role: final auditor\n"
                    "  final_reporter:\n"
                    "    inputs: [directive]\n"
                    "    outputs: [final_report]\n"
                    "    role: final reporter\n"
                    "  curator:\n"
                    "    inputs: [directive]\n"
                    "    outputs: [curation]\n"
                    "    role: curator\n"
                    "flow: [researcher, worker, auditor]\n",
                )
            )

            with (
                patch("long_exposure.exploration._call_exploration_agent", fake_agent),
                patch(
                    "long_exposure.auditing._run_final_auditor",
                    side_effect=RuntimeError("audit boom"),
                ),
                patch(
                    "long_exposure.exploration._run_final_reporter",
                    side_effect=RuntimeError("report boom"),
                ),
                patch(
                    "long_exposure.exploration._run_curator",
                    return_value="curator-session",
                ) as curator,
            ):
                run_exploration(
                    score_path=str(score),
                    config_path=str(config),
                    output_dir=inst / "output",
                    state_path=inst / "exploration_state.json",
                    task_override=None,
                    instance_dir=inst,
                )

            state = json.loads((inst / "exploration_state.json").read_text())
            status = (inst / "output" / "exploration_status.md").read_text()

        self.assertEqual(calls, ["researcher", "worker", "auditor"])
        curator.assert_called_once()
        self.assertEqual(state["failures"]["final_auditor"], 1)
        self.assertEqual(state["failures"]["final_reporter"], 1)
        self.assertIn("Status:** completed", status)


class CompactionBookkeepingTests(unittest.TestCase):
    """Reanchor/context bookkeeping must be popped only when compaction
    actually rotated the agent's session — the DB-failure path keeps the old
    near-full session, where the reanchor must stay armed."""

    def tearDown(self):
        telemetry.configure({"telemetry": {"enabled": False}}, None, None)

    def _run_with_fake_compaction(self, fake_compact):
        def fake_agent(agent_name, agent_def, **kwargs):
            output_name = agent_def["outputs"][0]
            # 28000 + 2100 = 30100 >= compact_at (29491 for a 32768 window
            # at 0.9), so compaction triggers after the call.
            return {
                "agent": agent_name,
                "outputs": {output_name: f"{agent_name} output " + ("x" * 2100)},
                "usage": {"input_tokens": 28000, "output_tokens": 2100},
                "duration_ms": 1,
                "status": "ok",
                "error": None,
            }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspace"
            instance = root / "instance"
            workspace.mkdir()
            instance.mkdir()
            score = root / "score.yaml"
            score.write_text(
                "task: test directive\n"
                "loop:\n"
                "  max_cycles: 1\n"
                "  cycle_cooldown_seconds: 0\n"
                "agents:\n"
                "  researcher:\n"
                "    inputs: [directive, audit_report]\n"
                "    outputs: [research_brief]\n"
                "    role: researcher\n"
                "flow: [researcher]\n"
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
            )
            state_path = instance / "exploration_state.json"
            state_path.write_text(json.dumps({
                "cycle": 0,
                "results": {"directive": "test directive", "audit_report": "prior"},
                "failures": {"researcher": 0},
                "agent_sessions": {"researcher": "sess-1"},
                "agent_sessions_provider": "local",
                "agent_summaries": {},
                "task": "test directive",
                "run_id": "run-test",
                "last_daily_sync_at": datetime.now(timezone.utc).isoformat(),
                "_reanchor_emitted": {"researcher": True},
                "agent_context_tokens": {"researcher": 30000},
            }))
            with (
                # Pin the provider env so the resume-time provider-mismatch
                # guard does not clear the seeded local agent_sessions when
                # an earlier test left a different provider configured.
                patch.dict(os.environ, {"LONG_EXPOSURE_LLM_PROVIDER": "local"}),
                patch("long_exposure.exploration._call_exploration_agent", fake_agent),
                patch("long_exposure.exploration._compact_agent_session", fake_compact),
            ):
                run_exploration(
                    score_path=str(score),
                    config_path=str(config),
                    output_dir=instance / "output",
                    state_path=state_path,
                    instance_dir=instance,
                )
            return json.loads(state_path.read_text())

    def test_keep_path_preserves_bookkeeping(self):
        def fake_compact_keep(agent_name, agent_def, config, agent_sessions,
                              agent_summaries, conn, cycle, last_session_id):
            # DB-write-failure path: session kept, nothing rotated.
            return last_session_id

        state = self._run_with_fake_compaction(fake_compact_keep)
        self.assertEqual(state["agent_sessions"], {"researcher": "sess-1"})
        self.assertTrue(state["_reanchor_emitted"].get("researcher"))
        self.assertEqual(state["agent_context_tokens"]["researcher"], 30100)

    def test_rotation_path_pops_bookkeeping(self):
        def fake_compact_rotate(agent_name, agent_def, config, agent_sessions,
                                agent_summaries, conn, cycle, last_session_id):
            del agent_sessions[agent_name]
            agent_summaries[agent_name] = "summary"
            return "compaction-row"

        state = self._run_with_fake_compaction(fake_compact_rotate)
        self.assertEqual(state["agent_sessions"], {})
        self.assertEqual(state["_reanchor_emitted"], {})
        self.assertEqual(state["agent_context_tokens"], {})


if __name__ == "__main__":
    unittest.main()
