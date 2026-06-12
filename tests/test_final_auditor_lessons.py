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

    def test_dedup_before_cap_lets_new_lessons_commit(self):
        """Already-committed slugs must not occupy the cap window.

        lessons.jsonl is append-only across passes. With cap applied before
        slug-dedup, run-1's slugs at the head of the file permanently filled
        the cap and new lessons never committed on later passes.
        """
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            audit_dir = workspace / "audits" / "final"
            audit_dir.mkdir(parents=True)
            lessons = audit_dir / "lessons.jsonl"
            lessons.write_text(
                '{"slug":"first-pass","content":"lesson one"}\n'
                '{"slug":"second-pass","content":"lesson two"}\n'
            )
            conn = init_db(workspace / "sessions.db")

            # Pass 1: cap=1 (total_cycles=1) → only the head slug commits.
            run1 = _commit_lessons(workspace, conn, run_id="run-1", total_cycles=1)
            self.assertEqual([l["slug"] for l in run1], ["first-pass"])

            # Pass 2: same file (append-only, never reset). The committed
            # slug must be filtered out BEFORE the cap, so the new lesson
            # commits instead of being shadowed forever.
            run2 = _commit_lessons(workspace, conn, run_id="run-2", total_cycles=1)
            self.assertEqual([l["slug"] for l in run2], ["second-pass"])

            rows = conn.execute(
                "SELECT topic FROM sessions WHERE record_type = 'lesson' "
                "ORDER BY topic"
            ).fetchall()
            conn.close()
        self.assertEqual(
            [r[0] for r in rows], ["lesson/first-pass", "lesson/second-pass"]
        )

    def test_no_duplicate_commits_when_nothing_new(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            audit_dir = workspace / "audits" / "final"
            audit_dir.mkdir(parents=True)
            (audit_dir / "lessons.jsonl").write_text(
                '{"slug":"only","content":"lesson"}\n'
            )
            conn = init_db(workspace / "sessions.db")
            run1 = _commit_lessons(workspace, conn, run_id="run-1", total_cycles=1)
            run2 = _commit_lessons(workspace, conn, run_id="run-2", total_cycles=1)
            count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE record_type = 'lesson'"
            ).fetchone()[0]
            conn.close()
        self.assertEqual(len(run1), 1)
        self.assertEqual(run2, [])
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
