"""The evidence gate: validated/high needs something real behind it.

Small on purpose. It is read-side (the ledger keeps the claim as written),
checks that evidence EXISTS rather than that it is good, and exempts
bookkeeping milestones. What it catches is the honest failure: a result
claimed from an output that was never written.
"""

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from long_exposure import workspace_bootstrap as wb

GATED = "medium (claimed high; no evidence found)"


def ev(mid="research/bound", status="validated", level="high", **extra):
    e = {"event_id": str(uuid.uuid4()), "ts": "2026-01-02T00:00:00Z",
         "run_id": "r", "cycle": 1, "agent": "auditor", "milestone_id": mid,
         "status": status, "narrative": "the bound is 3.2",
         "confidence": {"level": level, "rationale": "r", "assessor": "auditor"}}
    e.update(extra)
    return e


class GateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        (self.ws / "data").mkdir()
        (self.ws / "data/result.csv").write_text("x\n")

    def tearDown(self):
        self._tmp.cleanup()

    def summary_line(self, event):
        wb.append_ledger_event(self.ws, event)
        text = wb.summarize_ledger(self.ws)
        return next(l for l in text.splitlines()
                    if l.startswith(f"- [{event['milestone_id']}]"))

    # -- gated --

    def test_unevidenced_validated_high_is_shown_as_medium(self):
        line = self.summary_line(ev())
        self.assertIn(f"validated/{GATED}", line)

    def test_a_cited_file_that_does_not_exist_is_not_evidence(self):
        """The honest failure: the script crashed, the output was never written."""
        line = self.summary_line(ev(artifacts=["data/never_written.csv"]))
        self.assertIn(GATED, line)

    def test_a_path_outside_the_workspace_is_not_evidence(self):
        line = self.summary_line(ev(evidence=["../../../../etc/hosts"]))
        self.assertIn(GATED, line)

    def test_junk_evidence_fields_do_not_count_or_crash(self):
        for junk in ("data/result.csv", None, 42, {"a": 1}, [None, 3, ""], ["   "]):
            e = ev(milestone_id=f"research/{uuid.uuid4().hex[:6]}", evidence=junk)
            if isinstance(junk, str):
                # a bare string is not a list: not evidence, by the schema
                pass
            line = self.summary_line(e)
            self.assertIn(GATED, line, repr(junk))

    # -- not gated --

    def test_an_existing_produced_file_is_evidence(self):
        line = self.summary_line(ev(artifacts=["data/result.csv"]))
        self.assertIn("validated/high (", line)
        self.assertNotIn("claimed high", line)

    def test_a_leading_dot_slash_path_is_evidence(self):
        line = self.summary_line(ev(evidence=["./data/result.csv"]))
        self.assertNotIn("claimed high", line)

    def test_citing_a_prior_event_is_evidence(self):
        line = self.summary_line(ev(evidence=[str(uuid.uuid4())]))
        self.assertNotIn("claimed high", line)

    def test_bookkeeping_milestones_are_exempt(self):
        """A plan revision is validated by being decided; the score tells the
        researcher to log `_plan/<change>` events as validated. Gating them
        would downgrade every plan revision."""
        for mid in ("_plan/rescope", "_run/start", "_manager/directive",
                    "_archive/old", "_infra/tooling"):
            line = self.summary_line(ev(mid=mid))
            self.assertNotIn("claimed high", line, mid)

    def test_other_statuses_and_levels_are_untouched(self):
        for status, level in (("validated", "medium"), ("in-progress", "high"),
                              ("invalidated", "high"), ("validated", "low")):
            e = ev(mid=f"research/{status}-{level}", status=status, level=level)
            line = self.summary_line(e)
            self.assertIn(f"{status}/{level} (", line, (status, level))

    # -- the ledger itself --

    def test_the_ledger_keeps_the_claim_exactly_as_written(self):
        """Read-side only: the audit trail records what was claimed."""
        wb.append_ledger_event(self.ws, ev())
        wb.summarize_ledger(self.ws)
        row = json.loads((self.ws / "promise_ledger.jsonl").read_text().strip())
        self.assertEqual(row["confidence"]["level"], "high")

    def test_a_direct_append_is_gated_too(self):
        """Why read-side: the score lets agents write the ledger file directly,
        bypassing append_ledger_event. A write-side gate would miss this."""
        (self.ws / "promise_ledger.jsonl").write_text(json.dumps(ev()) + "\n")
        text = wb.summarize_ledger(self.ws)
        self.assertIn(GATED, text)

    def test_the_prompt_states_the_rule_the_gate_hardens(self):
        """House rule: a gate hardens a rule the prompt states; it must never
        be the only place the rule exists."""
        score = (Path(__file__).resolve().parent.parent
                 / "long_exposure" / "exploration-score.yaml").read_text()
        flat = " ".join(score.split())
        self.assertIn("`validated` at `high` confidence must cite what it rests on", flat)


if __name__ == "__main__":
    unittest.main()
