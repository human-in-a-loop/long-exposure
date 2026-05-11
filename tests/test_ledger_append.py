import json
import tempfile
import unittest
from pathlib import Path

from long_exposure.tools import ledger_append


def _valid_event() -> dict:
    return {
        "event_id": "00000000-0000-4000-8000-000000000001",
        "ts": "2026-05-11T00:00:00+00:00",
        "run_id": "run-test",
        "cycle": 1,
        "agent": "worker",
        "milestone_id": "F-1",
        "status": "validated",
        "confidence": {
            "level": "high",
            "rationale": "test",
            "assessor": "worker",
        },
        "narrative": "validated test artifact",
        "artifacts": ["reports/test.md"],
    }


class LedgerAppendTests(unittest.TestCase):
    def test_invalid_event_is_rejected_before_append(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rc = ledger_append.main([
                "--workspace",
                str(root),
                "--event",
                json.dumps({"status": "validated"}),
            ])

            self.assertEqual(rc, 2)
            self.assertFalse((root / "promise_ledger.jsonl").exists())

    def test_valid_event_appends(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            event = _valid_event()
            rc = ledger_append.main([
                "--workspace",
                str(root),
                "--event",
                json.dumps(event),
            ])

            self.assertEqual(rc, 0)
            lines = (root / "promise_ledger.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["event_id"], event["event_id"])


if __name__ == "__main__":
    unittest.main()
