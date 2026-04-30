# Architecture Overview

**Audience:** new contributors and operators who want one document that
explains how long-exposure works end-to-end. Read this first; drill into
the subsystem-specific docs (parallelism, pool, persistence, end-of-run,
workspace) once you have the shape.

---

## What long-exposure is

A continuous research loop. You give it an ambiguous, large-scope
directive ("Explore foundations of microlocal analysis", "Reconstruct
the compliance trail for requirement REQ-3311") and a working
directory; it runs a three-role cycle (researcher → worker →
auditor) repeatedly, surviving context-window resets and rate-limit
events, until the topic is exhausted or you stop it. At end-of-run it
produces a consolidated final report, an audit summary, and a packaged
skill bundle.

The pitch in one line: **ambiguous research directive in, packaged
report and traceable record out.**

---

## Conceptual map

```
                       CLI: long-exposure {start|stop|resume|clear}
                                      │
                                      ▼
              ┌─────────────────────────────────────────────┐
              │ exploration.py — deterministic cycle loop   │
              │  (researcher → worker → auditor; reporter   │
              │   every N; daily-sync every 24h; signal     │
              │   polling at cycle boundary; rotation       │
              │   on rate-limit OR every 24h; auto-compact  │
              │   at 90%)                                   │
              └─────────────────────────────────────────────┘
                ▲                  │                       │
                │                  ▼                       │
                │       conductor.py / orchestrator.py     │
                │       (claude -p subprocess; assemble    │
                │        4-layer prompt; agent-teams       │
                │        within turn; rate-limit detect)   │
                │                  │                       │
        ┌───────┴────────┐         ▼                       ▼
        │ pool.py        │  auto_compact (db.py,    fanout.py
        │ (slot acquire/ │  compact.py, proximity)  (parallel-cycle
        │  release;      │   sessions.db (WAL,      fork; barrier;
        │  freshness     │   FTS5, gems ranking,    merge synthesis;
        │  promotion;    │   depth-aware XML        depth-1 gate)
        │  per-acct      │   summaries)
        │  cooldown)     │
        └────────────────┘
                                   │
                                   ▼
                        End-of-run / daily-sync:
                  final_auditor → final_reporter → curator
                  (reporting.py, auditing.py, curator.py;
                   wall-cap as safety floor; file-gate rescue;
                   ledger reconciliation; ZIP package)
```

---

## The four design principles

These are load-bearing. Most subsystem decisions follow from them.

1. **Deterministic Python drives control flow; the model decides only
   what to work on.** No agent decides when to compact, when to
   rotate accounts, when to fan out, when to sync. Those are timer-
   and signal-driven Python loops. The model influences three things
   only: (a) verdicts and topic strings (heuristically parsed), (b)
   optional `<parallel_cycle_fanout>` blocks (validated and dispatched
   by the conductor), (c) intra-turn agent-team spawn (Claude Code's
   own affordance, not custom code).

2. **Convention over enforcement.** The plan-of-record, promise ledger,
   workspace folder layout, frontmatter, figure discipline — all of
   these are soft-guidance to agents and validators that *surface*
   non-compliance. None are gates that block the cycle. A run with no
   plan-of-record still completes; the auditor just sees fewer
   structured signals.

3. **Graceful absence.** Removing any one subsystem (no language pool,
   no figures, no daily sync) degrades a feature but doesn't crash the
   run. Every read of a file the system might write is wrapped against
   `FileNotFoundError`; every JSON parse is wrapped against malformed
   input; every external tool (pandoc, tectonic) has a
   degraded fallback.

4. **Simple enough to scale to n=100.** When extending the architecture
   we prefer flat fan-out + clamping over schedulers, freshness-based
   promotion over priority queues, single-writer SQLite WAL over
   distributed locks, append-only JSONL over mutable indexes.

---

## The three-role cycle

Each cycle runs three agents sequentially. They share state through:
(a) `results` dict in Python memory (output names → values, see
`configuration-reference.md`), (b) `sessions.db` for persistent context
across cycles via auto-compact, (c) workspace files on disk (POR,
ledger, reports).

| Role | Philosophy | What it does | Output |
|---|---|---|---|
| **Researcher** | research | Reads prior audit report, decides next sub-topic, produces a research brief naming expected artifacts | `research_brief` |
| **Worker** | efficient | Reads brief, builds tools/scripts/plots/data, writes ledger events | `work_output` |
| **Auditor** | audit | Validates worker's output, runs `promise_check` and `org_check`, produces canonical confidence verdicts, decides VALIDATED / CONTINUE / PIVOT | `audit_report` |

The audit report feeds back to the researcher on the next cycle. There
is no meta-orchestrator agent. The conductor (`exploration.py`) is the
control loop.

### Reporter and end-of-run agents

These don't run every cycle. They have their own conditions:

- **Reporter** — every `loop.report_interval` cycles (default 3),
  consolidates the cycle range into `reports/report_cycles_NNN-MMM.md`
  and the matching PDF.
- **Final auditor + final reporter + curator** — run at end-of-run
  (topic exhaustion / max_cycles / stop) AND on daily-sync cadence
  (default every 24h). See `end-of-run-pipeline.md`.

---

## The four-layer prompt

Every `claude -p` call assembles its system prompt from four templates:

```
1. Philosophy     — voice, tradeoff posture, depth calibration
2. Framework      — stages, gates, transition rules
3. Protocol       — checkpoint discipline, budget tracking, anti-patterns
4. Session Summary — restored context after compaction (if resuming)
```

Each layer is a `.md` template in `long_exposure/templates/` with
`{variable}` placeholders. Variables come from preset dictionaries in
`orchestrator.py` (PHILOSOPHY_PRESETS, FRAMEWORK_PRESETS) and the
score's per-agent overrides. Layer 4 is empty on first call and gets
populated with the depth-aware compaction summary after the first
compaction.

The agent role block (`<agent-role>...</agent-role>`) and any
runtime-injected blocks (gems, agent-teams guidance, parallel-cycle
guidance, live operator guidance) are appended after layer 3 before
layer 4. See `configuration-reference.md` for the full assembly order.

---

## What survives a cycle, a compaction, a stop, and a clear

| Lifetime event | Conversation | `agent_sessions` UUIDs | `sessions.db` | Workspace files | POR + ledger |
|---|---|---|---|---|---|
| Cycle boundary | continues | preserved | preserved | preserved | preserved |
| Auto-compact (90% ctx) | reset | new UUID issued | summary persisted | preserved | preserved |
| `stop` / Ctrl-C | preserved (state file) | preserved | preserved | preserved | preserved |
| `resume` | restored | restored | preserved | preserved | preserved |
| `clear` | reset | reset | preserved | preserved | preserved |
| Account rotation (rate-limit OR planned 24h) | reset | new UUIDs (per-account) | preserved (shared) | preserved | preserved |

Two key invariants:

- `sessions.db` is the **single source of truth**. Every agent output
  lands there as `record_type="exploration"`; every compaction summary
  as `record_type="compaction"`; every cross-run lesson as
  `record_type="lesson"`. Workspace files are produced from agent
  output but the canonical record is the DB.
- `exploration_state.json` is **ephemeral**. It holds the current
  cycle, last results dict, agent session UUIDs, and daily-sync
  bookkeeping. Overwritten each cycle; archived with a timestamp on
  `clear`.

---

## Subsystem map: where each concern lives

| Concern | Module | Doc |
|---|---|---|
| Cycle loop, signal handling, lifecycle | `long_exposure/exploration.py` | this doc + `usage-guide.md` |
| `claude -p` subprocess, env handling, rate-limit detection | `long_exposure/orchestrator.py` | `multi-account-pool.md` |
| Score loading, agent prompt assembly | `long_exposure/conductor.py`, `orchestrator.py` | `configuration-reference.md` |
| Multi-account pool, slot lifecycle, freshness promotion | `long_exposure/pool.py` | `multi-account-pool.md` |
| Parallel-cycle fan-out, barrier, merge synthesis, graceful preemption | `long_exposure/fanout.py` | `parallelism.md` |
| Auto-compact, sessions.db schema, FTS5 search, depth-aware XML summaries | `auto_compact/{db,compact,proximity}.py` | `persistence-and-gems.md` |
| Reporter, final reporter, file-gate rescue, PDF render | `long_exposure/reporting.py` | `end-of-run-pipeline.md` |
| Final auditor, reconciliation events | `long_exposure/auditing.py` | `end-of-run-pipeline.md` |
| Curator, ZIP package | `long_exposure/curator.py` | `end-of-run-pipeline.md` |
| Workspace bootstrap, plan-of-record, ledger | `long_exposure/workspace_bootstrap.py`, `tools/` | `workspace-conventions.md` |
| MCP search server (exposes session search to agents) | `long_exposure/mcp_search_server.py` | `persistence-and-gems.md` |
| Off-nominal events log | `long_exposure/health_events.py` | (see source comments) |

---

## Where to read next

- **Operating it** → `usage-guide.md`
- **Setting up an environment** → `local-setup.md`
- **Configuring a run** → `configuration-reference.md`
- **Multi-account scaling** → `multi-account-pool.md`
- **Fan-out and agent teams** → `parallelism.md`
- **Context that survives compaction** → `persistence-and-gems.md`
- **What happens at end of run** → `end-of-run-pipeline.md`
- **Workspace structure, POR, ledger** → `workspace-conventions.md`
- **Figures as first-class deliverables** → `figures.md`
- **Soft-guidance philosophy** → `soft-guidance.md`
