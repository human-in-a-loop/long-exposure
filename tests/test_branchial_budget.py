import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from auto_compact.db import init_db, store_session
from long_exposure.branchial_budget import score_branches


class BranchialBudgetTests(unittest.TestCase):
    def test_missing_db_returns_unknown_annotations(self):
        anns = score_branches([{"objective": "new topic"}], "/tmp/missing-budget.db")
        self.assertEqual(anns[0]["novelty_class"], "unknown")

    def test_scores_retread_and_novel_objectives(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "sessions.db"
            conn = init_db(db_path)
            base = datetime(2026, 5, 1, tzinfo=timezone.utc)
            store_session(
                conn,
                session_id="prior",
                parent_id=None,
                depth=1,
                timestamp=base.isoformat(),
                summary_xml="<summary/>",
                record_type="exploration",
                topic="spectral gap proof",
                subtopic="trace formula",
                keywords="eigenvalue trace formula",
            )
            store_session(
                conn,
                session_id="other",
                parent_id=None,
                depth=2,
                timestamp=(base + timedelta(minutes=1)).isoformat(),
                summary_xml="<summary/>",
                record_type="exploration",
                topic="latex rendering",
                subtopic="references",
                keywords="pandoc tectonic",
            )
            conn.close()

            anns = score_branches([
                {"objective": "spectral gap proof via trace formula eigenvalue analysis"},
                {"objective": "design a browser interface for annotation review"},
            ], db_path)

        self.assertEqual(anns[0]["novelty_class"], "likely-retread")
        self.assertIn("prior", anns[0]["matched_session_ids"])
        self.assertEqual(anns[1]["novelty_class"], "novel")


if __name__ == "__main__":
    unittest.main()
