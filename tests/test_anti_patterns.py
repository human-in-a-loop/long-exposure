import json
import tempfile
import unittest
from pathlib import Path

from long_exposure import anti_patterns


def ev(mid, status, ts, level="high", rationale="bad approach"):
    return {
        "event_id": ts,
        "ts": ts,
        "cycle": 3,
        "milestone_id": mid,
        "status": status,
        "confidence": {"level": level, "rationale": rationale},
    }


class AntiPatternsTests(unittest.TestCase):
    def test_missing_ledger_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(anti_patterns.build_block(Path(td)), "")

    def test_latest_event_must_still_be_invalidated(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            rows = [
                ev("M-1", "invalidated", "2026-05-01T00:00:01"),
                ev("M-1", "validated", "2026-05-01T00:00:02"),
                ev("M-2", "invalidated", "2026-05-01T00:00:03", rationale="x < y"),
                ev("M-3", "invalidated", "2026-05-01T00:00:04", level="low"),
            ]
            (ws / "promise_ledger.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n"
            )
            block = anti_patterns.build_block(ws)
        self.assertIn("M-2", block)
        self.assertIn("x &lt; y", block)
        self.assertNotIn("M-1", block)
        self.assertNotIn("M-3", block)

    def test_caps_entries_and_rationale(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            rows = [
                ev(f"M-{i}", "invalidated", f"2026-05-01T00:00:{i:02d}", rationale="x" * 100)
                for i in range(10)
            ]
            (ws / "promise_ledger.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n"
            )
            block = anti_patterns.build_block(ws, max_entries=2, max_rationale_chars=10)
        self.assertIn('count="2"', block)
        self.assertIn("xxxxxxx...", block)


if __name__ == "__main__":
    unittest.main()
