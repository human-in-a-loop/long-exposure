# End-of-Run Pipeline

This doc covers everything that happens at end-of-run and on the
daily-sync cadence: the periodic reporter, the final auditor, the
final reporter, the curator, the cross-cutting lessons mechanism,
and the PDF rendering path. All of these run *outside* the normal
researcher → worker → auditor cycle.

**Sources of truth:** `long_exposure/reporting.py`, `long_exposure/auditing.py`,
`long_exposure/curator.py`, `long_exposure/exploration.py` (daily-sync
scheduling).

---

## Two invocation modes

The same pipeline of agents runs under two conditions:

| Mode | Trigger | Consumer of artifacts |
|---|---|---|
| **End-of-run** | topic exhausted (low-output streak), `max_cycles` hit, operator stop / Ctrl-C | The operator; the curator's ZIP is the final handoff |
| **Daily sync** (Stage 3) | wall-clock 24h since last sync (`loop.daily_sync_interval_hours`) at cycle boundary | The operator mid-campaign; provides continuously-updated artifacts so a multi-week run is inspectable without stopping |

Both modes run the same sequence: `final_auditor → final_reporter →
curator`. The agents detect mode implicitly via the presence of prior
artifacts (revise mode kicks in when the artifact already exists);
there is no explicit `--revise-mode` flag passed in.

The periodic reporter (every `loop.report_interval` cycles, default 3)
runs in addition. It is **not** part of the end-of-run sequence — it
runs mid-run on its own cadence to consolidate cycle ranges.

---

## Periodic reporter

Runs every `report_interval` cycles. Reads:

- The cycle range covered (e.g., cycles 7–9).
- A summary of cycle audit reports for that range, via session pointers
  in `sessions.db`.
- The current working_directory.

Writes:

- `<workspace>/reports/report_cycles_NNN-MMM.md`.
- `<workspace>/reports/report_cycles_NNN-MMM.pdf` via `render_pdf`.

The reporter's role text instructs it to write its content into the
workspace using its declared `Write` permissions; the harness
post-processes via the file-gate rescue pattern (see below) if the
agent's output landed in the `[OUTPUT]` block instead of on disk.

---

## Final auditor

Runs once at end-of-run and on every daily-sync. Verifies run-scoped
trajectory-vs-directive integrity — the gap that the per-cycle
auditor cannot close because it's scoped to a single cycle's work.

### Dynamic staging

The final auditor's stage count scales with input size:

```
N = max(1, input_tokens // 20_000)         # capped above by wall-cap
total_stages = 1 + N + N + 1
            = explore → verify×N → test×N → document
```

Same heuristic as the final reporter (the
formula was deliberately unified). For a typical 40-cycle run with
~120k tokens of inputs, N ≈ 6, giving ~14 stages.

The four canonical stage purposes:

| Stage | Purpose |
|---|---|
| **explore** (1) | Survey inputs, map milestones from POR + ledger to evidence |
| **verify** (×N) | Cross-check claims; each verify stage handles one slice of milestone evidence |
| **test** (×N) | Adversarial check; runs `promise_check` and `org_check` against the ledger; flags silent supersessions |
| **document** (1) | Compose the narrative + structured JSON; commit reconciliation events to ledger |

### Inputs

| Input | Source |
|---|---|
| `directive` | The original task |
| `plan_of_record` | `<workspace>/plan_of_record.md` (POR — see `workspace-conventions.md`) |
| `promise_ledger_summary` | A token-bounded summary of `promise_ledger.jsonl`, capped at 8k tokens |
| `working_dir` | Workspace root path |
| `stage`, `total_stages`, `stage_index` | Current stage in the pass |
| `expected_file` | What this stage is supposed to write |
| `rescue_warning` | Set if the previous stage's file-gate rescue fired |
| `findings_file`, `lesson_candidates_file` | On-disk JSONL files the auditor appends to during verify/test |
| `wall_cap_hit` | Boolean — true when the wall-cap timer has expired |

POR + ledger are primary; without them the audit degrades to
prose-only mode. The harness pre-computes a deterministic one-line
headline of the audit summary (status distribution, severity
counts, promise_check status, wall-cap flag) and injects it as
`final_audit_headline` so the reporter has a reliable summary line
even if JSON parsing fails.

### Outputs

`<workspace>/final_audit_report.md` — the narrative. Sections (per
the role text):

- Findings by severity (CRITICAL / MODERATE / MINOR)
- Residual debt (in-progress at end, blocked-deferred,
  low-confidence validated, supersession-pending)
- Future-work proposals, **anchored to specific residual-debt items**
  (never unanchored)
- Reconciliation log (silent supersessions made explicit; silent
  edits surfaced)

`<workspace>/final_audit_summary.json` — structured machine-readable
record of the same content. Schema:

```json
{
  "run_id": "run-<TIMESTAMP>",
  "milestone_status_distribution": {
    "validated": N, "in-progress": N, "deferred": N,
    "reopened": N, "superseded": N, "invalidated": N, "not-started": N
  },
  "plan_milestone_state": {
    "M-ID": {
      "status": "validated|deferred|...",
      "confidence": "high|medium|low|provisional",
      "evidence_count": N
    }
  },
  "residual_debt": [
    {"milestone": "M-ID", "kind": "in-progress-at-end|blocked-deferred|low-confidence|supersession-pending", "narrative": "..."}
  ],
  "future_work": [
    {"anchored_to": "M-ID", "proposal": "..."}
  ],
  "findings": {"CRITICAL": N, "MODERATE": N, "MINOR": N},
  "reconciliation_events_emitted": N,
  "promise_check_status": "green|red"
}
```

### Reconciliation events

When the audit detects a milestone that was silently superseded
(e.g., the worker started over without emitting a `superseded` event),
the auditor stages a reconciliation finding to
`<workspace>/final_audit_findings.jsonl`. **All staged findings are
committed to `promise_ledger.jsonl` in a single batch at the document
stage** — never incrementally. This guarantees:

- No partial-ledger states.
- Crash-safe: if the audit dies before document stage, the staged
  findings are not yet committed; resume re-processes from scratch.
- Idempotent: each finding has a deterministic UUIDv5 derived from
  the finding data; re-running document stage doesn't duplicate.

### File-gate rescue

`reporting._rescue_stage_file` (shared with the reporter) handles the
case where the agent produced content in its `[OUTPUT]` block but
failed to write the expected file:

- For body stages (verify / test): appends content to a draft file
  if it exists, else writes anew.
- For boundary stages (explore / document): overwrites the target
  file.
- Sets `pending_rescue_warning` so the next stage is told to verify
  and re-write if needed.
- Logs an off-nominal event (`file_gate_rescue` in
  `health_events.jsonl`).

The document stage **always runs even if wall-cap is hit** — it
executes within its own `cli_timeout`, ensuring an audit pass always
produces both report.md and summary.json (or synthetic minimal
fallbacks if the agent fails entirely).

### Wall-cap

`WALL_CAP_SECONDS = 36000` (10h) total auditor pass time. The cap
is checked at each stage's pre-loop boundary and skips remaining
verify/test stages if exceeded. The document stage runs anyway.
The summary JSON includes `wall_cap_hit: true` so the reporter knows
the audit was truncated.

---

## Final reporter

Runs once at end-of-run and on every daily-sync, after the final
auditor. Reads the audit summary + headline + all prior periodic
reports, produces a consolidated final report.

### Dynamic staging

Same heuristic as the final auditor:

```
num_body_stages = max(1, total_tokens // 20_000)
total_stages = 1 + num_body_stages + 1
            = outline → body×N → finalize
```

The hard cap on N (was 10) was removed in Stage 3 for multi-day runs
that accumulate ~1M tokens of prior reports. Wall-cap (10h) is the
real ceiling.

### Stages

| Stage | Output |
|---|---|
| **outline** (1) | `<workspace>/final_report_outline.md` — narrative skeleton |
| **body** (×N) | Appends sections to `<workspace>/final_report_draft.md` |
| **finalize** (1) | Consolidates draft into `<workspace>/final_report.md` |

The reporter's session persists across stages (each stage is one
`claude -p` call with `--resume` on the previous session UUID).
Auto-compact within the reporter's session triggers at 90% as usual.

### Inputs

| Input | Source |
|---|---|
| `directive` | original task |
| `stage`, `total_stages`, `expected_file` | per-stage staging |
| `rescue_warning` | set if previous stage's file-gate fired |
| `outline_path` | workspace path to outline (after stage 1) |
| `prior_reports` | mtime-filtered list of `report_cycles_*.md` files (see daily-sync below) |
| `working_dir` | workspace root |
| `final_audit_summary` | the JSON from the final auditor |
| `final_audit_headline` | the one-line headline computed by the harness |
| `wall_cap_hit` | boolean |

### Outputs

- `<workspace>/final_report.md` — canonical synthesis.
- `<workspace>/final_report.pdf` — rendered via `render_pdf`.

`final_report.md` is contractual at workspace root (curator + curator
contract expects it there; org_check tolerates this).

---

## Curator

Runs after the final reporter, packages everything into a ZIP for
handoff.

### What it produces

```
<slug>_package.zip
└── <pkg>/
    ├── README.md           (auto-generated)
    ├── report/
    │   ├── final_report.md
    │   ├── final_report.pdf
    │   ├── final_audit_report.md
    │   ├── final_audit_report.pdf
    │   ├── final_audit_summary.json
    │   ├── MANIFEST.md
    │   ├── REFERENCES.md
    │   └── CURATION.yaml   (the validated effective manifest)
    ├── code/               (worker-authored scripts)
    ├── test/               (auditor-authored test artifacts)
    ├── data/               (curated source data)
    └── figures/            (entries with role: figure in CURATION.yaml)
```

On daily-sync mode: package gets a timestamp suffix
(`<slug>_package_<YYYY-MM-DDTHHMM>.zip`) plus a `<slug>_package_latest.zip`
symlink that's atomically re-pointed.

### How "cited" sources are determined

The curator agent reads `MANIFEST.md`'s "## Key Files" section and
selects entries. It writes `CURATION.yaml` with `include` list +
`role` per entry. The harness validates with
`_parse_curation_manifest` (sanitises and deduplicates) and applies a
hard exclude list:

- Process artifacts: `sessions.db`, signal files, per-cycle reports.
- Intermediate drafts: `final_report_draft.md`, `final_report_outline.md`.
- Previous packages: `<slug>_package*.zip`.

Entries with `role: figure` are staged under `figures/` in the bundle
(per Plan 06).

### Fallback to safety package

If the curator agent fails to produce a valid `CURATION.yaml`, the
harness falls back to a minimal "safety package" containing
report-only artifacts: `final_report.{md,pdf}`,
`final_audit_report.{md,pdf}`, `final_audit_summary.json`,
`MANIFEST.md`, `REFERENCES.md`, `CURATION.yaml` (the failed attempt
preserved as audit trail). The bundle always ships; coverage degrades
gracefully.

---

## Daily sync (Stage 3)

End-of-run vs daily-sync are the same agent pipeline running under
different triggering. The daily-sync trigger:

1. **At every cycle boundary**, the cycle loop checks
   `_daily_sync_due(last_daily_sync_at, interval_hours)`.
2. If true and not already in fan-out (deferred to post-merge cycle),
   sets `_daily_sync_in_progress = True`, runs the sequence, clears
   the flag, advances `last_daily_sync_at`, resumes cycles.
3. **Crash recovery:** on resume, `_daily_sync_in_progress` is
   unconditionally cleared. Without this, a mid-sync crash would leave
   the flag True forever, blocking all future syncs.

### Hours, not cycles

The interval is wall-clock hours (default 24h). Cycles vary in length
(seconds to ~30 minutes) and are the wrong unit for an
operator-check-in cadence. The system makes its time-based decisions
in clock units; the model makes its work-based decisions in cycles.

### Revise mode

Each agent detects revise mode by the existence of its prior
artifact:

- **Final auditor**: if `final_audit_report.md` exists, agent reads
  prior + delta inputs (ledger events since last sync, mtime-filtered)
  and produces a revised version.
- **Final reporter**: same pattern; mtime-filters
  `report_cycles_*.md` to inputs added since last sync.
- **Curator**: timestamped package always, regardless of mode.

The mtime delta filter at `reporting.py:_estimate_prior_report_tokens`
selects `p.stat().st_mtime > last_sync_epoch`.

### Failure handling per agent

| Agent | If it fails |
|---|---|
| Final auditor | Skip; keep prior `final_audit_summary.json`; reporter still runs with last good summary |
| Final reporter | File-gate rescue; if rescue also fails, keep prior `final_report.md` and log warning |
| Curator | Keep prior bundle and symlink; log warning |

In all cases: log failure, clear the in-progress flag, advance
`last_daily_sync_at`, continue cycles. Daily sync is best-effort by
design — the operator still has the last good artifacts and the run
keeps cycling.

### Per-account usage delta print (Plan A)

`_run_daily_sync` snapshots per-account token totals at sync entry
(`_snapshot_account_usage()`) and prints a delta + share % at sync
exit (`_print_account_usage_delta()`). The print uses the
`pool.get_usage_snapshot` snapshot before/after to compute four-field
deltas, then a quota-burn-proxy weighted sum (`input + cache_read*0.1
+ cache_creation*1.25 + output*5.0`) for the share %.

Example:

```
[long-exposure] Account usage delta since last sync (<TIMESTAMP>):
  acct-prim     in:  1.2M cr:  4.2M cc: 120K out: 210K  (share: 71.3%)
  acct2         in:  0.4M cr:  1.5M cc:  40K out:  72K  (share: 22.1%)
  acct3         in:  0.1M cr:  0.5M cc:  15K out:  24K  (share:  6.6%)
```

Pool inactive (single-account or pinned-without-pool): the snapshot
helper returns `{}` and the print is a silent no-op.

### Planned 24h rotation hook (Plan B)

Immediately after the daily-sync `finally` clause completes (state
saved with `daily_sync_in_progress=False`), the cycle loop runs a
**planned-rotation block** that pre-emptively rotates the primary
when no rotation has happened in the prior 24 hours. Conceptually:
the daily-sync just produced a snapshot of the run on the outgoing
primary; the next 24-hour window starts on a freshly-promoted
account.

Gates: `pool.is_active()` AND `not _is_clone()` AND
`not post_merge_pending` AND `not _stop_requested` AND
`pool.last_rotation_age_hours() >= planned_rotation_min_age_hours`
(or None — never rotated, treat as eligible).

The block calls `pool.promote_fresh()`. On a non-None return it does
THREE things, all load-bearing:

1. **`os.environ["CLAUDE_FORCE_ACCOUNT"] = new_primary`** — without
   this, the parent's `_active_account_dir()` reads the old pinned
   value and continues sending API calls to the old primary.
2. **`agent_sessions.clear()`** — Claude session UUIDs are
   per-account; resuming an old account's UUID on the new account
   fails. Forcing fresh sessions on the next cycle is the only way
   to make the rotation effective.
3. **`health_events.append_event("planned_rotation", ...)`** —
   informational event for operator observability.

When `promote_fresh` returns None (all accounts cooling), the block
records a `planned_rotation_skipped` event and falls through; the
existing adaptive-cooldown path drives recovery on the next cycle.

The block reuses the daily-sync block's `finally` semantics so it
fires regardless of whether the daily-sync agents succeeded or
failed — a partial / failed sync still warrants the rotation if the
24h window has elapsed.

Configurable via `loop.planned_rotation_min_age_hours` (defaults to
`loop.daily_sync_interval_hours`). Setting it higher than the sync
interval spaces planned rotations more sparsely than syncs. See
[`multi-account-pool.md`](multi-account-pool.md) for the full
mechanics + gates analysis.

---

## Cross-cutting lessons (Plan 5)

The final auditor's document stage emits durable cross-run findings
as `record_type='lesson'` rows in `sessions.db`. Lessons are surfaced
to future runs via gem ranking (+0.3 boost, immune from recency
decay for ~30 days).

### What a lesson looks like

A markdown record (500–2000 tokens) with:

```markdown
# Lesson: <short-title>
**Domain:** <tag>, <tag>
**Confidence:** <level> (<count> independent reproductions)
**Status:** validated
**Evidence runs:** [run-ids]

## Pattern observed
...

## Working recipes
...

## What doesn't work / Anti-patterns
...

## Cross-references
- ledger event: <event_id>
- prior lesson: <slug>
```

### Cap

Hard cap per run: `max(1, ceil(total_cycles / 3))`. Hybrid enforcement
— soft-guidance asks the agent to rank candidates and emit only top
N, harness truncates if the agent emits more.

### Cross-run merging

When the final auditor's document stage emits a lesson, it first
calls `_lookup_existing_lesson(slug)`. If the slug exists:

- **New evidence agrees**: append `evidence_runs`, leave content.
- **New evidence contradicts**: mark prior as superseded, write new.
- **Slug collision** (different topic, same slug): disambiguate by
  appending domain.

### MCP filter

Agents call `search_sessions(record_type='lesson')` to surface only
concentrated findings, not every narrative mentioning the term.

### Status

Designed end-to-end (this doc, code, soft-guidance text). All
deterministic components (cap enforcement, gem boost, MCP filter,
idempotent commit) are unit-tested.

---

## PDF rendering (Stage 8 — unified)

`reporting.render_pdf(working_dir, stem)` is the single canonical
PDF rendering path, used for:

- `final_report.{md,pdf}`
- `final_audit_report.{md,pdf}`
- `report_cycles_NNN-MMM.{md,pdf}` (periodic reporter)

### Pandoc command

```
pandoc <stem>.md
  -o <stem>.pdf
  --pdf-engine=tectonic
  --toc
  --number-sections
  -H header.tex
  -V geometry:margin=1in
  -V fontsize=11pt
  -V documentclass=article
  -V colorlinks=true
```

`header.tex` is written once (then optionally cleaned up) with a
LaTeX preamble that includes:

- DejaVu fonts for Unicode coverage (XeTeX via tectonic).
- `xurl` for line-breaking long URLs.
- `microtype` for typographic refinement.
- Table overflow tolerance.

### Failure handling

`render_pdf` returns `False` on subprocess failure or missing input.
Markdown is always intact. The harness logs and surfaces an
off-nominal event (`pdf_render_failed`). If pandoc / tectonic is
missing from PATH, the event records `rc=127` explicitly. The PDF is
a convenience artifact, not the source of truth — the markdown report
is canonical.

---

## File-gate rescue pattern

A safety net used by both final auditor and final reporter (and
mirrored to the periodic reporter via `render_pdf`). Each multi-stage
agent role tells the model to write its output to a specific file
(`expected_file` input). After the call returns:

1. Check whether `expected_file` exists on disk. If yes: continue.
2. If no, extract content from the agent's `[OUTPUT]` block via regex.
3. Write the extracted content to the expected path (append for body
   stages that should accumulate; overwrite for boundary stages).
4. Set `pending_rescue_warning` so the next stage is told to verify
   and re-write if the rescued content was incomplete.

Logs `file_gate_rescue` event to `health_events.jsonl`. Without this
safety net, an agent's content stuck in `[OUTPUT]` instead of on disk
would silently lose downstream consumers.

---

## Operational rules

1. End-of-run and daily-sync are the same agent pipeline; only
   trigger differs.
2. The agent sequence is fixed: `final_auditor → final_reporter →
   curator`. Each is best-effort; failure of one doesn't block the
   next.
3. Reconciliation events commit transactionally at the auditor's
   document stage; never incrementally. UUIDv5-based dedup makes the
   commit idempotent on resume.
4. The wall-cap (10h) is a safety floor only. Time-based caps on body
   stages and audit stages were removed; budget pressure in agent
   prompts handles output sizing.
5. Daily-sync timing is in hours, not cycles.
6. Crash recovery: `_daily_sync_in_progress` is unconditionally cleared
   on resume.
7. Failure of a sync stage does not abort the run; logs and continues.
8. PDF render failure surfaces via off-nominal events; markdown stays
   intact.
9. Lessons are emitted by the final auditor's document stage with a
   per-run cap of `max(1, ceil(cycles / 3))`.
10. Curator falls back to a report-only safety package on
    `CURATION.yaml` failure — the bundle always ships.

---

## Code citations

- Periodic reporter: `reporting.py:325–370` (entry point), `reporting._run_reporter`.
- Final reporter: `reporting._run_final_reporter`, `reporting.py:341–474`.
- Final auditor: `auditing._run_final_auditor`, `auditing.py:560–818`.
- Curator: `curator.py:279–456` (build), `curator.py:308–334` (safety package fallback).
- File-gate rescue: `reporting.py:187–214`, `auditing.py:700–718`.
- Daily-sync trigger: `exploration.py:_run_daily_sync`,
  `exploration.py:2808–2853`. Crash-recovery clear:
  `exploration.py:1947–1959`.
- `WALL_CAP_SECONDS`: `long_exposure/limits.py`.
- `render_pdf`: `reporting.py:258–322`.
- Reconciliation event commit: `auditing.py:743–755`.
- Lesson emission: `auditing._commit_lessons`, `auditing.py:757–765`.
- Audit summary headline: `reporting._load_audit_summary` (returns
  parsed text and one-line headline; `reporting.py:107–151`).
