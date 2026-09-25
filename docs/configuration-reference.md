# Configuration Reference

Complete reference for the two YAML files that drive a long-exposure
run. `config.yaml` carries deployment-wide knobs; the score YAML
(default `exploration-score.yaml`) carries the cycle definition and
per-agent overrides.

**Sources of truth:** `long_exposure/config.yaml`,
`long_exposure/exploration-score.yaml`, `long_exposure/orchestrator.py`
(`PHILOSOPHY_PRESETS`, `FRAMEWORK_PRESETS`,
`PHILOSOPHY_EFFORT_MAP`).

## Boolean values

Write `true` / `false`. The four opt-in features below
(`model_profiles`, `usage_allowance`, `startup_gate`,
`loop.cycle_planning`) parse their switches through
`long_exposure/flags.py`, which also accepts `yes` / `no`, `on` / `off`,
`1` / `0` and the quoted spellings of all of them, case-insensitively.

That exists because YAML makes it easy to write a boolean that is not one.
A quoted `enabled: "false"` is truthy to a bare `bool()`, so before this
a run the operator believed was uncapped would be killed by the spend limit,
and a flow they believed was fixed would start being planned by the
researcher. An unrecognised value warns once and falls back to the
documented default rather than silently choosing.

Note that the older switches (`fanout_enabled`, `end_of_run`, and the rest)
predate this and still use a plain `bool()`, so a quoted value there behaves
the old way.

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
provider_idle_timeout_seconds: 1800  # no-progress provider CLI watchdog (0 = disabled)
provider_idle_poll_seconds: 10       # watchdog poll interval
claude_transport: headless           # headless | interactive (opt-in; see gaps_interactive_mode.md)
interactive_driver_model: sonnet     # interactive mode: driver-loop model
interactive_permission_mode: skip    # skip | scoped (typos fall back to scoped)
interactive_recycle_turns: 40        # interactive mode: relaunch driver every N turns
interactive_fetch_window_seconds: 30 # interactive mode: bridge long-poll window
interactive_turn_timeout_seconds: 1800  # interactive mode: max wait per turn
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
| `provider_idle_timeout_seconds` | No-progress watchdog for provider CLI calls. Progress = growth of stdout/stderr or the Codex final-output file, a change in the process tree's shape, or cumulative CPU accumulation anywhere in the tree (coarsened to ~0.1 s buckets so idle scheduler wakeups don't count). A tree that is alive but accruing no CPU and writing nothing — including one merely *holding* an external child — is idle and gets killed at the timeout. `0` disables (e.g. for turns that legitimately block on quiet network waits). |
| `claude_transport` | `headless` (default; one `claude -p` per turn, all features intact) or `interactive` (opt-in; routes turns through a persistent interactive session — defers pooling and fan-out; see `gaps_interactive_mode.md`, including its compliance gate). Unknown values warn and fall back to `headless`. Env override: `LONG_EXPOSURE_CLAUDE_TRANSPORT`. |
| `interactive_*` | Interactive-transport tuning (driver model, permission mode, driver recycle cadence, bridge poll window, per-turn timeout). `interactive_permission_mode` falls back to `scoped` (the restrictive mode) on unknown values. |
| `provider_idle_poll_seconds` | Poll interval for the provider idle watchdog. |

### Per-agent LLM routing

The `agent_models` block is the **single, centralized template** for the
provider, model, and effort of each agent type. It lets one file route
different roles to different providers/models — for example a Claude Fable
researcher at `xhigh` effort alongside a Codex worker at `medium`.

```yaml
agent_models:
  researcher:     { provider: claude, model: opus, effort: xhigh }
  worker:         { provider: claude, model: opus, effort: xhigh }
  auditor:        { provider: claude, model: opus, effort: xhigh }
  reporter:       { provider: claude, model: opus, effort: xhigh }
  final_auditor:  { provider: claude, model: opus, effort: xhigh }
  final_reporter: { provider: claude, model: opus, effort: xhigh }
  manager:        { provider: claude, model: opus, effort: xhigh }
  curator:        { provider: claude, model: opus, effort: xhigh }
```

Each entry accepts three optional fields:

| Field | Values | Notes |
|---|---|---|
| `provider` | `claude` \| `codex` \| `gemini` \| `local` | Omit to inherit the global `llm_provider`. |
| `model` | provider alias or full id | Omit to use that provider's default model (`model` for Claude, `codex_model`, `gemini_model`, `local_model`). So `{provider: codex}` with no `model` runs `codex_model`. |
| `effort` | `low` \| `medium` \| `high` \| `xhigh` \| `max` | Canonical vocabulary (Claude's). See effort translation below. |

**Precedence.** A field set in `agent_models` overrides the score's per-agent
value and the global settings; a field left unset falls back to them. Deleting
the whole block restores the fully legacy behavior (global provider/model + the
score's per-agent `effort:`) — the block is entirely backward compatible.

**Default.** The shipped template routes every agent to Claude Opus at each
role's preset effort — `high` for researcher, worker, auditor, final_auditor,
and manager; `medium` for reporter, final_reporter, and curator (the same
per-role efforts the score defined before this block existed). This is
homogeneous, so it does not change any pooling behavior. Raise any role to
`xhigh`/`max` here when you want more depth for that agent.

**Effort translation per provider** (canonical → native control):

| Provider | Mechanism | Mapping |
|---|---|---|
| claude | `--effort <e>` CLI flag | pass-through (unknown values warn and fall back inside the CLI) |
| codex | `-c model_reasoning_effort="<e>"` config override | `max` → `xhigh` (Codex tops out at `xhigh`; `max`/`ultra` are gpt-5.6-only) |
| gemini | `.gemini/settings.json` `thinkingConfig.thinkingLevel` | `low`→LOW, `medium`→MEDIUM, `high`/`xhigh`/`max`→HIGH (best-effort; no CLI flag — see `docs/gaps.md`) |
| local | system-prompt text only | advisory (no reasoning field on the OpenAI-compatible payload) |

Unknown providers/models/efforts never abort a run: an unknown effort warns
once and falls back to `xhigh`; an unknown provider normalizes to `claude`.

**Interaction with multi-account pooling.** If agents resolve to **more than one
distinct provider** ("per-agent-pinned" mode), each agent is pinned to its
provider and multi-account pooling is deferred for the run — see
`docs/multi-account-pool.md`. Homogeneous routing (one provider across all
agents, including the default) leaves pooling fully intact.

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
| `compact_xml_retries` | Bounded retry on malformed XML summary; default 5. After exhaustion, store as-is with off-nominal event. **Standalone REPL only**: the cycle loop's compaction stores a plain-text summary, checks well-formedness once, logs `compaction_xml_invalid`, and never retries (`persistence-and-gems.md`, "Two compaction paths"). `max_summary_pct` and `depth_compression` are likewise REPL-only. |

### Run memoir (L1 narrative memory)

```yaml
memoir:
  enabled: true
  max_tokens: 3000
```

| Key | Meaning |
|---|---|
| `enabled` | Seed `MEMOIR.md`, inject it as the `run_memory` input to the researcher and worker, give the auditor its path as `memoir_path`, and archive changed versions after the auditor's turn. `false` strips both inputs from every agent at load so the prompt matches a pre-memoir run. Default `true` |
| `max_tokens` | Cap enforced at injection (chars/4 estimate), applied per section — the root memoir, a branch's own shadow, and the collapsed `branch_memoirs` block are each capped independently. Over-cap content is cut at the last paragraph boundary before the cap (hard cut if no boundary exists in the head), with a marker and a `memoir_over_cap` health event; the live file is never altered by the harness. Default 3000 |

The memoir is advisory — the plan of record and promise ledger win on any
conflict — and the auditor's role text carries the minimal-edit rule. See
`persistence-and-gems.md` ("The run memoir") and `tiered-memory-plan.md`.

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

test_runner: ""                 # optional; path to the project's test suite

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

`test_runner`, when set, adds a `TEST SUITE` section to every agent's
operating protocol naming the suite and how to run it. It is independent
of `wolfram_path`: with a kernel configured the section gives the
`<wolfram_path> -script <test_runner>` command, and without one it names
the suite and tells the agent to run it with the project's own runner
rather than pointing at a binary the deployment does not have. Empty (the
default) omits the section.

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

**Standalone REPL only.** The cycle loop never injects gems, so this
block and `relevance_profiles` have no effect on a long-exposure run
(`persistence-and-gems.md`, "Gems").

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

### `model_profiles` (opt-in)

Thins the *ceremony* in the soft-guidance stack when the model running a turn
is one you have declared advanced. Default off; with it off the assembled
prompt is byte-identical to the output from before the feature existed
(checked against that commit, not inferred — see `docs/soft-guidance.md`).

```yaml
model_profiles:
  enabled: false
  auto: true                 # derive the profile from the resolved model id
  profile: null              # explicit override: standard | advanced
  default: standard          # used when auto finds no family match
  families:
    advanced: [fable, astra] # case-insensitive substring match on the model id
  overrides: {}              # e.g. {anti_patterns_enabled: true}
```

| Key | Meaning |
|---|---|
| `enabled` | Master switch. `false` applies nothing at all. |
| `auto` | When true, match the resolved model id against `families`. |
| `profile` | Explicit profile; beats `auto`. Unknown value warns and falls back to `default`. |
| `default` | Profile when `auto` finds no match. Unknown value warns and falls back to `standard`. |
| `families` | Profile name -> list of substrings matched case-insensitively against the model id. Longest pattern wins; profile order is fixed, not dict order. |
| `overrides` | Per-key escape hatch applied last. Only the four guidance knobs below are accepted; anything else is ignored with a warning. |

The `advanced` profile sets exactly four keys — `require_checkpoint_first:
false`, `checkpoint_format: minimal`, `anti_patterns_enabled: false`,
`framework_verbosity: lean` — and can set nothing else (`ALLOWED_KNOBS` in
`long_exposure/model_profiles.py`). `framework_verbosity: lean` drops each
stage's `<exit-gates>`, `<failure-modes>` and `<depth-calibration>` and adds
one `<exit-gate-policy>` block so the transition rules still make sense
without an enumerated gate list.

`standard` sets nothing, so it can never override a knob you deliberately
turned off in this file.

**Resolution is per agent** on the conductor paths: `agent_models` routes
each role to its own model, and the profile is resolved from *that* model, so
a Fable researcher can run the lean prompt while an Opus auditor in the same
run keeps the full one. (The standalone REPL passes the run config, which is
correct for it — one model per session.)

**Measured saving**, `advanced` vs `standard`, across all 25 shipped
philosophy x framework combinations:

| framework | tokens saved per agent turn |
|---|---|
| `oversight` | ~1,055 |
| `audit` | ~1,136 |
| `reporter` | ~1,553 |
| `staged` (shipped default) | ~1,956 |
| `worker_staged` | ~2,033 |

The figure depends on the framework, not the philosophy: what `lean` drops is
the per-stage enumerations, so a framework with more stages saves more.

See `docs/soft-guidance.md` for what may and may not thin, and why.

### `usage_allowance` — one total spend limit, OPT-IN, default OFF

> **There is no spend limit unless you turn one on, and for most operators
> that is the right setting.** Long-exposure is built to run on a fixed-cost
> subscription (a Max plan, through `claude -p`), where the marginal cost of
> a run is zero. A per-run dollar cap buys nothing there, and it *can*
> truncate a run mid-cycle for no benefit. The shipped default — `enabled:
> false` — is the intended stance, not an unfinished setting.

Turn it on only for a specific reason: metered API billing rather than a
subscription, a shared account where one run must not monopolise capacity, or
an unattended experiment whose blast radius you want bounded.

The startup gate asks about it every launch (Q4) so the option is
discoverable without editing config.yaml, and **the default answer is "no
cap"**. Declining at the gate overrides an `enabled: true` in config.yaml —
otherwise answering the question would not mean anything.

When you do enable it: **one** total limit for the whole run, as a percentage
of a weekly allowance you declare. There are no per-agent, per-role,
per-cycle or per-clone sub-budgets; the run spends freely against the total
until it is gone.

```yaml
usage_allowance:
  enabled: false
  weekly_allowance_usd: 0    # operator-declared, API-equivalent dollars
  run_pct: 0                 # 1-100; this run's delta against that allowance
```

Cap = `weekly_allowance_usd * run_pct / 100`. `enabled: false`, `run_pct: 0`
and `weekly_allowance_usd: 0` all mean unlimited, which is the default;
`run_pct` above 100 clamps to the allowance. Garbage values (a string, a
negative, NaN, infinity) read as unlimited rather than raising.

**Set the cap with the overshoot in mind.** The check fires *after* a turn's
cost lands, so the run stops at cap-plus-one-turn. On expensive turns that is
not a rounding error: a live run with a $6.00 cap stopped at **$7.81 — 130%
of the cap** — because the turn that crossed it was a $3.45 compaction call.
Size the cap for the spend you will accept, not the spend you want.

**The allowance is a declared proxy, not a meter reading.** The harness
cannot read subscription usage: the `claude` CLI exposes no `usage`
subcommand, `/usage` is interactive-only, and the `-p` envelope carries only
`total_cost_usd`, token counts and `num_turns`. Every surface that shows the
cap says so.

The delta semantics you want come for free. The ledger totals only *this*
run's spend, so a cap measured from run start is already a delta on top of
whatever was used before it — you never need to know where you stand this
week.

#### It kills the run; it does not stop it

This is the one place the feature deliberately does *not* reuse
`loop.max_cost_usd` (below), which fires at the cycle boundary and ends the
run as a natural end-of-run — final auditor, final reporter and curator all
still run, and all still spend.

| | `loop.max_cost_usd` | `usage_allowance` |
|---|---|---|
| When checked | cycle boundary | wherever spend is recorded, plus the fan-out barrier poll |
| Overshoot | up to one cycle | up to one agent turn; during a fan-out, one barrier poll plus the 10 s grace |
| Measured overshoot | — | **130% of the cap** in a live run: a $6.00 cap stopped at $7.81, because the turn that crossed it was a $3.45 compaction call |
| End-of-run pipeline | **runs** | **skipped** |
| Fan-out clones | invisible until barrier collapse | summed live from each clone's `output/usage_summary.json` |
| Exit code | 0 | **3** |
| Artifacts | normal | plus `output/killed_spend_limit.json` |
| Status file | `completed` / `stopped` | `killed_spend_limit` |

On a kill: the next agent turn does not start, the end-of-run pipeline is
skipped, state is still saved (so raising the limit and resuming works), the
marker is written, and the process exits 3 from `launch`, `start`, `resume`
and `python -m long_exposure.exploration` alike. A marker left by an earlier
killed run is cleared at run start, so a resume that finishes cleanly does
not still look killed.

If a fan-out is in flight, the barrier writes each running clone's stop file,
waits `fanout.SPEND_KILL_GRACE_SECONDS` (10 s) for a merge report already
mid-write, then **terminates the clone's process group** — SIGTERM, 5 s, then
SIGKILL. The stop file alone would not do: a clone honours it at its next
cycle boundary, so one in the middle of a long agent turn would keep spending
until it finished, up to the 10 h `FANOUT_CAP_SECONDS`. A branch terminated
this way gets a `killed_spend_limit` outcome and a placeholder merge report
noting that whatever it wrote to the shared workspace is intact.

#### Enforcement lives at the root

Fan-out clones never enforce the cap themselves. A clone sees only its own
spend, so it would be wrong in both directions: it would self-kill after
spending the whole cap alone, yet three clones at 40% each — 120% of the cap
— would each stay under it. Instead the root's barrier poll sums its own
ledger plus every live clone's incrementally written
`output/usage_summary.json`, and a trip cascades the stop file to each
running clone before the sweep.

### `startup_gate` (opt-in)

Four questions asked once, before a run begins, so the expensive choices are
made deliberately rather than inherited from whatever this file happens to
say.

```yaml
startup_gate:
  enabled: false
  model_choices: [opus, fable, sonnet]   # plus "other (type a value)"
  instances_root: ./instances            # fallback enumeration source for Q3
  registry_path: ~/.long-exposure/runs.jsonl
  max_runs_listed: 10
```

| Q | Question | Flag |
|---|---|---|
| 1 | Which model should this run use? | `--gate-model` |
| 2 | Which workspace directory? | `--gate-workspace` |
| 3 | Resume a previous run, or start fresh? | `--gate-resume <state-path>\|fresh` |
| 4 | What share of the declared weekly allowance? *(only when `usage_allowance.enabled`)* | `--gate-usage-pct` |

**`launch` only.** `start`, `resume` and
`python -m long_exposure.exploration` stay non-interactive. That is what
keeps cron jobs, the fan-out clone spawn and the benchmark adapter working —
a clone that stopped to ask a human which model to use would hang the
barrier until the 10 h cap.

**Answers persist and are re-applied.** They go to
`<instance_dir>/gate_answers.json` and are read back by `run_exploration`,
so `resume` never re-asks and a crash-restart keeps the model and caps that
were chosen. A copy lands in the run's `output/` for provenance — that is
what lets a report or a benchmark appendix state the model, workspace and
caps the run *actually used* rather than what this file says now.

**Headless when flagged.** `--no-gate` skips it entirely; each `--gate-*`
flag pre-answers one question, so a fully-flagged `launch` never blocks. A
non-TTY with an unanswered question exits **4** and names the missing flag
rather than guessing: a wrong model is expensive to discover three hours in.

#### What Q1 rewrites

The shipped config pins all eight roles explicitly in `agent_models`, so
setting the global `model` alone would change nothing about any actual agent
turn — while overwriting all eight unconditionally would destroy a
deliberate heterogeneous routing. So Q1 rewrites `model` **and** every
`agent_models.*.model` that still equals the pre-gate global default,
leaving a Codex worker or a Sonnet reporter untouched.

Because that rule is subtle, the gate then prints the resulting table — role,
provider, model, effort, and each role's resolved capability profile — before
the run starts:

```
[gate] Resolved per-agent routing
  role            provider  model                 effort  profile
  --------------  --------  --------------------  ------  --------
  researcher      claude    claude-fable-5-1      high    advanced
  worker          codex     gpt-5.5               high    standard
  ...
```

#### Where Q3's run list comes from

There is no instances root in the harness — instance dirs come only from
`--instance-dir` or `AGENT_INSTANCE_DIR`. So runs are enumerated from an
append-only registry at `registry_path`, one JSON line per run, written at
run start. Clones are excluded: a fork is not a resumable run. If the
registry is missing or empty (an older run, a fresh checkout, a moved home
directory), the gate falls back to globbing
`instances_root/*/exploration_state.json`.

A run whose state file has since been deleted is listed as a tombstone
rather than dropped, so an operator looking for it learns it is gone. Both
the menu and the `--gate-resume` flag refuse it: accepting a vanished path
would start a *fresh* run at that path, silently losing the intent to
resume.

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
| `fanout_enabled` | `true` | Whole-cycle fan-out switch. `false` removes the `<parallel_cycle_fanout>` guidance from the researcher's live guidance and ignores any block it emits, so no clone processes are spawned. Env override for one launch: `LONG_EXPOSURE_FANOUT=0`. |
| `end_of_run` | all stages on | Bool or mapping `{enabled, final_auditor, final_reporter, curator}`. Gates the end-of-run pipeline at natural end, `max_cycles`, stop, budget cap, and the daily-sync re-run. A stage that is off is skipped even when its agent is defined. `LONG_EXPOSURE_END_OF_RUN=0` disables all stages for one launch. Note: with `final_reporter` off, the curator has no `## Key Files` section and ships the report-only safety package. |
| `max_cost_usd` | `null` | Stop when the run's cumulative cost (provider-reported plus `pricing:` estimates) reaches this figure. Checked at cycle boundaries, so it can overshoot by one cycle. Treated as a natural end-of-run (the end-of-run pipeline runs if enabled). |
| `max_tool_calls` | `null` | Same gate on cumulative tool invocations across every agent call. |

### Usage ledger, cost, and tool counts

Every provider call the harness makes (cycle agents, reporter, final
auditor/reporter, curator, merge synthesis, compaction) is folded into a
per-agent ledger: calls, tool calls, turns, input/output/cache tokens, wall
time, and cost. The ledger is persisted in `exploration_state.json` under
`usage_totals`, so totals survive stop/resume, and fan-out clone ledgers are
merged into the root at barrier collapse.

Where the numbers come from:

| Field | Claude (`claude -p`) | Codex | Gemini | local |
|---|---|---|---|---|
| tokens | envelope `usage` | `turn.completed` usage | `stats.models` | response usage |
| `cost_usd` | envelope `total_cost_usd` | not reported | not reported | not reported |
| `cost_estimated_usd` | never (reported cost wins) | from `pricing:` | from `pricing:` | from `pricing:` |
| `tool_calls` | `tool_use` blocks in the current turn of the session transcript | completed `item` events other than messages/reasoning | `stats.tools.totalCalls` | 0 |
| `turns` | envelope `num_turns` | — | — | — |

Read it with:

```bash
long-exposure status           # includes the "## Usage" table
long-exposure usage            # the table alone
long-exposure usage --json     # output/usage_summary.json verbatim
```

`config.yaml` `pricing:` supplies USD-per-million rates for the estimate
(`provider -> model -> {input, output, cache_read, cache_write}`; a model
key may be an exact id, a prefix, or `_default`; omitted cache rates
default to 10% / 125% of `input`). For Codex and Gemini the cached share is
subtracted from the input total before pricing, since those providers
report input inclusive of cached tokens. Without a row, cost for that
provider shows as `n/a` and does not count toward `max_cost_usd`.
Telemetry emits one `usage_recorded` event per ledger record from every
call site; `telemetry summarize` totals cost from those under `cost`
(`agent_call_end` carries the same per-call fields but only for the cycle
loop and periodic reporter).

Scope and limits of the accounting:

- **Failed calls count.** When a provider CLI fails after producing a
  parsable envelope (non-zero exit with JSON, or an in-band API error), the
  failed turn's tokens and cost are recorded with `ok_calls` unchanged.
  Calls that produce no envelope at all (timeouts, crashes) are not
  counted.
- **Fan-out.** Clones start with an empty ledger and evaluate
  `max_cost_usd` / `max_tool_calls` against their own spend only; the root
  folds clone ledgers in at barrier collapse. A cap therefore bounds the
  root plus whatever each clone spent before the merge, and a clone killed
  mid-cycle (10 h cap, post-merge termination, preemption) loses that
  partial cycle's spend.
- **Manager.** In `launch --manager` threaded mode the manager agent's
  calls land in the run ledger; a cron `manager poll` runs in its own
  process and its spend is not persisted.
- **Interactive transport** reports a chars/4 estimate, not provider
  usage; those tokens are recorded but never priced (`n/a`).
- **`clear`** resets the ledger; `stop`/`resume` and the standalone
  `run_final_reporter.py` extend it.

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
    disable_tools: false             # if true, claude -p --tools "" (conductor path only)
    inputs: [directive, research_brief, live_guidance, plan_of_record, promise_ledger_summary]
    outputs: [work_output]
    role: |
      <role text — agent's prompt body>
```

`disable_tools` is honoured by the **conductor** path
(`python -m long_exposure.conductor`, which runs a score once) and maps to
`claude -p --tools ""`, a read-only Codex sandbox, or Gemini
`--approval-mode plan`. The cycle loop does not read it; to run a cycle
agent without tools, give it an empty `allowed_tools` list instead.

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

## `loop.cycle_planning` (opt-in) — researcher-planned cycle tails

Lives in the **score** (`exploration-score.yaml`), under `loop`, because it
shapes the flow rather than the models.

```yaml
loop:
  cycle_planning:
    enabled: false
    max_worker_chain: 3
    max_turns_per_cycle: 4
    audit_floor_cycles: 2
    allow_in_clones: false
    worker_may_request_audit: true
```

When enabled, the researcher may emit a `<cycle_plan>` block that shapes the
**rest of the cycle it has just opened** — chaining workers, or omitting the
auditor when there is nothing to audit. It cannot schedule itself (it has
already run), so the plan replaces `flow[1:]` only. That framing is what
removes the self-scheduling paradox, and it means the plan governs the very
next turn rather than the next cycle.

```xml
<cycle_plan>
  <turn agent="worker">Build the sweep harness and run the N=64 grid.</turn>
  <turn agent="worker">Fit the scaling exponent to the grid output.</turn>
  <turn agent="auditor">Check the fit's residuals against the claim.</turn>
  <rationale>The fit depends on the grid completing.</rationale>
</cycle_plan>
```

`<rationale>` is required and recorded — it is what makes a skipped audit
reviewable afterwards — but it is not parsed for meaning.

### Validation

Same posture as `<parallel_cycle_fanout>`: reject the whole block on any
violation, log a reason, fall back to the fixed flow, never raise.

| Rule | On violation |
|---|---|
| Every `agent` must be a member of the score's `flow` | reject |
| `researcher` is not permitted (it has already run) | reject |
| At most `max_worker_chain` worker turns | reject (not truncate — truncating would run a plan the researcher did not write) |
| At most one `auditor` turn | reject |
| At most `max_turns_per_cycle` turns | reject |
| An `auditor` turn not last | **moved last** (an audit before its work is a formatting slip, not a bad plan) |
| Block absent, empty or malformed | fixed flow, no health event for "absent" |

A rejection emits the `cycle_plan_rejected` health event.

Note the first rule: the score's **`flow`**, not every agent it defines. The
cycle loop populates inputs only for flow members, so a plan naming
`final_auditor`, `final_reporter`, `curator` or `reporter` would run that
agent with `[UNAVAILABLE: stage]`, `[UNAVAILABLE: expected_file]` and so on —
a full turn spent producing something unusable, and two of those roles set
`agent_teams: true`. A score with a wider flow can schedule its own extra
roles; validation follows the flow, not a hard-coded list.

### Bounds the agent cannot waive

- **`audit_floor_cycles`** — the maximum consecutive *completed* cycles that
  may end without an auditor. At the floor, an auditor is appended
  regardless of the plan (`cycle_plan_audit_forced`). This bounds the
  researcher's self-interest — it is the role whose brief the auditor checks
  — and keeps `[[BRANCH_COMPLETE]]` reachable, since only the auditor emits
  it. The streak persists into run state, so a stop/resume cannot hand the
  run a fresh licence to skip audits. A failed or rate-limited cycle does not
  count toward the streak: it never got the chance to audit.
- **`max_worker_chain`** — prevents a plan turning a cycle into an unbounded
  worker loop that would starve the memoir, the reporter cadence and the
  exhaustion detector.

### The researcher's blind spot, and who covers it

The researcher plans *before* seeing this cycle's work, so it predicts
whether an audit will be needed rather than observing it. The worker covers
that: with `worker_may_request_audit`, a worker that hits something
surprising emits `[[REQUEST_AUDIT]]` on a line of its own and the auditor is
appended **at the end of the cycle**, the audit-floor streak resets, and
`cycle_plan_audit_requested` is logged.

At the end, not immediately after the escalating worker: if worker 1 of a
chain escalates, auditing before worker 2 would leave `audit_report` and the
memoir describing only part of the cycle, and the next cycle's researcher
would read that partial verdict as the cycle's. It would also contradict the
rule above that an auditor turn is always moved last. The event matters as much as the fix
— a run escalating every cycle is telling you the planning is wrong, not
that the work is surprising. Like `[[BRANCH_COMPLETE]]`, the token is matched
anchored to its own line, so a worker merely *discussing* it does not trigger
it.

### Fan-out wins

A fan-out already replaces worker and auditor for its cycle. The plan is
applied *after* the fan-out trigger in the cycle loop, and fan-out `break`s
out of the flow when it fires — so "fan-out wins" is structural rather than a
precedence rule, and a fan-out cycle never logs a plan that will not run.

### Root only

`allow_in_clones` is `false` and should stay so. A clone with no auditor
never emits `[[BRANCH_COMPLETE]]` and would burn to the 10 h
`FANOUT_CAP_SECONDS` wall with nothing to show; and branches that ran
different shapes are not comparable, which is what would make the merge's
divergence table meaningless.

### Worker chaining

Turn *k>1* of a chain resumes the same worker session (`agent_sessions`), so
it continues the conversation rather than restarting cold — no new input
plumbing. Its output is **appended** under a `## worker turn k` header rather
than replacing the previous turn, so the auditor sees the whole chain.

One thing to watch on a first live run: the exhaustion detector's low-output
floor is relative to the run's own peak cycle output, and chaining raises
that peak — making the floor stricter for later single-turn cycles.

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
