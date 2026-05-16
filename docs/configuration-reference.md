# Configuration Reference

Complete reference for the two YAML files that drive a long-exposure
run. `config.yaml` carries deployment-wide knobs; the score YAML
(default `exploration-score.yaml`) carries the cycle definition and
per-agent overrides.

**Sources of truth:** `long_exposure/config.yaml`,
`long_exposure/exploration-score.yaml`, `long_exposure/orchestrator.py`
(`PHILOSOPHY_PRESETS`, `FRAMEWORK_PRESETS`,
`PHILOSOPHY_EFFORT_MAP`).

---

## config.yaml

A single-file deployment configuration. Edit values; structure is
fixed.

### Model

```yaml
llm_provider: claude       # claude | codex | gemini | local
model: opus               # alias (sonnet, opus, haiku) or full name
codex_model: gpt-5.5
gemini_model: gemini-3-flash-preview
local_model: custom-local-model
local_base_url: http://127.0.0.1:18080/v1
local_context_window: 32768
local_max_tokens: 2048
local_recent_log_pct: 0.25
local_compact_max_tokens: 4096
local_temperature: 0.2
local_top_p: 0.95
context_window: 1000000
codex_context_window: 400000
gemini_context_window: 1000000
codex_yolo: true
gemini_yolo: true
gemini_auth_env: GOOGLE_GENAI_USE_GCA
gemini_auth_value: "true"
codex_subagents:
  max_threads: 3
  max_depth: 1
model_tier: opus
cli_timeout: 0            # seconds per claude -p call (0 = no timeout)
```

| Key | Meaning |
|---|---|
| `llm_provider` | Backend: `claude` by default, `codex`, `gemini`, or `local`. `local` is an unsupported OpenAI-compatible extension point, not a native long-exposure model path. Can be overridden with `LONG_EXPOSURE_LLM_PROVIDER`. |
| `model` | Claude model alias passed to `claude -p --model`. For Codex, agent calls use `codex_model` unless an agent explicitly overrides `model`. |
| `codex_model` | Codex CLI model used when `llm_provider: codex`. |
| `gemini_model` | Gemini CLI model used when `llm_provider: gemini`. Default is `gemini-3-flash-preview`, the robust Google-account/free-tier Gemini 3 model verified live. `gemini-3-pro-preview` is more capable but was not dependable on the free-tier path in live testing (`capacity exhausted`). |
| `local_model` | Operator-supplied model alias served by an OpenAI-compatible endpoint when `llm_provider: local`. |
| `local_base_url` | Operator-supplied OpenAI-compatible API base URL. |
| `local_context_window` | Operator-supplied local context budget used for compaction math and prompt budget guidance. |
| `local_max_tokens` | Maximum completion tokens requested from the local endpoint. |
| `local_recent_log_pct` | Fraction of local context reserved for injecting recent per-agent JSONL transcript memory. Default `0.25`, about 8k tokens at 32k context. |
| `local_compact_max_tokens` | Maximum tokens requested for local transcript compaction summaries. |
| `local_temperature` | Local sampling temperature. |
| `local_top_p` | Local nucleus sampling value. |
| `context_window` | Claude context budget used to compute compaction thresholds and budget pressure ranges |
| `codex_context_window` | Codex context budget. When `llm_provider: codex`, this overrides `context_window`; default `400000`, so compaction fires at `360000` tokens with `compact_threshold: 0.90`. |
| `gemini_context_window` | Gemini context budget. When `llm_provider: gemini`, this overrides `context_window`; default `1000000`, matching Gemini CLI's advertised 1M Google-account window. |
| `codex_yolo` | When true, normal Codex agent turns run with `codex exec --yolo`, bypassing approvals and sandboxing. This is the Codex analogue of long-exposure's autonomous `claude -p` posture; use only in an externally sandboxed environment. |
| `gemini_yolo` | When true, normal Gemini agent turns run with `gemini --yolo`; compaction uses `--approval-mode plan`. Long-exposure also writes project-local `.gemini/settings.json` tool/MCP settings for the agent's current permission scope. |
| `gemini_auth_env` / `gemini_auth_value` | Default Gemini auth selector. The default sets `GOOGLE_GENAI_USE_GCA=true` when no Gemini API/Vertex auth env is already set, keeping the integration on the Google-account / Code Assist path rather than API pay-as-you-go. |
| `codex_subagents` | Codex subagent runtime caps. `max_threads` limits concurrent child threads; `max_depth: 1` permits direct children but prevents recursive subagent trees. |
| `model_tier` | Used by template substitution; selects philosophy-tier-specific phrasing |
| `cli_timeout` | Per-provider CLI call timeout; `0` disables. Override per-agent in score |

### Compaction

```yaml
compact_threshold: 0.90
compact_db: ./data/sessions.db
max_summary_pct: 0.15
depth_compression: gentle
compact_xml_retries: 5    # bounded retry on malformed compaction summary XML
```

| Key | Meaning |
|---|---|
| `compact_threshold` | Fraction of `context_window` that triggers auto-compact |
| `compact_db` | Path to `sessions.db` (relative or absolute) |
| `max_summary_pct` | Soft guidance to model on summary size (15% × 1M = 150k tokens). Not enforced |
| `depth_compression` | `gentle` (current) or `aggressive`. Influences depth-aware compaction prompt |
| `compact_xml_retries` | Bounded retry on malformed XML summary; default 5. After exhaustion, store as-is with off-nominal event |

### Fan-out merge synthesis

```yaml
merge_synthesis_min_branches: 4
ledger_graph:
  enabled: true
anti_patterns:
  enabled: true
  max_entries: 5
  max_rationale_chars: 200
```

When a fan-out collapses with ≥ this many branches, the reporter
agent is invoked to compress N raw merge_reports into one bounded
synthesis. Below the threshold, raw concatenation goes through
unchanged. See `parallelism.md`.

`ledger_graph.enabled` controls the read-only ledger causal summary injected
into final auditor and final reporter stages. `anti_patterns` controls the
ledger-derived `<campaign_anti_patterns>` block injected into live guidance
when the latest event for a milestone is still high/medium-confidence
`invalidated`. Both default to enabled and fail closed to empty strings.

### Agent-teams

```yaml
agent_teams_defaults:
  enabled: true
  max_teammates: 3
  allow_peer_messages: true
  cleanup_residue: true
  teammate_response_budget_tokens: 20000
```

See `parallelism.md` for the full enable/inheritance story.

### Philosophy

```yaml
philosophy: efficient   # one of: efficient | research | audit | reporter | custom
```

Determines the system-prompt voice (layer 1) and the default
effort level. Five presets:

| Preset | Budget | Speed | Quality | Best for |
|---|---|---|---|---|
| `efficient` | low | high | medium | Ship fast, stay within budget |
| `research` | high | low | high | Deep investigation, hypothesis-driven |
| `audit` | high | medium | high | Multi-cycle defect finding and fixing |
| `reporter` | medium | medium | high | Synthesizing completed work into reports |
| `custom` | you decide | you decide | you decide | Your own voice and tradeoffs |

To use a custom philosophy:

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

Missing keys fall back to `efficient` defaults.

### Framework

```yaml
framework: staged   # one of: staged | worker_staged | audit | reporter | custom
```

Determines stages and transition rules (layer 2). Five presets:

| Preset | Stages | Transitions |
|---|---|---|
| `staged` | Explore → Plan → Execute → Test → Document | Strict gates, one-step regression |
| `worker_staged` | Explore → Plan → Execute×3 → Test → Document | Strict, triage-driven |
| `audit` | Explore → Execute → Test → Document | Multi-cycle, defect-driven |
| `reporter` | Gather → Outline → Compose | Strict, forward-only |
| `custom` | You define | You define |

For `custom`:

```yaml
framework: custom
custom_framework:
  transition_rule: strict
  regression_policy: one_step
  skip_policy: never
  max_regressions: 3
  stages:
    - name: read
      purpose: "..."
      gates: ["...", "..."]
      output: "..."
    - name: analyze
      ...
```

### Protocol

```yaml
checkpoint_format: standard       # standard | minimal | verbose
require_checkpoint_first: false   # require agent open every response with checkpoint
user_gate_approval: false         # require user approval at stage transitions (interactive only)
anti_patterns_enabled: true       # include named failure modes (Spiral, Leap, etc.)
```

### Permissions

```yaml
working_directory: /path/to/your/project   # absolute; scopes file tools

wolfram_path: "wolfram-batch"   # empty disables Wolfram guidance

allowed_tools:
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Bash
  - WebSearch
```

File tools (`Read`, `Write`, `Edit`, `Glob`, `Grep`) are automatically
scoped to `working_directory`. Bash is unrestricted by default; you
can pattern-restrict:

`wolfram-batch` is a bundled console command compatible with
`wolfram -script file.wls`. It runs scripts through the interactive
`wolfram` kernel, which covers installations where interactive Wolfram
works but `wolfram -script` or `wolframscript` hits licensing startup
problems. If `wolfram` is not on `PATH`, set `WOLFRAM_BIN=/path/to/wolfram`.

```yaml
allowed_tools:
  - Read
  - Write
  - "Bash(python3 *)"
  - "Bash(npm test)"
  - "Bash(git status)"
```

To bypass all permission checks (use only in isolated environments):

```yaml
allowed_tools: dangerously_skip_all
```

Provider translation:

| Concern | Claude | Codex | Gemini |
|---|---|---|---|
| Non-interactive autonomous execution | `claude -p` with explicit tool flags | `codex exec --yolo` by default (`codex_yolo: true`) | `gemini --yolo --skip-trust` by default (`gemini_yolo: true`) |
| File-tool scope | `Read/Write/Edit/Glob/Grep` are converted to scoped `--allowedTools` entries under `working_directory` | `-C working_directory` plus long-exposure directory-boundary guidance; `--yolo` deliberately removes Codex sandbox prompts | `cwd=working_directory`, Gemini `tools.core` / `--allowed-tools` mapped from the same long-exposure allowlist, plus directory-boundary guidance |
| Bash | Allowed or pattern-restricted through Claude `Bash(...)` allowlist entries | Allowed by `--yolo`; pattern restrictions remain soft guidance in the prompt | Mapped to Gemini `run_shell_command` or `run_shell_command(command)` where a command prefix is present |
| Web search | `WebSearch` allowed through Claude tool flags | Top-level `--search` is added (`codex --search exec ...`) when `WebSearch` is present | Mapped to Gemini `google_web_search`; `WebFetch` maps to `web_fetch` when used |
| Tool-disabled summary calls | `--tools ""` | read-only Codex sandbox where supported | `--approval-mode plan` |
| Parallel turn helpers | Claude agent-teams | Codex subagents, inheriting the lead's `--yolo` runtime posture | No native subagents; use whole-cycle fan-out for parallel Gemini threads |

Generic local connector translation:

| Concern | Local behavior |
|---|---|
| Runtime | Operator-supplied OpenAI-compatible HTTP endpoint. This is an extension point, not a natively supported long-exposure backend. |
| Tool execution | No native tool bridge. Workspace and tool guidance remain soft prompt guidance. |
| Session search MCP | Not advertised in prompts unless a provider actually receives MCP tools. Inline gems still appear as summaries. |
| Context persistence | Backend calls are stateless, but long-exposure writes per-agent local JSONL transcripts, injects bounded recent logs, and compacts them into `sessions.db`. |
| Parallel turn helpers | No native local equivalent yet. Whole-cycle fan-out remains in Python. |
| Tools | No executable local tool bridge. Tool permissions are prompt guidance only for the generic local connector. |

### Context proximity

```yaml
context_proximity:
  enabled: true
  max_gems: 7
  min_score: 0.3

relevance_profiles:
  efficient:
    topic_weights:
      _same_topic: 1.0
      _same_subtopic: 0.5
      _ancestor: 0.0       # opt-in parent-chain boost
      testing: -0.3        # named topic; can boost or penalise
    tool_weights:
      _shared_tools: 0.3
    keyword_weights:
      breaking_change: 0.4
      constraint: 0.3
  research:
    ...
  audit:
    ...
  reporter:
    ...
```

Each profile is keyed by philosophy name. Special keys
(`_same_topic`, `_same_subtopic`, `_any_topic`, `_ancestor`,
`_shared_tools`) are detected by `proximity.score_session`. `_ancestor`
defaults to `0.0`, so parent-chain scoring is opt-in. Named topics and
keywords add direct boosts (or penalties — negative values are valid).

See `persistence-and-gems.md` for the full scoring model.

---

## Score YAML (exploration-score.yaml)

A score defines the cycle: which agents run, in what order, with what
inputs and outputs. The default `long_exposure/exploration-score.yaml`
is the typical research configuration.

### Top-level structure

```yaml
task: |
  <directive — the original prompt>

metadata:
  name: "Continuous Exploration"
  version: "1.0"
  mode: continuous

seed:
  starting_subtopic: null
  starting_tools: null

citations: |
  <shared citation conventions injected into agents that don't
   define their own>

loop:
  max_cycles: null               # null = unlimited
  cycle_cooldown_seconds: 400
  report_interval: 3
  daily_sync_interval_hours: 24
  min_clone_cycles_before_preempt: 1
  barrier_preempt_timeout_seconds: 3600
  # Planned 24h rotation. Defaults to daily_sync_interval_hours
  # when unset. Pre-emptively rotates the primary after each daily sync
  # iff no rate-limit-driven rotation has happened in the prior window.
  # planned_rotation_min_age_hours: 24

allowed_tools:                   # score-level override of config.yaml
  - Read
  - Write
  ...

agents:
  researcher: { ... }
  worker: { ... }
  auditor: { ... }
  reporter: { ... }
  final_reporter: { ... }
  final_auditor: { ... }
  curator: { ... }

flow:                            # cycle order
  - researcher
  - worker
  - auditor
```

### Loop knobs

| Key | Default | Meaning |
|---|---|---|
| `max_cycles` | `null` | Unlimited if null. Stop after N cycles otherwise. |
| `cycle_cooldown_seconds` | `400` | Pause between cycles. `2 ×` this on failure cycles (adaptive cooldown). |
| `report_interval` | `3` | Periodic reporter runs every N cycles. |
| `daily_sync_interval_hours` | `24` | Wall-clock interval for end-of-run pipeline in revise mode. See `end-of-run-pipeline.md`. |
| `min_clone_cycles_before_preempt` | `1` | Clones must complete this many cycles before being eligible for graceful preemption. See `parallelism.md`. |
| `barrier_preempt_timeout_seconds` | `3600` | Backup timer for preemption when no organic exit has happened. |
| `planned_rotation_min_age_hours` | (defaults to `daily_sync_interval_hours`) | Minimum age of the last rotation before a planned rotation will fire after the next daily sync. Set to a value larger than `daily_sync_interval_hours` to space planned rotations farther apart than syncs. |

### Agent definitions

Each agent under `agents:` accepts:

```yaml
agents:
  worker:
    philosophy: efficient            # override config.yaml
    framework: worker_staged         # override config.yaml
    effort: high                     # explicit override of philosophy default
    model: opus                      # override config.yaml
    model_tier: opus                 # override
    working_directory: /path         # override
    allowed_tools: [...]             # override score-level + config.yaml
    cli_timeout: 36000               # 10h; override config.yaml
    mcp: true                        # connect to MCP search server
    agent_teams: true                # enable agent-teams (gated by master switch)
    disable_tools: false             # if true, claude -p --disallowedTools all
    inputs: [directive, research_brief, live_guidance, plan_of_record, promise_ledger_summary]
    outputs: [work_output]
    role: |
      <role text — agent's prompt body>
```

Custom philosophy / framework can be set per-agent:

```yaml
worker:
  custom_philosophy:
    budget: low
    speed: high
    voice: "..."
    explore_depth: "..."
  custom_framework:
    transition_rule: strict
    stages: [...]
```

### Inputs and outputs

The harness validates every agent's `inputs:` list at score load
time. Each declared input must come from one of three sources:

1. **Runtime allowlist** (`RUNTIME_INPUTS` constant in
   `exploration.py`): names the harness injects.
   - Cycle inputs: `directive`, `audit_report`, `live_guidance`,
     `plan_of_record`, `promise_ledger_summary`,
     `research_brief`, `work_output`, `starting_subtopic`,
     `starting_tools`.
   - Reporter inputs: `cycle_range`, `cycle_sessions`,
     `report_basename`, `working_dir`.
   - Stage inputs (final reporter / final auditor):
     `stage`, `total_stages`, `stage_index`, `expected_file`,
     `rescue_warning`, `outline_path`, `draft_path`,
     `final_report_path`, `report_glob`, `final_report_dir`,
     `prior_reports`, `final_audit_summary`, `final_audit_headline`,
     `ledger_causal_summary`, `wall_cap_hit`, `findings_file`,
     `lesson_candidates_file`, `audit_dir`.
   - Curator inputs: `clone_artifacts`.
   - Manager inputs: `manager_snapshot`.
2. Score-level top-level `inputs:` mapping (rare; usually empty).
3. Score-level `seed:` mapping.
4. Any other agent's declared `outputs:` list.

Typos fail at load time with a clear message naming the offending
agent and input. To extend the runtime allowlist, edit
`RUNTIME_INPUTS` in `long_exposure/exploration.py`.

### The `flow:` field

The cycle. Every agent named here runs in order, sharing the
`results` dict.

```yaml
flow:
  - researcher
  - worker
  - auditor
```

Reporter, final reporter, final auditor, curator are NOT in the
flow — they have their own scheduling (every N cycles for reporter,
end-of-run / daily-sync for the others).

### `allowed_tools` interactions

Three levels of precedence (highest wins):

1. **Per-agent `allowed_tools:`** in the agent definition.
2. **Score-level top-level `allowed_tools:`**.
3. **Config-level `allowed_tools:`** in `config.yaml`.

If none specified, all tools default to denied (Bash will fail).
Always at least specify Read and Bash for any agent that needs to
operate on files.

The MCP search tool (`search_sessions`, etc.) is added automatically
when `mcp: true` is set on a Claude-backed agent. Gemini-backed runs
write a project-local `.gemini/settings.json` with the `sessions` MCP
server and the current tool allowlist. Codex CLI MCP config is still a
separate integration point; durable memory remains available through
`sessions.db` and prompt-injected summaries.

---

## Effort levels and budget pressure

Two independent axes that influence model behaviour.

### Effort

Effort is **deterministic per agent**, derived from philosophy. Agents
may override with an explicit `effort:` key. It maps to the
`claude -p --effort` flag and controls reasoning depth, output length,
and tool-call frequency at the model level.

| Agent | Philosophy | Budget | Effort | Rationale |
|---|---|---|---|---|
| Researcher | research | high | `high` | Deep exploration, hypothesis formation |
| Worker | efficient | low | `high` | Builds + runs complex computations; high effort despite low-budget philosophy |
| Auditor | audit | high | `high` | Thorough defect finding |
| Reporter | reporter | medium | `medium` | Synthesis and composition |
| Final reporter | reporter | medium | `medium` | Cross-cycle synthesis |
| Final auditor | audit | high | `high` | Same posture as cycle auditor at run scope |
| Curator | efficient | low | `medium` | Reads MANIFEST.md + final report; constrained task |

Default mapping (`PHILOSOPHY_EFFORT_MAP` in `orchestrator.py`):

| Philosophy | Default effort |
|---|---|
| `efficient` | `medium` |
| `research` | `high` |
| `audit` | `high` |
| `reporter` | `medium` |
| `custom` | `high` |

### Budget pressure

Budget pressure is communicated via the **operating protocol** layer
of the system prompt and modifies output depth *within* the agent's
fixed effort level. The thresholds scale automatically with
`context_window`. Claude defaults to a 1M token budget. Codex runs use
`codex_context_window` (default 400k), so the same percentages map to
smaller absolute thresholds.

| Pressure | Token range | Behavior |
|---|---|---|
| `none` | below 40% of the active context budget | Work at full depth per philosophy |
| `mild` | 40–60% of the active context budget | Concise tool calls, shorter reasoning, skip nice-to-haves |
| `significant` | 60–80% of the active context budget | Minimal output, results and decisions only, no new branches |
| `critical` | above 80% of the active context budget | Finish current stage immediately, shortest correct output |

Compaction triggers at `compact_threshold × context_window` (default
900k tokens for Claude, 360k tokens for Codex).

## Telemetry

Telemetry is disabled by default and is passive. It writes local JSONL events
for later analysis and does not affect control flow.

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

`output_dir: null` writes to `<instance-dir>/telemetry`. See
[`telemetry.md`](telemetry.md) for event categories, privacy defaults, and
rollups.

### How they interact

- **`--effort` (CLI flag):** baseline reasoning depth. Fixed per
  agent at startup. Controls model-level behavior.
- **Budget pressure (system prompt):** soft behavioral guidance that
  tells the agent how to adapt as context fills up. The agent sees
  its token count in its checkpoint and follows the corresponding
  budget-level protocol.

The two are independent axes. A researcher at `high` effort under
`significant` pressure still reasons deeply but produces minimal
output. A curator at `medium` effort under `none` pressure works at
moderate depth with full output freedom.

---

## Settings that take effect when

| Setting type | When it takes effect |
|---|---|
| Philosophy / framework / checkpoint_format / anti_patterns_enabled / require_checkpoint_first / user_gate_approval | At next compaction (or `/compact` to force) |
| `compact_threshold` | At next compaction trigger evaluation |
| `agent_teams_defaults.enabled` (master switch) | Next subprocess call (env var stops being injected) |
| `model` / `context_window` / `compact_db` | Requires restart |
| `working_directory` / `allowed_tools` | Requires restart |
| Score YAML edits | At next `start` / `resume`. Score is loaded once at run start. |

---

## Code references

- `PHILOSOPHY_PRESETS`, `FRAMEWORK_PRESETS`,
  `PHILOSOPHY_EFFORT_MAP`: `long_exposure/orchestrator.py`.
- `RUNTIME_INPUTS` constant: `long_exposure/exploration.py`.
- Score validator: `exploration.validate_score_inputs`.
- Score loader: `exploration.load_exploration_score`.
- Per-agent config build: `conductor.build_agent_config`.
- Template assembly: `orchestrator.assemble_system_prompt` and the
  template files in `long_exposure/templates/`.
