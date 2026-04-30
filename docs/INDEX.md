# Long-Exposure Documentation Index

This directory documents long-exposure's architecture, operation, and
design decisions. Docs are organised by **concept**, not by
implementation history. For a one-line description of what
long-exposure does, see the project [README](../README.md).

---

## Reading paths

### "I want to run it"

1. **[`local-setup.md`](local-setup.md)** — install, verify, file
   layout.
2. **[`usage-guide.md`](usage-guide.md)** — `start` / `stop` / `resume`
   / `clear`, daily controls, working directory, monitoring.
3. **[`configuration-reference.md`](configuration-reference.md)** —
   every config and score YAML knob with defaults and tradeoffs.

### "I want to understand how it works"

1. **[`architecture-overview.md`](architecture-overview.md)** —
   conceptual map; the four design principles; subsystem layout.
2. **[`parallelism.md`](parallelism.md)** — fan-out and agent-teams.
3. **[`multi-account-pool.md`](multi-account-pool.md)** — pool state
   machine, slot lifecycle, rate-limit detection.
4. **[`persistence-and-gems.md`](persistence-and-gems.md)** —
   sessions.db, auto-compact, fork-scoped gems.
5. **[`end-of-run-pipeline.md`](end-of-run-pipeline.md)** — reporter,
   final auditor, final reporter, curator, lessons, daily-sync.
6. **[`workspace-conventions.md`](workspace-conventions.md)** —
   folder skeleton, plan-of-record, promise ledger, validators.
7. **[`figures.md`](figures.md)** — figures
   as deliverables; the (deferred) lexicon framework.
8. **[`soft-guidance.md`](soft-guidance.md)** — when to add
   soft-guidance vs. write code; the canonical refinements.

---

## Doc map

| Doc | What it covers | When to read |
|---|---|---|
| [`architecture-overview.md`](architecture-overview.md) | Conceptual map, three-role cycle, four-layer prompt, design principles | First arch read |
| [`usage-guide.md`](usage-guide.md) | Daily controls, instances, monitoring | Operating |
| [`local-setup.md`](local-setup.md) | Install, deps, directory layout, verify | First time |
| [`configuration-reference.md`](configuration-reference.md) | Every YAML knob (config + score) with effort/budget mapping | Configuring |
| [`parallelism.md`](parallelism.md) | Fan-out + agent-teams + depth=1 rationale | Scaling |
| [`multi-account-pool.md`](multi-account-pool.md) | Pool state machine, freshness promotion, slot lifecycle, rate-limit detection | Multi-account |
| [`persistence-and-gems.md`](persistence-and-gems.md) | sessions.db, auto-compact, gems, MCP search | Context lifecycle |
| [`end-of-run-pipeline.md`](end-of-run-pipeline.md) | Reporter, final auditor, final reporter, curator, lessons, daily-sync, PDF render | End-of-run / multi-day |
| [`workspace-conventions.md`](workspace-conventions.md) | Folder skeleton, POR, ledger, status taxonomy, validators, timestamping | Workspace structure |
| [`figures.md`](figures.md) | Figures as first-class; lexicon framework (scaffold only) | Figures / vocabulary |
| [`soft-guidance.md`](soft-guidance.md) | Why soft-guidance, where to put it, the two refinements | Adding/editing prompts |

---

## What you can skip

- Plan-style files (`final_auditor_plan.md`,
  `cross_cutting_lessons_plan.md`,
  `plan_of_record_and_promise_ledger.md`, `organization_plan.md`,
  `timestamping_plan.md`, `soft_guidance_refinements.md`,
  `holistic_review.md`, `multi-account-failover.md`,
  `agent-teams.md`, `effort_levels.md`, `INDEX.md` v1) used to live
  in this directory. They have been folded into the topical docs
  above and removed.

---

## Per-doc length and audience

| Doc | Lines | Audience |
|---|---|---|
| `architecture-overview.md` | ~150 | Anyone (entry point) |
| `usage-guide.md` | ~720 | Operator |
| `local-setup.md` | ~210 | New contributor |
| `configuration-reference.md` | ~370 | Operator + contributor |
| `parallelism.md` | ~280 | Contributor |
| `multi-account-pool.md` | ~270 | Operator + contributor |
| `persistence-and-gems.md` | ~270 | Contributor |
| `end-of-run-pipeline.md` | ~390 | Contributor |
| `workspace-conventions.md` | ~340 | Operator + contributor |
| `figures.md` | ~270 | Contributor |
| `soft-guidance.md` | ~200 | Contributor |

Total: ~4,400 lines across 13 files (consolidated from ~9,000 lines
across 28 files).
