import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from long_exposure.tools import promise_check


def _event(event_id, *, ts="2026-05-17T00:00:00+00:00", milestone_id="M1"):
    return {
        "event_id": event_id,
        "ts": ts,
        "run_id": "run-test",
        "cycle": 1,
        "agent": "worker",
        "milestone_id": milestone_id,
        "status": "validated",
        "confidence": {
            "level": "high",
            "rationale": "test",
            "assessor": "worker",
        },
        "narrative": "test event",
    }


def _write_ledger(ws: Path, rows: list[dict]) -> list[str]:
    lines = [json.dumps(row, separators=(",", ":")) for row in rows]
    (ws / "promise_ledger.jsonl").write_text("\n".join(lines) + "\n")
    return lines


class PromiseCheckExceptionTests(unittest.TestCase):
    def test_exact_fingerprint_exception_suppresses_only_named_bad_uuid(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            rows = [_event("historical-bad-id")]
            lines = _write_ledger(ws, rows)
            (ws / "reports").mkdir()
            (ws / "reports" / "promise_check_immutable_exceptions.json").write_text(
                json.dumps(
                    {
                        "exceptions": [
                            {
                                "line": 1,
                                "event_id": "historical-bad-id",
                                "ts": rows[0]["ts"],
                                "milestone_id": rows[0]["milestone_id"],
                                "raw_sha256": hashlib.sha256(lines[0].encode()).hexdigest(),
                                "error": "event_id is not a valid UUID",
                            }
                        ]
                    }
                )
            )

            findings = promise_check.run(ws)

        self.assertFalse(findings.errors)
        self.assertIn(
            "immutable exception consumed for ledger:line 1: event_id is not a valid UUID",
            findings.notes,
        )

    def test_exception_does_not_mask_unrelated_bad_uuid(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            rows = [_event("historical-bad-id"), _event("new-bad-id")]
            lines = _write_ledger(ws, rows)
            (ws / "reports").mkdir()
            (ws / "reports" / "promise_check_immutable_exceptions.json").write_text(
                json.dumps(
                    {
                        "exceptions": [
                            {
                                "line": 1,
                                "event_id": "historical-bad-id",
                                "ts": rows[0]["ts"],
                                "milestone_id": rows[0]["milestone_id"],
                                "raw_sha256": hashlib.sha256(lines[0].encode()).hexdigest(),
                                "error": "event_id is not a valid UUID",
                            }
                        ]
                    }
                )
            )

            findings = promise_check.run(ws)

        self.assertEqual(
            findings.errors,
            ["ledger:line 2: event_id is not a valid UUID"],
        )

    def test_exception_requires_exact_fingerprint(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            rows = [_event("historical-bad-id")]
            _write_ledger(ws, rows)
            (ws / "reports").mkdir()
            (ws / "reports" / "promise_check_immutable_exceptions.json").write_text(
                json.dumps(
                    {
                        "exceptions": [
                            {
                                "line": 1,
                                "event_id": "historical-bad-id",
                                "ts": rows[0]["ts"],
                                "milestone_id": rows[0]["milestone_id"],
                                "raw_sha256": "0" * 64,
                                "error": "event_id is not a valid UUID",
                            }
                        ]
                    }
                )
            )

            findings = promise_check.run(ws)

        self.assertEqual(
            findings.errors,
            ["ledger:line 1: event_id is not a valid UUID"],
        )

    def test_list_supersedes_is_accepted_but_non_string_refs_are_not(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            rows = [
                _event("00000000-0000-4000-8000-000000000001", milestone_id="_plan/a"),
                _event("00000000-0000-4000-8000-000000000002", milestone_id="_plan/b"),
                _event("00000000-0000-4000-8000-000000000003", milestone_id="_plan/c"),
            ]
            rows[1]["supersedes"] = ["00000000-0000-4000-8000-000000000001"]
            rows[2]["supersedes"] = ["00000000-0000-4000-8000-000000000001", 12]
            _write_ledger(ws, rows)

            findings = promise_check.run(ws)

        self.assertEqual(
            findings.errors,
            ["ledger:line 3: supersedes must be a string or list of strings"],
        )


if __name__ == "__main__":
    unittest.main()
