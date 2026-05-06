# Persistence and Gems

How long-exposure preserves context across cycles, compactions, stops,
and resumes. This doc covers the `sessions.db` schema, the auto-compact
mechanism, the depth-aware XML summaries, gem ranking, fork-scoped gems
(Stage 4), and the MCP search server agents use to query the past.

**Sources of truth:** `auto_compact/db.py`, `auto_compact/compact.py`,
`auto_compact/proximity.py`, `long_exposure/orchestrator.py` (compaction
integration), `long_exposure/exploration.py` (cycle write path),
`long_exposure/mcp_search_server.py`.

---

## sessions.db — single source of truth

Every agent output, every compaction summary, and every cross-run
lesson lands here. The DB is shared across cycles, across instances
(when run with `--instance-dir` separation), and across fan-out clones.

**Path:** `long_exposure/data/sessions.db` by default
(`config.compact_db`). Configurable.

### Schema

One main table plus an FTS5 index:

```sql
CREATE TABLE sessions (
    id              TEXT PRIMARY KEY,        -- UUIDv4
    parent_id       TEXT,                    -- chains compactions into a tree
    depth           INTEGER DEFAULT 0,       -- compaction generation
    created_at      TEXT NOT NULL,           -- ISO8601 UTC
    summary_xml     TEXT NOT NULL,           -- the full payload
    philosophy      TEXT,
    framework       TEXT,
    token_estimate  INTEGER,
    record_type     TEXT DEFAULT 'compaction',
    topic           TEXT,                    -- catalog
    subtopic        TEXT,                    -- catalog
    tools           TEXT,                    -- catalog (CSV)
    keywords        TEXT,                    -- catalog (CSV)
    fork_id         TEXT                     -- NULL for root, UUID inside clones
);

CREATE VIRTUAL TABLE sessions_fts USING fts5(
    summary_xml, topic, subtopic, tools, keywords,
    content=sessions, content_rowid=rowid
);
```

Plus catalog indexes on `(topic)` and `(topic, subtopic)`.

### `record_type` values

| Value | Written when | Used for |
|---|---|---|
| `exploration` | per-cycle agent output (researcher / worker / auditor) | Cycle history; rarely queried directly except via FTS |
| `compaction` | auto-compact fires (90% context threshold) | Bootstrap on next session resume; gem ranking |
| `checkpoint` | mid-context snapshot without context reset | Observability; not load-bearing |
| `lesson` | final auditor's document stage emits a cross-run finding | Cross-run wisdom; +0.3 gem boost; immune from recency decay for ~30 days |

### Concurrency model

- **WAL journal mode** + `busy_timeout=5000` + connection
  `timeout=30s` (`db.init_db`).
- One writer at a time, many concurrent readers. Writers serialise
  via the WAL lock; each compaction-write completes in ~10ms, so
  even at N=14 fan-out clones the worst case is ~140ms serialised.
- No application-level locking. The OS-level lock is sufficient at
  current scale.

### Schema migrations

`init_db()` runs `ALTER TABLE ... ADD COLUMN` with silent on-conflict
for each catalog column. Idempotent. Columns added since v1:
`philosophy`, `framework`, `token_estimate`, `record_type`, `topic`,
`subtopic`, `tools`, `keywords`, `fork_id`. No version row; missing
columns are detected by absence.D-adjacent
notes if you ever need to add another column.

---

## Auto-compact: the 90% trick

Each agent (researcher, worker, auditor, ...) maintains its own
Claude Code session UUID across cycles. Conversation length grows
each cycle. When token usage hits 90% of the active provider context
window, auto-compact fires. Claude defaults to
`context_window = 1_000_000` → 900k tokens. Codex defaults to
`codex_context_window = 400_000` → 360k tokens.

### The compaction cycle

1. The orchestrator builds a depth-aware summary system prompt
   (`build_summary_system_prompt`) that asks the model to produce a
   structured XML summary capturing objective, background, artifacts,
   decisions, working memory, active threads, completed items, and
   catalog metadata.
2. `claude -p` is invoked with that system prompt and the conversation
   so far as the user message.
3. The result is fence-stripped (`_strip_xml_fences`), guarded against
   empty payload (raises `ClaudeCliError` rather than store a blank
   summary), and parsed for well-formedness via
   `xml.etree.ElementTree.fromstring` with a synthetic-root wrap.
4. If parse fails, the orchestrator re-prompts the model up to
   `compact_xml_retries` times (default 5). After exhaustion, it
   stores the last attempt anyway and surfaces a
   `compaction_xml_unrecoverable` event to `health_events.jsonl`.
   The plaintext is still readable for resume bootstrap.
5. Catalog fields are extracted from `<catalog>` (topic, subtopic,
   tools, keywords) and stored alongside the summary.
6. `store_session` writes the record to `sessions.db` with
   `record_type="compaction"`, `parent_id` chained to the previous
   summary, `depth` incremented.
7. The orchestrator clears the agent's session UUID; the next
   `claude -p` call for that agent gets a fresh session UUID,
   bootstrapped from the new summary.

### What survives a compaction

The fresh session is rebuilt from the four-layer prompt with the new
summary in layer 4. Conversation history is gone; everything important
is in the summary.

The summary system prompt explicitly says "It must not exceed ~15%
of the context window" — soft guidance only. There's no hard
truncation. Live evidence: 1821-token summaries at depth 2 against
a 1M context window is ~0.18% — well under budget.

### Depth-aware compression

The same prompt template emphasises deeper compression at higher
depths:

> At `depth >= 2`, aggressively drop `<completed>` items and compress
> `<decisions>` to outcomes only (no rationale for old settled
> decisions).

The `<working_memory>` block is preserved across all depths unless
explicitly invalidated. This keeps long runs from drifting into
summary bloat.

### XML well-formedness check

The check is intentionally **flexible, not schema-rigid**. It accepts:

- Single-root XML with optional `<?xml ... ?>` prologue (handled by
  direct parse).
- Multi-root content like `<context/><state/>` with no outer wrapper
  (handled by wrap-in-`<_root>` fallback; prologue stripped before
  wrap if present).
- Plain prose with no tags (passes — wrapped becomes valid character
  data; storing the prose still allows resume bootstrap).

It rejects:

- Empty / whitespace-only.
- Truly malformed XML (unclosed tags, mismatched tags, invalid
  entities).

The flexibility is per the design directive: prefer one false-positive
(storing unusual-but-readable XML) over a retry storm on a benign
declaration prologue.12b.

---

## Gems — proximity-ranked relevance

When a fresh session bootstraps, the orchestrator runs `_compute_gems`
to find the most relevant past sessions and inject pointers to them in
the system prompt as a `<context_gems>` block. Default: top 7
sessions, score floor 0.3.

### Scoring

`proximity.score_session(session, profile, current_catalog, now)`:

```
score = 0
+ topic_weights["_same_topic"]      if session.topic == current.topic
+ topic_weights["_same_subtopic"]   if also same subtopic
+ topic_weights["_any_topic"]       (base for every session)
+ topic_weights[session.topic]      (named topic boost / penalty)
+ tool_weights["_shared_tools"] × |session_tools ∩ current_tools|
+ keyword_weights[kw]               for each kw in session.keywords found
+ 0.3                               if record_type == "lesson"
* recency_decay
```

### Recency decay

Two regimes:

- **Standard records** (`exploration` / `compaction` / `checkpoint`):
  half-life 30 days. `recency = 1 / (1 + age_days / 30)`.
- **Lessons**: full immunity for the first 30 days of age (covers
  ~10 runs at typical cadence), then a much gentler 365-day half-life:
  `recency = 1 / (1 + (age_days - 30) / 365)`. Durable lessons stay
  ranked without ossifying.

### Profile

The relevance profile defaults are baked into the orchestrator's
config defaults. Score-level overrides are possible via
`config.proximity_profile`.

### Fork-scoped gems (Stage 4)

`rank_sessions(fork_scope=...)` filters by `fork_id`:

- `"all"` — no filtering. Default for root-authored agents.
- `"root_only"` — only sessions with `fork_id IS NULL`. Useful when
  the caller wants to ignore all clone history.
- `"same_fork"` — root sessions OR sessions with matching
  `current_fork_id`. The right default for clones: they see root
  context + their own history, but never sibling clones' work.

`_compute_gems` in `orchestrator.py:2629–2645` reads
`AGENT_FORK_ID` from the env to choose:

```python
fork_id = os.environ.get("AGENT_FORK_ID")
if fork_id:
    fork_scope = "same_fork"
    current_fork_id = fork_id
else:
    fork_scope = "all"
    current_fork_id = None
```

Fork-scoped gems is the only sessions.db change required to make
gems clean across fan-out clones. No schema migration; the `fork_id`
column was added preemptively when the fan-out feature shipped.

---

## MCP search server — agents query the past

Long-exposure runs an MCP server (`long_exposure/mcp_search_server.py`)
in stdio mode and configures every `claude -p` agent call to connect
to it. The server exposes three tools:

| Tool | What it does | Backend |
|---|---|---|
| `search_sessions(query, limit, record_type?)` | FTS5 query against `summary_xml + catalog` columns; returns full `summary_xml` per match | `sessions_fts MATCH ?` |
| `search_sessions_by_id(session_id)` | Fetch one session by UUID | `WHERE id = ?` |
| `list_session_catalog(topic?, tools?)` | Browse catalog metadata; returns id + headline fields | `WHERE topic = ? AND tools LIKE ?` |

All queries are parameterised — no SQL injection risk. `record_type`
filter on `search_sessions` lets agents narrow to lessons only,
compactions only, or all records.

### Scope: global, by design

The MCP tool is **not** fork-scoped. An agent's raw query sees all
sessions in the DB, including sibling clones' work. This is
intentional: gems are pre-filtered context for the prompt, but
ad-hoc agent queries should be able to reach across branches when the
agent decides to. Fork-scoped gems and global search complement each
other.

### How agents actually use it

In practice the most valuable usage is on resume — an agent that
recognises a topic from a prior cycle (via the gems block) can run
`search_sessions_by_id(<gem_id>)` to fetch the full prior summary
and re-orient. The role-text in the score YAML for each agent
mentions the search tools but doesn't prescribe usage; the agent
decides.

---

## What survives what

| Event | sessions.db | agent session UUID | gem ranking |
|---|---|---|---|
| Cycle boundary | preserved | preserved | recomputed each session-bootstrap |
| Auto-compact | summary added | new UUID | recomputed |
| `stop` / `resume` | preserved | preserved (in `exploration_state.json`) | recomputed |
| `clear` | preserved (DB is *not* cleared) | reset | recomputed |
| Account rotation (rate-limit OR planned 24h, Plan B) | preserved (single shared DB) | reset (per-account UUIDs cleared by both rotation paths) | recomputed |
| Fan-out clone spawn | preserved | inherited from parent at spawn (then preserved until clone's own compaction or rate-limit; clones never migrate accounts) | scoped via `same_fork` |
| `--from-archive` resume | preserved | restored from archive | recomputed |

Note that `clear` does NOT wipe `sessions.db`. The DB is the long-run
memory of every campaign on this machine. To start truly fresh, move
`sessions.db` aside.

---

## Operational rules

1. `sessions.db` is the single source of truth. Workspace files are
   produced from agent output, but the canonical record is the DB.
2. WAL + busy_timeout + 30s connection timeout is sufficient for
   N=14 fan-out without further locking.
3. Empty summaries fail loudly (`ClaudeCliError`). Malformed XML is
   retried up to 5 times then stored as-is with an off-nominal event.
4. Gems are recomputed at every session bootstrap — there's no cache.
5. Fork-scoped gems use the `AGENT_FORK_ID` env var; clones see only
   root + own history.
6. The MCP search tool is global by design; agents can cross fork
   boundaries when they explicitly query.
7. Schema migrations are silent `ALTER TABLE` calls; do not require a
   version row at current scale.

---

## Code citations

- Schema + WAL setup: `auto_compact/db.py:10–69`.
- `store_session`: `auto_compact/db.py:129–170`.
- FTS5 triggers: `db.py:102–125`.
- `compact_with_conditioning`: `orchestrator.py:2731–2790` (empty
  guard, retry loop, store).
- Strip + parse helpers: `orchestrator.py:_strip_xml_fences`,
  `_is_well_formed_xml`.
- `_compute_gems` + fork-scope wire-up: `orchestrator.py:2620–2646`.
- `rank_sessions` + fork-scope filter: `auto_compact/proximity.py:142–208`.
- `score_session` (the recency-decay formula): `proximity.py:30–105`.
- MCP server: `long_exposure/mcp_search_server.py`.
- Cycle write path (per-agent output stored): `exploration.py:1005–1034`
  (`_store_agent_output`).
- Compaction within cycle: `exploration.py:834–938`
  (`_compact_agent_session`).
