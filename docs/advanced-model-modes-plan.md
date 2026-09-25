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
| 4 | One total spend limit, as a delta % of a declared weekly allowance | off | `usage_allowance` |

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

**Implemented.** `_failure_disposition(agent_name, flow_index)` in
`exploration.py` returns one of `FAILURE_ABORT_CYCLE`,
`FAILURE_AUDIT_FALLBACK`, `FAILURE_MARK_OUTPUTS`. Role name decides; the
index is consulted only for the one case where it carries real meaning — the
cycle's *first* agent failing means nothing upstream produced fresh input, so
there is nothing for a later turn to act on (the post-merge worker-only
cycle). Covered by `tests/test_failure_disposition.py`, which pins both of
today's flows to their pre-refactor outcomes and both variable-tail bugs to
their fixed ones.

### 1.7a Implementation notes

**Where the plan is applied.** `flow_this_cycle[1:] = planned_tail`, mutated
in place while the `for` loop is iterating it — Python iterates the live
list, so the remaining turns of that very cycle come from the plan. Three
consequences that had to be handled:

- **`flow_this_cycle = flow` was an alias**, not a copy. Rewriting the tail
  would have edited the score's own `flow` list, and every later cycle would
  have inherited one cycle's plan permanently. All three assignment sites now
  build a fresh list.
- **A rotation retry restarts the cycle from the researcher**, which emits a
  fresh plan — so the fixed flow is snapshotted pre-cycle and restored on
  retry, alongside the existing per-cycle rollbacks. Without that, the retry
  would run the abandoned attempt's tail while the new researcher believed it
  was planning it.
- **Fan-out wins structurally.** The plan block sits *after* the fan-out
  trigger, which `break`s out of the flow when it fires. Reaching the plan at
  all means fan-out did not fire, so no precedence rule has to be remembered
  and a fan-out cycle never logs a plan that will not run.

**The audit-floor streak** counts consecutive *completed* cycles with no
auditor, and persists in run state next to `low_output_streak` — a
stop/resume must not hand the run a fresh licence to skip audits. A failed or
rate-limited cycle does not count: it never got the chance to audit, and
holding that against the run would force audits onto cycles that produced
nothing.

**Verified against the real cycle loop**, not just the parser: a planned
chain runs four turns in one cycle; a plan that drops the auditor runs two;
the floor fires on exactly the third audit-free cycle; `[[REQUEST_AUDIT]]`
pulls the auditor back in while merely discussing the token does not; a
chained worker's output accumulates under `## worker turn 2`; the score's
flow survives two planned cycles; a clone ignores a plan block; a post-merge
cycle has no planner and does not crash; and fan-out firing leaves the
planned tail unrun.

Two failure paths worth calling out, because they are exactly what Stage 1's
refactor existed for and are now covered end to end: a worker failing as the
**last** turn of a planned tail gets a failure marker rather than
`FALLBACK_AUDIT` — pre-refactor it would have fabricated an audit the next
cycle's researcher would read as real — and a worker failing mid-chain still
lets the auditor run.

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

Shipped in the **score** (`exploration-score.yaml`, under `loop`) rather than
config.yaml, because it shapes the flow rather than the models.

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
| `checkpoint_format` | `standard` (9 fields) | `minimal` (6 fields; keeps a one-line `<gate-check>`, drops `<what-i-did>` / `<next-action>` and the multi-line gate answers) | existing branch, `orchestrator.py:1742` |
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

**Implemented, and the cost turned out to be zero.** I expected to thread a
resolved-model argument through every call site. Reading the call graph
showed that is unnecessary: `assemble_system_prompt` is *always* handed a
per-agent config from `build_agent_config` (`conductor.py:158`), whose
`model` key is already the model `agent_models` routed that role to —
`apply_agent_models` runs at `exploration.py:3427`, well before the cycle
loop. So resolving the profile at the top of the assembler gives per-agent
tiering with no signature change, and keeps it a pure function of the
config, which is what stops two roles on the same model from splitting the
prompt cache.

Verified through the real dispatch path rather than the assembler alone: a
config with a Fable researcher and an Opus auditor produces
`researcher → advanced` (23,984 chars, no anti-patterns, lean stages) and
`auditor → standard` (29,696 chars, full guidance) in one run.

### 2.5 Lean mode had to be made coherent, not just shorter

Found by reading the rendered lean prompt rather than the diff. Dropping the
exit-gate enumeration leaves the framework template still saying *"every
gate must be answered yes with evidence before advancing"* and the
checkpoint envelope still asking the agent to *"answer the current stage's
exit gates from the framework"* — pointing at a list that is no longer
there. That is worse than verbose: it is an instruction the agent cannot
follow.

So `lean` emits one `<exit-gate-policy>` block (~40 tokens) redefining what
a gate check means when the list is absent — derive the gates from the
stage's `<purpose>` and `<required-output>`, state in one line what you
produced and why it satisfies them — and says explicitly that cadence is
unchanged. Net saving on the shipped `staged` framework is still ~1,950
tokens per advanced-profile agent turn.

---

## 3. Feature 3 — the startup gate

### 3.1 Placement (as decided)

`launch` only. `start` and `resume` stay non-interactive, which is what keeps
cron, the fan-out clone spawn (`python -m long_exposure.exploration ...
resume`, `fanout.py:1073-1083`) and the benchmark adapter working unchanged.

### 3.2 The questions

| Q | Prompt | Choices | Applied to |
|---|---|---|---|
| 1 | Model to run on | `model_profiles`-aware list from `startup_gate.model_choices`, plus "other (type an id)" | `model`, plus every `agent_models.*.model` that still equals the pre-gate global default |
| 2 | Workspace directory | current `working_directory` as default; validated to exist and be writable | `working_directory` |
| 3 | Resume a previous run, or start fresh? | enumerated discoverable runs (see Q4 below), plus "fresh" | `--instance-dir` / state path selection |
| 4 | Usage limit for this run | integer 1–100, shown **only** when `usage_allowance.enabled` | `usage_allowance.run_pct` |

**How Q1 is applied (decided).** The shipped config pins all eight roles
explicitly in `agent_models`, so setting the global `model` alone would have
no effect — the gate answer has to reach the routing table. It rewrites
`model`, then rewrites each `agent_models.*.model` **whose value equals the
pre-gate global default**. A deliberately heterogeneous routing (a Codex
worker, a Sonnet reporter) survives untouched.

Because that rule is subtle, the gate then **prints the resulting routing
table** — role, provider, model, effort, and the resolved capability profile
per role (§2.4) — before the run starts. One answer, one visible consequence.
The same table is written to `gate_answers.json` and copied into `output/`.

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

**Decided: a registry file, with an instances-root scan as fallback.**

- **Primary.** Every run appends one JSON line to
  `~/.long-exposure/runs.jsonl` at startup: `run_id`, instance dir, state
  path, task, `started_at`. Append-only and atomic-append, so two concurrent
  runs (or a root and its clones) cannot corrupt it. Clones do **not**
  register — a fork is not a resumable run. Entries whose state path no
  longer exists are shown as tombstones and are not offerable.
- **Fallback.** If the registry is missing or has no usable entries (an older
  run, a fresh checkout, a moved home directory), glob
  `startup_gate.instances_root` (default `./instances`) for
  `*/exploration_state.json` and read `run_id` and cycle out of each.

Both paths feed the same display list, so the gate's behaviour does not
depend on which one answered. Two code paths is the cost; the benefit is that
the gate is useful on day one against runs that predate the registry.

---

### 3.4a Two bugs the tests caught

Recording these because both were silent failures that only surface hours
into a run:

- **`--gate-resume` pointing at a vanished state file was accepted.** The
  tombstone check ran only on the interactive menu branch. A flag naming a
  deleted run would have been taken as a state path, and the run would have
  started *fresh at that path* — silently losing the operator's intent to
  resume, which is the worst possible outcome for that question. The check
  now runs on both branches, and tests the path itself rather than the
  listing, so a run absent from the registry but present on disk still
  resumes.
- **An out-of-range menu number was accepted as free text.** With
  `allow_other=True`, typing `99` at Q1 fell through to the free-text branch
  and became a model id. A mistyped number is a mistyped menu choice; it now
  reprompts.

### 3.5 Config surface

```yaml
startup_gate:
  enabled: false                 # off → `launch` behaves exactly as today
  model_choices: [opus, fable, sonnet]   # plus "other (type an id)"
  instances_root: ./instances    # fallback enumeration source for Q3
  registry_path: ~/.long-exposure/runs.jsonl
  max_runs_listed: 10
```

**Implemented** in `long_exposure/startup_gate.py`. Verified end to end
through the real CLI: a flagged `launch` answered all four questions
headlessly, printed the routing table, persisted the answers, wrote the
provenance copy, appended the registry and completed with exit 0 — and a
subsequent `resume` with no TTY and no flags asked nothing while still
applying the persisted model and the $200 spend limit.

---

## 4. Feature 4 — one total spend limit, as a delta percentage

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
  enabled: false              # optional; false (or run_pct 0) = unlimited
  weekly_allowance_usd: 0     # user-declared, API-equivalent notional dollars
  run_pct: 0                  # 1-100; gate Q4 writes this
```

**One total limit, and only one.** The cap is a single number for the whole
run: `weekly_allowance_usd * run_pct / 100`. There are no per-agent, per-role,
per-cycle or per-clone sub-budgets — that is a deliberate design constraint,
not an omission. The run spends freely against the total until the total is
gone.

The delta semantics you asked for come for free from this shape: the ledger
totals only this run's spend, so a cap measured from run start *is* a delta on
top of whatever was already consumed, with no knowledge of prior usage
required.

### 4.3 Hitting the limit kills the run

This is the part that does *not* reuse the existing budget machinery. The
current `loop.max_cost_usd` gate calls `UsageLedger.budget_exceeded`
(`usage_ledger.py:331`) at the cycle boundary and ends the run as a natural
end-of-run — the final auditor, final reporter and curator all still run. A
total spend limit is a different contract: **when the total is hit the run is
killed, not stopped.**

Concretely, the kill path:

1. **Terminates immediately**, not at the next cycle boundary. The check runs
   wherever spend is recorded — after every agent turn — and additionally on
   the fan-out barrier poll, because that is the only place clone spend
   becomes visible while clones are alive.
2. **Kills the clone process groups** before exiting, reusing the existing
   SIGTERM → 10 s grace → SIGKILL `killpg` sweep (`fanout.py:1998-2019`).
   Clones were spawned with `start_new_session=True`, so this takes their
   provider CLI subprocesses with them. Without this, clones outlive the root
   and keep spending past the limit that just killed it.
3. **Skips the end-of-run pipeline entirely** — no final auditor, no final
   reporter, no curator. Those stages cost money, and spending past the cap
   to write a report about hitting the cap is incoherent.
4. **Leaves a marker** (`output/killed_spend_limit.json`: the cap, the
   observed total, the cycle, the timestamp) and exits non-zero, so an
   operator or a wrapper script can tell a kill from a clean finish. The
   normal stop signal path stays untouched and still means graceful.

`enabled: false`, `run_pct: 0`, or `weekly_allowance_usd: 0` → the feature is
inert and the run is unlimited, which stays the default.

### 4.4 Seeing clone spend before the kill is too late

A total-only limit has one hard requirement: the root has to observe clone
spend *while clones run*. Today it cannot — clone ledgers merge into the root
only at barrier collapse (`fanout.py:~1980`), and clones are spawned with the
same score file and no budget environment at all (`fanout.py:1073-1139` sets
fork, instance, account and pool variables, nothing about spend). So up to
three clones can each run for 10 h (`FANOUT_CAP_SECONDS`) entirely unseen.

The fix does not need sub-budgets. Every process already writes an
incrementally-updated `output/usage_summary.json` on each status write
(`exploration.py:2341`), including clones in their own instance dirs. So the
root's existing barrier poll sums its own ledger plus each live clone's
summary file and checks the **total** against the single cap. Missing or
unparseable clone files count as zero and are logged, never fatal.

Residual overshoot is bounded by the poll interval rather than by anything
architectural, and gets stated plainly in the docs: the limit is enforced
within one poll of being crossed, not to the dollar.

**Implemented** in `long_exposure/spend_limit.py`, with three check sites:

| Site | What it catches |
|---|---|
| `_record_usage` (`source=agent:<name>`) | the primary detector — fires as soon as a turn's cost lands, and the between-turns guard in the flow loop then stops the *next* turn starting |
| cycle boundary (`source=cycle_boundary`) | spend recorded outside an agent turn — compaction, out-of-cycle agents |
| fan-out barrier poll (`source=fanout_barrier`) | live clone spend, summed from each clone's `usage_summary.json` |

Two things worth recording because they are easy to get wrong:

- **The hook reads the RUN config, not the per-agent config** it is handed.
  A per-agent `usage_allowance` override would be exactly the sub-budget
  this feature rules out, and reading one source also means a future
  `_record_usage` call site passing a narrower config cannot silently
  disable enforcement.
- **The raise happens last** — after `save_state`, after the status file,
  after `conn.close()`. A kill must not cost the run its resumability.

Verified end to end against the real cycle loop with a stubbed provider
(`tests/test_spend_limit.py::KillPathIntegrationTests`): a $10 cap and a $25
first turn stops after `researcher` alone, writes the marker, saves state,
writes `killed_spend_limit` to the status file, and calls none of the three
end-of-run stages — while a run under the cap finishes normally and a run
with the feature off never trips at $10,000 a turn. Clone non-enforcement
was checked by driving a real clone (`AGENT_FORK_ID` set) at $500/turn
against a $1 cap: all three agents ran, nothing tripped, no marker.

### 4.5 Reporting

`usage_summary.md` gains one line and one disclaimer:

```
Run allowance: 20% of a declared $400.00 weekly allowance = $80.00 cap
Consumed:      $61.42 (76.8% of the run allowance)
Note: the weekly allowance is operator-declared. The harness cannot read
subscription usage; this is an API-equivalent proxy, not a meter reading.
```

On a kill, `usage_summary.md` says so explicitly and names the marker file,
so the summary never reads like a completed run.

---

## 5. Cross-cutting work

### 5.1 Sequencing

The name-based failure-handling refactor (§1.7) is a hard prerequisite for
Feature 1 and touches the busiest part of the cycle loop, so it goes first
and lands on its own, verified against the existing suite before anything
else moves.

1. **§1.7 refactor** — `i == 0` / `i == len(...)-1` → name-based. No
   behaviour change, so it lands and is verified against the existing suite
   on its own.
2. **Feature 2 (profiles)** — one new branch in `render_stages_block` plus
   per-agent profile resolution. The riskiest part is threading the resolved
   model through every `assemble_system_prompt` call site, which is
   mechanical and fully covered by the byte-identical-prompt test.
3. **Feature 4 (spend limit)** — bigger than it first looked, because the
   kill path and the live clone-spend poll are both new (§4.3, §4.4). It
   comes before the gate because gate Q4 exists only if this does.
4. **Feature 3 (the gate)** — surfaces Features 2 and 4 to the operator, so
   it wants both already working.
5. **Feature 1 (cycle planning)** — the largest, and the one that most
   benefits from the other three being observable when it first runs live.

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
- **Spend limit**: percentage → cap arithmetic; `enabled: false`,
  `run_pct: 0`, or `weekly_allowance_usd: 0` → feature inert and the run
  unlimited; the check fires after an agent turn rather than waiting for the
  cycle boundary; the kill skips final auditor, final reporter and curator;
  the clone process-group sweep runs before exit; the root's barrier poll
  sums root + live clone `usage_summary.json` files and a missing or
  unparseable clone file counts as zero without raising;
  `killed_spend_limit.json` is written and the exit code is non-zero; the
  summary line renders with its disclaimer and says "killed" on a kill.

### 5.3 Docs

`docs/configuration-reference.md` gains all four blocks;
`docs/soft-guidance.md` gains the thins/never-thins table with the
"exhortation may thin, machinery may not" line; `docs/usage-guide.md` gains
the gate walkthrough; `docs/parallelism.md` gains the live clone-spend poll
and the kill sweep. `docs/rcb-benchmark-plan.md` is affected — a benchmark run would now
record `gate_answers.json` as provenance, which strengthens §7 of that plan,
and the benchmark will **enable both Feature 1 and Feature 2** (decided).
That is the "most advanced tool" framing of that plan taken seriously: the
run measures Fable on a harness that is not fighting it.

Two consequences to write into that plan rather than discover during it:

- **It supersedes the pre-registration.** `docs/rcb-benchmark-plan.md` §7
  currently describes a fixed-flow, full-guidance harness. Enabling both
  features changes what is measured, so the plan's configuration appendix and
  its honesty section both need updating before launch, and the run must be
  reported as measuring the advanced-mode harness — not as the design
  originally pre-registered.
- **Live testing gates the launch.** You have already said this branch needs
  live testing before it merges; benchmarking on two features that have never
  run live would make a bad result uninterpretable (harness bug or model
  limit?). Sequence: implement → live-test the branch → update the benchmark
  plan's appendix → launch.

### 5.4 Deliberately not built

- No auditor-emitted plan, and no second planner of any kind (§1.2).
- No auditor fan-out authority.
- No researcher self-scheduling (index 0 is never planned).
- No reading of real subscription usage (§4.1) — not deferred, impossible.
- **No per-agent, per-role, per-cycle or per-clone spend sub-budgets.** One
  total limit, or none (§4.2). Explicitly ruled out, not deferred.
- No graceful end-of-run on a spend kill — the whole point is that the run
  stops spending (§4.3).
- No account-pool changes of any kind; that feature stays untouched.
- No new memory tier. The memoir (L1) is closed.
- No philosophy or protocol thinning.

---

## 6. Decision log

Every question raised by this plan has been answered across two interactive
rounds. Recorded here so the implementation does not relitigate them.

### Resolved (round 1)

| Q | Decision |
|---|---|
| Worker escalation | `[[REQUEST_AUDIT]]` is built, **mandatory to honour**, resets the audit-floor counter, and emits a health event so a bad planner is visible. |
| Cycle planning in clones | **Root only.** Clones keep the fixed flow, like the fan-out decision itself. |
| Profile resolution | **Per agent**, from that agent's routed model; resolved after `agent_routing`, as a pure function of model id + config. |
| `audit_floor_cycles` | **2.** |

### Resolved (round 2)

| Q | Decision |
|---|---|
| Gate run enumeration | **Registry file** (`~/.long-exposure/runs.jsonl`, append-only, clones excluded), **falling back** to an `instances_root` scan. |
| Gate model answer | Rewrites `model` **plus** every `agent_models` entry still on the old default, then **prints the resulting routing table** and the per-role profile. |
| Benchmark configuration | **Enable both** Feature 1 and Feature 2 — which supersedes the current pre-registration and must follow live testing. |
| Spend limit shape | **One total limit only** — no per-agent, per-cycle or per-clone sub-budgets. Optional/disable-able. On hitting it the run is **killed**, not gracefully stopped: clone process groups swept, end-of-run pipeline skipped, marker written, non-zero exit. |

### Still open

Nothing blocking. Two things to settle when implementation reaches them,
neither worth a decision in the abstract:

- The barrier-poll interval that bounds spend-limit overshoot (§4.4) — pick
  it from the observed poll cadence once the limit is wired up, not now.
- Whether the exhaustion detector's per-cycle output floor needs adjusting
  once worker chains raise peak observed output (§1.6) — a question for the
  first live run, not for the design.

---

## 7. Post-implementation audit

Seven defects found by stress and live testing after all five stages were
green. Recorded because each one was invisible to the unit tests that
already passed.

| # | Defect | Why it mattered | Found by |
|---|---|---|---|
| 1 | On a spend trip the fan-out barrier only wrote **stop files**, which a clone honours at its *next* cycle boundary | A clone mid-agent-turn kept spending until it finished — up to the 10 h `FANOUT_CAP_SECONDS` — while the root waited in the barrier. For a limit whose purpose is to stop spending, the opposite of the documented kill. Measured: 61 s of waiting, and the clones ran to completion | A live barrier with real `sleep 60` subprocesses |
| 2 | The escalated auditor was inserted **immediately after** the escalating worker | Worker 1 of a chain escalating ran the audit before worker 2, so `audit_report` and the memoir described only part of the cycle and the next researcher read that partial verdict as the cycle's. Also contradicted the parser's own auditor-last rule | Reading the seam adversarially; a test I had written asserted the buggy behaviour |
| 3 | A plan could schedule `final_auditor`, `final_reporter`, `curator`, `reporter` | `parse` validated against `score.agents`, but the cycle loop populates inputs only for **flow** members. Those agents would have run with `[UNAVAILABLE: stage]` and `[UNAVAILABLE: expected_file]` — a full turn for nothing, and two of them set `agent_teams: true` | Same |
| 4 | `enabled: "false"` in YAML read as **truthy** | A run the operator believed was uncapped got killed; a flow they believed was fixed started being planned. Quoted booleans happen by accident constantly (a template, a `sed`, an editor) | An adversarial value sweep over the config parsers |
| 5 | The spend tripwire had **no lock** | `UsageLedger` documents that spend is recorded from the cycle loop *and* the manager poller thread (`manager.py` → `_call_agent_with_rotation` → `_record_usage` → `check`), so two threads could both build a trip record | Reading the ledger's own threading note, then a 16-thread race |
| 6 | A marker from a killed run **survived a clean resume** | A wrapper checking for `killed_spend_limit.json` would report a successful resume as killed — exactly the question the marker exists to answer | A kill-then-resume cycle |
| 7 | Lean mode emitted the `<exit-gate-policy>` block for a framework with **no stages** | The block points at `<purpose>` and `<required-output>` elements not in the prompt — the incoherence the block was added to prevent | Sweeping all 25 philosophy x framework combinations |

Also fixed as cosmetics, because they mislead an operator reading the
artifacts: a cycle plan logged for a cycle the spend limit had just killed,
and two differently-worded `**Status:**` lines in the status file.

### What the testing actually covered

| Exercise | Result |
|---|---|
| 200,000 fuzzed `<cycle_plan>` inputs (structured, fragment-shuffled, random) | 24,272 accepted; **0** exceptions, **0** invariant breaks (agent outside the flow, planner self-scheduled, chain over cap, two auditors, auditor not last, over the turn cap) |
| 3,200 concurrent registry appends across 16 threads | 3,200 lines, **0** torn; the menu deduped to 1 entry per state path |
| 16 threads racing the spend tripwire, 8,000 calls | exactly **1** distinct trip record |
| 40-cycle run mixing planned chains, audit-free cycles, malformed plans, worker failures and simulated rate limits, under a stack-dumping watchdog | 118 agent turns, **0** invariant breaks across all 40 cycles |
| Same run with the cap lowered | killed at cycle 12, marker written, end-of-run skipped, state resumable |
| Kill, raise the cap, resume | resumed at the right cycle with prior spend carried forward |
| Live fan-out barrier, real subprocesses, clone-only spend | tripped on clone spend alone (root $0.50 of a $10 cap), stop files written, process groups terminated, barrier 61 s → 15 s |
| All 25 philosophy x framework combinations | **0** problems; saving 1,055–2,033 tokens/turn by framework |
| Every gate entry path (`start`, `resume`, `--no-gate`, flagged, unflagged, bad workspace, missing resume target) | correct exit codes; `start`/`resume` never ask |
| Profiles-off prompt vs the commit before the feature existed, 4 off-modes x 9 roles | byte-identical, modulo `harness_root` (derived from the code's own directory) |

### What remains untested

- **No live model has emitted a `<cycle_plan>` block.** Everything above
  used a stubbed provider. Two unknowns follow: whether a real model emits
  well-formed blocks, and whether its scheduling judgement is any good.
  `docs/rcb-benchmark-plan.md` §2 gap 5 carries this, and §9 carries the
  kill criterion.
- **The interactive Claude transport** was not exercised with any of these
  features.
- **Multi-account pooling** was left untouched by design and not tested.
