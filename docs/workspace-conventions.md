# Workspace Conventions

Long-exposure runs against a working directory (the **workspace**)
that the agents read, write, and organise. This doc covers the
conventions that make a workspace legible to itself across cycles:
the folder skeleton, the plan-of-record, the promise ledger, the
timestamping rules, and the validators that surface drift.

**Sources of truth:** `long_exposure/workspace_bootstrap.py`,
`long_exposure/tools/{org_check,promise_check,ledger_append}.py`,
`long_exposure/exploration-score.yaml` (per-agent role text).

---

## Design principles

Three principles govern every choice in this layer:

1. **Convention over enforcement.** Soft-guidance + validators that
   *surface* non-compliance. None of these are gates that block the
   cycle. A run with no plan-of-record still completes; the auditor
   just sees fewer structured signals.
2. **Graceful absence.** Removing any one piece (no POR, no ledger,
   no STRUCTURE.md) degrades a feature but doesn't crash the run.
3. **Append-only history.** Plans and ledgers are immutable-by-append.
   Mutations are emitted as new events with explicit
   `supersedes` / `_archive` semantics.

---

## Folder skeleton

When a fresh run starts (cycle 1, no pre-existing
`plan_of_record.md`), `workspace_bootstrap.ensure_skeleton` creates
the standard folders:

```
<workspace>/
├── reports/      cycle reports and final-reporter scratch
├── audits/       final-auditor scratch and sidecar JSONL files
├── scripts/      worker-authored code
├── tests/        auditor-authored test artifacts
├── data/         curated source data
├── docs/         narrative documentation
├── tools/        utility scripts
└── stale/        archive (files moved here, never deleted)
```

The bootstrap is **idempotent** — re-running on an existing skeleton
is a no-op. It fires only on cycle 1 of a *fresh* run; on
`resume`, no bootstrap. This is intentional
B.1: an existing workspace stays as-is until the operator chooses to
adopt the convention.

Domain-specific folders (e.g., `benchmark-01-...`,
`microlocal-foundations/`) coexist freely. They are not enforced and
not validated. The standard folders are for *cross-cutting* artifacts
that don't have an obvious domain home.

### STRUCTURE.md

The researcher writes `<workspace>/STRUCTURE.md` on cycle 1 (also
idempotent; skipped if present). It documents:

- The standard folders and their purposes.
- Domain-specific folders the researcher has named.
- Local conventions (e.g., "plots co-located with their source data,
  not a separate `figures/` folder").

Subsequent cycles consult STRUCTURE.md for organisation guidance.
The auditor verifies new files conform to the documented structure
via `org_check.py`.

---

## Plan of record (POR)

`<workspace>/plan_of_record.md` is the contract between the run and
its directive. It's a markdown file, table-structured (not narrative
prose), with a small fixed schema:

```markdown
# Plan of Record — <title>
**Created:** <date>, **Run id:** <run-id>

## Directive (immutable)
<original directive text>

## Goals
| Goal ID | Goal | Owner |
| ...     | ...  | ...   |

## Milestones
| Milestone ID | Goal | Description | Success criteria | Dependencies |
| ...          | ...  | ...         | ...              | ...          |

## Out of scope
- ...
- ...
```

The directive is **immutable**. Goals and milestones evolve over the
run; revisions are emitted as ledger events with
`milestone_id: "_plan/<change>"`. The ledger is the single canonical
decision log — POR doesn't carry its own changelog section.

### Who maintains it

- **Researcher** writes the initial POR on cycle 1; updates it as
  scope clarifies. Emits a `_plan/...` event in the ledger on every
  change.
- **Worker** reads it; doesn't edit.
- **Auditor** validates that mtime changes are accompanied by
  `_plan/` events (silent-edit detection).

---

## Promise ledger

`<workspace>/promise_ledger.jsonl` is the structured judgment history.
JSONL append-only. Every meaningful judgment about a milestone (start,
progress, validation, supersession, deferral, invalidation) is one
line.

### Schema

Required fields:

| Field | Type | Meaning |
|---|---|---|
| `event_id` | UUIDv4 | Globally unique; deterministic across re-runs (UUIDv5 for reconciliation events) |
| `ts` | ISO8601 | When the event was emitted |
| `run_id` | string | Run identifier |
| `cycle` | int | Cycle number within the run |
| `agent` | string | `researcher` / `worker` / `auditor` / `manager` / `human` / `harness` |
| `milestone_id` | string | The milestone touched, OR `_plan/<change>` for plan revisions, OR `_run/<event>` for run-level events |
| `status` | enum | See "Status taxonomy" below |
| `confidence` | object | `{level, rationale, assessor}` |
| `narrative` | string | One-paragraph human-readable explanation |

Optional fields:

| Field | Type | Meaning |
|---|---|---|
| `scope` | string | For narrow validations: which slice was actually validated |
| `evidence` | list | File paths or prior `event_id`s supporting this judgment |
| `supersedes` | string | A prior `event_id` this event replaces |
| `dependencies` | list | Milestone IDs this depends on |
| `reopen_conditions` | string | What would cause a deferred / superseded milestone to reopen |
| `artifacts` | list | Workspace paths produced by this event |
| `process_artifacts` | list | Non-workspace run artifacts, such as manager assessment logs |

### Status taxonomy (eight values)

These are **immutable** — the same vocabulary across in-cycle audits
and final audits:

| Status | Meaning |
|---|---|
| `not-started` | Milestone declared, no work yet |
| `in-progress` | Active work, no closure |
| `action_required` | Manager or auditor requires a specific next-cycle action before normal continuation |
| `validated` | Closed positively; evidence supports the milestone |
| `deferred` | Closed without progress, blocked on something not in scope |
| `reopened` | Re-opened after closure, usually because new evidence contradicts prior closure |
| `superseded` | Prior event was correct then, newer work makes it obsolete |
| `invalidated` | Prior event was *wrong* — distinct from superseded |

`superseded` vs `invalidated` is a load-bearing distinction.
Superseded means "X used to be the right answer; now Y is, but X
wasn't an error." Invalidated means "X was wrong all along."

### Confidence framework

`confidence` is orthogonal to status:

```json
{
  "level": "high|medium|low|provisional",
  "rationale": "one-line reason",
  "assessor": "researcher|worker|auditor|manager|harness|human|final_auditor"
}
```

`validated` + `low` confidence is **common and fine** for narrow-claim
validations. The bar is independent verification, not certainty. A
worker self-reporting `validated/medium` becomes
`validated/high` once the auditor cross-checks.

### Current state of a milestone

Walk events in `ts` order; the latest event per `milestone_id`
defines current status. `promise_check.py` computes this.

### Ledger summary injection

Every cycle's agent prompts get a `promise_ledger_summary` input.
The harness builds it from:

- Most-recent event per milestone_id.
- All `in-progress` items.
- Any `validated` with `low` or `provisional` confidence in the last
  N cycles.

Capped at ≤8k tokens. Truncation drops oldest first.

### Per-clone shadow ledgers (Plan 1 Phase 2)

Fan-out clones never write directly to the workspace's main ledger.
Instead, each clone writes to
`<workspace>/fork-<id>/clone-<k>/promise_ledger.jsonl`.
`workspace_bootstrap.resolve_ledger_path` routes writes to the right
file based on `AGENT_FORK_ID` env var.

After the barrier collapses, the conductor calls
`concat_clone_ledgers(workspace, fork_dir)` which reads each clone's
shadow file, deduplicates by `event_id` (idempotent on partial
collapses), and appends new events to the workspace main ledger in
timestamp order.

The CLI helper for agents:

```bash
python -m long_exposure.tools.ledger_append \
  --workspace <ws> \
  --event '{...json...}'
```

Auto-routes via `resolve_ledger_path`. Worker and auditor role text
references this helper. The helper validates the required ledger
schema before appending so malformed agent JSON is rejected at the
entry point rather than discovered later by `promise_check`.

---

## Timestamping conventions

How does the system answer "when was this file produced?" Without
universal filename timestamps (which pollute names and break
references).

### Canonical timestamp source: ledger `artifacts` field

When an event produces a workspace file, the agent lists the path in
the event's `artifacts` field:

```jsonl
{"event_id": "...", "ts": "<ISO-8601>", ...,
 "artifacts": ["data/result_summary.csv", "scripts/plot_result.py"],
 "narrative": "Computed summary statistics for milestone M-Foo."}
```

Query: `grep <path> promise_ledger.jsonl` → latest event's `ts` is
the authoritative creation time. No filename parsing needed; mtime
is unreliable across moves.

For unpredictable tools (e.g., a script that produces multiple plots
the agent can't enumerate ahead of time), the recommended pattern is
diff before/after:

```python
before = set(p.iterdir())
run_tool()
after = set(p.iterdir())
new_files = after - before
```

The auditor verifies listed artifacts match what's actually present
via `promise_check`.

`artifacts` is only for paths inside the workspace. Sidecar files that
support operation but are not workspace deliverables, such as cron
manager assessments under the instance directory, go in
`process_artifacts` and are not checked by the workspace artifact walk.

### Markdown frontmatter (soft-guidance)

Narrative documents (reports, lessons, plan-of-record) carry a YAML
frontmatter block:

```yaml
---
created: <TIMESTAMP>
cycle: 17
run_id: run-<TIMESTAMP>
agent: worker
milestone: M-BM02a-Q
---

# Document title
...
```

Frontmatter is forward-only — pre-existing files are not retroactively
backfilled. Not enforced by `promise_check`. Pandoc tolerates the
block as document metadata, so PDFs render correctly.

### Stable filenames for working files

Data, scripts, models, figures all keep stable names across
regeneration. Filename timestamps are reserved for:

- Snapshot archives (`exploration_state_20260425T031322.json`).
- Fan-out fork directories (`fork-<id>/`).
- Daily-sync packages (`<slug>_package_<YYYY-MM-DDTHHMM>.zip`).

### Archive ritual

When moving a file to `stale/`, emit an
`_archive/<short-name>` event with `supersedes_path` pointing to the
original location:

```jsonl
{"event_id": "...", ..., "milestone_id": "_archive/bm02a_summary",
 "supersedes_path": "data/bm02a_summary_old.csv",
 "narrative": "Superseded by data/bm02a_summary.csv after correcting units."}
```

This tells `promise_check` that the original path's absence is
*intentional*. Without this, the validator would flag the missing
path as broken.

---

## Validators

Two validators run as auditor / final-auditor tools (invoked via
Bash, not as Python imports):

### `promise_check.py`

Walks `promise_ledger.jsonl` and `plan_of_record.md`. Surfaces:

- **Schema errors**: malformed events, missing required fields.
- **Status transitions** that violate the taxonomy (e.g., `validated`
  then `not-started` without a `reopened` event in between).
- **Plan silent edits**: `plan_of_record.md` mtime change without a
  matching `_plan/...` event in the ledger.
- **Artifact coherence** (single-walk `_check_artifact_coherence`):
  - `ORPHAN`: file present in managed folders but not referenced by any
    event's `artifacts`.
  - `MISSING`: ledger references a path that doesn't exist on disk
    and isn't archived.
- **Cross-reference integrity**: `supersedes` and `evidence` event
  IDs must point to events that exist.

Output: JSON + text. Exit code 0 = green. The auditor reads results
and incorporates findings into `audit_report`.

### `org_check.py`

Walks the workspace and surfaces:

- Files outside the allowed-at-root set.
- Reports in wrong folders (e.g., `report_*.md` at workspace root
  instead of `reports/cycles/`).
- Scripts at root.
- Large binaries at root.
- Stale-looking files outside `stale/`.

Allowlist for root: `MANIFEST.md`, `STRUCTURE.md`, `plan_of_record.md`,
and `promise_ledger.jsonl`. Final report artifacts live under
`reports/final/`; final audit artifacts live under `audits/final/`.
Legacy root-stage artifacts are reported as notes during the layout
transition.

The validator scopes orphan detection to managed paths only:
standard folders + domain folders declared in STRUCTURE.md +
allowlisted root files. Explicitly excludes `.venv/`, `.git/`,
`stale/`, gitignored paths, and any directory STRUCTURE.md tags as
"External".

### Frontmatter not enforced (deliberate)

There is no validator for missing or malformed frontmatter. Reasons
documented in `promise_check.py`:

1. Soft-guidance for an aesthetic convention; lowest-value artifact
   in the plan set.
2. Forward-only / backfill rules require knowing each file's
   first-touched cycle, which the harness doesn't track.
3. The ledger's `artifacts` field already answers when/who/which-cycle
   — frontmatter is convenience, not essential.

The comment also says when to revisit: real run reveals silent
slipping AND the slip blocks downstream work.

---

## Per-agent file-organization soft-guidance

Each agent's role text in `exploration-score.yaml` carries a
`<file-organization>` block giving directional guidance:

| Agent | Block content |
|---|---|
| Researcher (cycle 1) | Bootstrap STRUCTURE.md and plan_of_record.md from templates; preserve them on subsequent cycles. |
| Researcher (subsequent) | Read STRUCTURE.md and plan_of_record.md; emit `_plan/...` events for any changes. |
| Worker | Scripts → `scripts/`; data → `data/`; tests → `tests/`; figures co-located with source data, NOT a separate `figures/` folder. List artifacts in ledger. |
| Auditor | Test artifacts → `tests/`; run `promise_check` and `org_check`; cite milestone IDs. |
| Reporter | Periodic reports → `reports/cycles/`. CRITICAL: write content to disk, not just `[OUTPUT]` block. |
| Final reporter | Final report artifacts under `reports/final/`, including `final_report.{md,pdf}` and commit marker. |
| Final auditor | Final audit artifacts under `audits/final/`, including `final_audit_report.{md,pdf}`, summary JSON, and commit marker. |
| Curator | Reads MANIFEST.md "## Key Files"; writes CURATION.yaml; outputs ZIP package. |

---

## Periodic reports → `reports/cycles/` folder

The periodic reporter writes `report_cycles_<N>-<M>.{md,pdf}` to
`<workspace>/reports/cycles/`.
Final report artifacts live under `reports/final/`; final audit artifacts
live under `audits/final/`. `org_check` notes legacy root-stage artifacts
without failing so old workspaces remain inspectable.

Fan-out clones append a clone suffix to periodic report basenames:
`report_cycles_<N>-<M>_clone_<K>.{md,pdf}`. This prevents sibling
clones from clobbering the same filename in a shared workspace.

Root periodic reports also add a deterministic `Fan-Out Artifact Index`
when branch artifacts exist under `reports/cycles/cycle<N>/`. The index
lists branch markdown artifacts, sizes, and first headings so substantive
branch findings remain discoverable even if the root reporter writes a
brief synthesis.

After the harness writes a periodic report, it appends a `_run/report_*`
ledger event with the report markdown and any successfully rendered PDF
listed in `artifacts`. This keeps cycle reports auditable without asking
the reporter agent to produce its own bookkeeping event.

---

## Operational rules summary

1. Bootstrap fires only on cycle 1 of a *fresh* run (no
   pre-existing `plan_of_record.md`). On resume, no bootstrap.
2. Standard folders are idempotent — re-creating an existing skeleton
   is a no-op.
3. Plans and ledgers are append-only. Mutations are new events with
   `supersedes` / `_archive` semantics.
4. `validated` + `low` confidence is normal and fine. The bar is
   independent verification, not certainty.
5. `superseded` ≠ `invalidated`. Both are first-class status values.
6. Per-clone ledgers are shadows; concatenated post-barrier with
   UUIDv4 dedup.
7. Frontmatter is forward-only and not enforced.
8. Filenames stay stable; timestamps are in the ledger and
   frontmatter, not in filenames.
9. Validators surface; they do not gate the cycle.
10. Pre-existing workspaces are left as-is by design; adoption is
    `clear` + fresh start, or hand-write a minimal POR + ledger.

---

## Code references

- `workspace_bootstrap.is_fresh_start`,
  `workspace_bootstrap.ensure_skeleton`,
  `workspace_bootstrap.write_plan_of_record`,
  `workspace_bootstrap.emit_run_start_event`.
- Ledger routing: `workspace_bootstrap.resolve_ledger_path`,
  `workspace_bootstrap.append_ledger_event`,
  `workspace_bootstrap.concat_clone_ledgers`.
- `summarize_ledger`: cycle-input summarizer; cap ≤8k tokens.
- Validators: `long_exposure/tools/promise_check.py`,
  `long_exposure/tools/org_check.py`,
  `long_exposure/tools/ledger_append.py`.
- Per-agent file-organization blocks: `exploration-score.yaml`
  (search for `<file-organization>`).
- `STANDARD_FOLDERS`: `workspace_bootstrap.py:21`.
- Allowed-at-root expansions: `org_check.ALLOWED_AT_ROOT_FILES`.
