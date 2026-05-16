import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from auto_compact.db import init_db, store_session
from long_exposure.branchial import compute_branchial_signal


class BranchialSignalTests(unittest.TestCase):
    def test_missing_db_returns_none(self):
        self.assertIsNone(compute_branchial_signal("/tmp/does-not-exist-branchial.db"))

    def test_cold_db_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "sessions.db"
            conn = init_db(db_path)
            store_session(
                conn,
                session_id="s1",
                parent_id=None,
                depth=1,
                timestamp=datetime.now(timezone.utc).isoformat(),
                summary_xml="<summary/>",
                record_type="exploration",
                topic="alpha",
                subtopic="one",
            )
            conn.close()
            self.assertIsNone(compute_branchial_signal(db_path, min_cycles=2))

    def test_repeated_recent_catalog_classifies_collapsed(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "sessions.db"
            conn = init_db(db_path)
            base = datetime(2026, 5, 1, tzinfo=timezone.utc)
            for i in range(20):
                topic = f"topic-{i % 10}" if i < 10 else "fixed"
                subtopic = f"sub-{i % 10}" if i < 10 else "same"
                store_session(
                    conn,
                    session_id=f"s{i}",
                    parent_id=None,
                    depth=i,
                    timestamp=(base + timedelta(minutes=i)).isoformat(),
                    summary_xml="<summary/>",
                    record_type="exploration",
                    topic=topic,
                    subtopic=subtopic,
                )
            conn.close()

            signal = compute_branchial_signal(db_path, window=10, min_cycles=10)

        self.assertIsNotNone(signal)
        self.assertEqual(signal["classification"], "collapsed")
        self.assertEqual(signal["window_size"], 10)
        self.assertEqual(signal["top_recent_tuples"][0]["topic"], "fixed")


if __name__ == "__main__":
    unittest.main()
