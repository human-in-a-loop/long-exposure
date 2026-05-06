# Gemini Integration Stage 1 Audit

This audit records the current long-exposure shape before Gemini changes.
It is change-neutral: no implementation changes were made for this stage.

## A. High-Level Conceptual View

Long-exposure is a deterministic research harness around provider CLIs. Python
owns lifecycle, cycle order, fan-out dispatch, account rotation, compaction,
workspace conventions, reporting, and final packaging. The model owns research
judgment and emits structured text that Python may parse.

Load-bearing properties to preserve:

| Property | Current Behavior | Gemini Implication |
|---|---|---|
| Deterministic control loop | `exploration.py` runs researcher -> worker -> auditor and decides when to report, compact, fan out, stop, and rotate. | Gemini must be another CLI adapter; it must not introduce a second scheduler. |
| Soft guidance layers | Philosophy, framework, protocol, role, gems, fan-out guidance, and team/subagent guidance are prompt text. | Gemini should receive the same layered prompt surface. |
| File-backed continuity | `sessions.db`, `exploration_state.json`, workspace files, POR, ledger, reports, and summaries are durable. | Provider sessions are disposable; memory remains provider-neutral. |
| Plan/free-tier usage | Claude and Codex paths use local CLIs and account homes, not pay-as-you-go APIs. | Gemini should default to Google-account / Code Assist auth, not Gemini API billing. |
| Emergent parallelism | Whole-cycle fan-out is Python subprocess topology; intra-turn helper guidance is provider-specific. | Gemini free tier can preserve process fan-out; native Gemini subagents cannot be assumed on Google-account auth. |

Stage 0 installed Gemini CLI `0.41.1` under
`gemini` and verified:

```bash
GOOGLE_GENAI_USE_GCA=true gemini --skip-trust -p "Reply with exactly: gemini-ok" --output-format json
```

The command returned JSON with `"response": "gemini-ok"` and usage stats under
`stats.models.gemini-3-flash-preview`. This confirms the Google-account /
Gemini Code Assist path works on this machine.

Context-limit finding: official Gemini CLI docs advertise a 1M-token context
window for Google-account use, and current Gemini API model docs list Gemini
2.5 Pro / Flash and Gemini 3 models at 1,048,576 input tokens or 1M input
tokens. I did not find official support for a 2M-token Gemini CLI free-plan
window. Treat 2M as unavailable for this integration unless Google changes the
CLI/model docs later.

## B. Meso-Scale Architecture

The repo already has partial provider abstraction from prior Codex/local work:

| Concern | Current Location | Gemini Fit |
|---|---|---|
| Provider normalization and env names | `long_exposure/provider.py` | Add `gemini` aliases and provider-specific env/state names. |
| Config defaults and model/context override | `long_exposure/orchestrator.py::load_config` and `long_exposure/config.yaml` | Add `gemini_model`, `gemini_context_window`, `gemini_yolo`, and auth/env defaults. |
| Stateless provider calls | `long_exposure/orchestrator.py::call_claude`, `_invoke_claude` | Extend existing CLI command builder/parser rather than renaming the whole stack. |
| Sessionful cycle calls | `long_exposure/exploration.py::_call_exploration_agent` | Gemini has `--session-id` and `--resume`; use the same shape as Claude where possible. |
| Compaction | `long_exposure/exploration.py::_compact_agent_session` | Use `gemini -p ... --output-format json --resume <session_id>` for summary extraction. |
| Account pool | `long_exposure/pool.py` via provider helpers | Disabled for Gemini for now; single authenticated Gemini CLI account only. |
| Fan-out clone pinning | `long_exposure/fanout.py` via provider helpers | Already provider-aware enough; verify Gemini env names flow through. |
| Intra-turn helpers | `orchestrator.build_team_guidance_block` | Add Gemini-specific guidance. Free-tier path should not promise native subagents. |
| MCP session search | Claude-only invocation today | Keep Claude-only initially; Gemini CLI MCP integration can be a later additive enhancement. |

The safest implementation keeps the public `call_claude` name for now because
many modules import it. Internally, provider branches already exist; Gemini can
join those branches with minimal surface-area churn.

## C. Low-Level Implementation Details

### Gemini CLI Surface

Observed and documented command surface:

- `gemini -p/--prompt` runs headless.
- `--output-format json` returns one JSON object with `response` and `stats`.
- `--output-format stream-json` exists but is not needed for this pass.
- `--skip-trust` is required for headless automation in untrusted directories.
- `--yolo` or `--approval-mode yolo` auto-accepts actions.
- `--approval-mode plan` is the read-only analogue for audits or dry runs.
- `--session-id` and `--resume` exist in the current help output.
- `--include-directories` can expand workspace scope, but long-exposure should
  keep using `cwd` / working-directory scoping first.

### Envelope Differences

Claude returns:

```json
{"result": "...", "usage": {...}, "session_id": "..."}
```

Gemini returns:

```json
{"response": "...", "stats": {"models": {...}}, "session_id": "..."}
```

Gemini usage needs normalization. The observed stats tree has per-model,
per-role token counts: `input`, `prompt`, `candidates`, `thoughts`, `total`,
and `cached`. Long-exposure only needs rough fields for compaction thresholds,
so the robust first mapping is:

| Existing Field | Gemini Source |
|---|---|
| `input_tokens` | max/sum of model `tokens.input` or `tokens.prompt` |
| `output_tokens` | max/sum of model `tokens.candidates` |
| `cache_creation_input_tokens` | 0 |
| `cache_read_input_tokens` | `tokens.cached` |

### Authentication and Pooling

The Google-account free-tier path can be selected with `GOOGLE_GENAI_USE_GCA=true`
and cached OAuth credentials. Gemini CLI's user config home is controlled by
`GEMINI_CLI_HOME`:

```bash
GEMINI_CLI_HOME=$HOME/.gemini-acctA GOOGLE_GENAI_USE_GCA=true gemini
```

Long-exposure does not currently enable Gemini multi-account pooling. Separate
Google-account homes need operator-managed OAuth setup and have not been
live-validated under the pool state machine.

### Tools and Permissions

Gemini's `--yolo` is a close match for autonomous long-exposure agent turns.
For disabled-tool summary calls, use `--approval-mode plan` rather than yolo.
Long-exposure maps the same `allowed_tools` scope into Gemini `tools.core`,
`tools.allowed`, and `--allowed-tools` entries. Exact Claude-style hard path
scoping still relies on `cwd`, prompt guidance, and external sandboxing.

### Subagents

Gemini CLI documents subagents, but the docs also say they are not available
with “Sign in with Google” for at least some subagent modes. Because the goal
explicitly includes Gemini free tier, native Gemini subagents cannot be a hard
dependency. The practical parity strategy is:

- Preserve Python whole-cycle fan-out unchanged.
- Inject Gemini guidance that explicitly says native Gemini subagents are not
  enabled and that real parallelism should use whole-cycle fan-out.
- Avoid claiming feature parity that would silently break on free-tier auth.

## D. Cross-Cutting Findings

| Finding | Impact | Handling |
|---|---|---|
| Existing Codex/local changes already made the architecture provider-aware. | Gemini can be small and additive. | Extend `provider.py`, config defaults, command builders, and parsers. |
| The old names `call_claude` and `ClaudeCliError` are now semantic legacy names. | Renaming them would touch many files and add risk. | Keep names for now; add Gemini branches internally. |
| Gemini JSON usage is nested and model-family-specific. | Exact token accounting may drift. | Normalize conservatively and keep compaction threshold at 90% of 1M. |
| Gemini free-tier native subagents are not reliable. | Full Claude agent-team parity cannot be promised through Gemini CLI alone. | Disable native Gemini subagents and preserve process fan-out. |
| MCP configuration is project-settings based. | Gemini does not take Claude's `--mcp-config` flag. | Write project-local `.gemini/settings.json` with sessions MCP and tool allowlists. |
| Gemini account pooling is not validated. | Misconfigured OAuth homes would produce fragile rotation failures. | Disable Gemini pooling for now; document as a known limitation. |
