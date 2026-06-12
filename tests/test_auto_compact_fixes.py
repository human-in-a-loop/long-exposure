"""Tests for auto_compact audited fixes: FTS5 query fallback, busy_timeout,
atomic FTS migration, and truncated-summary retry."""

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from auto_compact import db
from auto_compact.compact import generate_summary
from auto_compact.db import init_db, search_sessions, store_session


def _store(conn, session_id, summary, **kwargs):
    store_session(conn, session_id, None, 0, "2026-01-01T00:00:00", summary, **kwargs)


class Fts5QueryFallbackTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.conn = init_db(Path(self._td.name) / "sessions.db")
        self.addCleanup(self.conn.close)

    def test_apostrophe_query_falls_back_to_phrase_and_matches(self):
        _store(self.conn, "s1", "the alpha's value was measured carefully")
        results = search_sessions(self.conn, "alpha's")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "s1")

    def test_unbalanced_paren_does_not_raise(self):
        _store(self.conn, "s1", "alpha beta gamma")
        results = search_sessions(self.conn, "(alpha")
        self.assertIsInstance(results, list)

    def test_hostile_queries_never_raise(self):
        _store(self.conn, "s1", "alpha beta gamma")
        hostile = [
            "don't (crash",
            "AND OR NOT",
            '"',
            "",
            "NEAR(",
            "a*b(c'd\"e",
            "col:value)",
        ]
        for q in hostile:
            with self.subTest(query=q):
                results = search_sessions(self.conn, q)
                self.assertIsInstance(results, list)

    def test_valid_query_unaffected(self):
        _store(self.conn, "s1", "retry logic for the api client")
        results = search_sessions(self.conn, "retry logic")
        self.assertEqual(len(results), 1)


class BusyTimeoutTests(unittest.TestCase):
    def test_busy_timeout_matches_documented_30s(self):
        with tempfile.TemporaryDirectory() as td:
            conn = init_db(Path(td) / "sessions.db")
            try:
                value = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            finally:
                conn.close()
        self.assertEqual(value, 30000)


def _make_old_schema_db(path: Path) -> None:
    """Pre-catalog schema: sessions without catalog columns, old FTS."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE sessions (
            id              TEXT PRIMARY KEY,
            parent_id       TEXT,
            depth           INTEGER DEFAULT 0,
            created_at      TEXT NOT NULL,
            summary_xml     TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE sessions_fts USING fts5(
            summary_xml, content=sessions, content_rowid=rowid
        );
        INSERT INTO sessions (id, parent_id, depth, created_at, summary_xml)
        VALUES ('old-1', NULL, 0, '2026-01-01T00:00:00', 'legacy alpha summary');
        INSERT INTO sessions_fts(rowid, summary_xml)
        SELECT rowid, summary_xml FROM sessions;
    """)
    conn.commit()
    conn.close()


class FtsMigrationTests(unittest.TestCase):
    def test_sequential_init_db_on_old_schema(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sessions.db"
            _make_old_schema_db(path)

            conn1 = init_db(path)
            conn2 = init_db(path)  # second process probing after migration
            try:
                for conn in (conn1, conn2):
                    # FTS now has catalog columns and old data is searchable
                    conn.execute("SELECT topic FROM sessions_fts LIMIT 0")
                    results = search_sessions(conn, "alpha")
                    self.assertEqual([r["id"] for r in results], ["old-1"])
            finally:
                conn1.close()
                conn2.close()

    def test_migration_loser_reprobes_instead_of_raising(self):
        """Simulate losing the BEGIN IMMEDIATE race: the winner migrates
        out-of-band, the loser gets 'database is locked' and must re-probe."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sessions.db"
            _make_old_schema_db(path)

            class LoserConnection(sqlite3.Connection):
                _raced = False

                def execute(self, sql, *parameters):
                    if (
                        isinstance(sql, str)
                        and sql.strip().upper().startswith("BEGIN IMMEDIATE")
                        and not self._raced
                    ):
                        self._raced = True
                        winner = init_db(path)  # winner completes migration
                        winner.close()
                        raise sqlite3.OperationalError("database is locked")
                    return super().execute(sql, *parameters)

            conn = sqlite3.connect(str(path), factory=LoserConnection)
            conn.row_factory = sqlite3.Row
            try:
                db._ensure_fts_with_catalog(conn)  # must not raise
                self.assertTrue(conn._raced)
                conn.execute("SELECT topic FROM sessions_fts LIMIT 0")
                results = search_sessions(conn, "alpha")
                self.assertEqual([r["id"] for r in results], ["old-1"])
            finally:
                conn.close()

    def test_loser_probe_retries_until_winner_commits(self):
        """The loser's re-probe can fire BEFORE the winner commits: the first
        probe still sees the old schema and raises. The loser must retry in a
        bounded loop instead of letting OperationalError escape init_db."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sessions.db"
            _make_old_schema_db(path)

            class SlowWinnerConnection(sqlite3.Connection):
                _raced = False
                _probe_state = 0  # 0=not committed, 1=failed once, 2=committed

                def execute(self, sql, *parameters):
                    s = sql.strip().upper() if isinstance(sql, str) else ""
                    if s.startswith("BEGIN IMMEDIATE") and not type(self)._raced:
                        type(self)._raced = True
                        raise sqlite3.OperationalError("database is locked")
                    if type(self)._raced and s.startswith(
                        "SELECT TOPIC FROM SESSIONS_FTS"
                    ):
                        if type(self)._probe_state == 0:
                            # Winner has not committed yet — first re-probe
                            # fails just like the real race.
                            type(self)._probe_state = 1
                            raise sqlite3.OperationalError(
                                "no such column: topic"
                            )
                        if type(self)._probe_state == 1:
                            # Winner commits before the second probe.
                            type(self)._probe_state = 2
                            winner = init_db(path)
                            winner.close()
                    return super().execute(sql, *parameters)

            conn = sqlite3.connect(str(path), factory=SlowWinnerConnection)
            conn.row_factory = sqlite3.Row
            try:
                with mock.patch.object(db.time, "sleep") as fake_sleep:
                    db._ensure_fts_with_catalog(conn)  # must not raise
                self.assertTrue(fake_sleep.called)  # it actually retried
                self.assertEqual(SlowWinnerConnection._probe_state, 2)
                conn.execute("SELECT topic FROM sessions_fts LIMIT 0")
                results = search_sessions(conn, "alpha")
                self.assertEqual([r["id"] for r in results], ["old-1"])
            finally:
                conn.close()

    def test_loser_probe_budget_exhaustion_raises_named_error(self):
        """If the winner never commits within the budget, the loser raises a
        clear error naming the migration contention (not a bare schema error)."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sessions.db"
            _make_old_schema_db(path)

            class StuckWinnerConnection(sqlite3.Connection):
                _raced = False

                def execute(self, sql, *parameters):
                    s = sql.strip().upper() if isinstance(sql, str) else ""
                    if s.startswith("BEGIN IMMEDIATE") and not type(self)._raced:
                        type(self)._raced = True
                        raise sqlite3.OperationalError("database is locked")
                    return super().execute(sql, *parameters)

            conn = sqlite3.connect(str(path), factory=StuckWinnerConnection)
            conn.row_factory = sqlite3.Row
            try:
                with mock.patch.object(db, "_FTS_PROBE_RETRY_BUDGET_S", 0.0):
                    with self.assertRaises(sqlite3.OperationalError) as ctx:
                        db._ensure_fts_with_catalog(conn)
                self.assertIn("migration contention", str(ctx.exception))
            finally:
                conn.close()


class _FakeResponse:
    def __init__(self, stop_reason, text="<session_summary/>", output_tokens=10):
        self.stop_reason = stop_reason
        self.content = [SimpleNamespace(text=text)]
        self.usage = SimpleNamespace(output_tokens=output_tokens)


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class TruncatedSummaryRetryTests(unittest.TestCase):
    def test_no_truncation_single_call(self):
        client = _FakeClient([_FakeResponse("end_turn", text="<ok/>")])
        _, _, summary_xml, _, _ = generate_summary(
            client, "model-x", [{"role": "user", "content": "hi"}], None, 0
        )
        self.assertEqual(summary_xml, "<ok/>")
        self.assertEqual(len(client.calls), 1)

    def test_truncation_retries_once_with_doubled_budget(self):
        # Truncated AND missing a closed <catalog> → the retry is justified.
        client = _FakeClient([
            _FakeResponse("max_tokens", text="<cut"),
            _FakeResponse("end_turn", text="<full/>"),
        ])
        _, _, summary_xml, _, _ = generate_summary(
            client, "model-x", [{"role": "user", "content": "hi"}], None, 0,
            max_tokens=4096,
        )
        self.assertEqual(summary_xml, "<full/>")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0]["max_tokens"], 4096)
        self.assertEqual(client.calls[1]["max_tokens"], 8192)

    def test_double_truncation_warns_and_proceeds(self):
        client = _FakeClient([
            _FakeResponse("max_tokens", text="<cut1"),
            _FakeResponse("max_tokens", text="<cut2"),
        ])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            _, _, summary_xml, _, _ = generate_summary(
                client, "model-x", [{"role": "user", "content": "hi"}], None, 0,
                max_tokens=4096,
            )
        self.assertEqual(summary_xml, "<cut2")  # graceful: stores what we got
        self.assertEqual(len(client.calls), 2)
        self.assertIn("WARNING", stderr.getvalue())
        self.assertIn("truncated", stderr.getvalue())

    def test_truncation_with_complete_catalog_skips_retry(self):
        """The retry exists to recover the trailing <catalog>. If the
        truncated response already closed it, retrying would re-bill the
        full conversation for nothing — accept the response with a warning."""
        truncated_but_cataloged = (
            "<session_summary><catalog><topic>api_client</topic></catalog>"
            "trailing prose that got cut of"
        )
        client = _FakeClient([
            _FakeResponse("max_tokens", text=truncated_but_cataloged),
        ])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            _, _, summary_xml, _, _ = generate_summary(
                client, "model-x", [{"role": "user", "content": "hi"}], None, 0,
                max_tokens=4096,
            )
        self.assertEqual(summary_xml, truncated_but_cataloged)
        self.assertEqual(len(client.calls), 1)  # no re-billed retry
        self.assertIn("truncated", stderr.getvalue())
        self.assertIn("accepting without retry", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
