"""SQLite storage for session summaries with FTS5 search."""

import sqlite3
import time
from pathlib import Path


DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "auto-compact" / "sessions.db"

# How long a migration-race loser keeps re-probing for the winner's committed
# FTS schema before giving up (seconds), and how long it sleeps between probes.
_FTS_PROBE_RETRY_BUDGET_S = 30.0
_FTS_PROBE_RETRY_SLEEP_S = 0.5


def init_db(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Initialize the database, creating tables if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # timeout: block up to 30s for a write lock before raising, so brief
    # contention between concurrent sessions doesn't surface as errors.
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    # Enable WAL so multiple processes can read and write concurrently.
    # busy_timeout is the per-statement wait for a held lock; keep it at
    # 30s to match the connect timeout above (it overrides it otherwise).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id              TEXT PRIMARY KEY,
            parent_id       TEXT,
            depth           INTEGER DEFAULT 0,
            created_at      TEXT NOT NULL,
            summary_xml     TEXT NOT NULL,
            philosophy      TEXT,
            framework       TEXT,
            token_estimate  INTEGER,
            record_type     TEXT DEFAULT 'compaction',
            topic           TEXT,
            subtopic        TEXT,
            tools           TEXT,
            keywords        TEXT,
            fork_id         TEXT
        );
    """)

    # Migrate existing databases: add columns if missing
    for col, col_type in [
        ("philosophy", "TEXT"),
        ("framework", "TEXT"),
        ("token_estimate", "INTEGER"),
        ("record_type", "TEXT DEFAULT 'compaction'"),
        ("topic", "TEXT"),
        ("subtopic", "TEXT"),
        ("tools", "TEXT"),
        ("keywords", "TEXT"),
        ("fork_id", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Create indexes (safe after column migration)
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_sessions_topic
            ON sessions(topic);
        CREATE INDEX IF NOT EXISTS idx_sessions_topic_subtopic
            ON sessions(topic, subtopic);
    """)

    # Ensure FTS table includes catalog columns (rebuild if needed)
    _ensure_fts_with_catalog(conn)

    return conn


# FTS rebuild migration statements, executed inside one explicit
# transaction by _ensure_fts_with_catalog (executescript would
# autocommit per statement, racing concurrent processes).
_FTS_MIGRATION_STATEMENTS = [
    # Drop old triggers and FTS table
    "DROP TRIGGER IF EXISTS sessions_ai",
    "DROP TRIGGER IF EXISTS sessions_ad",
    "DROP TRIGGER IF EXISTS sessions_au",
    "DROP TABLE IF EXISTS sessions_fts",

    # Create FTS with catalog columns
    """CREATE VIRTUAL TABLE sessions_fts USING fts5(
        summary_xml, topic, subtopic, tools, keywords,
        content=sessions, content_rowid=rowid
    )""",

    # Populate from existing data
    """INSERT INTO sessions_fts(rowid, summary_xml, topic, subtopic, tools, keywords)
    SELECT rowid, summary_xml,
           COALESCE(topic, ''), COALESCE(subtopic, ''),
           COALESCE(tools, ''), COALESCE(keywords, '')
    FROM sessions""",

    # Triggers to keep FTS in sync
    """CREATE TRIGGER sessions_ai AFTER INSERT ON sessions BEGIN
        INSERT INTO sessions_fts(rowid, summary_xml, topic, subtopic, tools, keywords)
        VALUES (new.rowid, new.summary_xml,
                COALESCE(new.topic, ''), COALESCE(new.subtopic, ''),
                COALESCE(new.tools, ''), COALESCE(new.keywords, ''));
    END""",

    """CREATE TRIGGER sessions_ad AFTER DELETE ON sessions BEGIN
        INSERT INTO sessions_fts(sessions_fts, rowid, summary_xml, topic, subtopic, tools, keywords)
        VALUES ('delete', old.rowid, old.summary_xml,
                COALESCE(old.topic, ''), COALESCE(old.subtopic, ''),
                COALESCE(old.tools, ''), COALESCE(old.keywords, ''));
    END""",

    """CREATE TRIGGER sessions_au AFTER UPDATE ON sessions BEGIN
        INSERT INTO sessions_fts(sessions_fts, rowid, summary_xml, topic, subtopic, tools, keywords)
        VALUES ('delete', old.rowid, old.summary_xml,
                COALESCE(old.topic, ''), COALESCE(old.subtopic, ''),
                COALESCE(old.tools, ''), COALESCE(old.keywords, ''));
        INSERT INTO sessions_fts(rowid, summary_xml, topic, subtopic, tools, keywords)
        VALUES (new.rowid, new.summary_xml,
                COALESCE(new.topic, ''), COALESCE(new.subtopic, ''),
                COALESCE(new.tools, ''), COALESCE(new.keywords, ''));
    END""",
]


def _ensure_fts_with_catalog(conn: sqlite3.Connection) -> None:
    """Create or rebuild FTS table to include catalog columns."""
    # Check if FTS already has catalog columns
    try:
        conn.execute("SELECT topic FROM sessions_fts LIMIT 0")
        return  # Already up to date
    except sqlite3.OperationalError:
        pass  # Needs creation or rebuild

    # Run the rebuild as one BEGIN IMMEDIATE transaction so two
    # processes initializing concurrently (e.g. root run + MCP server)
    # serialize instead of interleaving autocommitted statements.
    if conn.in_transaction:
        conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for stmt in _FTS_MIGRATION_STATEMENTS:
            conn.execute(stmt)
        conn.commit()
    except sqlite3.OperationalError:
        # Lost the migration race ("database is locked" / "table ...
        # already exists"): roll back and re-probe — the winner's schema
        # is the one we want. The winner may not have COMMITTED yet, so a
        # single immediate probe can still see the old schema and raise;
        # retry in a short bounded loop instead of crashing init_db (and
        # with it e.g. the MCP server at startup).
        conn.rollback()
        deadline = time.monotonic() + _FTS_PROBE_RETRY_BUDGET_S
        while True:
            try:
                conn.execute("SELECT topic FROM sessions_fts LIMIT 0")
                return
            except sqlite3.OperationalError as probe_err:
                if time.monotonic() >= deadline:
                    raise sqlite3.OperationalError(
                        "FTS catalog migration contention: lost the "
                        "sessions_fts migration race and the winning "
                        "process did not commit the migrated schema "
                        f"within {_FTS_PROBE_RETRY_BUDGET_S:.0f}s "
                        f"(last probe error: {probe_err})"
                    ) from probe_err
                time.sleep(_FTS_PROBE_RETRY_SLEEP_S)


def store_session(
    conn: sqlite3.Connection,
    session_id: str,
    parent_id: str | None,
    depth: int,
    timestamp: str,
    summary_xml: str,
    philosophy: str | None = None,
    framework: str | None = None,
    token_estimate: int | None = None,
    record_type: str = "compaction",
    topic: str | None = None,
    subtopic: str | None = None,
    tools: str | None = None,
    keywords: str | None = None,
    fork_id: str | None = None,
) -> None:
    """Store a session summary.

    Args:
        record_type: "compaction" (context was reset) or "checkpoint"
            (mid-context snapshot, conversation continues).
        topic: Broad domain area (e.g. "api_client", "auth").
        subtopic: Specific focus (e.g. "retry_logic", "oauth_flow").
        tools: Comma-separated tools/libs used (e.g. "httpx, tenacity").
        keywords: Comma-separated freeform terms for search matching.
        fork_id: Fan-out fork identifier. None for root-authored sessions;
            set to the parallel_cycle_fanout UUID for gems/compactions
            produced inside a fan-out clone. Consumers can filter on this
            column (e.g. WHERE fork_id IS NULL) to separate branch work
            from mainline gems when scoring relevance.
    """
    conn.execute(
        "INSERT INTO sessions (id, parent_id, depth, created_at, summary_xml, "
        "philosophy, framework, token_estimate, record_type, "
        "topic, subtopic, tools, keywords, fork_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, parent_id, depth, timestamp, summary_xml, philosophy,
         framework, token_estimate, record_type, topic, subtopic, tools,
         keywords, fork_id),
    )
    conn.commit()


def search_sessions(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 5,
    record_type: str | None = None,
) -> list[dict]:
    """Search past sessions using FTS5. Returns matches ranked by relevance, recent first.

    Args:
        record_type: Optional filter — when set, only sessions with this
            record_type are returned. Backward-compatible default (None)
            preserves prior behavior. Plan 5 §2.3 introduces 'lesson' as a
            valid value alongside 'compaction' / 'checkpoint' / 'exploration'.
    """
    sql = (
        "SELECT s.id, s.parent_id, s.depth, s.created_at, s.summary_xml, "
        "s.topic, s.subtopic, s.tools, s.keywords, s.record_type "
        "FROM sessions_fts f "
        "JOIN sessions s ON s.rowid = f.rowid "
        "WHERE sessions_fts MATCH ?"
    )
    params: list = [query]
    if record_type is not None:
        sql += " AND s.record_type = ?"
        params.append(record_type)
    sql += " ORDER BY rank, s.created_at DESC LIMIT ?"
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # Raw model text is often invalid FTS5 syntax (apostrophes,
        # parens, bare operators). Retry once with the query escaped as
        # a quoted phrase; if that also fails, return no matches rather
        # than crashing the caller (REPL or MCP server).
        params[0] = '"' + query.replace('"', '""') + '"'
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
    return [dict(r) for r in rows]


def get_session_by_id(conn: sqlite3.Connection, session_id: str) -> dict | None:
    """Get a specific session by its ID."""
    row = conn.execute(
        "SELECT id, parent_id, depth, created_at, summary_xml, "
        "philosophy, framework, token_estimate, record_type, "
        "topic, subtopic, tools, keywords "
        "FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


def list_session_catalog(
    conn: sqlite3.Connection,
    topic_filter: str | None = None,
    tools_filter: str | None = None,
    limit: int = 25,
) -> list[dict]:
    """List sessions with catalog metadata. Compact table for browsing."""
    query = (
        "SELECT id, created_at, topic, subtopic, tools, keywords, "
        "substr(summary_xml, 1, 200) as preview "
        "FROM sessions WHERE 1=1"
    )
    params: list = []
    if topic_filter:
        query += " AND topic = ?"
        params.append(topic_filter)
    if tools_filter:
        query += " AND tools LIKE ?"
        params.append(f"%{tools_filter}%")
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_all_sessions_with_catalog(conn: sqlite3.Connection) -> list[dict]:
    """Get all sessions with catalog fields for scoring. Ordered by recency."""
    rows = conn.execute(
        "SELECT id, parent_id, depth, created_at, summary_xml, "
        "philosophy, framework, token_estimate, record_type, "
        "topic, subtopic, tools, keywords, fork_id "
        "FROM sessions ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_latest_session(conn: sqlite3.Connection) -> dict | None:
    """Get the most recent session summary."""
    row = conn.execute(
        "SELECT id, parent_id, depth, created_at, summary_xml, "
        "philosophy, framework, token_estimate, "
        "topic, subtopic, tools, keywords "
        "FROM sessions ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def count_sessions(conn: sqlite3.Connection) -> int:
    """Count total stored sessions."""
    return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
