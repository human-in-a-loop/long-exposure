import sqlite3
import tempfile
import unittest
from pathlib import Path

from auto_compact.db import init_db
from long_exposure.lemmas import (
    MAX_LEMMAS_PER_OUTPUT,
    extract_and_store_lemmas,
    parse_lemma_blocks,
)


class LemmaTests(unittest.TestCase):
    def test_parse_valid_and_drop_invalid_blocks(self):
        text = """
<lemma_proposal category="env_quirk" label="pandoc">
  <claim>Pandoc needs this flag.</claim>
  <evidence>scripts/render.py</evidence>
  <confidence>high</confidence>
</lemma_proposal>
<lemma_proposal category="research_result" label="bad">
  <claim>not infrastructure</claim>
</lemma_proposal>
<lemma_proposal category="data_format" label="">
  <claim>missing label</claim>
</lemma_proposal>
"""
        parsed = parse_lemma_blocks(text)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["category"], "env_quirk")
        self.assertEqual(parsed[0]["confidence"], "high")

    def test_parse_caps_runaway_output(self):
        block = (
            '<lemma_proposal category="failed_attempt" label="x{0}">'
            "<claim>claim</claim></lemma_proposal>"
        )
        parsed = parse_lemma_blocks("\n".join(block.format(i) for i in range(20)))
        self.assertEqual(len(parsed), MAX_LEMMAS_PER_OUTPUT)

    def test_extract_and_store_lemmas(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "sessions.db"
            conn = init_db(db_path)
            stored = extract_and_store_lemmas(
                '<lemma_proposal category="tool_invocation" label="gap">'
                "<claim>Use gap -q for batch mode.</claim>"
                "</lemma_proposal>",
                conn,
            )
            rows = conn.execute(
                "SELECT record_type, topic, subtopic FROM sessions"
            ).fetchall()
            conn.close()

        self.assertEqual(stored, 1)
        self.assertEqual(rows[0]["record_type"], "lemma")
        self.assertEqual(rows[0]["topic"], "lemma_tool_invocation")
        self.assertEqual(rows[0]["subtopic"], "gap")

    def test_stored_lemma_summary_escapes_xml_content(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "sessions.db"
            conn = init_db(db_path)
            stored = extract_and_store_lemmas(
                '<lemma_proposal category="data_format" label="a&b">'
                "<claim>Use x < y & z.</claim>"
                "<evidence>docs/a&b.md</evidence>"
                "</lemma_proposal>",
                conn,
            )
            row = conn.execute("SELECT summary_xml FROM sessions").fetchone()
            conn.close()

        self.assertEqual(stored, 1)
        self.assertIn('label="a&amp;b"', row["summary_xml"])
        self.assertIn("Use x &lt; y &amp; z.", row["summary_xml"])
        self.assertIn("docs/a&amp;b.md", row["summary_xml"])


if __name__ == "__main__":
    unittest.main()
