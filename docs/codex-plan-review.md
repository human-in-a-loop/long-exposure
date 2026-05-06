# Codex Integration Plan Review

This review checks the Stage 2 plan before implementation.

## Reviewed Assumptions

| Assumption | Evidence | Status |
|---|---|---|
| Codex has noninteractive CLI mode. | Local `codex exec --help`; smoke test returned `ok`. | Confirmed. |
| Codex can persist and resume sessions. | Local `codex exec resume --help` accepts a session/thread id; JSONL smoke emitted `thread.started.thread_id`. | Confirmed enough for implementation. |
| Codex has a direct `--system-prompt` equivalent. | Local help does not show one. | Rejected; first-turn prompt wrapping required. |
| Codex can use paid-plan auth without API keys. | Local CLI is authenticated under `~/.codex/auth.json`; help says auth still uses `CODEX_HOME` when user config is ignored. | Confirmed locally. |
| Codex subagents are the right analogue to Claude agent-teams. | Official Codex subagents docs say subagent workflows are enabled by default, explicit, and visible in CLI. | Confirmed. |
| Account pooling can be provider-neutral. | Existing pool state machine is env/path based. | Confirmed with provider-specific names. |

OpenAI documentation confirms Codex is a coding agent product and current Codex
models are optimized for agentic coding workflows; local CLI help is the source
of truth for installed command flags. Official docs used for product/model
context:

- https://developers.openai.com/
- https://developers.openai.com/api/docs/models

## Design Adjustments Before Implementation

1. Keep the public exception names `ClaudeCliError` and `ClaudeRateLimitError`
   for compatibility, but internally treat them as provider CLI errors. A
   broad rename would touch too much code for little value.
2. Keep `call_claude` as the public function name for now. Add provider-aware
   behavior underneath and document the compatibility naming debt in
   `docs/gaps.md`.
3. Avoid changing `conductor.py` unless tests show a need. It already routes
   through `call_claude`.
4. Translate Claude's autonomous permission posture to Codex with `--yolo`,
   `-C working_directory`, top-level `--search` when `WebSearch` is present,
   and prompt-level command boundaries.
5. Do not require Codex MCP support in the first implementation. If a Codex
   MCP config flag is not confirmed locally, leave session search available
   through prompt guidance and durable `sessions.db`, then log the gap.

## Implementation Checklist

| Step | Major Gap If Missed | Planned Fix |
|---|---|---|
| Provider selection from config/env. | Codex cannot be enabled without editing internals. | Add helper functions in orchestrator. |
| Codex JSONL parser and final-message file. | Output parsing sees empty result. | Normalize Codex output into existing envelope. |
| Session/thread storage. | Resume and compaction cannot work. | Store Codex `thread_id` in `agent_sessions`. |
| Provider-aware account env. | Multi-account Codex cannot work; Claude may regress. | Add provider-specific env helpers with Claude defaults. |
| Provider-aware pool state path. | Claude/Codex pool state collides. | Use separate state/lock files. |
| Fan-out pinning env. | Clones inherit wrong account or rotate unexpectedly. | Switch env names by provider. |
| Team block selection. | Codex prompts contain unusable Claude tool instructions. | Render provider-specific team guidance. |
| Live smoke. | Integration compiles but fails in the CLI boundary. | Run Codex direct call and a tiny Codex run. |
