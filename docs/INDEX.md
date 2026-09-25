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
4. **[`telemetry.md`](telemetry.md)** — optional passive telemetry for
   future run analysis.

### "I want to understand how it works"

1. **[`architecture-overview.md`](architecture-overview.md)** —
   conceptual map; the four design principles; subsystem layout.
2. **[`parallelism.md`](parallelism.md)** — fan-out and agent-teams.
3. **[`multi-account-pool.md`](multi-account-pool.md)** — pool state
   machine, slot lifecycle, rate-limit detection.
4. **[`persistence-and-gems.md`](persistence-and-gems.md)** —
   sessions.db, auto-compact, fork-scoped gems, shared lemmas.
5. **[`end-of-run-pipeline.md`](end-of-run-pipeline.md)** — reporter,
   final auditor, final reporter, curator, lessons, ledger summaries,
   daily-sync.
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
| [`telemetry.md`](telemetry.md) | Disabled-by-default local telemetry, rollups, privacy defaults | Run improvement analysis |
| [`parallelism.md`](parallelism.md) | Fan-out + agent-teams + depth=1 rationale | Scaling |
| [`multi-account-pool.md`](multi-account-pool.md) | Pool state machine, freshness promotion, slot lifecycle, rate-limit detection | Multi-account |
| [`persistence-and-gems.md`](persistence-and-gems.md) | sessions.db, auto-compact, gems, MCP search, shared infrastructure lemmas | Context lifecycle |
| [`end-of-run-pipeline.md`](end-of-run-pipeline.md) | Reporter, final auditor, final reporter, curator, lessons, ledger causal summary, daily-sync, PDF render | End-of-run / multi-day |
| [`workspace-conventions.md`](workspace-conventions.md) | Folder skeleton, POR, ledger, status taxonomy, validators, timestamping | Workspace structure |
| [`figures.md`](figures.md) | Figures as first-class; lexicon framework (scaffold only) | Figures / vocabulary |
| [`soft-guidance.md`](soft-guidance.md) | Why soft-guidance, where to put it, the two refinements | Adding/editing prompts |
| [`google-cloud-open-source-llm-costs.md`](google-cloud-open-source-llm-costs.md) | Cost trade-offs showing why Gemini Flash free tier is preferred over self-hosted open models on Google Cloud | Provider planning |
| [`gaps.md`](gaps.md) | Known gaps: resolved incidents (with root-cause notes) and deliberately deferred items with justifications | Before filing/fixing a bug |
| [`gaps_interactive_mode.md`](gaps_interactive_mode.md) | Opt-in interactive Claude transport: design, configuration, verification, and its own gaps table | Considering `claude_transport: interactive` |
| [`benchmarking-plan.md`](benchmarking-plan.md) | Sept 2026 evaluation of the harness (verified code-vs-docs findings), survey of harness-labelled public benchmarks, and a phased plan to benchmark long-exposure against peer harnesses | Planning or reading a benchmark campaign |
| [`rcb-benchmark-plan.md`](rcb-benchmark-plan.md) | Runnable pre-registration for the single primary venue (ResearchClawBench): one arm on Claude-Opus-4.6 (the only model with two published same-model harness rows) at main's stock budget, 40 tasks, one shot each, plus contamination controls, judge controls, and the exact bench config | Actually executing the benchmark |
| [`worktrees-hooks-swarms-plan.md`](worktrees-hooks-swarms-plan.md) | Proposal: where git worktrees do and do not belong, the Claude/Codex/Gemini lifecycle-hook intersection and a vendor-neutral way to use it, a git sync layer for two operators on one repo, and why swarms are cost-gated rather than engineering-gated | Considering worktrees, hooks, or multi-operator / swarm scaling |
| [`advanced-model-modes-plan.md`](advanced-model-modes-plan.md) | Design and decision log for the four opt-in features for more capable models: researcher-planned cycle tails, model capability profiles, the startup gate, and the total spend limit. Includes the researcher-vs-auditor planner comparison and the defects found auditing the implementation | Implementing or reviewing the advanced-model features |
| [`tiered-memory-plan.md`](tiered-memory-plan.md) | Design for the run memoir (L1) over existing L2 reports and L3 compaction rows: auditor-written, minimal-edit, injected into researcher and worker only, archived per cycle; fixed skeleton, guardrails, integration seams | Implementing or reviewing the tiered narrative memory |

---

## What you can skip

- Historical implementation plans and one-off audit notes used to live in this
  directory. They have been folded into the topical docs above and removed so
  `docs/` stays concept-oriented.

---

## Per-doc length and audience

| Doc | Lines | Audience |
|---|---|---|
| `architecture-overview.md` | ~150 | Anyone (entry point) |
| `usage-guide.md` | ~500 | Operator |
| `local-setup.md` | ~210 | New contributor |
| `configuration-reference.md` | ~1,100 | Operator + contributor |
| `parallelism.md` | ~590 | Contributor |
| `multi-account-pool.md` | ~360 | Operator + contributor |
| `persistence-and-gems.md` | ~320 | Contributor |
| `end-of-run-pipeline.md` | ~620 | Contributor |
| `workspace-conventions.md` | ~340 | Operator + contributor |
| `figures.md` | ~270 | Contributor |
| `soft-guidance.md` | ~340 | Contributor |

Total: ~5,000 lines of concept docs (implementation-plan and one-off
incident artifacts are folded into these and removed; `gaps.md` keeps
the condensed incident record).
