# Advanced-model modes: four config expansions

Status: **plan only — no code written.** Branch: `claude/long-exposure-tiered-memory`
(the branch carrying the bench-mode and memoir work). Nothing here lands on `main`.

## 0. Why these four, and the one rule they share

The harness was tuned for Opus 4.6/4.7: an explicit three-role flow, a
four-layer soft-guidance stack, and a fixed cycle shape. For a model like
Fable that ceremony is a tax — the observed failure is *over-auditing and
under-working*, because the flow spends one of every three agent turns on an
audit whether or not there is anything to audit, and because roughly a third
of the system prompt is exhortation a capable model does not need.

The rule every feature below obeys: **expansion, never replacement.** Each
one is a config key that is off (or set to today's behaviour) by default.
A run that does not opt in produces byte-identical prompts and an identical
flow to what ships now. Every feature also has a deterministic fallback so a
malformed or absent agent decision degrades to current behaviour rather than
to an error.

| # | Feature | Default | Opt-in surface |
|---|---------|---------|----------------|
| 1 | Agent-planned cycle tail (`<cycle_plan>`) | off | `loop.cycle_planning` |
| 2 | Model capability profiles (guidance tiering) | off | `model_profiles` |
| 3 | Startup gate (4 questions, `launch` only) | off | `startup_gate` |
| 4 | Usage cap as a delta % of a declared weekly allowance | off | `usage_allowance` |

---

## 1. Feature 1 — the researcher plans the cycle tail

### 1.1 The framing that removes the paradox

The researcher is agent 0 of every cycle (`flow: [researcher, worker, auditor]`,
`exploration-score.yaml:1909`). So it does **not** plan a cycle it might be
scheduled out of — it plans the *remainder* of the cycle it has just opened.
Concretely: its block replaces `flow_this_cycle[1:]`, never index 0.

That framing buys three things for free:
- no re-entry problem (the planner has already run when its plan is read);
- no plan staleness (the plan governs the next agent turn, not the next cycle);
- no new `results` key and no displacement — the block lives inside
  `research_brief`, which is already stored, archived, and compacted.

It mirrors an existing precedent exactly: `<parallel_cycle_fanout>` is already
parsed out of researcher output, validated, clamped, and silently ignored when
malformed (`fanout.py:464-560`). `<cycle_plan>` is the same machine with a
smaller blast radius — it schedules in-process turns rather than spawning
subprocesses.

### 1.2 The requested double-check: researcher vs. auditor as the planner

You picked the researcher and asked me to verify the trade-off rather than
just record it. I did, and I agree with the choice — but the auditor is a
closer call than it first looks, and two of the researcher's weaknesses are
real enough to need structural mitigation (§1.5). The comparison:

| Dimension | Researcher plans (chosen) | Auditor plans |
|---|---|---|
| **Latency of the plan** | Zero. Plan is written at the top of the cycle it governs. | One cycle. Written at the end of N, applies to N+1 — and after a fan-out the audit slot is displaced to N+2 (`exploration.py:4573`), so the plan can be two cycles stale. |
| **Evidence behind the plan** | **Predictive.** It has last cycle's `audit_report`, `plan_of_record`, `promise_ledger_summary`, `run_memory` — but not this cycle's work. It must *guess* whether an audit will be needed. | **Observed.** It has just read `work_output` and knows whether the work was thin, whether gates were met, whether a claim needs verification. Strictly better information. |
| **Authority coherence** | **Good.** The researcher already owns fan-out. All branching authority in one role, one parser, one `_is_clone()` depth short-circuit. | **Splits it.** The auditor would own flow while the researcher owns fan-out. Worse: if the auditor schedules a researcher-less cycle, fan-out becomes structurally unreachable that cycle, because only the researcher can request it. Fixing that means giving the auditor fan-out authority too — a much larger change. |
| **Self-interest** | **Conflicted.** The researcher's own brief is what the auditor checks; letting it say "no audit needed" is self-exculpating. | **Clean on the current cycle** (it cannot skip its own live run), conflicted on future ones in the same way. |
| **Coupling to the artifact** | **Tight, and useful.** The plan and the brief are one document, so "worker 1 builds X, worker 2 then measures Y" is stated in the same prose the worker reads. | **Loose.** The plan would arrive as `audit_report` prose that the *next* researcher also reads, inviting the two to disagree about the cycle shape. |
| **Availability of the planner** | Absent on post-merge cycles (`flow_this_cycle = [worker]`, `exploration.py:4078`). Those cycles keep the fixed shape. | Absent on exactly the cycles the plan skipped it — and in the advanced profile those are the *common* case, so the planner would routinely be missing. Needs a carry-forward plan or the same fixed-flow fallback, i.e. it pays the researcher's cost too, more often. |
| **Load on the role we are thinning** | None added. | Adds scheduling to the role that already carries the most inputs (`work_output`, `research_brief`, `plan_of_record`, `promise_ledger_summary`, `memoir_path`, `branch_memoirs`) and that the advanced profile exists to *reduce*. Working against the goal. |
| **Closure authority** | Unchanged — `[[BRANCH_COMPLETE]]` stays with the auditor (`conductor.py:345`). But a researcher that plans an audit-free cycle removes the only path to declaring completion that cycle. Needs the §1.5 floor. | Unchanged and co-located: the role that judges also schedules. |

**Verdict.** Researcher, confirmed — the latency, authority-coherence, and
planner-availability arguments are each decisive on their own, and the
auditor's one genuine advantage (it has observed the work) is recoverable
cheaply: **the worker escalates, and the harness must honour it**
(decided). A worker that finds something surprising emits
`[[REQUEST_AUDIT]]`; the harness re-inserts the auditor into the tail even
if the plan omitted it, **resets the audit-floor counter**, and emits a
`cycle_plan_audit_requested` health event. The researcher's only
irrecoverable weakness — that it cannot see the work it is planning around —
is answered by the role that can, and the health event makes a researcher
that plans badly *visible* rather than merely corrected: a run where
escalation fires every cycle is telling you the planner is not earning its
keep.

### 1.3 Block grammar

Emitted anywhere in `research_brief`; parsed by a new
`long_exposure/cycle_plan.py` (sibling to `fanout.py`):

```xml
<cycle_plan>
  <turn agent="worker">Build the sweep harness and run the N=64 grid.</turn>
  <turn agent="worker">Fit the scaling exponent to the grid output.</turn>
  <turn agent="auditor">Check the fit's residuals against the claim.</turn>
  <rationale>Two worker turns because the fit depends on the grid
  completing; one audit at the end is enough.</rationale>
</cycle_plan>
```

`<rationale>` is required and logged (it is what makes a skipped audit
reviewable after the fact), but it is not parsed for meaning.

### 1.4 Deterministic validation

Same posture as the fan-out parser: reject the whole block on any violation,
log a one-line reason, fall back to the fixed flow. Never raise.

1. Every `agent` must be a key of `score["agents"]`.
2. `researcher` is not permitted in the block (it has already run).
3. `worker` count ∈ `[0, loop.cycle_planning.max_worker_chain]` (default 3).
4. `auditor` count ∈ `[0, 1]`.
5. Order is honoured as emitted, except that any `auditor` turn is moved to
   last — an audit before the work it audits is a parse error in disguise.
6. Total turns ≤ `max_turns_per_cycle` (default 4) as a belt-and-braces bound.
7. A block co-occurring with a valid `<parallel_cycle_fanout>`: **fan-out
   wins**, plan ignored with a logged reason (fan-out already replaces
   worker *and* auditor, `exploration.py:4574`).
8. Absent, empty, or malformed → fixed flow. No health event for "absent";
   a `cycle_plan_rejected` health event for malformed. Three registry
   entries in `health_events.py`: `cycle_plan_rejected`,
   `cycle_plan_audit_forced`, `cycle_plan_audit_requested`.

### 1.5 Bounds the agent cannot waive

Two harness-enforced floors, both checked after parsing, both non-negotiable
by the model:

- **`audit_floor_cycles` (default 2, decided).** At most two consecutive
  cycles may end without an auditor turn. On the cycle that would break the
  floor, the auditor is appended regardless of the plan, and the fact is
  logged and surfaced in the cycle banner. This roughly doubles worker share
  against today's audit-every-cycle flow while keeping the self-exculpation
  conflict bounded and `[[BRANCH_COMPLETE]]` reachable often enough to close
  a run promptly. A `[[REQUEST_AUDIT]]` escalation also resets the counter,
  so an honoured escalation buys the run two more planned cycles rather than
  consuming the floor's budget.
- **`max_worker_chain` (default 3).** Prevents a plan that turns a cycle into
  an unbounded worker loop, which would starve the memoir, the reporter
  cadence (`report_interval`), and the exhaustion detector.

Neither floor has to carry clones, because **cycle planning is root-only**
(decided, `allow_in_clones: false`). Clones keep the fixed flow, exactly as
they already skip the fan-out decision. This removes the sharpest failure
mode the feature could have introduced: a clone with no auditor never emits
`[[BRANCH_COMPLETE]]` and would burn to the 10 h `FANOUT_CAP_SECONDS` wall
with nothing to show. It also keeps branches comparable to each other, which
is what makes the merge's divergence table mean anything.

### 1.6 Worker chaining: session continuity, not new plumbing

Turn 2 of a worker chain should *continue* turn 1, not restart it. The
harness already keeps a per-agent session id (`agent_sessions[agent_name]`,
used for compaction and reanchoring), so chaining needs no new input: turn
*k>1* resumes the same worker session and receives a short continuation
directive plus its own `<turn>` text. No `prior_work_output` input, no
`RUNTIME_INPUTS` addition, no score change for the worker.

Consequences to handle explicitly:
- **`work_output` for the auditor** becomes the concatenation of the chain's
  turns under `## worker turn k` headers, so the auditor sees all of it.
- **Usage ledger** rows are keyed by agent name, so three worker turns fold
  into one `worker` row. That is the behaviour we want; no change.
- **Exhaustion detector** (`LOW_OUTPUT_FRACTION = 0.05` of peak observed
  output, `exploration.py:3917`) is per-cycle. Chaining raises per-cycle
  volume and therefore raises `peak`, which makes the 5% floor stricter for
  later single-turn cycles. Worth a note in the doc and a look at the first
  live run; not worth a code change up front.

### 1.7 Positional assumptions that must become name-based first

This is prerequisite refactor work, not optional. The cycle loop currently
infers roles from flow position (`exploration.py:4598`, `:4605`):

- `if i == 0:` → "research failed, skip the rest of the cycle"
- `elif i == len(flow_this_cycle) - 1:` → "audit failed, substitute
  `FALLBACK_AUDIT`"

With a variable tail, the last agent is often a worker, so a failed worker
would be handed the audit fallback and a failed audit in a non-final slot
would be treated as an ordinary mid-flow failure. Both must be rewritten as
`agent_name == "researcher"` / `agent_name == "auditor"` **before** any
variable flow is switched on. The three existing `agent_name == "auditor"`
checks in the same region are already name-based and need no change.

### 1.8 Config surface

```yaml
loop:
  cycle_planning:
    enabled: false          # off → today's fixed flow, byte-identical prompts
    max_worker_chain: 3
    max_turns_per_cycle: 4
    audit_floor_cycles: 2
    allow_in_clones: false           # root-only, like the fan-out decision
    worker_may_request_audit: true   # [[REQUEST_AUDIT]]; honoured, resets the floor
```

Guidance is injected into the researcher only when `enabled` — same pattern as
`fanout_guide` (`exploration.py:4119`), which is skipped for clones and
post-merge cycles precisely to avoid dead prompt weight.

`telemetry.emit("cycle_start", ...)` already logs `flow`; it gains
`planned_by: "researcher" | "fixed"` and `plan_rejected_reason`.

---

## 2. Feature 2 — model capability profiles

### 2.1 What thins

Scope is exactly what you approved: ceremony and framework enumerations.

| Knob | `standard` (today) | `advanced` | Mechanism |
|---|---|---|---|
| `require_checkpoint_first` | `false` | `false` | already off; profile pins it |
| `checkpoint_format` | `standard` (9 fields incl. `<gate-check>`) | `minimal` | existing branch, `orchestrator.py:1727` |
| `anti_patterns_enabled` | `true` | `false` | existing branch, `orchestrator.py:1872` |
| `framework_verbosity` | `full` | `lean` | **new**: `render_stages_block` emits `<purpose>` + `<required-output>` only, dropping `<exit-gates>`, `<failure-modes>`, `<depth-calibration>` (`orchestrator.py:1684`) |

Three of the four are existing switches, so the feature is non-deprecating by
construction. The only new code is one branch inside `render_stages_block`.

### 2.2 What never thins

- **Philosophy (layer 1)** — preset text stays whole. You ruled it out of
  scope and I agree: it is what makes outputs comparable across cycles.
- **Operating protocol prescriptive rules** — off-limits paths, the derived
  `harness_root` fence, tool contracts, the `[INPUT: x]` / `[OUTPUT: x]`
  envelope, compaction thresholds, the Wolfram and test-runner blocks. These
  are *mechanism*: dropping them breaks parsing or safety, not just tone.
- **Role blocks (layer 3.5)** — each role's job description and its
  `<authority-and-commitment>` invariants.

The line is: **exhortation may thin, machinery may not.**

### 2.3 Automatic, deterministic selection

Data-driven so a new model needs no code release:

```yaml
model_profiles:
  enabled: false
  auto: true
  profile: null              # explicit override wins over auto
  default: standard          # unknown model → standard, never silently thin
  families:
    advanced: ["fable", "astra"]   # substring match, lowercased, on the RESOLVED model id
  overrides: {}              # e.g. {anti_patterns_enabled: true} to re-add one knob
```

Resolution order: `overrides` > `profile` > `auto` family match > `default`.

### 2.4 The per-agent subtlety

`agent_models` already routes each role to its own provider/model, so a run
can have a Fable researcher and an Opus auditor. But
`assemble_system_prompt(config, ...)` reads config-level switches, so a naive
implementation would apply one profile to every role. **Decided: per agent, from that agent's
routed model.** So a Fable researcher gets the lean prompt while an Opus
auditor in the same run keeps the full one — which is the only reading that
respects a config feature the harness already ships and documents.

The cost is real and worth naming: `assemble_system_prompt` needs the
resolved model threaded in, which touches every call site (the cycle loop,
the REPL, the final auditor/reporter, the curator, the manager). Two
consequences follow:

- The profile must be resolved **after** `agent_routing` picks the model,
  not from the config's global `model` key.
- The prompt cache keys on the system prompt, so two roles on the same model
  must produce the *same* profile deterministically — the resolution has to
  be a pure function of the resolved model id plus config, with no ordering
  or per-cycle state in it.

---

## 3. Feature 3 — the startup gate

### 3.1 Placement (as decided)

`launch` only. `start` and `resume` stay non-interactive, which is what keeps
cron, the fan-out clone spawn (`python -m long_exposure.exploration ...
resume`, `fanout.py:1073-1083`) and the benchmark adapter working unchanged.

### 3.2 The questions

| Q | Prompt | Choices | Applied to |
|---|---|---|---|
| 1 | Model to run on | `model_profiles`-aware list from `startup_gate.model_choices`, plus "other (type an id)" | `model` and the matching `agent_models.*.model` entries |
| 2 | Workspace directory | current `working_directory` as default; validated to exist and be writable | `working_directory` |
| 3 | Resume a previous run, or start fresh? | enumerated discoverable runs (see Q4 below), plus "fresh" | `--instance-dir` / state path selection |
| 4 | Usage limit for this run | integer 1–100, shown **only** when `usage_allowance.enabled` | `usage_allowance.run_pct` |

### 3.3 Persistence and headless escape hatches

- Answers are written to `<instance_dir>/gate_answers.json` and applied as an
  in-memory config overlay. `resume` reads the file and never re-asks.
- A copy lands in the run's `output/` for provenance — this is what lets the
  benchmark's honesty appendix state the exact model and caps the run used.
- `launch --no-gate` skips the gate entirely.
- Every answer has a flag (`--gate-model`, `--gate-workspace`,
  `--gate-resume`, `--gate-usage-pct`). A supplied flag is not re-asked, so a
  fully-flagged `launch` is non-interactive.
- Non-TTY stdin with an unanswered question → exit with a message naming the
  missing flag. Never silently default; a wrong model is an expensive mistake.

### 3.4 Run enumeration for Q3

There is no instances root today — instance dirs come only from
`--instance-dir` or `AGENT_INSTANCE_DIR`, and the legacy default is a single
`exploration_state.json` in the data dir (`exploration.py:294`, `:297-311`).
So Q3 needs a source of truth for "what runs exist". Options in Q4 below.

---

## 4. Feature 4 — usage cap as a delta percentage

### 4.1 State the honesty constraint first

The harness **cannot read real subscription usage.** I verified this: the
`claude` CLI exposes no `usage` subcommand, `/usage` is interactive-only, and
the `-p` JSON envelope carries `total_cost_usd`, token counts and `num_turns`
— nothing about plan consumption. Anything that claims "20% of your weekly
Max limit" would be inventing a denominator.

So the feature is built and **labelled** as a proxy: a percentage of a
*user-declared* weekly allowance in API-equivalent dollars. That is exactly
the shape you chose, and it has one genuinely nice property — the delta
semantics you asked for come for free. The run's ledger totals only this
run's spend (clone rows fold in via `UsageLedger.merge`), so a cap measured
from run start *is* a delta on top of whatever was already used, with no
knowledge of prior consumption required.

### 4.2 Config and derivation

```yaml
usage_allowance:
  enabled: false
  weekly_allowance_usd: 0     # user-declared, API-equivalent notional dollars
  run_pct: 0                  # 1-100; gate Q4 writes this
  basis: cost                 # cost | tokens
  action: end_of_run          # end_of_run (graceful pipeline) | hard_stop
```

Implementation is deliberately small: at startup, resolve
`weekly_allowance_usd * run_pct / 100` and fold it into `loop_cfg` as
`max_cost_usd`, taking the **minimum** of it and any score-set value. The
existing `UsageLedger.budget_exceeded(loop_cfg)` check at the cycle boundary
(`usage_ledger.py:331`) then does the work, and `action: end_of_run` is
already its behaviour. No new gate site, no new stop path.

### 4.3 The clone overshoot — a real hazard, not a hypothetical

Clones are spawned with the **same score file** and no budget environment
(`fanout.py:1073-1139` sets fork, instance, account and pool vars — nothing
about budget). So each of up to `FANOUT_MAX_BRANCHES = 3` clones would
independently permit the full run cap, and because clone ledgers merge only
at barrier collapse, the root cannot see the spend while it happens. Worst
case is ~4× the declared cap (root + 3 clones), bounded in time by the 10 h
`FANOUT_CAP_SECONDS` per clone.

Mitigation to build with the feature, not after: pass an explicit per-clone
sub-allowance in the spawn env (`LONG_EXPOSURE_MAX_COST_USD = remaining / K`)
and have the clone's `loop_cfg` resolution honour it. The residual overshoot
is then one cycle's spend per clone rather than one full cap per clone, and
that residual gets stated in the docs rather than hidden.

### 4.4 Reporting

`usage_summary.md` gains one line and one disclaimer:

```
Run allowance: 20% of a declared $400.00 weekly allowance = $80.00 cap
Consumed:      $61.42 (76.8% of the run allowance)
Note: the weekly allowance is operator-declared. The harness cannot read
subscription usage; this is an API-equivalent proxy, not a meter reading.
```

---

## 5. Cross-cutting work

### 5.1 Sequencing

The name-based failure-handling refactor (§1.7) is a hard prerequisite for
Feature 1 and touches the busiest part of the cycle loop, so it goes first
and lands on its own, verified against the existing suite before anything
else moves.

1. §1.7 refactor — `i == 0` / `i == len(...)-1` → name-based. No behaviour change.
2. Feature 4 — smallest, self-contained, and the gate's Q4 depends on it.
3. Feature 2 — one new branch in `render_stages_block` plus profile resolution.
4. Feature 3 — the gate, which surfaces Features 2 and 4 to the operator.
5. Feature 1 — the largest, and the one that benefits from the other three
   being observable when it first runs live.

### 5.2 Tests each feature owes

- **Cycle plan**: parse/valid, parse/malformed → fixed flow, researcher in
  block → reject, worker chain clamp, auditor moved to last, fan-out
  co-occurrence → fan-out wins, audit floor forces an auditor on cycle N+1,
  a clone ignores a `<cycle_plan>` block and keeps the fixed flow,
  `[[REQUEST_AUDIT]]` re-inserts the auditor *and* resets the floor counter,
  name-based failure handling for a failed mid-tail worker and a failed
  non-final auditor.
- **Profiles**: `enabled: false` produces a prompt byte-identical to today
  for every one of the eight routed roles
  (the non-deprecation guarantee, asserted not asserted-about); family match;
  unknown model → `standard`; `overrides` beats `profile` beats `auto`;
  `lean` stages block omits exactly three element types and nothing else.
- **Gate**: every flag suppresses its question; non-TTY + missing answer →
  non-zero exit naming the flag; `gate_answers.json` round-trips and `resume`
  does not re-ask; `--no-gate` is a no-op path.
- **Allowance**: percentage → cap arithmetic; min-wins against a score cap;
  `run_pct: 0` or `weekly_allowance_usd: 0` → feature inert; clone
  sub-allowance present in the spawn env; the summary line renders with its
  disclaimer.

### 5.3 Docs

`docs/configuration-reference.md` gains all four blocks;
`docs/soft-guidance.md` gains the thins/never-thins table with the
"exhortation may thin, machinery may not" line; `docs/usage-guide.md` gains
the gate walkthrough; `docs/parallelism.md` gains the clone sub-allowance
note. `docs/rcb-benchmark-plan.md` is affected — a benchmark run would now
record `gate_answers.json` as provenance, which strengthens §7 of that plan,
but the benchmark itself must decide whether to enable Features 1 and 2 (that
changes what is being measured). Flagged, not decided here.

### 5.4 Deliberately not built

- No auditor-emitted plan, and no second planner of any kind (§1.2).
- No auditor fan-out authority.
- No researcher self-scheduling (index 0 is never planned).
- No reading of real subscription usage (§4.1) — not deferred, impossible.
- No account-pool changes of any kind; that feature stays untouched.
- No new memory tier. The memoir (L1) is closed.
- No philosophy or protocol thinning.

---

## 6. Open questions

### Resolved (round 1)

| Q | Decision |
|---|---|
| Worker escalation | `[[REQUEST_AUDIT]]` is built, **mandatory to honour**, resets the audit-floor counter, and emits a health event so a bad planner is visible. |
| Cycle planning in clones | **Root only.** Clones keep the fixed flow, like the fan-out decision itself. |
| Profile resolution | **Per agent**, from that agent's routed model; resolved after `agent_routing`, as a pure function of model id + config. |
| `audit_floor_cycles` | **2.** |

### Still open (round 2)

- **Q4** — run enumeration for gate Q3: a new `instances_root` scan, a
  registry file the harness appends to, or `sessions.db`?
- **Q5** — what gate Q1 rewrites: the global `model` only, or also the
  `agent_models` entries that still point at the old default?
- **Q6** — does the benchmark run (`docs/rcb-benchmark-plan.md`) enable
  Features 1 and 2, or stay on the fixed flow and full guidance?
- **Q7** — the per-clone sub-allowance (§4.3): split the remaining budget
  evenly, or leave clones uncapped and accept the documented overshoot?
