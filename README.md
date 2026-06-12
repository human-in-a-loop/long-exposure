# Long Exposure

**Long-exposure is an autonomous research harness for large-scope, complex,
and ambiguous problems.** Give it a directive that is too broad for a one-shot
prompt; it runs a persistent researcher -> worker -> auditor loop, fans out
when the work is genuinely independent, and produces traceable reports and
artifacts.

The short version: **ambiguous research directive in, packaged report and
audit trail out.**

> **Run in a sandbox.** Long-exposure is meant to run for hours to weeks with
> broad file and shell access inside its working directory. Use a VM, container,
> or otherwise isolated workspace. See
> [docs/local-setup.md](docs/local-setup.md) and
> [docs/configuration-reference.md](docs/configuration-reference.md).

## What It Does

- Runs a deterministic three-role cycle: researcher -> worker -> auditor.
- Preserves context across cycles, stops, resumes, account rotation, and
  compaction through `sessions.db`, state files, and workspace artifacts.
- Supports Claude, Codex, and Gemini CLI providers, plus a generic local
  OpenAI-compatible connector as an extension point.
- Fans out whole cycles into parallel clone runs when the researcher emits a
  valid `<parallel_cycle_fanout>` block.
- Supports intra-turn parallel helpers where providers expose them: Claude
  agent-teams and Codex subagents. Gemini uses whole-cycle fan-out only.
- Writes periodic cycle reports and runs a final auditor -> final reporter ->
  curator pipeline at graceful stop, topic exhaustion, max cycles, or daily
  sync.
- Builds a standard workspace structure with a plan of record, append-only
  promise ledger, reports, audits, scripts, tests, data, tools, and stale
  archive folders.
- Optionally records local passive telemetry for later run analysis.

## Requirements

- Python 3.10+
- `uv` recommended for setup
- At least one supported provider CLI on `PATH`:
  - Claude Code CLI for `llm_provider: claude`
  - Codex CLI for `llm_provider: codex`
  - Gemini CLI for `llm_provider: gemini`
- `pandoc` and `tectonic` for standard PDF rendering
- Optional: Wolfram Engine for tasks that need Wolfram scripts

`long-exposure-setup` runs `uv sync`, checks provider CLIs, and installs/checks
`pandoc` and `tectonic` through supported platform package managers where
possible.

## Quick Start

```bash
git clone <repo> long-exposure
cd long-exposure
uv run long-exposure-setup --yes
```

For a non-mutating environment check, run:

```bash
long-exposure-doctor --config long_exposure/config.yaml
```

Edit `long_exposure/config.yaml` and set:

```yaml
working_directory: /path/to/workspace
llm_provider: claude   # or codex, gemini, local
```

Launch:

```bash
long-exposure launch "Explore foundations of microlocal analysis"
```

Lower-level script entry:

```bash
long-exposure start "Explore foundations of microlocal analysis"
```

Fallback without the console script:

```bash
python3 -m long_exposure.exploration start "Explore foundations of microlocal analysis"
```

## Common Commands

```bash
long-exposure status
long-exposure guide "Focus the next cycle on falsifying the current assumption."
long-exposure stop
long-exposure resume
long-exposure clear
long-exposure telemetry summarize --instance-dir /path/to/instance
long-exposure cli-install --target all --directory .
```

`stop` is graceful: after the current agent/cycle boundary, long-exposure runs
the final auditor, final reporter, and curator unless the run is being cleared.

## Provider Notes

Claude is the default provider. Codex and Gemini are native provider paths using
their local CLIs. Long-exposure normalizes provider outputs into the same
internal envelope and keeps durable continuity in files and `sessions.db`, not
only in provider-native sessions.

Provider selection:

```yaml
llm_provider: claude        # claude | codex | gemini | local
codex_model: gpt-5.5
gemini_model: gemini-3-flash-preview
```

Environment override:

```bash
LONG_EXPOSURE_LLM_PROVIDER=codex long-exposure launch "..."
```

Codex runs use `codex exec --yolo` by default. Gemini runs use `gemini --yolo`
and project-local `.gemini/settings.json`. These autonomous modes should only
be used in an externally sandboxed environment.

## Multi-Account Pools

Claude and Codex can each use a pool of authenticated config directories.

```bash
export CLAUDE_ACCOUNT_POOL="$HOME/.claude-a,$HOME/.claude-b"
export CODEX_ACCOUNT_POOL="$HOME/.codex-a,$HOME/.codex-b"
```

When only one provider pool is active, the provider-local pool handles
rate-limit cooling, slot accounting, fan-out capacity, and freshness promotion.
When both Claude and Codex pools are configured, unified mode is active by
default: the root process and fan-out clones can acquire capacity across both
provider pools. Set `LONG_EXPOSURE_UNIFIED_POOL=disabled` to force normal
single-provider behavior.

Gemini multi-account pooling is intentionally disabled; Gemini fan-out still
works through concurrent sessions on the active authenticated account.

See [docs/multi-account-pool.md](docs/multi-account-pool.md).

## Architecture

```
CLI launcher / adapters
        |
        v
exploration.py  -> deterministic cycle loop
        |
        +-- orchestrator.py / conductor.py
        |      provider CLI calls, prompt layers, compaction, teams
        |
        +-- pool.py / unified_pool.py
        |      account slots, cooling, rotation, fan-out capacity
        |
        +-- auto_compact/*
        |      sessions.db, FTS search, proximity gems, ancestor scoring
        |
        +-- fanout.py
        |      branch validation, clone lifecycle, merge synthesis,
        |      branchial budget and divergence table
        |
        +-- reporting.py / auditing.py / curator.py
               periodic reports, final audit/report, ledger graph summary,
               package generation
```

The model decides what to investigate. Python decides when to compact, rotate,
fan out, sync, stop, package, and write state.

## Documentation Map

Start with [docs/INDEX.md](docs/INDEX.md). Core docs:

| Topic | Doc |
|---|---|
| Operation | [docs/usage-guide.md](docs/usage-guide.md) |
| Setup | [docs/local-setup.md](docs/local-setup.md) |
| Architecture | [docs/architecture-overview.md](docs/architecture-overview.md) |
| Configuration | [docs/configuration-reference.md](docs/configuration-reference.md) |
| Parallelism | [docs/parallelism.md](docs/parallelism.md) |
| Multi-account pools | [docs/multi-account-pool.md](docs/multi-account-pool.md) |
| Persistence, gems, lemmas | [docs/persistence-and-gems.md](docs/persistence-and-gems.md) |
| End-of-run pipeline | [docs/end-of-run-pipeline.md](docs/end-of-run-pipeline.md) |
| Workspace conventions | [docs/workspace-conventions.md](docs/workspace-conventions.md) |
| Telemetry | [docs/telemetry.md](docs/telemetry.md) |
| Soft guidance | [docs/soft-guidance.md](docs/soft-guidance.md) |
| Known gaps | [docs/gaps.md](docs/gaps.md) |
| Interactive transport (opt-in) | [docs/gaps_interactive_mode.md](docs/gaps_interactive_mode.md) |

## Current Design Boundaries

- Unified provider pooling covers Claude and Codex only.
- Provider-native sessions are cleared on cross-provider rotation; durable
  continuity is file-backed.
- Telemetry is local, passive, and disabled by default.
- The generic local provider has no native tool bridge or subagent runtime.
- `docs/gaps.md` tracks known deferred items that are not worth complicating
  the core path yet.
