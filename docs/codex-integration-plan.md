# Codex Integration Plan

Goal: make long-exposure LLM-flexible while preserving Claude Code Max plan
usage, Codex paid-plan CLI usage, the three-role cycle, soft guidance,
auto-compact, memory persistence, emergent parallelism, multi-account pooling,
and end-of-run reporting.

Primary constraint: simplicity over completeness. The integration should add
one provider boundary and leave orchestration behavior alone.

## Stage A: Provider Selection and Adapter Boundary

Add a small provider adapter module or equivalent functions with one public
responsibility: turn a normalized request into a normalized envelope.

Configuration:

```yaml
llm_provider: claude        # claude | codex
model: opus                 # provider-specific default remains valid
codex_model: gpt-5.5
codex_context_window: 400000
codex_yolo: true
codex_subagents:
  max_threads: 3
  max_depth: 1
```

Environment override:

```bash
LONG_EXPOSURE_LLM_PROVIDER=codex
```

Normalized call input:

| Field | Meaning |
|---|---|
| `prompt` | User turn text. |
| `system_prompt` | Layered conditioning. |
| `model` | Provider model id or alias. |
| `effort` | Provider effort/reasoning setting where supported. |
| `cwd` | Working directory for CLI subprocess. |
| `timeout` | Existing timeout behavior. |
| `session_id` | Existing provider session/thread id, optional. |
| `new_session` | Whether this is the first call for an agent. |
| `mcp_config` | MCP config path, if supported. |
| `permission_flags` | Provider-specific tool/sandbox flags. |

Normalized envelope:

```json
{
  "result": "assistant text",
  "usage": {},
  "duration_ms": 0,
  "session_id": "provider-session-or-thread-id"
}
```

Claude path must be behavior-preserving. Codex path should be additive and
disabled by default.

## Stage B: Codex CLI Invocation

Implement Codex using local `codex exec`:

Fresh call:

```bash
codex exec --yolo --json -m <model> -C <cwd> -o <tmp-last-message> <combined-prompt>
```

Resume call:

```bash
codex exec resume --yolo --json -m <model> -o <tmp-last-message> <thread_id> <prompt>
```

Prompt strategy:

- Fresh call wraps the layered system prompt above the agent user prompt.
- Resume call sends only the new user prompt.
- Compaction resumes the existing thread with the compaction prompt.
- If a compaction succeeds, the next fresh thread gets restored context in
  the first combined prompt, matching the current Claude behavior.

Parsing strategy:

- Read final text from `--output-last-message`.
- Parse JSONL stdout for `thread.started.thread_id` and
  `turn.completed.usage`.
- Convert Codex usage keys to the existing usage dict without requiring exact
  Claude field names.

Rate-limit strategy:

- Reuse broad rate-limit text signatures over stderr/stdout/final text.
- Treat non-zero exit with rate-limit text as provider rate-limit.
- Treat malformed JSONL as a CLI error, not success.

## Stage C: Account Pool Generalization

Keep the existing pool state machine. Make its env and state file names
provider-aware.

Claude:

| Purpose | Name |
|---|---|
| Pool | `CLAUDE_ACCOUNT_POOL` or `CLAUDE_ACCOUNTS` |
| Force pin | `CLAUDE_FORCE_ACCOUNT` |
| Child config | `CLAUDE_CONFIG_DIR` |
| State | `~/.claude-pool-state.json` |

Codex:

| Purpose | Name |
|---|---|
| Pool | `CODEX_ACCOUNT_POOL` or `CODEX_HOMES` |
| Force pin | `CODEX_FORCE_ACCOUNT` |
| Child config | `CODEX_HOME` |
| State | `~/.codex-pool-state.json` |

Do not change Claude variable behavior. Add Codex names alongside it.

## Stage D: Exploration Session Calls

Refactor only the agent-call and compaction chokepoints:

- `_call_exploration_agent`
- `_compact_agent_session`
- `call_claude`
- `_invoke_claude`

Keep return shapes, failure statuses, output parsing, compaction storage, and
cycle retry behavior unchanged.

`agent_sessions` should remain a dict keyed by agent name. The values are
provider session IDs and are tagged by active account as today.

## Stage E: Codex Subagent Guidance

Keep Claude agent-teams exactly as-is. Add provider-specific Codex subagent
guidance:

- Claude gets the current `<agent-teams>` block and env flag.
- Codex gets a `<subagents>` block that explicitly instructs worker/auditor
  leads to spawn Codex subagents for independent sub-work.
- Codex subagents inherit the lead's `--yolo` runtime posture and are capped
  by `codex_subagents.max_threads` and `codex_subagents.max_depth`.

This preserves the architecture without inventing a custom team scheduler.

## Stage F: Fan-Out Clone Pinning

Make clone env pinning provider-aware:

- Claude clones set `CLAUDE_FORCE_ACCOUNT` and remove `CLAUDE_ACCOUNTS`.
- Codex clones set `CODEX_FORCE_ACCOUNT` and remove `CODEX_ACCOUNT_POOL` /
  `CODEX_HOMES`.

Clone process topology, state seeding, barrier, merge synthesis, slot release,
and post-merge worker behavior stay unchanged.

## Stage G: Documentation and Verification

Update docs only after implementation:

- README requirements and quick start.
- `local-setup.md` provider setup.
- `multi-account-pool.md` provider-specific env vars.
- `parallelism.md` Claude agent-teams versus Codex subagents.
- `configuration-reference.md` provider config keys.

Verification:

1. Unit-level smoke for provider selection and command construction.
2. Live `codex exec` smoke using a harmless prompt.
3. Stateless `call_claude` compatibility smoke through default Claude path
   if Claude CLI is available.
4. Codex provider direct call smoke.
5. One miniature long-exposure run with Codex and `max_cycles: 1`, tools
   sandboxed to a temp workspace.

## Non-Goals

- No OpenAI API / Responses API integration in this pass.
- No custom Python scheduler for Codex teammates.
- No exact translation of every Claude permission flag.
- No migration of historical docs from Claude-first language except where
  operator setup would be misleading.
- No change to default provider; Claude remains the default.
