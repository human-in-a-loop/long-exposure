# Gemini Integration Plan

Goal: make long-exposure support Gemini CLI on the Google-account free tier
while preserving the existing Claude paid-plan and Codex paid-plan paths. The
implementation should prefer small provider branches over new orchestration.

## Stage A: Provider Registration

Add `gemini` as a first-class provider in `long_exposure/provider.py`.

Configuration:

```yaml
llm_provider: gemini
gemini_model: gemini-3-flash-preview
gemini_context_window: 1000000
gemini_yolo: true
gemini_auth_env: GOOGLE_GENAI_USE_GCA
gemini_auth_value: "true"
```

Environment names:

| Purpose | Gemini Name |
|---|---|
| Provider override | `LONG_EXPOSURE_LLM_PROVIDER=gemini` |
| Pool | Disabled for now |
| Force pin | `GEMINI_FORCE_ACCOUNT` |
| Child config dir | `GEMINI_CLI_HOME` (not used for pooling yet) |
| Pool state | `~/.gemini-pool-state.json` |
| Rotation state | `~/.gemini-accounts-state.json` |

Keep Claude as the default.

## Stage B: Gemini CLI Adapter

Extend the existing CLI invocation boundary.

Fresh session:

```bash
gemini --skip-trust --output-format json --session-id <uuid> \
  -m <model> --yolo -p <combined system/user prompt>
```

Resume session:

```bash
gemini --skip-trust --output-format json --resume <session-id> \
  -m <model> --yolo -p <user prompt>
```

Summary/disabled-tool call:

```bash
gemini --skip-trust --output-format json --resume <session-id> \
  -m <model> --approval-mode plan -p <compaction prompt>
```

Normalize Gemini output to the existing envelope:

```json
{"result": "...", "usage": {...}, "duration_ms": 0, "session_id": "..."}
```

Rate-limit detection should reuse the current broad text signatures over
stderr/stdout/JSON `error.message`.

## Stage C: Config and Prompt Guidance

Update config defaults and docs. Gemini receives the same philosophy,
framework, protocol, role, gems, workspace, ledger, figure, and fan-out
guidance as Claude/Codex.

Add a Gemini-specific helper block for worker/auditor turns:

- Prefer whole-cycle fan-out for independent work that needs its own audit.
- Use Gemini native delegation only if available in the authenticated CLI.
- Never block waiting for unavailable subagents.

This is less glamorous than promising Claude-equivalent teams, but it matches
Gemini free-tier constraints and keeps the run from stalling.

## Stage D: Fan-Out Verification and Pool Limitation

Gemini multi-account pooling is disabled for now. Fan-out does not require
multi-account pooling: root Python launches clone processes, and each clone
creates its own Gemini CLI session/thread against the same authenticated
account.

Verify:

- `GEMINI_ACCOUNT_POOL` and `GEMINI_HOMES` are ignored.
- Concurrent Gemini CLI sessions work.
- Clone process topology remains provider-neutral.

## Stage E: Tests

Add focused unit tests before live tests:

1. Provider normalization and env/state path selection for Gemini.
2. `load_config` applies `gemini_model` and `gemini_context_window`.
3. Gemini JSON envelope parsing maps `response` to `result` and stats to usage.
4. Gemini command construction includes `--skip-trust`, `--output-format json`,
   `--session-id` or `--resume`, and `--yolo` for normal turns.
5. Disabled-tool Gemini command uses `--approval-mode plan`.

## Stage F: Live Smoke

Run:

```bash
LONG_EXPOSURE_LLM_PROVIDER=gemini \
GOOGLE_GENAI_USE_GCA=true \
gemini --skip-trust -p "Reply with exactly: gemini-ok" --output-format json
```

Then run a direct adapter smoke through `call_claude` with Gemini selected.
If that passes, run a miniature long-exposure one-cycle smoke in a temp
workspace with a tiny score and no destructive task.

## Non-Goals

- No Gemini API / Vertex implementation in this pass.
- No hard dependency on Gemini native subagents for the free tier.
- No perfect Claude tool allowlist translation.
- No broad rename from `call_claude` to provider-neutral names.
- No change to Claude default behavior.
