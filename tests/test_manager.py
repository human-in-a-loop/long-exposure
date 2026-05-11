import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from long_exposure import cli
from long_exposure.exploration import DEFAULT_SCORE_PATH, load_exploration_score
from long_exposure.manager import (
    VERDICT_ACT,
    VERDICT_HEALTHY,
    build_manager_snapshot,
    decide_from_snapshot,
    run_manager_poll,
)
from long_exposure.curator import _is_package_hard_excluded
from long_exposure.tools import promise_check


def _event(cycle, milestone_id, status="in-progress", narrative="progress"):
    return {
        "event_id": f"00000000-0000-4000-8000-{cycle:012d}",
        "ts": f"2026-05-10T00:0{cycle}:00+00:00",
        "run_id": "run-test",
        "cycle": cycle,
        "agent": "researcher",
        "milestone_id": milestone_id,
        "status": status,
        "confidence": {
            "level": "medium",
            "rationale": "test",
            "assessor": "researcher",
        },
        "narrative": narrative,
    }


class ManagerTests(unittest.TestCase):
    def test_default_score_validates_manager_and_final_runtime_inputs(self):
        score = load_exploration_score(DEFAULT_SCORE_PATH)
        self.assertIn("manager", score["agents"])

    def test_promise_check_accepts_manager_action_required(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "promise_ledger.jsonl").write_text(json.dumps({
                "event_id": "00000000-0000-4000-8000-000000000001",
                "ts": "2026-05-10T00:00:00+00:00",
                "run_id": "run-test",
                "cycle": 3,
                "agent": "manager",
                "milestone_id": "_manager/mechanism-overdue",
                "status": "action_required",
                "confidence": {
                    "level": "medium",
                    "rationale": "three active cycles",
                    "assessor": "manager",
                },
                "narrative": "next cycle requires mechanism",
            }) + "\n")
            findings = promise_check.run(ws)
        self.assertFalse(findings.errors)

    def test_snapshot_detects_mechanism_overdue(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "workspace"
            data = root / "data"
            ws.mkdir()
            data.mkdir()
            ledger = "\n".join(
                json.dumps(_event(cycle, "F-1"))
                for cycle in (1, 2, 3)
            ) + "\n"
            (ws / "promise_ledger.jsonl").write_text(ledger)
            state = data / "exploration_state.json"
            state.write_text(json.dumps({
                "cycle": 3,
                "run_id": "run-test",
                "results": {"research_brief": "## Brief"},
            }))

            snapshot = build_manager_snapshot(
                workspace=ws,
                state_path=state,
                data_dir=data,
            )
            decision = decide_from_snapshot(snapshot)

        self.assertEqual(decision.verdict, VERDICT_ACT)
        self.assertEqual(decision.event_class, "mechanism-overdue")

    def test_snapshot_tolerates_malformed_confidence_field(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "workspace"
            data = root / "data"
            ws.mkdir()
            data.mkdir()
            bad = _event(1, "F-1")
            bad["confidence"] = "high"
            (ws / "promise_ledger.jsonl").write_text(json.dumps(bad) + "\n")
            state = data / "exploration_state.json"
            state.write_text(json.dumps({"cycle": 1, "run_id": "run-test"}))

            snapshot = build_manager_snapshot(
                workspace=ws,
                state_path=state,
                data_dir=data,
            )

        self.assertEqual(snapshot["ledger"]["events"], 1)

    def test_poll_writes_log_ledger_and_guidance_without_agent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "workspace"
            inst = root / "instance"
            data = inst
            ws.mkdir()
            data.mkdir()
            (ws / "promise_ledger.jsonl").write_text(
                "".join(json.dumps(_event(cycle, "F-1")) + "\n" for cycle in (1, 2, 3))
            )
            state = inst / "exploration_state.json"
            state.write_text(json.dumps({
                "cycle": 3,
                "run_id": "run-test",
                "task": "test directive",
                "results": {"research_brief": "## Brief"},
            }))
            score = root / "score.yaml"
            score.write_text(
                "task: test directive\n"
                "agents:\n"
                "  manager:\n"
                "    inputs: [directive, manager_snapshot, promise_ledger_summary]\n"
                "    outputs: [manager_intervention]\n"
                "    role: manager\n"
                "flow: []\n"
            )
            config = root / "config.yaml"
            config.write_text(
                "llm_provider: local\n"
                "model: opus\n"
                "model_tier: opus\n"
                "local_model: test\n"
                "local_context_window: 32768\n"
                "context_window: 32768\n"
                "compact_threshold: 0.9\n"
                "compact_db: " + str(inst / "sessions.db") + "\n"
                "working_directory: " + str(ws) + "\n"
                "checkpoint_format: standard\n"
                "require_checkpoint_first: false\n"
                "user_gate_approval: false\n"
                "anti_patterns_enabled: true\n"
            )

            with patch.dict("os.environ", {"LONG_EXPOSURE_LLM_PROVIDER": "local"}, clear=False):
                rc = run_manager_poll(
                    score_path=score,
                    config_path=config,
                    state_path=state,
                    instance_dir=inst,
                    no_agent=True,
                )

            self.assertEqual(rc, 0)
            self.assertTrue((data / "long-exposure.guide").exists())
            self.assertTrue(list((data / "manager_assessments").glob("*.md")))
            notices = [
                json.loads(line)
                for line in (data / "manager_notifications.jsonl").read_text().splitlines()
            ]
            self.assertEqual(notices[-1]["verdict"], VERDICT_ACT)
            self.assertTrue(notices[-1]["guide_written"])
            events = [
                json.loads(line)
                for line in (ws / "promise_ledger.jsonl").read_text().splitlines()
            ]
            self.assertEqual(events[-1]["agent"], "manager")
            self.assertEqual(events[-1]["status"], "action_required")
            self.assertNotIn("artifacts", events[-1])
            self.assertIn("process_artifacts", events[-1])
            self.assertIn("manager_assessments", events[-1]["process_artifacts"][0])
            findings = promise_check.run(ws)
            self.assertFalse(findings.errors)
            self.assertFalse([
                warning for warning in findings.warnings
                if "manager_assessments" in warning
            ])

    def test_poll_falls_back_when_manager_agent_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "workspace"
            inst = root / "instance"
            ws.mkdir()
            inst.mkdir()
            (ws / "promise_ledger.jsonl").write_text(
                "".join(json.dumps(_event(cycle, "F-1")) + "\n" for cycle in (1, 2, 3))
            )
            state = inst / "exploration_state.json"
            state.write_text(json.dumps({
                "cycle": 3,
                "run_id": "run-test",
                "task": "test directive",
                "results": {"research_brief": "## Brief"},
            }))
            score = root / "score.yaml"
            score.write_text(
                "task: test directive\n"
                "agents:\n"
                "  manager:\n"
                "    philosophy: oversight\n"
                "    framework: oversight\n"
                "    inputs: [directive, manager_snapshot, promise_ledger_summary]\n"
                "    outputs: [manager_intervention]\n"
                "    role: manager\n"
                "flow: []\n"
            )
            config = root / "config.yaml"
            config.write_text(
                "llm_provider: local\n"
                "model: opus\n"
                "model_tier: opus\n"
                "local_model: test\n"
                "local_context_window: 32768\n"
                "context_window: 32768\n"
                "compact_threshold: 0.9\n"
                "compact_db: " + str(inst / "sessions.db") + "\n"
                "working_directory: " + str(ws) + "\n"
                "checkpoint_format: standard\n"
                "require_checkpoint_first: false\n"
                "user_gate_approval: false\n"
                "anti_patterns_enabled: true\n"
            )

            with patch("long_exposure.manager._call_manager_agent", side_effect=RuntimeError("boom")):
                rc = run_manager_poll(
                    score_path=score,
                    config_path=config,
                    state_path=state,
                    instance_dir=inst,
                )

            self.assertEqual(rc, 0)
            guidance = (inst / "long-exposure.guide").read_text()
            self.assertIn("deterministic fallback used", guidance)
            self.assertIn("mechanism-overdue", guidance)

    def test_healthy_snapshot_no_action(self):
        decision = decide_from_snapshot({
            "cycle": 1,
            "promise_check": {"errors": [], "warnings": [], "notes": []},
            "ledger": {"stale_milestones": {}, "repeated_recent_manager_action": False},
            "latest_research_brief_contract": {"present": False},
        })
        self.assertEqual(decision.verdict, VERDICT_HEALTHY)

    def test_manager_process_artifacts_are_hard_excluded_from_package(self):
        self.assertTrue(_is_package_hard_excluded("manager.lock"))
        self.assertTrue(_is_package_hard_excluded("long-exposure.pause-for-user"))
        self.assertTrue(_is_package_hard_excluded("data/manager_assessments/poll.md"))

    def test_unified_cli_manager_poll_fails_gracefully(self):
        with tempfile.TemporaryDirectory() as td:
            inst = Path(td) / "instance"
            with patch(
                "long_exposure.cli.run_manager_poll",
                side_effect=RuntimeError("boom"),
            ):
                rc = cli.main([
                    "--instance-dir", str(inst),
                    "manager", "poll", "--no-agent",
                ])

        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
