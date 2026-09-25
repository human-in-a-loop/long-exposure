"""Researcher-planned cycle tails (Feature 1 of docs/advanced-model-modes-plan.md).

The parser follows the `<parallel_cycle_fanout>` posture: reject the whole
block on any violation, log a reason, fall back to the fixed flow, never
raise. The floors are the part the model cannot argue with, so they get
end-to-end coverage against the real cycle loop rather than unit coverage
alone.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from long_exposure import cycle_plan as cp

# The contract is the score's FLOW, not every agent it defines: only flow
# members have their inputs populated by the cycle loop.
CYCLE_FLOW = ("researcher", "worker", "auditor")
KNOWN = CYCLE_FLOW  # back-compat alias for the tests below


def _loop(**overrides):
    block = {"enabled": True}
    block.update(overrides)
    return {"cycle_planning": block}


def _block(*turns, rationale="because"):
    body = "".join(f'<turn agent="{a}">do {a}</turn>' for a in turns)
    return f"<cycle_plan>{body}<rationale>{rationale}</rationale></cycle_plan>"


class ConfigTests(unittest.TestCase):
    def test_shipped_score_ships_the_feature_off(self):
        score = yaml.safe_load(Path("long_exposure/exploration-score.yaml").read_text())
        self.assertFalse(cp.enabled(score["loop"]))

    def test_absent_block_is_off(self):
        self.assertFalse(cp.enabled({}))
        self.assertFalse(cp.enabled(None))

    def test_clones_are_off_even_when_enabled(self):
        self.assertTrue(cp.enabled(_loop(), is_clone=False))
        self.assertFalse(cp.enabled(_loop(), is_clone=True))

    def test_allow_in_clones_opens_it(self):
        self.assertTrue(cp.enabled(_loop(allow_in_clones=True), is_clone=True))


class ParseTests(unittest.TestCase):
    def test_a_valid_worker_chain(self):
        self.assertEqual(
            cp.parse(_block("worker", "worker", "auditor"), _loop(), KNOWN),
            ["worker", "worker", "auditor"],
        )

    def test_a_tail_with_no_auditor(self):
        self.assertEqual(
            cp.parse(_block("worker", "worker"), _loop(), KNOWN),
            ["worker", "worker"],
        )

    def test_absent_block_is_none(self):
        self.assertIsNone(cp.parse("no block here", _loop(), KNOWN))
        self.assertIsNone(cp.parse("", _loop(), KNOWN))
        self.assertIsNone(cp.parse(None, _loop(), KNOWN))

    def test_planning_disabled_ignores_a_present_block(self):
        self.assertIsNone(
            cp.parse(_block("worker"), {"cycle_planning": {"enabled": False}}, KNOWN)
        )

    def test_a_clone_ignores_a_present_block(self):
        self.assertIsNone(cp.parse(_block("worker"), _loop(), KNOWN, is_clone=True))

    def test_the_researcher_cannot_schedule_itself(self):
        self.assertIsNone(cp.parse(_block("researcher", "worker"), _loop(), KNOWN))

    def test_an_unknown_agent_rejects_the_block(self):
        self.assertIsNone(cp.parse(_block("ghost"), _loop(), KNOWN))

    def test_an_empty_block_is_rejected(self):
        self.assertIsNone(cp.parse("<cycle_plan></cycle_plan>", _loop(), KNOWN))

    def test_worker_chain_cap_rejects_rather_than_truncates(self):
        """Truncating would run a plan the researcher did not write."""
        self.assertIsNone(
            cp.parse(_block(*["worker"] * 4), _loop(max_worker_chain=3), KNOWN)
        )
        self.assertEqual(
            len(cp.parse(_block(*["worker"] * 3), _loop(max_worker_chain=3), KNOWN)),
            3,
        )

    def test_two_auditors_are_rejected(self):
        self.assertIsNone(cp.parse(_block("auditor", "auditor"), _loop(), KNOWN))

    def test_total_turn_cap(self):
        self.assertIsNone(
            cp.parse(
                _block("worker", "worker", "auditor"),
                _loop(max_turns_per_cycle=2),
                KNOWN,
            )
        )

    def test_an_auditor_before_the_work_is_moved_last(self):
        self.assertEqual(
            cp.parse(_block("auditor", "worker", "worker"), _loop(), KNOWN),
            ["worker", "worker", "auditor"],
        )

    def test_case_and_quoting_variants_parse(self):
        for text in (
            '<CYCLE_PLAN><TURN AGENT="WORKER">x</TURN></CYCLE_PLAN>',
            "<cycle_plan><turn agent='worker'>x</turn></cycle_plan>",
            "<cycle_plan><turn agent=worker>x</turn></cycle_plan>",
        ):
            self.assertEqual(cp.parse(text, _loop(), KNOWN), ["worker"], text)

    def test_a_malformed_block_never_raises(self):
        for text in (
            "<cycle_plan><turn agent=>x</turn></cycle_plan>",
            "<cycle_plan><turn>x</turn></cycle_plan>",
            "<cycle_plan>prose only</cycle_plan>",
            "<cycle_plan>",
        ):
            self.assertIsNone(cp.parse(text, _loop(), KNOWN), text)

    def test_out_of_cycle_agents_are_not_schedulable(self):
        """Only flow members have their inputs populated by the cycle loop.

        A plan naming final_auditor / final_reporter / curator / reporter
        would run that agent with [UNAVAILABLE: stage],
        [UNAVAILABLE: expected_file] and so on — a full turn spent producing
        something unusable, and for the roles with agent_teams: true,
        possibly teammates spawned to do it.
        """
        for role in ("reporter", "curator", "final_auditor", "final_reporter"):
            self.assertIsNone(
                cp.parse(
                    _block("worker", role),
                    _loop(max_turns_per_cycle=4),
                    CYCLE_FLOW,
                ),
                role,
            )

    def test_a_score_with_a_wider_flow_can_schedule_its_own_roles(self):
        """Validation follows the score's flow, not a hard-coded role list."""
        self.assertEqual(
            cp.parse(
                _block("worker", "verifier"),
                _loop(max_turns_per_cycle=4),
                ["researcher", "worker", "verifier", "auditor"],
            ),
            ["worker", "verifier"],
        )

    def test_rationale_and_directives_are_extracted(self):
        text = _block("worker", "auditor", rationale="the fit needs the grid")
        self.assertEqual(cp.rationale(text), "the fit needs the grid")
        self.assertEqual(
            cp.turn_directives(text),
            [("worker", "do worker"), ("auditor", "do auditor")],
        )


class AuditFloorTests(unittest.TestCase):
    def test_no_force_while_under_the_floor(self):
        for streak in (0, 1):
            tail, forced = cp.apply_floor(["worker"], _loop(audit_floor_cycles=2), streak)
            self.assertEqual(tail, ["worker"], streak)
            self.assertFalse(forced)

    def test_force_at_the_floor(self):
        tail, forced = cp.apply_floor(["worker"], _loop(audit_floor_cycles=2), 2)
        self.assertEqual(tail, ["worker", "auditor"])
        self.assertTrue(forced)

    def test_a_plan_that_already_audits_is_untouched(self):
        tail, forced = cp.apply_floor(
            ["worker", "auditor"], _loop(audit_floor_cycles=1), 99,
        )
        self.assertEqual(tail, ["worker", "auditor"])
        self.assertFalse(forced)

    def test_floor_zero_disables_the_forcing(self):
        tail, forced = cp.apply_floor(["worker"], _loop(audit_floor_cycles=0), 99)
        self.assertEqual(tail, ["worker"])
        self.assertFalse(forced)

    def test_build_flow_keeps_the_planner_first(self):
        flow, planned, forced = cp.build_flow(
            ["worker", "worker"], ["researcher", "worker", "auditor"], _loop(), 0,
        )
        self.assertEqual(flow, ["researcher", "worker", "worker"])
        self.assertTrue(planned)
        self.assertFalse(forced)

    def test_build_flow_without_a_plan_returns_the_fixed_flow(self):
        fixed = ["researcher", "worker", "auditor"]
        flow, planned, forced = cp.build_flow(None, fixed, _loop(), 5)
        self.assertEqual(flow, fixed)
        self.assertFalse(planned)
        self.assertFalse(forced)
        self.assertIsNot(flow, fixed, "must not alias the caller's list")


class EscalationTests(unittest.TestCase):
    def test_token_on_its_own_line_triggers(self):
        self.assertTrue(cp.wants_audit("did the work\n[[REQUEST_AUDIT]]\n"))
        self.assertTrue(cp.wants_audit("[[REQUEST_AUDIT]]"))
        self.assertTrue(cp.wants_audit("x\n  [[REQUEST_AUDIT]]  \ny"))

    def test_merely_discussing_the_token_does_not_trigger(self):
        """Same trap BRANCH_COMPLETE_RE exists to avoid."""
        self.assertFalse(
            cp.wants_audit("I am not emitting [[REQUEST_AUDIT]] because it is fine")
        )
        self.assertFalse(cp.wants_audit("see [[REQUEST_AUDIT]] in the docs"))

    def test_empty_input(self):
        self.assertFalse(cp.wants_audit(""))
        self.assertFalse(cp.wants_audit(None))

    def test_the_escalated_auditor_goes_last_not_mid_chain(self):
        """Auditing between chained workers would audit half a cycle.

        Inserting at the escalating worker's position would run the audit
        before worker 2, so audit_report and the memoir would describe only
        part of the cycle's work and the next researcher would read that
        partial verdict as the cycle's. It would also contradict the
        parser's own "auditor is always moved last" rule.
        """
        self.assertEqual(
            cp.insert_requested_audit(["researcher", "worker", "worker"], 1),
            ["researcher", "worker", "worker", "auditor"],
        )
        self.assertEqual(
            cp.insert_requested_audit(["researcher", "worker", "worker"], 2),
            ["researcher", "worker", "worker", "auditor"],
        )

    def test_insert_is_a_no_op_when_an_auditor_is_already_scheduled(self):
        flow = ["researcher", "worker", "auditor"]
        self.assertEqual(cp.insert_requested_audit(flow, 1), flow)


class GuidanceTests(unittest.TestCase):
    def test_guidance_states_the_enforced_bounds(self):
        text = cp.guidance(_loop(max_worker_chain=5, audit_floor_cycles=3))
        self.assertIn("at most 5 worker turns", text)
        self.assertIn("after 3 consecutive cycles", text)
        self.assertIn("cannot schedule yourself", text)

    def test_worker_guidance_names_the_token(self):
        self.assertIn(cp.REQUEST_AUDIT_SIGNAL, cp.worker_escalation_guidance())

    def test_guidance_round_trips_through_the_parser(self):
        """The example in the prompt must be something the parser accepts."""
        example = cp.guidance(_loop())
        example = example[example.index("<cycle_plan>"):]
        example = example[: example.index("</cycle_plan>") + len("</cycle_plan>")]
        example = example.replace("\n    ", " ")
        self.assertEqual(
            cp.parse(example, _loop(), KNOWN), ["worker", "worker", "auditor"],
        )


# ---------------------------------------------------------------------------
# End-to-end against the real cycle loop
# ---------------------------------------------------------------------------

from long_exposure import telemetry  # noqa: E402
from long_exposure.exploration import run_exploration  # noqa: E402
from tests.test_run_switches import _write_files  # noqa: E402


def _planning_agent(seen, *, brief_extra="", work_extra=""):
    """Stub provider that lets the researcher emit a plan and the worker escalate."""

    def fake_agent(agent_name, agent_def, **kwargs):
        seen.append(agent_name)
        output_name = agent_def["outputs"][0]
        body = f"{agent_name} output " + ("x" * 2100)
        if agent_name == "researcher":
            body += "\n" + brief_extra
        elif agent_name == "worker":
            body += "\n" + work_extra
        return {
            "agent": agent_name,
            "outputs": {output_name: body},
            "usage": {"input_tokens": 100, "output_tokens": 2100},
            "duration_ms": 10,
            "status": "ok",
            "error": None,
            "cost_usd": 0.0,
            "num_turns": 1,
            "tool_calls": 1,
        }

    return fake_agent


class CycleLoopIntegrationTests(unittest.TestCase):
    def tearDown(self):
        telemetry.configure({"telemetry": {"enabled": False}}, None, None)

    def _run(self, *, planning, brief_extra="", work_extra="", max_cycles=1,
             floor=2):
        seen = []
        td = tempfile.mkdtemp()
        extra = f"  max_cycles: {max_cycles}\n"
        if planning:
            extra += (
                "  cycle_planning:\n"
                "    enabled: true\n"
                "    max_worker_chain: 3\n"
                "    max_turns_per_cycle: 4\n"
                f"    audit_floor_cycles: {floor}\n"
                "    worker_may_request_audit: true\n"
            )
        score, config, inst = _write_files(Path(td), loop_extra=extra)
        with patch(
            "long_exposure.exploration._call_exploration_agent",
            _planning_agent(seen, brief_extra=brief_extra, work_extra=work_extra),
        ):
            run_exploration(
                score_path=str(score), config_path=str(config),
                output_dir=inst / "output",
                state_path=inst / "exploration_state.json",
                task_override=None, instance_dir=inst,
            )
        return seen, inst

    def test_feature_off_runs_the_fixed_flow_even_with_a_block_present(self):
        seen, _ = self._run(
            planning=False, brief_extra=_block("worker", "worker", "worker"),
        )
        self.assertEqual(seen, ["researcher", "worker", "auditor"])

    def test_a_planned_worker_chain_runs_in_the_same_cycle(self):
        seen, _ = self._run(
            planning=True, brief_extra=_block("worker", "worker", "auditor"),
        )
        self.assertEqual(seen, ["researcher", "worker", "worker", "auditor"])

    def test_a_plan_can_drop_the_auditor(self):
        seen, _ = self._run(planning=True, brief_extra=_block("worker"))
        self.assertEqual(seen, ["researcher", "worker"])

    def test_a_malformed_plan_falls_back_to_the_fixed_flow(self):
        seen, _ = self._run(
            planning=True,
            brief_extra="<cycle_plan><turn agent='ghost'>x</turn></cycle_plan>",
        )
        self.assertEqual(seen, ["researcher", "worker", "auditor"])

    def test_the_audit_floor_forces_an_auditor_on_the_third_cycle(self):
        """Two audit-free cycles are allowed; the third gets an auditor."""
        seen, _ = self._run(
            planning=True, brief_extra=_block("worker"), max_cycles=3, floor=2,
        )
        self.assertEqual(
            seen,
            [
                "researcher", "worker",             # cycle 1: audit-free
                "researcher", "worker",             # cycle 2: audit-free
                "researcher", "worker", "auditor",  # cycle 3: floor forced
            ],
        )

    def test_a_worker_escalation_pulls_the_auditor_back_in(self):
        seen, _ = self._run(
            planning=True,
            brief_extra=_block("worker"),
            work_extra="\n[[REQUEST_AUDIT]]\n",
        )
        self.assertEqual(seen, ["researcher", "worker", "auditor"])

    def test_discussing_the_escalation_token_does_not_pull_the_auditor_in(self):
        seen, _ = self._run(
            planning=True,
            brief_extra=_block("worker"),
            work_extra="I considered emitting [[REQUEST_AUDIT]] but did not.",
        )
        self.assertEqual(seen, ["researcher", "worker"])

    def test_an_escalation_resets_the_audit_floor(self):
        """An auditor ran, so the streak restarts like any audited cycle."""
        import json

        seen, inst = self._run(
            planning=True,
            brief_extra=_block("worker"),
            work_extra="\n[[REQUEST_AUDIT]]\n",
            max_cycles=1,
        )
        state = json.loads((inst / "exploration_state.json").read_text())
        self.assertEqual(state.get("audit_free_streak"), 0)

    def test_the_streak_persists_into_state(self):
        import json

        _, inst = self._run(planning=True, brief_extra=_block("worker"), max_cycles=2)
        state = json.loads((inst / "exploration_state.json").read_text())
        self.assertEqual(state.get("audit_free_streak"), 2)

    def test_a_chained_worker_accumulates_rather_than_overwrites(self):
        """The auditor must see every turn, not only the last."""
        seen, inst = self._run(
            planning=True, brief_extra=_block("worker", "worker", "auditor"),
        )
        import json

        state = json.loads((inst / "exploration_state.json").read_text())
        work = state["results"]["work_output"]
        self.assertIn("## worker turn 2", work)
        self.assertGreater(len(work), 4000)  # both turns present, not just one

    def test_the_score_flow_is_not_mutated_by_a_plan(self):
        """A plan rewrites the cycle's tail; the score's own flow must survive."""
        seen, _ = self._run(
            planning=True, brief_extra=_block("worker", "worker"), max_cycles=2,
        )
        # Cycle 2 must start from the fixed flow and re-plan, not inherit a
        # permanently-widened flow from cycle 1.
        self.assertEqual(
            seen,
            ["researcher", "worker", "worker", "researcher", "worker", "worker"],
        )


if __name__ == "__main__":
    unittest.main()


class FanOutPrecedenceTests(unittest.TestCase):
    """Fan-out already replaces worker and auditor, so a plan must lose to it."""

    def tearDown(self):
        telemetry.configure({"telemetry": {"enabled": False}}, None, None)

    def test_fanout_wins_and_the_plan_never_runs(self):
        seen = []
        td = tempfile.mkdtemp()
        score, config, inst = _write_files(
            Path(td),
            loop_extra=(
                "  max_cycles: 1\n"
                "  cycle_planning:\n"
                "    enabled: true\n"
                "    audit_floor_cycles: 9\n"
            ),
        )
        # The researcher emits BOTH blocks; fan-out fires first and breaks.
        brief = _block("worker", "worker", "worker")
        fired = {"v": False}

        def fake_fanout(*a, **kw):
            fired["v"] = True
            return {
                "aggregated_report": "merged",
                "fork_id": "fork-test",
                "branches": 2,
                "outcomes": [],
            }

        with patch(
            "long_exposure.exploration._call_exploration_agent",
            _planning_agent(seen, brief_extra=brief),
        ), patch(
            "long_exposure.exploration._parse_fanout_block",
            lambda _t: [
                {"objective": "a", "output_artifact": "a.md"},
                {"objective": "b", "output_artifact": "b.md"},
            ],
        ), patch(
            "long_exposure.exploration._run_fanout_conductor", fake_fanout
        ):
            run_exploration(
                score_path=str(score), config_path=str(config),
                output_dir=inst / "output",
                state_path=inst / "exploration_state.json",
                task_override=None, instance_dir=inst,
            )
        self.assertTrue(fired["v"], "fan-out did not fire")
        # Only the researcher ran: fan-out replaced the rest, plan included.
        self.assertEqual(seen, ["researcher"])

    def test_the_plan_runs_when_fanout_does_not_fire(self):
        """Negative control for the test above."""
        seen = []
        td = tempfile.mkdtemp()
        score, config, inst = _write_files(
            Path(td),
            loop_extra=(
                "  max_cycles: 1\n"
                "  cycle_planning:\n"
                "    enabled: true\n"
                "    audit_floor_cycles: 9\n"
            ),
        )
        with patch(
            "long_exposure.exploration._call_exploration_agent",
            _planning_agent(seen, brief_extra=_block("worker", "worker")),
        ), patch(
            "long_exposure.exploration._parse_fanout_block", lambda _t: None
        ):
            run_exploration(
                score_path=str(score), config_path=str(config),
                output_dir=inst / "output",
                state_path=inst / "exploration_state.json",
                task_override=None, instance_dir=inst,
            )
        self.assertEqual(seen, ["researcher", "worker", "worker"])


class FailureHandlingUnderPlanningTests(unittest.TestCase):
    """Stage 1's name-based disposition, exercised through a planned tail."""

    def tearDown(self):
        telemetry.configure({"telemetry": {"enabled": False}}, None, None)

    def _run(self, *, brief, fail_worker_number, max_cycles=1):
        seen = []

        def fake(agent_name, agent_def, **kwargs):
            seen.append(agent_name)
            if (
                agent_name == "worker"
                and seen.count("worker") == fail_worker_number
            ):
                return {
                    "agent": agent_name, "outputs": {}, "usage": {},
                    "duration_ms": 10, "status": "error", "error": "boom",
                    "cost_usd": 0.0, "num_turns": 0, "tool_calls": 0,
                }
            body = agent_name + " out " + ("x" * 2100)
            if agent_name == "researcher":
                body += "\n" + brief
            return {
                "agent": agent_name,
                "outputs": {agent_def["outputs"][0]: body},
                "usage": {"input_tokens": 100, "output_tokens": 2100},
                "duration_ms": 10, "status": "ok", "error": None,
                "cost_usd": 0.0, "num_turns": 1, "tool_calls": 1,
            }

        td = tempfile.mkdtemp()
        score, config, inst = _write_files(
            Path(td),
            loop_extra=(
                f"  max_cycles: {max_cycles}\n"
                "  cycle_planning:\n"
                "    enabled: true\n"
                "    audit_floor_cycles: 9\n"
            ),
        )
        with patch("long_exposure.exploration._call_exploration_agent", fake):
            run_exploration(
                score_path=str(score), config_path=str(config),
                output_dir=inst / "output",
                state_path=inst / "exploration_state.json",
                task_override=None, instance_dir=inst,
            )
        import json

        return seen, json.loads((inst / "exploration_state.json").read_text())

    def test_a_worker_failing_LAST_in_the_tail_is_not_given_the_audit_fallback(self):
        """The headline bug the Stage 1 refactor existed to prevent.

        Pre-refactor, `i == len(flow) - 1` classified this as an audit
        failure and wrote FALLBACK_AUDIT into results["audit_report"] —
        inventing an audit that never ran, which the next cycle's researcher
        would then read as real.
        """
        seen, state = self._run(
            brief=_block("worker", "worker"), fail_worker_number=2,
        )
        self.assertEqual(seen, ["researcher", "worker", "worker"])
        self.assertIn(
            "[AGENT FAILED: worker]", state["results"].get("work_output", ""),
        )
        self.assertNotIn(
            "Audit unavailable this cycle",
            state["results"].get("audit_report", ""),
        )

    def test_a_worker_failing_mid_chain_still_lets_the_auditor_run(self):
        seen, state = self._run(
            brief=_block("worker", "worker", "auditor"), fail_worker_number=2,
        )
        self.assertEqual(seen, ["researcher", "worker", "worker", "auditor"])
        self.assertIn(
            "[AGENT FAILED: worker]", state["results"].get("work_output", ""),
        )
        self.assertNotIn(
            "Audit unavailable this cycle",
            state["results"].get("audit_report", ""),
        )


class PostMergeTests(unittest.TestCase):
    def tearDown(self):
        telemetry.configure({"telemetry": {"enabled": False}}, None, None)

    def test_a_post_merge_cycle_has_no_planner_and_does_not_crash(self):
        """flow_this_cycle is [worker]: index 0 is not the researcher."""
        import json

        seen = []

        def fake(agent_name, agent_def, **kwargs):
            seen.append(agent_name)
            return {
                "agent": agent_name,
                "outputs": {agent_def["outputs"][0]: agent_name + " " + "x" * 2100},
                "usage": {"input_tokens": 100, "output_tokens": 2100},
                "duration_ms": 10, "status": "ok", "error": None,
                "cost_usd": 0.0, "num_turns": 1, "tool_calls": 1,
            }

        td = tempfile.mkdtemp()
        score, config, inst = _write_files(
            Path(td),
            loop_extra=(
                "  max_cycles: 4\n"
                "  cycle_planning:\n    enabled: true\n"
            ),
        )
        (inst / "exploration_state.json").write_text(json.dumps({
            "cycle": 3,
            "results": {"directive": "d", "audit_report": "merge text"},
            "failures": {}, "post_merge_pending": True,
            "task": "test directive",
        }))
        with patch("long_exposure.exploration._call_exploration_agent", fake):
            run_exploration(
                score_path=str(score), config_path=str(config),
                output_dir=inst / "output",
                state_path=inst / "exploration_state.json",
                task_override=None, instance_dir=inst,
            )
        self.assertEqual(seen[0], "worker")


class CloneTests(unittest.TestCase):
    def tearDown(self):
        telemetry.configure({"telemetry": {"enabled": False}}, None, None)

    def test_a_clone_runs_the_fixed_flow_despite_a_plan_block(self):
        import os

        seen = []

        def fake(agent_name, agent_def, **kwargs):
            seen.append(agent_name)
            body = agent_name + " out " + ("x" * 2100)
            if agent_name == "researcher":
                body += "\n" + _block("worker", "worker", "worker")
            return {
                "agent": agent_name,
                "outputs": {agent_def["outputs"][0]: body},
                "usage": {"input_tokens": 100, "output_tokens": 2100},
                "duration_ms": 10, "status": "ok", "error": None,
                "cost_usd": 0.0, "num_turns": 1, "tool_calls": 1,
            }

        td = tempfile.mkdtemp()
        score, config, inst = _write_files(
            Path(td),
            loop_extra=(
                "  max_cycles: 1\n"
                "  cycle_planning:\n"
                "    enabled: true\n"
                "    allow_in_clones: false\n"
            ),
        )
        with patch.dict(os.environ, {"AGENT_FORK_ID": "fork-cp",
                                     "AGENT_FORK_CLONE_K": "0"}), \
             patch("long_exposure.exploration._call_exploration_agent", fake):
            run_exploration(
                score_path=str(score), config_path=str(config),
                output_dir=inst / "output",
                state_path=inst / "exploration_state.json",
                task_override=None, instance_dir=inst,
            )
        self.assertEqual(seen, ["researcher", "worker", "auditor"])


class EscalationPlacementIntegrationTests(unittest.TestCase):
    """An escalated audit must cover the WHOLE cycle, not part of it."""

    def tearDown(self):
        telemetry.configure({"telemetry": {"enabled": False}}, None, None)

    def test_worker_one_of_a_chain_escalating_still_audits_last(self):
        seen = []

        def fake(agent_name, agent_def, **kwargs):
            seen.append(agent_name)
            body = agent_name + " out " + ("x" * 2100)
            if agent_name == "researcher":
                body += "\n" + _block("worker", "worker")
            # Only the FIRST worker escalates.
            if agent_name == "worker" and seen.count("worker") == 1:
                body += "\n[[REQUEST_AUDIT]]\n"
            return {
                "agent": agent_name,
                "outputs": {agent_def["outputs"][0]: body},
                "usage": {"input_tokens": 100, "output_tokens": 2100},
                "duration_ms": 10, "status": "ok", "error": None,
                "cost_usd": 0.0, "num_turns": 1, "tool_calls": 1,
            }

        td = tempfile.mkdtemp()
        score, config, inst = _write_files(
            Path(td),
            loop_extra=(
                "  max_cycles: 1\n"
                "  cycle_planning:\n"
                "    enabled: true\n"
                "    audit_floor_cycles: 9\n"
            ),
        )
        with patch("long_exposure.exploration._call_exploration_agent", fake):
            run_exploration(
                score_path=str(score), config_path=str(config),
                output_dir=inst / "output",
                state_path=inst / "exploration_state.json",
                task_override=None, instance_dir=inst,
            )
        self.assertEqual(
            seen, ["researcher", "worker", "worker", "auditor"],
            "the escalated auditor must run after BOTH workers",
        )

    def test_the_auditor_sees_both_worker_turns(self):
        """The reason placement matters: audit input completeness."""
        import json

        seen = []
        audit_inputs = {}

        def fake(agent_name, agent_def, **kwargs):
            seen.append(agent_name)
            if agent_name == "auditor":
                audit_inputs["work_output"] = (
                    kwargs.get("results") or {}
                ).get("work_output", "")
            body = agent_name + " out " + ("x" * 2100)
            if agent_name == "researcher":
                body += "\n" + _block("worker", "worker")
            if agent_name == "worker":
                body += f"\nTURN-MARKER-{seen.count('worker')}"
                if seen.count("worker") == 1:
                    body += "\n[[REQUEST_AUDIT]]\n"
            return {
                "agent": agent_name,
                "outputs": {agent_def["outputs"][0]: body},
                "usage": {"input_tokens": 100, "output_tokens": 2100},
                "duration_ms": 10, "status": "ok", "error": None,
                "cost_usd": 0.0, "num_turns": 1, "tool_calls": 1,
            }

        td = tempfile.mkdtemp()
        score, config, inst = _write_files(
            Path(td),
            loop_extra=(
                "  max_cycles: 1\n"
                "  cycle_planning:\n"
                "    enabled: true\n"
                "    audit_floor_cycles: 9\n"
            ),
        )
        with patch("long_exposure.exploration._call_exploration_agent", fake):
            run_exploration(
                score_path=str(score), config_path=str(config),
                output_dir=inst / "output",
                state_path=inst / "exploration_state.json",
                task_override=None, instance_dir=inst,
            )
        work = audit_inputs.get("work_output", "")
        self.assertIn("TURN-MARKER-1", work)
        self.assertIn("TURN-MARKER-2", work)
