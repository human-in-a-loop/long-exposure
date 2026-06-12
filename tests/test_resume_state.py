import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import long_exposure.exploration as exploration
from long_exposure.exploration import load_state, run_exploration


def _write_score_and_config(root: Path, max_cycles: int = 3):
    workspace = root / "workspace"
    instance = root / "instance"
    workspace.mkdir(exist_ok=True)
    instance.mkdir(exist_ok=True)
    score = root / "score.yaml"
    score.write_text(
        "task: yaml task\n"
        "loop:\n"
        f"  max_cycles: {max_cycles}\n"
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
    return score, config, instance


def _fake_agent_factory(calls, output_tokens=2100):
    def fake_agent(agent_name, agent_def, **kwargs):
        calls.append(agent_name)
        output_name = agent_def["outputs"][0]
        return {
            "agent": agent_name,
            "outputs": {output_name: f"{agent_name} output " + ("x" * 2100)},
            "usage": {"output_tokens": output_tokens},
            "duration_ms": 1,
            "status": "ok",
            "error": None,
        }
    return fake_agent


class ResumeStateTests(unittest.TestCase):
    def test_provider_mismatch_clears_native_sessions_but_keeps_summaries(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspace"
            instance = root / "instance"
            workspace.mkdir()
            instance.mkdir()
            state_path = instance / "exploration_state.json"
            state_path.write_text(json.dumps({
                "cycle": 3,
                "results": {"directive": "saved task", "audit_report": "prior"},
                "failures": {"researcher": 0},
                "agent_sessions": {"researcher": "codex-thread"},
                "agent_sessions_provider": "codex",
                "agent_summaries": {"researcher": "summary survives"},
                "task": "saved task",
                "run_id": "run-test",
            }))
            score = root / "score.yaml"
            score.write_text(
                "task: yaml task\n"
                "loop:\n"
                "  max_cycles: 3\n"
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

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("LONG_EXPOSURE_LLM_PROVIDER", None)
                run_exploration(
                    score_path=str(score),
                    config_path=str(config),
                    output_dir=instance / "output",
                    state_path=state_path,
                    instance_dir=instance,
                )

            state = json.loads(state_path.read_text())

        self.assertEqual(state["agent_sessions"], {})
        self.assertEqual(state["agent_summaries"]["researcher"], "summary survives")
        self.assertEqual(state["task"], "saved task")

    def test_corrupt_state_is_archived_and_treated_as_fresh(self):
        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / "exploration_state.json"
            sp.write_text("{this is not json")
            self.assertIsNone(load_state(sp))
            # Original moved aside, named archive preserves the evidence.
            self.assertFalse(sp.exists())
            archives = list(Path(td).glob("exploration_state.json.corrupt-*"))
            self.assertEqual(len(archives), 1)
            self.assertEqual(archives[0].read_text(), "{this is not json")

    def test_exhaustion_detector_state_survives_stop_resume(self):
        # Seeded mid-run state: one low-output cycle already counted against
        # a 100k-token peak. One more low cycle after resume must close the
        # topic — previously both counters reset to 0 on resume.
        calls = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            score, config, instance = _write_score_and_config(root, max_cycles=10)
            state_path = instance / "exploration_state.json"
            state_path.write_text(json.dumps({
                "cycle": 5,
                "results": {"directive": "saved task", "audit_report": "prior"},
                "failures": {"researcher": 0},
                "agent_sessions": {},
                "agent_sessions_provider": "local",
                "agent_summaries": {},
                "task": "saved task",
                "run_id": "run-test",
                "last_daily_sync_at": datetime.now(timezone.utc).isoformat(),
                "peak_cycle_output": 100_000,
                "low_output_streak": 1,
            }))
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("LONG_EXPOSURE_LLM_PROVIDER", None)
                with patch(
                    "long_exposure.exploration._call_exploration_agent",
                    _fake_agent_factory(calls, output_tokens=100),
                ):
                    run_exploration(
                        score_path=str(score),
                        config_path=str(config),
                        output_dir=instance / "output",
                        state_path=state_path,
                        instance_dir=instance,
                    )
            state = json.loads(state_path.read_text())

        # Closure on the FIRST resumed cycle (streak 1 -> 2), not at max_cycles.
        self.assertEqual(calls, ["researcher"])
        self.assertEqual(state["cycle"], 6)
        self.assertEqual(state["low_output_streak"], 2)
        self.assertEqual(state["peak_cycle_output"], 100_000)

    def test_usage_basis_switch_resets_exhaustion_calibration(self):
        # State saved by an interactive-transport run (chars/4 usage
        # estimates) resumed on a headless provider: the persisted peak and
        # streak are measured on an incomparable basis and must reset,
        # otherwise the first resumed cycle would falsely close the topic.
        calls = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            score, config, instance = _write_score_and_config(root, max_cycles=7)
            state_path = instance / "exploration_state.json"
            state_path.write_text(json.dumps({
                "cycle": 5,
                "results": {"directive": "saved task", "audit_report": "prior"},
                "failures": {"researcher": 0},
                "agent_sessions": {},
                "agent_sessions_provider": "local",
                "agent_summaries": {},
                "task": "saved task",
                "run_id": "run-test",
                "last_daily_sync_at": datetime.now(timezone.utc).isoformat(),
                "peak_cycle_output": 100_000,
                "low_output_streak": 1,
                "usage_basis": "interactive",
            }))
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("LONG_EXPOSURE_LLM_PROVIDER", None)
                with patch(
                    "long_exposure.exploration._call_exploration_agent",
                    _fake_agent_factory(calls, output_tokens=2100),
                ):
                    run_exploration(
                        score_path=str(score),
                        config_path=str(config),
                        output_dir=instance / "output",
                        state_path=state_path,
                        instance_dir=instance,
                    )
            state = json.loads(state_path.read_text())

        # Without the reset, the stale 100k peak makes 2100-token cycles
        # "low" (threshold 5000) and the run closes after ONE resumed cycle.
        # With the reset, the detector re-learns: both remaining cycles run.
        self.assertEqual(calls, ["researcher", "researcher"])
        self.assertEqual(state["cycle"], 7)
        self.assertEqual(state["peak_cycle_output"], 2100)
        self.assertEqual(state["low_output_streak"], 0)
        # The new state is stamped with the basis it was measured on.
        self.assertEqual(state["usage_basis"], "local")

    def test_matching_usage_basis_preserves_calibration(self):
        # Same transport on both sides of the stop/resume: calibration
        # survives and one more low cycle closes the topic (mirrors
        # test_exhaustion_detector_state_survives_stop_resume, which guards
        # the legacy no-usage_basis state on headless resume).
        calls = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            score, config, instance = _write_score_and_config(root, max_cycles=10)
            state_path = instance / "exploration_state.json"
            state_path.write_text(json.dumps({
                "cycle": 5,
                "results": {"directive": "saved task", "audit_report": "prior"},
                "failures": {"researcher": 0},
                "agent_sessions": {},
                "agent_sessions_provider": "local",
                "agent_summaries": {},
                "task": "saved task",
                "run_id": "run-test",
                "last_daily_sync_at": datetime.now(timezone.utc).isoformat(),
                "peak_cycle_output": 100_000,
                "low_output_streak": 1,
                "usage_basis": "local",
            }))
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("LONG_EXPOSURE_LLM_PROVIDER", None)
                with patch(
                    "long_exposure.exploration._call_exploration_agent",
                    _fake_agent_factory(calls, output_tokens=100),
                ):
                    run_exploration(
                        score_path=str(score),
                        config_path=str(config),
                        output_dir=instance / "output",
                        state_path=state_path,
                        instance_dir=instance,
                    )
            state = json.loads(state_path.read_text())

        self.assertEqual(calls, ["researcher"])
        self.assertEqual(state["cycle"], 6)
        self.assertEqual(state["low_output_streak"], 2)
        self.assertEqual(state["peak_cycle_output"], 100_000)

    def test_stale_stop_signal_is_cleared_at_startup(self):
        calls = []
        original_stop = exploration._stop_requested
        original_clear = exploration._clear_requested
        try:
            exploration._stop_requested = False
            exploration._clear_requested = False
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                score, config, instance = _write_score_and_config(root, max_cycles=1)
                state_path = instance / "exploration_state.json"
                # Stale signals from a previous session — nothing is running.
                (instance / "long-exposure.stop").write_text("")
                (instance / "exploration.clear").write_text("")
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("LONG_EXPOSURE_LLM_PROVIDER", None)
                    with patch(
                        "long_exposure.exploration._call_exploration_agent",
                        _fake_agent_factory(calls),
                    ):
                        run_exploration(
                            score_path=str(score),
                            config_path=str(config),
                            output_dir=instance / "output",
                            state_path=state_path,
                            instance_dir=instance,
                        )
                state = json.loads(state_path.read_text())
                self.assertFalse((instance / "long-exposure.stop").exists())
                self.assertFalse((instance / "exploration.clear").exists())

            # The run executed its cycle instead of consuming the stale
            # signals into a zero-cycle final-synthesis pass.
            self.assertEqual(calls, ["researcher"])
            self.assertEqual(state["cycle"], 1)
            self.assertFalse(exploration._stop_requested)
            self.assertFalse(exploration._clear_requested)
        finally:
            exploration._stop_requested = original_stop
            exploration._clear_requested = original_clear

    def test_clone_startup_does_not_sweep_live_conductor_signal(self):
        # Clone instance dirs are freshly created per fan-out, so nothing in
        # them can be stale — but the fan-out conductor may write a LIVE
        # graceful-stop into a clone's dir while the clone is still booting.
        # The startup sweep must skip clones entirely; the loop-top
        # _check_signal_files then honors the signal (zero cycles run).
        calls = []
        original_stop = exploration._stop_requested
        original_clear = exploration._clear_requested
        original_graceful = exploration._graceful_stop_requested
        try:
            exploration._stop_requested = False
            exploration._clear_requested = False
            exploration._graceful_stop_requested = False
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                score, config, instance = _write_score_and_config(root, max_cycles=3)
                state_path = instance / "exploration_state.json"
                # Conductor-written live signal, present before the clone's
                # startup sweep line is reached.
                (instance / "long-exposure.graceful-stop").write_text("")
                with patch.dict(
                    os.environ,
                    {"AGENT_FORK_ID": "fork-test", "AGENT_FORK_CLONE_K": "0"},
                    clear=False,
                ):
                    os.environ.pop("LONG_EXPOSURE_LLM_PROVIDER", None)
                    with patch(
                        "long_exposure.exploration._call_exploration_agent",
                        _fake_agent_factory(calls),
                    ):
                        run_exploration(
                            score_path=str(score),
                            config_path=str(config),
                            output_dir=instance / "output",
                            state_path=state_path,
                            instance_dir=instance,
                        )
                # Signal was consumed by _check_signal_files, not swept as
                # stale; the run honored it and exited before any cycle.
                self.assertFalse(
                    (instance / "long-exposure.graceful-stop").exists()
                )
            self.assertEqual(calls, [])
            self.assertTrue(exploration._graceful_stop_requested)
        finally:
            exploration._stop_requested = original_stop
            exploration._clear_requested = original_clear
            exploration._graceful_stop_requested = original_graceful


if __name__ == "__main__":
    unittest.main()
