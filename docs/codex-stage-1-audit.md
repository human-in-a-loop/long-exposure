# Codex Integration Stage 1 Audit

This audit records the existing shape of long-exposure before any Codex
integration work. It is intentionally provider-aware but change-neutral.

## A. High-Level Conceptual View

Long-exposure is a deterministic research harness around one paid-plan CLI
model backend. The Python process owns lifecycle, persistence, fan-out,
rate-limit recovery, workspace conventions, and reporting. The model owns
research judgment and produces structured text signals that Python may parse.

Load-bearing principles:

| Principle | Existing Behavior | Integration Implication |
|---|---|---|
| Python controls flow | Cycles, compaction, fan-out dispatch, reporter cadence, daily sync, stop/clear, and rotation are Python decisions. | Provider integration must be a subprocess adapter, not a second scheduler. |
| Soft guidance over hard gates | POR, ledger, figure discipline, citations, and structure are instructions plus validators. | Codex prompts must receive the same guidance surface. |
| Paid plan, not API billing | Claude calls use `claude -p` and account config dirs, not API keys. | Codex should use ChatGPT/Codex CLI authentication and `CODEX_HOME`, not Responses API calls. |
| File-backed continuity | `sessions.db`, workspace files, ledgers, reports, and summaries are account-portable. | Provider session IDs are disposable; durable memory stays in long-exposure. |
| Emergent parallelism | Researcher emits whole-cycle fan-out; worker/auditor may use agent-teams. | Codex integration must preserve both scales, with provider-specific team mechanics only inside the adapter/prompt layer. |

The core architecture is provider-neutral in intent but Claude-specific in
names and flags. The safest integration path is to make the CLI boundary
provider-aware while leaving the research cycle and file-state model intact.

## B. Meso-Scale Architecture

Primary subsystems:

| Subsystem | Key Files | Provider Coupling |
|---|---|---|
| Cycle loop | `long_exposure/exploration.py` | Strong: builds `claude -p`, manages Claude session UUIDs, team residue paths, env vars. |
| Prompt assembly | `long_exposure/orchestrator.py`, `long_exposure/templates/` | Mostly neutral: layered prompts are plain text. Team block is Claude-specific. |
| Stateless conductor | `long_exposure/conductor.py` | Medium: calls `call_claude`, otherwise neutral. |
| CLI invocation and rate limits | `long_exposure/orchestrator.py` | Strong: `_invoke_claude`, `call_claude`, Claude JSON envelope assumptions. |
| Account pool | `long_exposure/pool.py` | Strong: env names and state files are Claude-specific, but state machine is generic. |
| Fan-out clones | `long_exposure/fanout.py` | Medium-strong: clone pinning uses `CLAUDE_FORCE_ACCOUNT`; process model is neutral. |
| Persistence | `auto_compact/*`, `sessions.db` | Neutral. Stores summaries and outputs, not provider transcripts. |
| End-of-run agents | `reporting.py`, `auditing.py`, `curator.py` | Mostly neutral through `_call_agent_with_rotation`. |
| MCP session search | `mcp_search_server.py`, `generate_mcp_config` | Claude-specific invocation flag, but MCP server itself is neutral. |

The integration choke points are small in count but deep in consequence:

1. Sessionful agent calls in `exploration._call_exploration_agent`.
2. Session compaction in `exploration._compact_agent_session`.
3. Stateless calls in `orchestrator.call_claude`.
4. Single-attempt invocation and rate classification in `orchestrator._invoke_claude`.
5. Account/env selection in `orchestrator._active_account_dir`, `_parse_accounts`, `_resolve_force_account`, and `pool.parse_pool_config`.
6. Fan-out env pinning in `fanout._spawn_clone`.
7. Agent-team prompt/env handling in `orchestrator.build_team_guidance_block` and `_call_exploration_agent`.

## C. Low-Level Implementation Details

### Claude Session Model

Long-exposure currently creates a UUID itself and passes it to Claude with
`--session-id` on first call, then uses `--resume <uuid>` on later calls.
The system prompt is only sent on first call and retained by Claude.

Codex CLI behavior differs:

- `codex exec --json` emits JSONL events.
- The `thread.started` event contains `thread_id`.
- `-o/--output-last-message` writes the final assistant message to a file.
- `codex exec resume <thread_id>` resumes a prior session.
- There is no `--system-prompt` flag in the local `codex exec` help; the
  adapter must wrap the layered prompt into the first user prompt.

This means `agent_sessions` can stay as a map from agent name to provider
session/thread ID, but the provider must own how first-call bootstrapping
works.

### Output Envelopes

Claude returns one JSON object with `result`, `usage`, and sometimes error
fields. Codex JSONL events include at least:

- `thread.started` with `thread_id`.
- `item.completed` with assistant message text.
- `turn.completed` with `usage`.

The adapter should normalize both into the existing envelope shape:

```json
{"result": "...", "usage": {...}, "duration_ms": 123, "session_id": "..."}
```

### Rate-Limit Semantics

Claude detection currently checks non-zero stderr/stdout, `api_error_status`,
and `is_error` result text. Codex must get a parallel detector over exit code,
JSONL error events, stderr, and final output. The exact text signatures can
reuse the current broad list because false positives recover by rotation and
false negatives silently degrade runs.

### Tools and Permissions

Claude tools are controlled through `--allowedTools`, `--disallowedTools`,
`--mcp-config`, and the Claude Code permission model. Codex does not expose
the same per-tool allowlist surface in `codex exec`. The implemented Codex
translation is:

- Run normal Codex agent turns with `--yolo` so non-interactive runs do not
  stall on approvals, matching long-exposure's autonomous `claude -p` posture.
- Set Codex `-C` to `working_directory`.
- Put `--search` at top level (`codex --search exec ...`) when `WebSearch`
  appears in the allowlist.
- Keep Bash/file restrictions as soft guidance and require VM/container
  isolation for hard boundaries.

### Codex Subagents

Claude agent-teams are a native experimental feature enabled with
`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, and the prompt tells leads how to
spawn teammates. Codex uses native subagent workflows instead. Current Codex
docs say subagents are enabled by default, spawn only when explicitly asked,
inherit the lead's sandbox/approval posture including `--yolo`, and are capped
by `agents.max_threads` / `agents.max_depth`. Long-exposure injects a Codex
`<subagents>` block for Codex-backed worker/auditor turns.

### Account Pool

The state machine is generic: account dirs, primary/cold/overflow/cooling,
slot holders, cooldown, promotion, usage counters. The names are not:
`CLAUDE_ACCOUNT_POOL`, `CLAUDE_ACCOUNTS`, `CLAUDE_FORCE_ACCOUNT`,
`CLAUDE_CONFIG_DIR`, and `~/.claude-pool-state.json`.

Codex can mirror this with:

- `CODEX_ACCOUNT_POOL` or `CODEX_HOMES`.
- `CODEX_FORCE_ACCOUNT`.
- `CODEX_HOME` in child env.
- `~/.codex-pool-state.json`.

The simplest robust implementation is provider-aware env/state naming inside
the existing pool functions, not a second pool implementation.

## D. Cross-Cutting Findings

| Finding | Impact | Recommended Handling |
|---|---|---|
| Provider coupling is concentrated but semantically important. | Small edits can break rotation, compaction, and resume. | Introduce one adapter layer and keep old Claude names as compatibility wrappers. |
| Durable memory is already provider-neutral. | Codex does not need Claude transcript portability. | Treat provider sessions as cache, `sessions.db` as source of truth. |
| Prompt layers are the strongest preservation mechanism. | Feature parity depends more on prompt continuity than CLI flags. | Reuse the exact layered prompt for both providers. |
| Tool permission models cannot be perfectly translated. | Overfitting translation would be fragile. | Start with simple sandbox/search/cwd mapping and soft guidance. |
| Intra-turn helpers are provider-specific. | Claude uses agent-teams; Codex uses subagents. | Keep Python fan-out identical; inject provider-specific guidance for the intra-turn helper. |
| Multi-account pooling should stay file/env based. | API-style rate tiers would violate the paid-plan constraint. | Add provider-specific env names and state files. |
| Documentation is Claude-first throughout. | Operators will misconfigure mixed-provider runs. | Update docs after implementation, but keep Claude defaults unchanged. |
