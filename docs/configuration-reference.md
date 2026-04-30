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
model: opus               # alias (sonnet, opus, haiku) or full name
context_window: 1000000
model_tier: opus
cli_timeout: 0            # seconds per claude -p call (0 = no timeout)
```

| Key | Meaning |
|---|---|
| `model` | Model alias passed to `claude -p --model`. Aliases resolve to current canonical names. |
| `context_window` | Used to compute compaction thresholds and budget pressure ranges |
| `model_tier` | Used by template substitution; selects philosophy-tier-specific phrasing |
| `cli_timeout` | Per-`claude -p` call timeout; `0` disables. Override per-agent in score |

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
```

When a fan-out collapses with ≥ this many branches, the reporter
agent is invoked to compress N raw merge_reports into one bounded
synthesis. Below the threshold, raw concatenation goes through
unchanged. See `parallelism.md`.

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

wolfram_path: ""   # empty disables wolfram tool guidance

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
(`_same_topic`, `_same_subtopic`, `_any_topic`, `_shared_tools`) are
detected by `proximity.score_session`. Named topics and keywords add
direct boosts (or penalties — negative values are valid).

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
  # Planned 24h rotation (Plan B). Defaults to daily_sync_interval_hours
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
| `min_clone_cycles_before_preempt` | `1` | Stage 9: clones must complete this many cycles before being eligible for graceful preemption. See `parallelism.md`. |
| `barrier_preempt_timeout_seconds` | `3600` | Backup timer for preemption when no organic exit has happened. |
| `planned_rotation_min_age_hours` | (defaults to `daily_sync_interval_hours`) | Plan B: the minimum age of the last rotation before a planned rotation will fire after the next daily sync. Set to a value larger than `daily_sync_interval_hours` to space planned rotations farther apart than syncs. |

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
   `exploration.py`): names the harness injects. Currently 25
   entries:
   - Cycle inputs: `directive`, `audit_report`, `live_guidance`,
     `plan_of_record`, `promise_ledger_summary`,
     `research_brief`, `work_output`, `starting_subtopic`,
     `starting_tools`.
   - Reporter inputs: `cycle_range`, `cycle_sessions`,
     `report_basename`, `working_dir`.
   - Stage inputs (final reporter / final auditor):
     `stage`, `total_stages`, `stage_index`, `expected_file`,
     `rescue_warning`, `outline_path`, `prior_reports`,
     `final_audit_summary`, `final_audit_headline`, `wall_cap_hit`,
     `findings_file`, `lesson_candidates_file`.
   - Curator inputs: `clone_artifacts`.
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
when `mcp: true` is set on the agent.

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

### Budget pressure (1M context window)

Budget pressure is communicated via the **operating protocol** layer
of the system prompt and modifies output depth *within* the agent's
fixed effort level. The thresholds scale automatically with
`context_window`.

| Pressure | Token range | Behavior |
|---|---|---|
| `none` | below 400k | Work at full depth per philosophy |
| `mild` | 400k–600k | Concise tool calls, shorter reasoning, skip nice-to-haves |
| `significant` | 600k–800k | Minimal output, results and decisions only, no new branches |
| `critical` | above 800k | Finish current stage immediately, shortest correct output |

Compaction triggers at `compact_threshold × context_window` (default
900k tokens).

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

## Code citations

- `PHILOSOPHY_PRESETS`, `FRAMEWORK_PRESETS`,
  `PHILOSOPHY_EFFORT_MAP`: `long_exposure/orchestrator.py`.
- `RUNTIME_INPUTS` constant: `long_exposure/exploration.py`.
- Score validator: `exploration.validate_score_inputs`.
- Score loader: `exploration.load_exploration_score`.
- Per-agent config build: `conductor.build_agent_config`.
- Template assembly: `orchestrator.assemble_system_prompt` and the
  template files in `long_exposure/templates/`.
