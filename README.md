# Long Exposure

**Long-exposure is designed for autonomous research on large-scope, complex, and ambiguous problems — in any field — with emergent parallelism.**

Point it at a research directive that's too open-ended for a one-shot prompt: a literature survey across a whole subfield, a multi-week investigation into a design question, a compliance trail reconstruction, a physics problem where the right sub-topics aren't known up front. Long-exposure then runs a three-role research loop (researcher → worker → auditor) continuously, across context-window resets, for as long as the topic keeps producing useful output. When the researcher detects genuinely independent sub-problems, it fans out into parallel loops; when the worker or auditor face embarrassingly-parallel sub-tasks, they spawn teammate agents for the turn. The parallelism is not scheduled — it emerges from what the problem actually requires.

The pitch in one line: you give it an ambiguous research directive; it gives you a packaged report and a traceable record of how it got there.

By default, long-exposure runs on the **Claude Code Max plan** via the
Claude Code CLI, with no API key needed. It also has native Codex and Gemini
CLI provider paths; for low-cost Google-hosted use, Gemini Flash on the
Google-account/free-tier path is the preferred non-Claude backend. Context
persistence and session search are bundled in (formerly a separate
`auto-compact` package).

> **Permissions: LONG-EXPOSURE SHOULD BE RUN IN A SANDBOX ENVIRONMENT.** Agents run autonomously for hours-to-weeks with broad file and tool access (Read/Write/Edit/Bash on the working directory; Bash unrestricted by default). Run in a VM, container, or otherwise isolated environment. See [`docs/local-setup.md`](docs/local-setup.md) and [`docs/configuration-reference.md`](docs/configuration-reference.md) for permission scoping options.

### What you get

- **Three-role research loop** — researcher structures the next sub-topic, worker builds/computes/writes, auditor validates and decides *validated / continue / pivot*. The audit report feeds the next researcher turn.
- **Persistent context across sessions.** At 90% of the 1M window, each agent compacts itself into a depth-aware XML summary stored in SQLite, then bootstraps a fresh session from the summary. Nothing important is forgotten across cycles.
- **Emergent parallelism at two scales.** The researcher can fan out an entire cycle into parallel researcher→worker→auditor loops (`<parallel_cycle_fanout>`) when it sees independent sub-problems; branch count is bounded by pool capacity, not user knobs. Within a single cycle, worker and auditor can spawn teammate agents for parallel sub-work. Both are automatic; the system decides based on the shape of the problem. See [`docs/parallelism.md`](docs/parallelism.md).
- **Staged final reporting + daily sync.** A reporter consolidates every N cycles into a Markdown report; on a 24h cadence and at run end, a final auditor → final reporter → curator pipeline produces a cross-cycle synthesis, audit summary, and packaged ZIP. See [`docs/end-of-run-pipeline.md`](docs/end-of-run-pipeline.md).
- **Multi-account pool.** A freshness-promoted state machine across multiple Claude Code config dirs handles rate-limits transparently. Pool capacity sets the fan-out cap automatically. See [`docs/multi-account-pool.md`](docs/multi-account-pool.md).
- **Workspace conventions.** Standard folder skeleton, plan-of-record, append-only promise ledger, validators that surface drift without enforcing. See [`docs/workspace-conventions.md`](docs/workspace-conventions.md).
- **Resilience knobs.** Resume-with-directive mid-run, signal-file control (stop / clear / live guidance), concurrent named-instance runs, off-nominal events log surfacing silent fallbacks.

## Requirements

- **Python** 3.10+
- One supported model CLI on your `PATH`: **[Claude Code CLI](https://docs.claude.com/en/docs/claude-code)** for the default `claude` provider, Codex CLI for `llm_provider: codex`, or Gemini CLI for `llm_provider: gemini`.
- **[pandoc](https://pandoc.org/installing.html)** + **[tectonic](https://tectonic-typesetting.github.io/)** — optional; only needed for PDF rendering of the final report. Runs skip PDF if missing.
- **Wolfram Engine** — optional; only if your exploration uses Wolfram scripts. `wolfram_path: "wolfram-batch"` uses long-exposure's portable script wrapper; set `WOLFRAM_BIN` if `wolfram` is not on `PATH`.

### Provider Support

Long-exposure has native integration paths for Claude, Codex, and Gemini CLI.
For low-cost Google-hosted use, Gemini CLI with the Flash/free-tier path is
the preferred non-Claude backend.

There is also a generic `llm_provider: local` OpenAI-compatible HTTP connector
left in the codebase for operators who want to bring their own model server.
Long-exposure does not natively support or recommend a bundled open-source
model path. The local connector has no provider-native tool runtime, no native
subagents, and no managed hosting story; use it only as an extension point.
See [`docs/google-cloud-open-source-llm-costs.md`](docs/google-cloud-open-source-llm-costs.md)
for the cost trade-off that led to preferring Gemini Flash free tier over
self-hosted open models on Google Cloud.

## Quick Start

Install once:

```bash
pip install -e .          # or: uv sync  (uses uv.lock for reproducibility)
```

Edit `long_exposure/config.yaml` → set `working_directory` to the project dir agents should read and write.

Then launch the run. **The preferred interface is the `/long-exposure` slash command from within Claude Code** — it handles scope confirmation and streams logs into your transcript:

```
/long-exposure Explore foundations of microlocal analysis
```

Equivalent from a terminal:

```bash
long-exposure start "Explore foundations of microlocal analysis"
```

Or, if the console script isn't installed:

```bash
python3 -m long_exposure.exploration start "Explore foundations of microlocal analysis"
```

For the default provider, no API key is required: `long-exposure` calls
`claude -p` under the hood, which uses your Max plan. Gemini-backed runs use
Gemini CLI with Google-account / Code Assist auth by default.

The default score runs researcher → worker → auditor sequentially per cycle, with a reporter every `report_interval` cycles, and auto-compacts at 90% of the 1M context window.

**Documentation map** (start with [`docs/INDEX.md`](docs/INDEX.md) for the full set):

| Topic | Doc |
|---|---|
| How to drive it (start / stop / resume / clear, monitoring) | [`docs/usage-guide.md`](docs/usage-guide.md) |
| Install + environment setup | [`docs/local-setup.md`](docs/local-setup.md) |
| Conceptual map + design principles | [`docs/architecture-overview.md`](docs/architecture-overview.md) |
| Every YAML knob (config + score) | [`docs/configuration-reference.md`](docs/configuration-reference.md) |
| Fan-out + agent-teams | [`docs/parallelism.md`](docs/parallelism.md) |
| Multi-account pool | [`docs/multi-account-pool.md`](docs/multi-account-pool.md) |
| sessions.db, auto-compact, gems | [`docs/persistence-and-gems.md`](docs/persistence-and-gems.md) |
| End-of-run + daily-sync pipeline | [`docs/end-of-run-pipeline.md`](docs/end-of-run-pipeline.md) |
| Workspace conventions (POR, ledger, folders) | [`docs/workspace-conventions.md`](docs/workspace-conventions.md) |

## How It Works

### Conceptual map

```
                       CLI: long-exposure {start|stop|resume|clear}
                                      │
                                      ▼
              ┌─────────────────────────────────────────────┐
              │ exploration.py — deterministic cycle loop   │
              │  (researcher → worker → auditor; reporter   │
              │   every N; daily-sync every 24h; signal     │
              │   polling at cycle boundary; rotation       │
              │   on rate-limit; auto-compact at 90%)       │
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

Control flow is deterministic Python; the model decides only **what** to
work on, never **when** to compact, rotate, fan out, or sync. Model output
influences three things: (a) verdicts and topic strings (heuristically
parsed), (b) optional `<parallel_cycle_fanout>` blocks (validated then
dispatched), (c) intra-turn agent-team spawn (Claude Code's own
affordance, not custom code).

### Template layers

The orchestrator reads `config.yaml` and assembles a system prompt from four template layers:

```
1. Philosophy     — voice, tradeoff posture, depth calibration
2. Framework      — stages, gates, transition rules
3. Protocol       — checkpoint discipline, budget tracking, anti-patterns
4. Session Summary — restored context after compaction (if resuming)
```

Each layer is a `.md` template in `long_exposure/templates/` with `{variable}` placeholders filled from the config and preset defaults. The assembled prompt is passed to `claude -p --system-prompt "..."` via subprocess.

When token usage hits the compact threshold, the orchestrator generates a depth-aware XML summary, stores it in SQLite (via auto-compact), rebuilds the system prompt with the summary in layer 4, and continues with a fresh conversation. The agent picks up exactly where it left off.

### Claude Code Integration

The orchestrator uses the same pattern proven in the superprompt project:

```python
subprocess.run(
    ["claude", "-p", "--output-format", "json", "--model", model,
     "--no-session-persistence", "--system-prompt", system_prompt],
    input=prompt, env={...without CLAUDECODE...}, ...
)
```

Key details:
- `claude -p` runs in non-interactive print mode
- `--output-format json` returns `{"result": "...", "usage": {"input_tokens": N, "output_tokens": N}}`
- `--no-session-persistence` keeps each call stateless
- `env.pop("CLAUDECODE")` allows nested invocation from within Claude Code
- `search_sessions` is exposed via an MCP server (`--mcp-config`)

### Multi-Account Failover

Set `CLAUDE_ACCOUNTS` to a comma-separated list of Claude config dirs to run across multiple Max plan accounts:

```bash
export CLAUDE_ACCOUNTS="$HOME/.claude,$HOME/.claude-acctA"
```

Each dir is a full Claude home (its own `.credentials.json`). Every `claude -p` subprocess is run with `CLAUDE_CONFIG_DIR` pointing at the currently active account. The active account sticks across calls via `~/.claude-accounts-state.json`.

**Rate-limit detection** (`orchestrator._is_rate_limit`) is layered and catches all three CLI signalling paths:
1. Non-zero exit + rate-limit text in stderr/stdout.
2. Envelope `api_error_status == 429` (authoritative structured signal; can occur even with exit 0).
3. Envelope `is_error: true` + rate-limit text in `result`.

The third layer is load-bearing: it prevents an exit-0 rate-limit envelope from being silently returned as a low-output "successful" cycle, which would spuriously trip the exhaustion heuristic (`LOW_OUTPUT_THRESHOLD`) that drives the final reporter.

**Rotation is cycle-level.** When any agent call in a cycle detects a rate-limit, the long-exposure cycle loop:
- Records the active account in a `tried_accounts_this_cycle` set.
- Rotates to the next account in `CLAUDE_ACCOUNTS`.
- Clears in-memory `agent_sessions` (forces fresh session UUIDs on the new account).
- Restarts the cycle from the top (researcher).

Gems (`sessions.db`), workspace files, tool permissions, and restored-context summaries are all file-based shared state and transfer automatically — no re-injection plumbing.

**When every account is capped within one cycle**, the loop marks the cycle failed and falls through to the existing failure-streak / `adaptive_cooldown` path. The next cycle rediscovers which accounts have cleared by simply trying them. No new timers, no reset-time parsing.

**Unset** → single-account behavior (the CLI's default `~/.claude`).

**Debug pin:** `CLAUDE_FORCE_ACCOUNT=0` (index into `CLAUDE_ACCOUNTS`) or `CLAUDE_FORCE_ACCOUNT=/path/to/.claude-dir` pins every call to one account and disables rotation. Rate-limits raise immediately.

### Adding a New Account to the Pool

Each account is an independent Claude Code config directory (`$HOME/.claude*`), each with its own `.credentials.json`. Adding one is four steps; **each step runs in your shell**, not inside long-exposure.

#### 1. Create the directory

```bash
mkdir -p ~/.claude-acctN
```

The exact name is convention only — pick something descriptive (`.claude-acctA`, `.claude-pro1`, etc.). Long-exposure refers to accounts by full path, not by index.

#### 2. Authenticate the directory

```bash
CLAUDE_CONFIG_DIR=~/.claude-acctN claude
# Inside the interactive session, run:
#   /login
# Pick the Claude account you want to associate with this directory,
# complete the OAuth flow, then /exit.
```

You should see a `.credentials.json` materialize in `~/.claude-acctN/` after login.

#### 3. Verify the account works in `-p` (non-interactive) mode

This is what long-exposure uses, so `claude` working interactively isn't sufficient:

```bash
CLAUDE_CONFIG_DIR=~/.claude-acctN claude -p "say ok" --output-format json
```

Expect a JSON envelope with `"is_error":false` and `"result":"ok"` (or similar). A non-zero exit or `is_error:true` means the credentials are bad — re-run step 2.

#### 4. Add the directory to the pool

The pool reads `CLAUDE_ACCOUNT_POOL` (preferred) or legacy `CLAUDE_ACCOUNTS` (still works). Order matters on **fresh init**: the first dir becomes the primary; the rest start cold.

```bash
export CLAUDE_ACCOUNT_POOL="$HOME/.claude-acctA,$HOME/.claude-acctB,$HOME/.claude-acctC,$HOME/.claude-acctN"
# then start or resume:
uv run --project /path/to/long-exposure long-exposure resume
```

`pool.init_pool()` runs at startup and adds the new directory as a `cold` entry in `~/.claude-pool-state.json`. You don't edit the JSON manually. Verify with:

```bash
python3 -c "
import os
os.environ['CLAUDE_ACCOUNT_POOL']='\$HOME/.claude-acctA,\$HOME/.claude-acctB,\$HOME/.claude-acctC,\$HOME/.claude-acctN'
from long_exposure import pool
print(pool.format_pool_summary())
"
```

#### Re-seeding the primary

If you want a different account as primary on the next run, archive both pool-state files first so `init_pool` reseeds from your env-var ordering:

```bash
mv ~/.claude-pool-state.json ~/.claude-pool-state.json.bak.$(date +%Y%m%dT%H%M%S) 2>/dev/null
mv ~/.claude-accounts-state.json ~/.claude-accounts-state.json.bak.$(date +%Y%m%dT%H%M%S) 2>/dev/null
# Now the FIRST entry in CLAUDE_ACCOUNT_POOL becomes the new primary.
```

#### Quota-overlap caveat

If you also use one of these directories from another Claude Code session (e.g., a debug session you spin up to inspect the live run), that session and long-exposure will compete for the same Max/Pro plan quota on that account. Symptoms: unexpected 429s on an account that should be fresh. Mitigation: dedicate at least one directory exclusively to long-exposure, or create a fresh debug account that's NOT in the pool.

#### Removing an account

Drop the directory from `CLAUDE_ACCOUNT_POOL` and restart. `_ensure_account_entries` removes any account entry not present in the new env-var list at the next init.

## The Config File

Edit `long_exposure/config.yaml`. The minimal version:

```yaml
model: opus
context_window: 1000000
codex_model: gpt-5.5
codex_context_window: 400000
codex_yolo: true
model_tier: opus
compact_threshold: 0.90
philosophy: efficient
framework: staged
checkpoint_format: standard
```

The `model` field accepts aliases (`sonnet`, `opus`, `haiku`) or full
Claude model names. When `llm_provider: codex`, long-exposure uses
`codex_model` and budgets compaction against `codex_context_window`
(default 400k, so 90% compaction is 360k).

Codex runs use `codex exec --yolo` by default. That intentionally
bypasses Codex approvals and sandboxing so autonomous long-exposure
runs behave like the existing `claude -p` path. Run Codex-backed
long-exposure only inside an externally sandboxed VM/container or a
workspace where full file and command access is acceptable.

### Philosophy Presets

| Preset | Budget | Speed | Quality | Best For |
|--------|--------|-------|---------|----------|
| `efficient` | low | high | medium | Ship fast, stay within budget |
| `research` | high | low | high | Deep investigation, hypothesis-driven |
| `audit` | high | medium | high | Multi-cycle defect finding and fixing |
| `reporter` | medium | medium | high | Synthesizing completed work into reports |
| `custom` | you decide | you decide | you decide | Your own voice and tradeoffs |

### Framework Presets

| Preset | Stages | Transitions | Best For |
|--------|--------|-------------|----------|
| `staged` | Explore -> Plan -> Execute -> Test -> Document | Strict gates, one-step regression | Most tasks |
| `worker_staged` | Explore -> Plan -> Execute x3 -> Test -> Document | Strict, triage-driven execution | Worker agent (multi-deliverable cycles) |
| `audit` | Explore -> Execute -> Test -> Document | Strict, multi-cycle | Defect-driven auditing |
| `reporter` | Gather -> Outline -> Compose | Strict, forward-only | Report generation |
| `custom` | You define | You define | Domain-specific workflows |

### Other Settings

```yaml
# Checkpoint format: standard | minimal | verbose
checkpoint_format: standard

# Force agent to open every response with a checkpoint
require_checkpoint_first: false

# Require user approval at stage transitions
user_gate_approval: false

# Include named failure modes (The Leap, The Spiral, etc.) in the prompt
anti_patterns_enabled: true

# Compaction settings
compact_db: ./data/sessions.db
max_summary_pct: 0.15          # max % of context window for summaries
depth_compression: gentle      # gentle | aggressive
```

### Permissions

Since the orchestrator runs `claude -p` (non-interactive mode), there is no way to approve tool permissions interactively. All permissions must be pre-configured in `config.yaml`.

```yaml
# Directory the agent can access (absolute path).
# File tools are automatically scoped to this path.
# Leave empty ("") for no path restriction.
working_directory: /path/to/your/project

# Tools allowed without interactive approval
allowed_tools:
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Bash
```

**How it works:** File tools (`Read`, `Write`, `Edit`, `Glob`, `Grep`) are automatically scoped to `working_directory` using Claude Code's path permission syntax (e.g. `Read(//path/to/your/project/**)`). `Bash` is unrestricted by default. The MCP tool `search_sessions` is always added automatically.

**To configure for your environment**, change `working_directory` to your project root or home directory. The agent will only be able to read, write, and search files under that path.

**Restricting Bash commands:** Replace `Bash` with specific patterns to limit what the agent can run:

```yaml
allowed_tools:
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - "Bash(python3 *)"
  - "Bash(npm test)"
  - "Bash(git *)"
```

**Skip all permission checks** (not recommended — use only in isolated environments):

```yaml
allowed_tools: dangerously_skip_all
```

**If the agent hits a permission error**, the CLI call will fail and the orchestrator will print the error. To fix it, add the needed tool to `allowed_tools` in `config.yaml` and restart.

**Other tools you may want to add:**

| Tool | Purpose | When to add |
|------|---------|-------------|
| `WebFetch` | Fetch URLs | Agent needs to read web pages or APIs |
| `WebSearch` | Web search | Agent needs to search the internet |

### Custom Philosophy

Set `philosophy: custom` and provide your own values. Missing keys fall back to the `efficient` defaults:

```yaml
philosophy: custom
custom_philosophy:
  budget: medium
  speed: high
  quality: high
  complexity: low
  voice: |
    You are a startup engineer building an MVP. Speed matters,
    but this is going to production. Move fast, don't ship bugs.
  explore_depth: |
    Quick scan. One viable approach unless the problem is novel.
```

### Custom Framework

Set `framework: custom` and define your own stages with gates:

```yaml
framework: custom
custom_framework:
  transition_rule: strict
  regression_policy: one_step
  skip_policy: never
  max_regressions: 3
  stages:
    - name: read
      purpose: "Read the code under review in full."
      gates:
        - "Have I read every file in the changeset?"
        - "Can I describe what the change does?"
      output: "Changeset summary."
    - name: analyze
      purpose: "Evaluate correctness and maintainability."
      gates:
        - "Are findings categorized (blocking / suggestion / nit)?"
      output: "Categorized findings list."
```

## Architecture

```
long-exposure/
├── README.md
├── pyproject.toml                             # long-exposure console script
├── run_final_reporter.py                      # Standalone final-reporter entry
├── long_exposure/
│   ├── config.yaml                            # Your settings
│   ├── orchestrator.py                        # Interactive single-agent loop
│   ├── conductor.py                           # Multi-agent score execution
│   ├── exploration.py                         # Continuous exploration conductor
│   ├── exploration-score.yaml                 # Default exploration score
│   ├── mcp_search_server.py                   # MCP server for session tools
│   ├── curator.py                             # Output packaging
│   ├── fanout.py                              # Parallel-cycle fan-out
│   ├── reporting.py                           # Final report generation
│   ├── templates/
│   │   ├── philosophy-template.md             # Layer 1
│   │   ├── framework-template.md              # Layer 2
│   │   ├── operating-protocol-template.md     # Layer 3
│   │   └── session-summary-template.md        # Layer 4
│   ├── data/                                  # Created at runtime
│   │   ├── sessions.db                        # Single source of truth
│   │   ├── exploration_state.json             # Ephemeral exploration state
│   │   └── mcp_config.json                    # Generated at runtime
│   └── output/
│       └── exploration_status.md              # Overwritten each cycle
└── auto_compact/                              # Bundled context-persistence package
    ├── db.py                                  # SQLite schema + FTS5 search
    ├── compact.py                             # Depth-aware compression
    ├── proximity.py                           # Session relevance ranking
    └── cli.py                                 # `auto-compact` standalone CLI
```

**Dependencies:**
- one provider CLI: `claude`, `codex`, or `gemini`
- `pyyaml`, `anthropic`, `prompt_toolkit` — installed via pip
- `auto_compact` — bundled (formerly a sibling package)

## Bundled auto-compact

`auto_compact/` handles persistent context management: SQLite schema, FTS5 search, session storage/retrieval, depth-aware summary compression. It was previously a separate sibling repo; as of v0.2.0 it's bundled into this package under the same module name, so any code that does `from auto_compact.X import ...` still works.

`auto_compact` exposes its own `auto-compact` CLI (an interactive tool that uses the Anthropic API directly — needs `ANTHROPIC_API_KEY`). Long-exposure itself does NOT use that CLI; it consumes `auto_compact` purely as a Python library, and model calls go through the configured provider CLI.

## Continuous Exploration

A three-role loop (researcher → worker → auditor) that explores a domain autonomously, running indefinitely until stopped or topic-exhausted. Each role maintains persistent context across cycles via Claude Code session persistence. For day-to-day controls (start / stop / resume / clear / live guidance / concurrent instances), see [`docs/usage-guide.md`](docs/usage-guide.md). The subsections below document architecture-level semantics.

### Commands

All commands accept optional `--score`, `--config`, `--output`, `--state`, `--instance-dir` flags.

| Command | Effect |
|---------|--------|
| `long-exposure start` | Start exploration using task from score YAML |
| `long-exposure start "topic description"` | Start with inline task (archives + clears any existing state) |
| `long-exposure stop` | Send stop signal — finishes current agent, saves state |
| `Ctrl+C` | Same as `stop`, when watching the terminal |
| `long-exposure clear` | Stop + archive state + clear context for new topic |
| `long-exposure resume` | Continue from saved state, with the directive saved at stop time |
| `long-exposure resume "new directive"` | Resume and redirect to a new directive (keeps accumulated context) |
| `long-exposure resume --from-archive FILE` | Restore and continue a specific past exploration |

### Lifecycle

```
long-exposure start "topic A"       # begins cycles
  ... cycles run ...
long-exposure stop                  # saves state, exits
long-exposure resume                # picks up where it left off
  ... more cycles ...
long-exposure resume "Pivot to ..."  # redirect without clearing context
  ... more cycles ...
long-exposure clear                 # archives state, clears context
long-exposure start "topic B"       # fresh start, new topic
  ... cycles run ...
long-exposure resume --from-archive data/exploration_state_20260316T1400.json
                                    # revisit topic A from archived state
```

### How It Works

Each cycle runs three roles sequentially:

1. **Researcher** (philosophy: research) — reads the audit report from the previous cycle, determines the next sub-topic, produces a research brief.
2. **Worker** (philosophy: efficient) — reads the research brief, builds tools/models/scripts, produces concrete results.
3. **Auditor** (philosophy: audit) — validates the worker's results, decides: VALIDATED (move on), CONTINUE (same topic), or PIVOT (change direction).

The audit report feeds back to the researcher on the next cycle. There is no meta-orchestrator agent — the conductor is a deterministic loop. Control flow, cooldowns, compaction triggers, rate-limit rotation, and signal-file polling are all handled in Python, not the model.

### Emergent Parallelism

Long-exposure doesn't schedule parallelism — it lets the problem decide. Two mechanisms fire only when the structure of the work makes them worthwhile:

- **Parallel-cycle fan-out** (coarse-grained). The researcher may emit a `<parallel_cycle_fanout>` block naming up to three genuinely independent sub-problems. The conductor spawns each as a separate long-exposure clone (its own `--instance-dir`, seeded with the parent's agent sessions so each clone inherits gems + context), runs them concurrently under a wall-clock cap, then has the reporter merge their outputs. Recursive fan-out is blocked — clones stay depth-1. A gating protocol forces the researcher to self-check independence, own-audit, and iteration criteria before emitting the block; if any criterion fails, it stays linear.

- **Agent-teams fan-out** (fine-grained, within a turn). Worker and auditor can spawn up to three teammates for a single turn when they face embarrassingly-parallel sub-work (parameter sweeps, cross-comparisons, batch post-processing). Teammates inherit the lead's model, effort, and token budget verbatim; the team is created and deleted within the turn. Researcher / reporter / curator don't team by design — their work isn't parallelizable in that shape. See [`docs/parallelism.md`](docs/parallelism.md) for details.

Both mechanisms are opt-in-by-signal from the agent, not scheduled by the conductor. They cost quota when they fire (≈4× baseline for a 2-teammate turn, higher for a cycle fan-out), so the conditioning is deliberate about when to trigger them.

### End-of-run: reporter → final_reporter → curator

- **Reporter** runs every `loop.report_interval` cycles (default 3), consolidating the cycle range into a markdown report using session pointers from sessions.db.
- **Final reporter** runs once at shutdown (topic exhaustion, max_cycles reached, or stop). It assembles prior reports into a multi-stage synthesis (outline → body stages → finalize), renders to PDF via pandoc + tectonic when those are available, and applies a file-gate rescue so the output file lands on disk even if the agent tries to stuff content into an `[OUTPUT]` block.
- **Curator** packages the run's artifacts into a reusable Claude Code skill bundle (`MANIFEST.md`, `CURATION.yaml`, final report, cited source files). The result is a standalone skill you can drop into another machine or share.

### Context Persistence

Each agent type has its own Claude Code session UUID, persisted across cycles and stop/resume. Context accumulates naturally — the researcher remembers its prior reasoning, the worker remembers what it built, the auditor remembers what it validated.

When an agent's context exceeds the compact threshold (default 90% of 1M = 900k tokens), auto-compact triggers: the agent summarizes its context, the summary is stored in sessions.db, and a fresh session is bootstrapped with the summary.

### Data Storage

**sessions.db** is the single source of truth. Every agent output and compaction summary is stored with `record_type="exploration"` or `"compaction"`. All records are FTS-searchable.

**exploration_state.json** is ephemeral — it holds the current cycle, results, failure counters, and agent session UUIDs for resume. Overwritten each cycle.

When you **clear**, the state file is archived with a timestamp (e.g., `exploration_state_20260316T1400.json`) before being reset. Use `--resume <file>` to restore a previous exploration.

### Failure Handling

| Agent fails | Response |
|---|---|
| Researcher | Skip cycle, retry next cycle with same audit report |
| Worker | Pass failure marker to auditor, auditor decides next step |
| Auditor | Use fallback "CONTINUE", research stays on current topic |
| 3x same agent | 10s pause, then auto-retry |
| 3x any failure | 10s pause |
| 1+ total failure cycles | 2x base cooldown (rate limit likely) |

### Configuration

Edit `long_exposure/exploration-score.yaml`:

```yaml
task: |
  Your exploration directive here.

loop:
  max_cycles: null               # null = unlimited
  cycle_cooldown_seconds: 400    # pause between cycles (tier-dependent)
```

Each agent can override philosophy, framework, and model in the score file. See `exploration-score.yaml` for the full agent role definitions.

## Security

### Exploration Agent Permissions

Exploration agents (researcher, worker, auditor) are guided by the `allowed_tools` list in `exploration-score.yaml`.
Claude Code enforces this at `--allowedTools` level. Codex-backed runs use
`codex exec --yolo` by default, so Codex does not hard-enforce the Claude
allowlist; it receives the same workspace and command-boundary guidance,
uses `-C working_directory`, and gets `codex --search exec ...` only when
`WebSearch` is present.

The default score for this deployment permits:

| Tool | Scope | Enforcement |
|------|-------|-------------|
| Read, Write, Edit, Glob, Grep | `working_directory` only | Claude Code path permissions — hard enforcement |
| Bash | Pattern allowlist: `wolfram-batch *`, `wolfram *`, `wolframscript *`, `python *`, `python3 *`, `pip *`, `pip3 *`, `cmake *`, `make *`, `ls *`, `mkdir *`, `mv *`, `cp *`, `bash *` (build-wrapper scripts), and any project-specific wrappers you add | Claude Code `--allowedTools` pattern — hard enforcement |
| WebSearch | Unrestricted | Read-only, no exfiltration risk |
| Read/Glob/Grep on shared corpus | `//shared-corpus/**` (configure to your shared corpus path) | Read-only access to a shared knowledge corpus |
| MCP session tools | Own sessions.db | Scoped to configured DB path |

In Claude-backed runs, agents **cannot** run arbitrary shell commands, access
git/gh, read `.env` or credentials, modify the long-exposure codebase itself,
or execute binaries outside the allowlist. In Codex-backed runs, those
restrictions are soft guidance because `--yolo` is deliberately autonomous;
use an external VM/container boundary for hard isolation.

### Security Layers

| Layer | Protects against | Notes |
|-------|-----------------|-------|
| Claude's built-in safety | Malicious content generation | Baseline — no replication needed |
| File tool scoping | File access outside `working_directory` | Hard enforcement by Claude Code; Codex uses `-C working_directory` plus soft guidance under `--yolo` |
| Bash pattern allowlist | Shell commands outside the listed binaries | Hard enforcement by Claude Code; soft guidance for Codex under `--yolo` |
| Wolfram/Python interpreter shell functions (e.g. `Run[]`, `subprocess`, `system()`) | Not blocked at CLI level once the interpreter is allowed | Low practical risk in trusted-corpus settings — Claude's conditioning resists generating shell payloads through multi-agent research pipelines. Tighten `allowed_tools` if your corpus contains hostile input |
| VM / virtual desktop isolation | Cross-user access, system compromise | Recommended for multi-user deployment. The VM is the blast radius — nothing beyond it to compromise |

### Multi-User Deployment

For multi-user, each user needs:
- Own `working_directory` (file tool isolation)
- Own `sessions.db` (session data isolation)
- Own `exploration_state.json` (state isolation)
- VM or virtual desktop (OS-level blast radius)

OS-level containerization beyond VM isolation is not required. The realistic threat is resource abuse (infinite loops, disk fill), not security exploits. Address with timeouts and disk quotas at the hosting level.

## Changing Settings

**Safe to change anytime** (takes effect at next compaction):
- `philosophy`, `framework`, `checkpoint_format`
- `compact_threshold`, `anti_patterns_enabled`
- `require_checkpoint_first`, `user_gate_approval`

**Requires restart:**
- `model`, `context_window`, `compact_db`
- `working_directory`, `allowed_tools`

Edit `config.yaml` and either wait for the next compaction or type `/compact` to force one.

## Commands

| Input | Effect |
|-------|--------|
| `/compact` | Force immediate compaction |
| `quit` or `exit` | Exit the session |
| Ctrl+C / Ctrl+D | Exit the session |
