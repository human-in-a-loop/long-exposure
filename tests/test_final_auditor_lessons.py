import tempfile
import unittest
from pathlib import Path

from auto_compact.db import init_db
from long_exposure.auditing import _commit_lessons


class FinalAuditorLessonTests(unittest.TestCase):
    def test_commit_lessons_accepts_list_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            audit_dir = workspace / "audits" / "final"
            audit_dir.mkdir(parents=True)
            (audit_dir / "lessons.jsonl").write_text(
                '{"slug":"list-meta","content":"lesson body",'
                '"keywords":["audit","lesson"],'
                '"subtopic":"metadata",'
                '"tools":["promise_check","org_check"]}\n'
            )
            conn = init_db(workspace / "sessions.db")

            committed = _commit_lessons(
                workspace,
                conn,
                run_id="run-test",
                total_cycles=1,
            )
            row = conn.execute(
                "SELECT topic, tools, keywords FROM sessions "
                "WHERE record_type = 'lesson'"
            ).fetchone()
            conn.close()

        self.assertEqual(len(committed), 1)
        self.assertEqual(row[0], "lesson/list-meta")
        self.assertEqual(row[1], "promise_check, org_check")
        self.assertEqual(row[2], "audit, lesson")


if __name__ == "__main__":
    unittest.main()
