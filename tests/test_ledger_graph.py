import json
import tempfile
import unittest
from pathlib import Path

from long_exposure.tools import ledger_graph


def event(eid, mid, status, ts, evidence=None, confidence="high"):
    return {
        "event_id": eid,
        "ts": ts,
        "run_id": "run",
        "cycle": int(ts[-2:]),
        "agent": "tester",
        "milestone_id": mid,
        "status": status,
        "confidence": {"level": confidence, "rationale": "because"},
        "evidence": evidence or [],
        "artifacts": ["artifact.md"] if status == "validated" else [],
    }


class LedgerGraphTests(unittest.TestCase):
    def test_missing_ledger_returns_empty_summary(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(ledger_graph.build(Path(td)))
            self.assertEqual(ledger_graph.render_summary(None), "")

    def test_summary_renders_chain_and_evidence_density(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            rows = [
                event("00000000-0000-4000-8000-000000000001", "M-1", "in-progress", "2026-05-01T00:00:01"),
                event(
                    "00000000-0000-4000-8000-000000000002",
                    "M-1",
                    "validated",
                    "2026-05-01T00:00:02",
                    evidence=["00000000-0000-4000-8000-000000000001", "artifact.md"],
                ),
            ]
            (ws / "promise_ledger.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n"
            )
            summary = ledger_graph.render_summary(ledger_graph.build(ws))
        self.assertIn("M-1 (validated, high)", summary)
        self.assertIn("cycle 1: M-1 in-progress/high", summary)
        self.assertIn("artifact.md", summary)

    def test_contradictions_ignore_reserved_namespaces(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            rows = [
                event("00000000-0000-4000-8000-000000000001", "M-1", "validated", "2026-05-01T00:00:01"),
                event("00000000-0000-4000-8000-000000000002", "M-1", "invalidated", "2026-05-01T00:00:02"),
                event("00000000-0000-4000-8000-000000000003", "_manager/x", "validated", "2026-05-01T00:00:03"),
                event("00000000-0000-4000-8000-000000000004", "_manager/x", "invalidated", "2026-05-01T00:00:04"),
            ]
            (ws / "promise_ledger.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n"
            )
            graph = ledger_graph.build(ws)
        self.assertEqual(graph.contradiction_clusters(), [("M-1", [
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002",
        ])])

    def test_summary_truncates(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "promise_ledger.jsonl").write_text(
                json.dumps(event(
                    "00000000-0000-4000-8000-000000000001",
                    "M-1",
                    "validated",
                    "2026-05-01T00:00:01",
                )) + "\n"
            )
            summary = ledger_graph.render_summary(ledger_graph.build(ws), max_chars=40)
        self.assertIn("truncated", summary)

    def test_summary_handles_non_string_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            row = event(
                "00000000-0000-4000-8000-000000000001",
                "M-1",
                "validated",
                "2026-05-01T00:00:01",
            )
            row["artifacts"] = [{"path": "artifact.md"}]
            (ws / "promise_ledger.jsonl").write_text(json.dumps(row) + "\n")
            summary = ledger_graph.render_summary(ledger_graph.build(ws))
        self.assertIn("{'path': 'artifact.md'}", summary)


if __name__ == "__main__":
    unittest.main()
