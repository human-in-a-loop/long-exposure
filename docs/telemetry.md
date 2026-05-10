# Opt-In Telemetry

Long-exposure telemetry is disabled by default. When enabled, it writes local
append-only JSONL events that help analyze reliability, provider/account usage,
fan-out outcomes, report health, manager interventions, and agent-call metadata.

Telemetry is passive. It must not control routing, prompts, retries, manager
decisions, or final reporting. If telemetry fails, the run continues.

## Enable

In `long_exposure/config.yaml`:

```yaml
telemetry:
  enabled: true
  level: standard
```

Or for one run:

```bash
LONG_EXPOSURE_TELEMETRY=1 long-exposure launch "<directive>"
```

## Files

For an instance directory `DIR`, telemetry writes:

```text
DIR/telemetry/
  events.jsonl
  telemetry_manifest.json
  rollups/
    summary.json
    summary.md
  lessons/
    lessons_summary.md
```

Legacy single-session runs use `long_exposure/data/telemetry/`.

## Captured By Default

- run start/resume/end metadata
- cycle start/end, duration, failure counters, low-output counters
- agent call status, duration, output keys, token usage
- provider/model metadata
- account usage snapshots when available
- reporter markdown/PDF status
- fan-out start/collapse outcomes
- manager poll verdicts and guide writes

Telemetry does **not** capture full prompts, full responses, tool stdout/stderr,
environment variables, credentials, or provider account paths by default.
The event writer also sanitizes common sensitive field names (`prompt`,
`response`, `stdout`, `stderr`, `env`, `messages`, etc.) centrally, so future
call sites cannot accidentally record those fields unless the matching opt-in
flag is enabled.

## Summarize

```bash
long-exposure --instance-dir DIR telemetry summarize
```

This reads `DIR/telemetry/events.jsonl` and writes deterministic rollups under
`DIR/telemetry/rollups/`, plus a deterministic lessons shell under
`DIR/telemetry/lessons/` for later human or agent review.

## Configuration

```yaml
telemetry:
  enabled: false
  level: standard
  output_dir: null
  include_prompt_text: false
  include_response_text: false
  include_tool_stdout: false
  max_text_field_chars: 2000
  max_event_bytes: 65536
  redact_paths: false
  redact_env: true
```

`output_dir` defaults to `<instance-dir>/telemetry`.

The `include_*` flags are explicit privacy opt-ins:

- `include_prompt_text: true` permits prompt/directive/message fields.
- `include_response_text: true` permits response/output/transcript fields.
- `include_tool_stdout: true` permits tool stdout/stderr fields.
- `redact_env: false` permits environment maps.

Leave these disabled for normal runs. The default event stream is intended for
reliability and usage analysis, not transcript capture.

## Design Guarantees

- Disabled by default.
- Local-only.
- Append-only event stream.
- Schema-versioned events.
- Public telemetry functions catch exceptions.
- Event size is bounded.
- Rollups are derived and can be regenerated.
- Future agentic analysis should consume rollups or lessons files, not the hot
  control path.

## Future Agentic Use

Future manager or analyst agents can consume telemetry rollups to identify
patterns such as repeated tool failures, audit follow-through gaps, fan-out
branches that finish without deliverables, or provider-specific rate-limit
patterns. Those agents should treat telemetry as evidence for review, not as a
control signal that changes an active cycle in real time.
