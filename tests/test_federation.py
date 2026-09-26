"""Operator identity: the §7.1 defect, closed, and the second one it exposed.

`docs/git-federation.md` §7.1 measured a ledger that dropped one operator's
confident result when two operators reached the same milestone. These tests
pin the fix, and — more importantly — pin the two properties that make it
safe to ship: a single-operator ledger is byte-identical to before, and a
ledger written before the field existed does not suddenly report two
operators.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from long_exposure import anti_patterns, federation as fed
from long_exposure import workspace_bootstrap as wb


def ev(eid, ts, mid, narrative, *, operator=None, status="validated",
       level="high", agent="auditor", cycle=3):
    e = {"event_id": eid, "ts": ts, "run_id": "r", "cycle": cycle,
         "agent": agent, "milestone_id": mid, "status": status,
         "confidence": {"level": level, "rationale": "because",
                        "assessor": agent},
         "narrative": narrative}
    if operator is not None:
        e["operator"] = operator
    return e


class OperatorNameTests(unittest.TestCase):
    def test_env_wins_over_config(self):
        with mock.patch.dict(os.environ, {fed.ENV_OPERATOR: "from-env"}):
            self.assertEqual(
                fed.operator_name({"federation": {"operator": "from-config"}}),
                "from-env",
            )

    def test_config_wins_over_hostname(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(fed.ENV_OPERATOR, None)
            self.assertEqual(
                fed.operator_name({"federation": {"operator": "Alice Laptop!"}}),
                "alice-laptop",
            )

    def test_hostname_is_the_default_so_federation_works_unconfigured(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(fed.ENV_OPERATOR, None)
            with mock.patch("socket.gethostname", return_value="Alices-MBP.local"):
                self.assertEqual(fed.operator_name({}), "alices-mbp")

    def test_falls_back_when_the_hostname_is_unusable(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(fed.ENV_OPERATOR, None)
            for host in ("", "...", "!!!"):
                with mock.patch("socket.gethostname", return_value=host):
                    self.assertEqual(fed.operator_name({}), fed.FALLBACK_OPERATOR)
            with mock.patch("socket.gethostname", side_effect=OSError):
                self.assertEqual(fed.operator_name({}), fed.FALLBACK_OPERATOR)

    def test_the_name_is_safe_in_a_branch_name_and_a_path(self):
        for raw in ("a/b", "a b", "../../etc", "A_B", "x" * 200, "--x--"):
            name = fed.operator_name({"federation": {"operator": raw}})
            self.assertNotIn("/", name)
            self.assertNotIn(" ", name)
            self.assertNotIn("..", name)
            self.assertLessEqual(len(name), fed.MAX_NAME)
            self.assertTrue(name)


class StampTests(unittest.TestCase):
    def test_the_single_chokepoint_stamps_every_writer(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            wb.emit_run_start_event(ws, "run-x", "a directive")
            wb.append_ledger_event(ws, ev("e2", "2026-01-01T00:00:00Z", "m/x", "n"))
            rows = [json.loads(l) for l in
                    (ws / "promise_ledger.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertTrue(row.get("operator"), row)

    def test_an_existing_operator_is_never_overwritten(self):
        """A clone's shadow ledger is concatenated in; re-stamping would
        relabel another process's events."""
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            wb.append_ledger_event(
                ws, ev("e1", "2026-01-01T00:00:00Z", "m/x", "n", operator="someone-else"))
            row = json.loads((ws / "promise_ledger.jsonl").read_text().strip())
        self.assertEqual(row["operator"], "someone-else")

    def test_stamping_never_blocks_an_append(self):
        """Identity is useful, not load-bearing."""
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            with mock.patch.object(fed, "operator_name", side_effect=RuntimeError):
                wb.append_ledger_event(ws, ev("e1", "2026-01-01T00:00:00Z", "m/x", "n"))
            self.assertTrue((ws / "promise_ledger.jsonl").exists())

    def test_a_non_dict_event_does_not_raise(self):
        self.assertEqual(fed.stamp(None), None)

    def test_the_agent_facing_tool_agrees_with_the_harness(self):
        """The ledger_append tool is a separate process; it must resolve the
        SAME operator or one machine writes under two names."""
        from long_exposure.orchestrator import _add_federation_env

        config = {"federation": {"operator": "alice"}}
        fed._reset_binding()
        env = {}
        _add_federation_env(env, config)
        self.assertEqual(env[fed.ENV_OPERATOR], "alice")
        with mock.patch.dict(os.environ, env):
            self.assertEqual(fed.operator_name(None), "alice")

    def test_a_second_run_in_one_process_does_not_inherit_the_first(self):
        """`bind` mutates the process environment, so an embedder that calls
        run_exploration twice must not give run B run A's operator. Found by
        the full suite leaking one test's operator into every later test."""
        fed._reset_binding()
        self.assertEqual(fed.bind({"federation": {"operator": "alice"}}), "alice")
        self.assertEqual(fed.bind({"federation": {"operator": "bob"}}), "bob")

    def test_an_inherited_name_survives_repeated_binds(self):
        """A clone never bound its own name, so the root's value keeps winning
        however many times the clone's config is loaded."""
        fed._reset_binding()
        os.environ[fed.ENV_OPERATOR] = "root-op"
        for _ in range(3):
            self.assertEqual(fed.bind({"federation": {"operator": "bob"}}),
                             "root-op")


class ContradictionVisibilityTests(unittest.TestCase):
    """The §7.1 defect itself."""

    def _summary(self, *events):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            for e in events:
                wb.append_ledger_event(ws, e)
            return wb.summarize_ledger(ws)

    def test_both_operators_settled_results_are_shown(self):
        s = self._summary(
            ev("a", "2026-01-02T00:00:00Z", "spectral/bound",
               "ALICE bound is 3.2", operator="alice"),
            ev("b", "2026-01-03T00:00:00Z", "spectral/bound",
               "BOB bound is 7.9", operator="bob"),
        )
        self.assertIn("ALICE bound is 3.2", s)
        self.assertIn("BOB bound is 7.9", s)
        self.assertIn("alice", s)
        self.assertIn("bob", s)

    def test_the_summary_says_the_disagreement_is_the_finding(self):
        s = self._summary(
            ev("a", "2026-01-02T00:00:00Z", "m/x", "A", operator="alice"),
            ev("b", "2026-01-03T00:00:00Z", "m/x", "B", operator="bob"),
        )
        self.assertIn("Operators on this ledger: alice, bob", s)

    def test_one_operators_own_supersession_still_collapses(self):
        """Within ONE operator, latest-per-milestone must still win —
        otherwise every superseded event returns to the summary."""
        s = self._summary(
            ev("a", "2026-01-02T00:00:00Z", "m/x", "EARLY", operator="alice"),
            ev("b", "2026-01-03T00:00:00Z", "m/x", "LATE", operator="alice"),
        )
        self.assertIn("LATE", s)
        self.assertNotIn("EARLY", s)

    def test_a_single_operator_summary_is_unchanged(self):
        """No operator column, no operators line. This string goes into the
        prompt, so the single-operator case must not drift."""
        s = self._summary(ev("a", "2026-01-02T00:00:00Z", "m/x", "n",
                             operator="alice"))
        self.assertNotIn("Operators on this ledger", s)
        self.assertIn("(cycle 3, auditor, 2026-01-02T00:00:00Z)", s)

    def test_a_legacy_ledger_does_not_report_a_phantom_operator(self):
        """Events written before the field existed must read as LOCAL, not as
        a distinct empty operator, or resuming would split every milestone."""
        legacy = ev("a", "2026-01-02T00:00:00Z", "m/x", "LEGACY")
        legacy.pop("operator", None)
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            # Written straight to the file, bypassing the stamping chokepoint,
            # which is exactly the shape a pre-upgrade ledger has on disk.
            (ws / "promise_ledger.jsonl").write_text(json.dumps(legacy) + "\n")
            wb.append_ledger_event(ws, ev("b", "2026-01-03T00:00:00Z", "m/y", "NEW"))
            s = wb.summarize_ledger(ws)
        self.assertNotIn("Operators on this ledger", s)
        self.assertIn("NEW", s)

    def test_distinct_milestone_count_counts_milestones_not_pairs(self):
        s = self._summary(
            ev("a", "2026-01-02T00:00:00Z", "m/x", "A", operator="alice"),
            ev("b", "2026-01-03T00:00:00Z", "m/x", "B", operator="bob"),
        )
        self.assertIn("distinct milestones: 1", s)


class AntiPatternMaskingTests(unittest.TestCase):
    """The second defect the same key change closed.

    `anti_patterns` surfaces a milestone whose LATEST event is `invalidated`.
    Keyed on milestone_id alone, a second operator's later non-invalidated
    event silently stopped the warning being surfaced at all.
    """

    @staticmethod
    def _ap(eid, ts, rationale, operator, status):
        """anti_patterns renders confidence.RATIONALE, not narrative."""
        e = ev(eid, ts, "approach/monte-carlo", rationale,
               operator=operator, status=status)
        e["confidence"]["rationale"] = rationale
        return e

    def _block(self, *events):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            for e in events:
                wb.append_ledger_event(ws, e)
            return anti_patterns.build_block(ws) or ""

    def test_an_invalidation_survives_another_operators_later_event(self):
        block = self._block(
            self._ap("a", "2026-01-02T00:00:00Z",
                     "ALICE: monte-carlo diverges", "alice", "invalidated"),
            self._ap("b", "2026-01-03T00:00:00Z",
                     "BOB: trying monte-carlo", "bob", "in-progress"),
        )
        self.assertIn("ALICE: monte-carlo diverges", block)

    def test_an_operators_own_later_event_still_clears_the_warning(self):
        """Within one operator, a revisited approach must stop warning."""
        block = self._block(
            self._ap("a", "2026-01-02T00:00:00Z", "diverges", "alice",
                     "invalidated"),
            self._ap("b", "2026-01-03T00:00:00Z", "works now", "alice",
                     "validated"),
        )
        self.assertEqual(block, "")


class SliceNamingTests(unittest.TestCase):
    """§6.2: the near-miss half of the claim-name problem."""

    def test_separator_and_case_near_misses_fold_together(self):
        forms = ["spectral-theory", "spectral_theory", "Spectral Theory",
                 "SPECTRAL--THEORY", " spectral theory "]
        self.assertEqual(len({fed.canonical_slice(f) for f in forms}), 1)

    def test_simple_plurals_fold(self):
        for plural, singular in (("reports", "report"), ("audits", "audit"),
                                 ("scripts", "script"), ("tests", "test"),
                                 ("tools", "tool"), ("cycles", "cycle"),
                                 ("boundaries", "boundary"),
                                 ("hypotheses", "hypothesis"),
                                 ("analyses", "analysis"),
                                 ("indices", "index")):
            self.assertEqual(fed.canonical_slice(plural),
                             fed.canonical_slice(singular), plural)

    def test_synonyms_do_NOT_fold_and_the_docs_say_so(self):
        """The honest limit. §6.2 states it; this stops it being forgotten."""
        self.assertNotEqual(fed.canonical_slice("spectral-theory"),
                            fed.canonical_slice("eigenvalue-bounds"))

    def test_over_stemming_is_avoided(self):
        """Folding two distinct slices together is worse than a near-miss
        surviving, so short words and -ss endings are left alone."""
        for word in ("bias", "loss", "analysis", "corpus", "chaos", "gas",
                     "basis", "status"):
            self.assertEqual(fed.canonical_slice(word), word, word)

    def test_empty_and_unusable_input_returns_empty(self):
        for raw in ("", "   ", "///", None):
            self.assertEqual(fed.canonical_slice(raw), "")

    def test_slice_for_path_is_directory_shaped(self):
        self.assertEqual(fed.slice_for_path("data/spectral/bound.py"),
                         "data-spectral")
        self.assertEqual(fed.slice_for_path("data/spectral/other.py"),
                         "data-spectral")
        self.assertEqual(fed.slice_for_path("MEMOIR.md"), "memoir")
        self.assertEqual(fed.slice_for_path("./reports/cycles/r.md"),
                         "report-cycle")
        self.assertEqual(fed.slice_for_path(""), "")


class SettingsTests(unittest.TestCase):
    def test_defaults_apply_when_the_block_is_absent_or_junk(self):
        for config in ({}, {"federation": None}, {"federation": "no"}):
            s = fed.settings(config)
            self.assertEqual(s["operator"], "")
            self.assertEqual(s["conflict_radar"], fed.DEFAULTS["conflict_radar"])

    def test_a_partial_radar_block_keeps_the_other_defaults(self):
        s = fed.settings({"federation": {"conflict_radar": {"enabled": True}}})
        self.assertTrue(s["conflict_radar"]["enabled"])
        self.assertEqual(s["conflict_radar"]["shared_branch"], "main")

    def test_settings_never_mutates_the_defaults(self):
        fed.settings({"federation": {"conflict_radar": {"shared_branch": "dev"}}})
        self.assertEqual(fed.DEFAULTS["conflict_radar"]["shared_branch"], "main")


if __name__ == "__main__":
    unittest.main()
