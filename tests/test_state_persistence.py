"""Every save_state site must persist every field.

`run_exploration` used to carry six hand-maintained copies of an 18-argument
`save_state` call. They drifted: `audit_free_streak` reached three of them.
Because the daily-sync and periodic-report paths save AFTER the cycle-end
save, the last write on any such cycle persisted the default of 0, and a
resumed run restarted the cycle-planning audit floor from zero.

The existing suite passed with that bug in place, and so did the first
version of the test written to catch it — which checked the state file at the
END of a run. That is masked: the run's final save rewrites the correct value.
The bug only shows if the process dies between the bad save and the next good
one, a window that spans the whole next cycle (~16 minutes live). So the right
invariant is not "the final state is correct" but "EVERY save writes the true
value", because any save can be the last one before a crash. These tests spy on
every `save_state` call.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from long_exposure import telemetry
from long_exposure.exploration import run_exploration


def _plan(*turns):
    body = "".join(f'<turn agent="{a}">do {a}</turn>' for a in turns)
    return f"<cycle_plan>{body}<rationale>skip the audit</rationale></cycle_plan>"


def _agent(seen):
    def fake(agent_name, agent_def, **kwargs):
        seen.append(agent_name)
        out = agent_def["outputs"][0]
        body = f"{agent_name} output " + "x" * 2100
        if agent_name == "researcher":
            # A plan that drops the auditor: this cycle is audit-free, so the
            # in-memory streak becomes 1.
            body += "\n" + _plan("worker")
        return {"agent": agent_name, "outputs": {out: body},
                "usage": {"input_tokens": 100, "output_tokens": 2100},
                "duration_ms": 10, "status": "ok", "error": None,
                "cost_usd": 0.0, "num_turns": 1, "tool_calls": 1}
    return fake


def _reporter_noop(*args, **kwargs):
    # _run_reporter returns the (possibly new) last_session_id, its 10th arg.
    return args[9] if len(args) > 9 else None


class LastSaveOfACycleTests(unittest.TestCase):
    def tearDown(self):
        telemetry.configure({"telemetry": {"enabled": False}}, None, None)

    def _run(self, *, report_interval):
        root = Path(tempfile.mkdtemp())
        ws, inst = root / "workspace", root / "instance"
        ws.mkdir(); inst.mkdir()
        (root / "score.yaml").write_text(
            "task: test directive\n"
            "loop:\n  max_cycles: 1\n  cycle_cooldown_seconds: 0\n"
            f"  report_interval: {report_interval}\n"
            "  daily_sync_interval_hours: 0\n"
            "  cycle_planning:\n    enabled: true\n    max_worker_chain: 3\n"
            "    max_turns_per_cycle: 4\n    audit_floor_cycles: 5\n"
            "    worker_may_request_audit: true\n"
            "agents:\n"
            "  researcher:\n    inputs: [directive, audit_report, live_guidance]\n"
            "    outputs: [research_brief]\n    role: researcher\n"
            "  worker:\n    inputs: [directive, research_brief]\n"
            "    outputs: [work_output]\n    role: worker\n"
            "  auditor:\n    inputs: [directive, work_output]\n"
            "    outputs: [audit_report]\n    role: auditor\n"
            "  reporter:\n    inputs: [directive]\n"
            "    outputs: [report]\n    role: reporter\n"
            "flow: [researcher, worker, auditor]\n")
        (root / "config.yaml").write_text(
            "llm_provider: local\nmodel: test\nlocal_model: test\n"
            "local_context_window: 32768\ncontext_window: 32768\n"
            "compact_threshold: 0.9\n"
            f"compact_db: {inst / 'sessions.db'}\n"
            f"working_directory: {ws}\n"
            "checkpoint_format: standard\nrequire_checkpoint_first: false\n"
            "user_gate_approval: false\ntelemetry:\n  enabled: false\n")
        seen = []
        from long_exposure import exploration as ex
        real_save = ex.save_state
        writes = []   # audit_free_streak as persisted by EACH save, in order

        def spy(*args, **kwargs):
            writes.append(kwargs.get("audit_free_streak", "<default 0>"))
            return real_save(*args, **kwargs)

        with patch("long_exposure.exploration._call_exploration_agent", _agent(seen)), \
             patch("long_exposure.exploration._run_reporter", side_effect=_reporter_noop) as rep, \
             patch("long_exposure.exploration.save_state", side_effect=spy):
            run_exploration(score_path=str(root / "score.yaml"),
                            config_path=str(root / "config.yaml"),
                            output_dir=inst / "output",
                            state_path=inst / "exploration_state.json",
                            task_override=None, instance_dir=inst)
        state = json.loads((inst / "exploration_state.json").read_text())
        self.writes = writes
        return seen, state, rep

    def test_the_cycle_really_was_audit_free(self):
        """Guard the premise: if the plan stopped skipping the auditor, the
        tests below would pass vacuously on a streak of 0."""
        seen, _, _ = self._run(report_interval=100)
        self.assertNotIn("auditor", seen, seen)

    def test_cycle_end_save_persists_the_streak(self):
        _, state, _ = self._run(report_interval=100)
        self.assertEqual(state.get("audit_free_streak"), 1)

    def test_a_periodic_report_save_does_not_reset_the_streak(self):
        """The defect: the report path's save persisted 0."""
        self._run(report_interval=1)
        self.assertGreaterEqual(len(self.writes), 2,
                                "expected the cycle-end save AND the report save")
        self.assertNotIn("<default 0>", self.writes,
                         f"a save omitted audit_free_streak: {self.writes}")
        self.assertEqual(set(self.writes), {1},
                         f"every save must persist the true streak: {self.writes}")

    def test_checking_only_the_final_state_would_have_missed_it(self):
        """Documents WHY the spy exists: the end state is correct even on the
        pre-fix code, because the final save masks the bad one."""
        _, state, rep = self._run(report_interval=1)
        self.assertTrue(rep.called)
        self.assertEqual(state.get("audit_free_streak"), 1)

    def test_every_non_clear_save_goes_through_one_builder(self):
        """Structural pin: new fields cannot drift if there is one call."""
        import ast
        src = Path(__file__).resolve().parent.parent.joinpath(
            "long_exposure", "exploration.py").read_text()
        tree = ast.parse(src)
        # The run path spans run_exploration and _finish_run (its extracted
        # terminal phase). Across both there must be exactly two direct calls:
        # the one inside _persist_state, and the deliberate clear path.
        direct = []
        for name in ("run_exploration", "_finish_run"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            direct += [f"{name}:{n.lineno}" for n in ast.walk(fn)
                       if isinstance(n, ast.Call)
                       and getattr(n.func, "id", None) == "save_state"]
        self.assertEqual(len(direct), 2, f"direct save_state calls: {direct}")


class FinishRunSessionIdTests(unittest.TestCase):
    """The terminal phase rebinds last_session_id; state must see the new one.

    `_finish_run` was extracted from run_exploration. Inline, the closing
    reporter's returned session id and the `_persist_state` closure shared one
    variable. Extracted, `_finish_run` rebinds its OWN copy, which the closure
    cannot see — so the state would persist the pre-report session id, a stale
    resume point. Caught by checking which parameters the extracted function
    reassigns; pinned here because no existing test covered it.
    """

    NEW_ID = "NEW-SESSION-FROM-CLOSING-REPORT"

    def tearDown(self):
        telemetry.configure({"telemetry": {"enabled": False}}, None, None)

    def test_the_closing_reporters_session_id_is_persisted(self):
        def closing_reporter(*args, **kwargs):
            return self.NEW_ID

        root = Path(tempfile.mkdtemp())
        ws, inst = root / "workspace", root / "instance"
        ws.mkdir(); inst.mkdir()
        (root / "score.yaml").write_text(
            "task: t\nloop:\n  max_cycles: 1\n  cycle_cooldown_seconds: 0\n"
            "  report_interval: 100\n  daily_sync_interval_hours: 0\n"
            "agents:\n"
            "  researcher:\n    inputs: [directive]\n    outputs: [research_brief]\n    role: r\n"
            "  worker:\n    inputs: [research_brief]\n    outputs: [work_output]\n    role: w\n"
            "  auditor:\n    inputs: [work_output]\n    outputs: [audit_report]\n    role: a\n"
            "  reporter:\n    inputs: [directive]\n    outputs: [report]\n    role: rep\n"
            "flow: [researcher, worker, auditor]\n")
        (root / "config.yaml").write_text(
            "llm_provider: local\nmodel: test\nlocal_model: test\n"
            "local_context_window: 32768\ncontext_window: 32768\ncompact_threshold: 0.9\n"
            f"compact_db: {inst / 'sessions.db'}\nworking_directory: {ws}\n"
            "checkpoint_format: standard\nrequire_checkpoint_first: false\n"
            "user_gate_approval: false\ntelemetry:\n  enabled: false\n")
        seen = []
        with patch("long_exposure.exploration._call_exploration_agent", _agent(seen)), \
             patch("long_exposure.exploration._run_reporter",
                   side_effect=closing_reporter) as rep:
            run_exploration(score_path=str(root / "score.yaml"),
                            config_path=str(root / "config.yaml"),
                            output_dir=inst / "output",
                            state_path=inst / "exploration_state.json",
                            task_override=None, instance_dir=inst)
        self.assertTrue(rep.called, "the closing reporter must have run")
        state = json.loads((inst / "exploration_state.json").read_text())
        self.assertEqual(state.get("last_session_id"), self.NEW_ID,
                         "state persisted a stale session id")


if __name__ == "__main__":
    unittest.main()
