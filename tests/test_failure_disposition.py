"""Name-based failure disposition in the cycle loop.

Stage 1 of docs/advanced-model-modes-plan.md. These tests pin the two
behaviours the refactor must preserve for today's fixed flows, and the two it
must get right once `loop.cycle_planning` makes the cycle tail variable.
"""

import unittest

from long_exposure.exploration import (
    FAILURE_ABORT_CYCLE,
    FAILURE_AUDIT_FALLBACK,
    FAILURE_MARK_OUTPUTS,
    _failure_disposition,
)


class FailureDispositionTests(unittest.TestCase):
    # -- today's flows: behaviour must be unchanged ----------------------

    def test_standard_flow_matches_pre_refactor_behaviour(self):
        """[researcher, worker, auditor] — the shipped flow."""
        flow = ["researcher", "worker", "auditor"]
        expected = [
            FAILURE_ABORT_CYCLE,     # was: i == 0
            FAILURE_MARK_OUTPUTS,    # was: middle
            FAILURE_AUDIT_FALLBACK,  # was: i == len(flow) - 1
        ]
        got = [_failure_disposition(name, i) for i, name in enumerate(flow)]
        self.assertEqual(got, expected)

    def test_post_merge_worker_only_flow_aborts(self):
        """[worker] — the post-merge cycle. Pre-refactor, i == 0 won."""
        self.assertEqual(
            _failure_disposition("worker", 0), FAILURE_ABORT_CYCLE,
        )

    # -- variable tails: what the refactor exists for --------------------

    def test_failed_worker_last_in_tail_is_not_given_the_audit_fallback(self):
        """The bug the refactor fixes.

        With a planned tail ending in a worker, the pre-refactor
        `i == len(flow) - 1` test would classify the worker's failure as an
        audit failure and write FALLBACK_AUDIT into results["audit_report"],
        inventing an audit that never ran.
        """
        flow = ["researcher", "worker", "worker"]
        self.assertEqual(
            _failure_disposition(flow[-1], len(flow) - 1),
            FAILURE_MARK_OUTPUTS,
        )

    def test_failed_auditor_not_last_still_gets_the_fallback(self):
        """The mirror-image bug.

        An auditor scheduled before another turn would have been treated as
        an ordinary mid-flow failure, leaving the next cycle's researcher
        with a stale audit_report instead of the fallback.
        """
        self.assertEqual(
            _failure_disposition("auditor", 1), FAILURE_AUDIT_FALLBACK,
        )

    def test_worker_chain_middle_turn_marks_outputs(self):
        flow = ["researcher", "worker", "worker", "auditor"]
        self.assertEqual(
            _failure_disposition(flow[2], 2), FAILURE_MARK_OUTPUTS,
        )

    def test_researcher_aborts_regardless_of_index(self):
        """Role wins over position: a researcher is never a mid-flow failure."""
        for i in (0, 1, 5):
            self.assertEqual(
                _failure_disposition("researcher", i), FAILURE_ABORT_CYCLE,
            )

    def test_auditor_at_index_zero_gets_the_fallback_not_an_abort(self):
        """An auditor-only tail: the name check must precede the index check."""
        self.assertEqual(
            _failure_disposition("auditor", 0), FAILURE_AUDIT_FALLBACK,
        )

    def test_unknown_agent_name_falls_through_to_marking(self):
        """A score with a custom mid-flow role must not crash the loop."""
        self.assertEqual(
            _failure_disposition("verifier", 2), FAILURE_MARK_OUTPUTS,
        )


if __name__ == "__main__":
    unittest.main()
