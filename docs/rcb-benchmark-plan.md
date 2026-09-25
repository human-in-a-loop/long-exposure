# ResearchClawBench: execution plan for the bench-mode branch

Scope: one venue, one model, **one arm**, one shot per task. This is the
runnable plan for the advanced-mode branch. The wider survey and the
rejected venues stay in `docs/benchmarking-plan.md`; this document
supersedes its §5.2.

Status: **pre-registration, revised.** Nothing here has been run. Peer
numbers are from the published paper, not from our runs.

**Revision (2026-09-25).** The original pre-registration described a
fixed-flow, full-guidance harness. The run now also enables
`loop.cycle_planning`, so the researcher shapes each cycle's tail — a real
behavioural change, and the substance of what this run measures beyond
stock. `model_profiles` is enabled too but is **inert on Opus 4.6**, so the
guidance stack is *not* thinned. §5.5 states exactly what the configuration
yields, §2 gap 5 records that cycle planning has no prior live mileage, and
§9 carries the kill criterion for it. The results must not be described as
measuring thinned guidance.

**Sequence before launch:** implement → live-test the branch → re-read this
plan's §5 and §10 against the code → launch. The features are covered by
650 tests, 200k fuzzed plan blocks and multi-cycle runs against a stubbed
provider. One live cycle has now run (2026-09-25): the planning guidance
reached the researcher and the researcher declined to plan, so the plumbing
is confirmed and the planner's judgement is not — see §2 gap 5.

---

## 1. The venue, and the two rows we can stand next to

**ResearchClawBench** (InternScience, arXiv 2606.07591, MIT licence;
[repo](https://github.com/InternScience/ResearchClawBench),
[data](https://huggingface.co/datasets/InternScience/ResearchClawBench)).
40 tasks across 10 scientific domains. Each task is grounded in a real
published paper, ships `data/` and `related_work/`, hides the target
paper, and asks the agent to produce `report/report.md` plus code and
figures. An expert-curated multimodal rubric is scored by an LLM judge on
a 100-point scale where **50 = reference-level evidence (the target paper
re-discovered)** and >50 implies discovery beyond it.

Chosen because the published unit of comparison is the harness, the task
shape is a long-exposure directive, the grader is public, and it needs no
GPU.

### Autonomous agent harnesses (paper: 280 runs, 7 agents × 40 tasks, one run each)

| Agent (harness) | Model | Avg score |
|---|---|---|
| **Claude Code** | **Claude-Opus-4.6** | **21.5** |
| EvoScientist v0.1.1 | GPT-5.4 | 18.8 |
| Codex CLI | GPT-5.4 | 18.4 |
| OpenClaw | GPT-5.4 | 16.6 |
| ResearchClaw | GPT-5.4 | 16.3 |
| EvoScientist v0.0.4 | GPT-5.4 | 15.5 |
| ARIS Codex | Codex/GPT-5.4 | 13.6 |
| Nanobot | GPT-5.4 | 12.8 |

No agent ran on more than one model.

### ResearchHarness thin baseline, 17 native LLMs (paper Table 5, top of list)

| Model | Avg score |
|---|---|
| Claude-Opus-4.7 | 20.7 |
| **Claude-Opus-4.6** | **19.9** |
| Qwen3.7-Max | 18.7 |
| GLM-5.1 | 18.2 |
| … (13 more, down to Grok-4.3 at 12.4) | |

### Why this settles the model choice

The two tables intersect at exactly one model.

| Candidate | Same-model published harnesses | What they are |
|---|---|---|
| **Claude-Opus-4.6** | **2** | Claude Code **21.5** (the best published *agent* harness) and ResearchHarness **19.9** (a thin scaffold) |
| Claude-Opus-4.7 | 1 | ResearchHarness **20.7** only — no agent harness ran on 4.7 |

Opus 4.6 gives twice the comparability, and the extra row is the one that
matters most: the strongest published agent harness, the direct
competitor. It also produces something a single-arm run normally cannot
have — **a bracket measured on our exact model**:

> On Claude-Opus-4.6, a thin scaffold scores **19.9** and the best agent
> harness scores **21.5**. That 1.6-point band is the observed harness
> effect on this venue, on this model.

That band is the number long-exposure has to beat to be interesting, and
it is measured under the same model we will run. Nothing about Fable 5.1
could have given us that: no published row runs that tier, so model and
harness would have moved together.

**Decision: `claude-opus-4-6`.** Fallback order and its cost is in §3.

---

## 2. What this run is, and what it is not

**One arm.** Long-exposure, one configuration, everything on except the
final auditor and final reporter (§5), on `claude-opus-4-6`, across all 40
tasks, one attempt each. No baseline arm, no ablation grid. 41 runs
including the smoke test.

**The configuration in one line:** main's stock budget, this branch's bug
fixes, the run memoir on, and **researcher-planned cycle tails on**. Model
capability profiles are enabled but inert on this model, so the guidance
stack is *not* thinned — §5.5 spells that out, and the results must not be
described as measuring thinned guidance.

### What it measures

- **A harness comparison on a shared model.** Our score sits next to
  21.5 and 19.9, both produced on Claude-Opus-4.6. The model is held
  fixed, so a difference is attributable to harness and conditions rather
  than to model generation. This is the whole reason for choosing 4.6.
- **A leaderboard row**: this harness on this model, honestly labelled.
- **Whether the pair approaches the benchmark's own threshold.** Fifty
  means the hidden target paper was re-discovered; nothing published is
  close.
- **How the harness behaves on 40 real research tasks at this operating
  point** — cycles to exhaustion, auditor verdict patterns, fan-out
  incidence, spend, failure modes (§7.6). Engineering evidence that stands
  on its own. Note "this operating point", not "its default": cycle
  planning is on, which the shipped default is not.

### What it still cannot establish

The model is controlled; the *conditions* are not. Five gaps, all of which
must travel with the number:

1. **Different judge invocation.** Same judge model name (`gpt-5.1`), but
   the paper's grading run happened earlier, possibly on a different
   snapshot. Judge drift between their run and ours is uncontrolled.
2. **Different harness vintage on their side.** Their Claude Code row used
   whatever version was current then; the CLI has moved since.
3. **One run per task on both sides.** Neither our number nor theirs
   carries a variance estimate, so a small gap is not distinguishable from
   run-to-run noise.
4. **No compute-matched control.** Long-exposure at this operating point
   spends one to two orders of magnitude more tokens than Claude Code's
   single call. A win is a win *at the harness's own operating point*, not
   at equal spend.
5. **Cycle planning has one live sample, and in it the researcher declined
   to plan.** A 2026-09-25 smoke run (one cycle, killed by a deliberate $6
   spend cap) confirmed the `<cycle_plan_guidance>` block reaches the
   researcher's prompt, and that the researcher emitted no plan — no block,
   no mention of one, in an 11,825-character brief. That is permitted
   behaviour ("omitting it is always valid") and was arguably right there:
   the brief scheduled a single build step, which is the default shape. But
   the sample is one cycle, and the cycle least likely to need a chain. So
   the open question narrows rather than closes: the plumbing works; the
   planner's *judgement* is unmeasured. If the researcher never plans across
   40 tasks, this run measures stock-plus-fixes and the feature contributes
   nothing — which is a reportable null result, not a failure. See §9's kill
   criterion and §7.6's counters.

Four sentences are therefore pre-registered for the conclusions, so they
cannot be dropped when the numbers arrive:

1. The comparison to 21.5 and 19.9 is **same-model but not
   same-conditions**: different judge invocation, different harness
   vintage, one run per task on both sides.
2. There was **no in-house baseline** run under our own conditions, so the
   conditions gap above is unquantified.
3. There was **no compute-matched control**, so any advantage is
   confounded with spending far more tokens.
4. The run used **researcher-planned cycle tails, a configuration with a
   single prior live cycle in which the researcher declined to plan**, and
   the guidance stack was **not** thinned (`model_profiles` is inert on this
   model). If the planned-cycle count comes back at or near zero, say so:
   the number then describes stock-plus-fixes, not agent-planned scheduling.

An in-house baseline — RCB's Claude Code preset on `claude-opus-4-6`, 40
runs, roughly $80–$200 notional — is what would close gap 1 and 2 and turn
the published rows into a cross-check rather than the primary reference.
It is out of scope by decision, recorded in §11.

---

## 3. Model: `claude-opus-4-6`

Pinned for every role at `high` effort, through
`claude -p --model claude-opus-4-6 --effort high`. Max plan, subscription
billing, nothing through the API.

### Why this is the easier model to run, as well as the comparable one

Reverting from Fable 5.1 to Opus 4.6 removes four complications, not just
the confound:

1. **Half the notional price.** $5/$25 per MTok against Fable's $10/$50,
   which halves every figure in §8.
2. **The prompt-fit worry largely goes away.** Fable 5.1 is the model that
   penalises over-prescriptive prompts, and long-exposure's four-layer
   system prompt — philosophy + framework + operating protocol + role,
   with checkpoint rules and a named anti-pattern list on every call — is
   exactly that shape. It was also developed against Opus-generation
   models, so 4.6 is the model it was tuned for. §4 item 4 keeps the
   prompt-fit read because it is nearly free, but it drops from a risk to
   a sanity check.
3. **Turn lengths are ordinary again.** Main's `cli_timeout: 0` and
   `provider_idle_timeout_seconds: 1800` need no special justification on
   this model.
4. **An earlier training cutoff is a benchmark virtue here.** Fewer of the
   hidden target papers are likely to be in weights than with a
   current-generation model, which narrows (without closing) the
   memorisation caveat in §7.3.

Effort note: Opus 4.6 supports `low`/`medium`/`high`/`max` — `xhigh`
arrived with Opus 4.7 — so `high` is valid and is what both the config and
the published rows' generation would use.

### Availability is the real risk, and the fallback is pre-decided

Opus 4.6 is two generations old. Whether Max-plan `claude -p` still serves
it is an empirical question, which §4 item 0 answers in one call. The
fallback is decided now rather than under time pressure:

| Situation | Action | Comparability cost |
|---|---|---|
| `claude-opus-4-6` serves | Run it | None — 2 same-model rows |
| 4.6 gone, `claude-opus-4-7` serves | Run 4.7 | Lose the Claude Code row (21.5); keep ResearchHarness (20.7). Down to one thin-baseline comparison, and the §1 bracket collapses |
| Neither serves | Stop and re-decide with the operator | — |

Falling back to 4.7 is a materially weaker experiment, so it is worth
confirming availability before any build work, not after.

### Constraints

- **The model string is passed verbatim** to the CLI
  (`orchestrator.py:3750`); `claude --model` accepts full names.
- **Use the exact ID, never the `opus` alias.** `model_tier: opus` and
  every `model: opus` in `agent_models` becomes `claude-opus-4-6`. The
  alias resolves to the *current* Opus, which on this branch would silently
  run a different model and destroy the entire comparability argument of
  §1. This is the single most important line in the bench config.

---

## 4. What must be built first

Already done on this branch: fan-out switch, end-of-run switches, usage
ledger with tool counts and cost capture, budget gates (unused here — see
§5), headless prompt hygiene, run-config threading. What remains:

**0. Model-availability probe** (~20 lines, half a day). One
`claude -p --model claude-opus-4-6 --effort high` call with a trivial
prompt; then the same for `claude-opus-4-7`. Record success, the served
model reported in the envelope, and latency. Run this **first** — it
decides which experiment we are running (§3).

**1. Per-task `sessions.db` isolation** (config-only, one day).
`compact_db` resolves relative to the config file
(`orchestrator.py:1662-1666`) and the MCP `search_sessions` tool is global
with no run scoping. Forty tasks against one DB would leak task A's
outputs, lemmas and lessons into task B's prompts — a contamination bug
that would invalidate every number, and one that matters more with fan-out
on, since clones share the parent's instance dir by design. Fix without a
code change: the adapter writes a per-task config carrying an **absolute**
`compact_db` under that task's instance dir. Verify per task: the DB path
in `result.json` is unique, and `search_sessions` in task B returns
nothing from task A.

**2. `long-exposure bench` subcommand** (~150 lines, three days). Inputs:
`--prompt-file`, `--workspace`, `--out-dir`. Behaviour:

- generate the per-task config and score (directive = prompt file
  contents, verbatim);
- run the loop as a subprocess — importing the module installs signal
  handlers and is one-run-per-process (`exploration.py:94-128`);
- apply the harness's own 10 h stance at the root (§5) by writing
  `long-exposure.stop` into the instance dir. `long-exposure.stop` is the
  right signal, not `long-exposure.graceful-stop`: the plain stop sets
  `_stop_requested`, which finishes the current agent, flushes a periodic
  report, and runs the enabled end-of-run stages
  (`_should_run_final_synthesis`, `exploration.py:946-965`), whereas
  graceful-stop exits at the cycle boundary for resume and does **not**
  trigger the final pipeline;
- copy the deliverable to `<workspace>/report/report.md` — RCB's expected
  path — from the newest `reports/report_cycles_*.md` (§5);
- write `result.json`: report path *and which file it came from*, artifact
  list, tokens, notional cost, tool calls, turns, wall time, cooldown
  seconds, cycles run, fan-out branches spawned, rate-limit events,
  termination reason, served model, and the commit SHA.

Encode two footguns rather than rediscover them: `report_interval: 0`
means *every cycle*, not *never* (`exploration.py:4980` tests `>=`); and
`cli_timeout: 0` means no per-call ceiling at all.

**3. RCB adapter** (~40 lines, one day). `bench/rcb_agent.sh` plus the
`agents.json` entry in §10. It also carries the retrieval denylist and
egress policy from §7.3.

**4. Smoke test and three reads** (one day). One validation task, the real
config, stopped after **six** cycles — not three, because the audit floor
is 2 and a shorter run cannot show the floor firing. Asserts:
`report/report.md` exists, is non-empty, and `result.json` records its
source file; `result.json` has non-zero cost, tool calls and turns; the
`compact_db` path is task-local; the served model is `claude-opus-4-6`; no
call hit the idle watchdog; the retrieval log is being captured; and the
resolved capability profile is `standard` (it must be — §5.5; an
`advanced` here means the family list was edited by accident).

**The served-model assertion is the one that has already caught something.**
A 2026-09-25 smoke run whose config used `model: opus` silently served
`claude-opus-5-5`. Assert the exact id against what the envelope reports,
per agent, and fail the gate on a mismatch — not just at `model` but across
every `agent_models` entry.

**Also capture the per-cycle cost here** (§8): read `usage_summary.json`
after the smoke cycles and re-derive §8's per-task range on the actual
benchmark model before the main pass. The one measured cycle available so
far was on the wrong model and suggests the current range is optimistic.

Then, read by a human from the artifacts:

- **Report shape.** The deliverable must read as clear, concise
  synthesized findings, not a process log. The periodic reporter is
  cumulative by design, so this is the check most likely to fail.
- **Cycle-plan fit** — the new read, and the one with no prior mileage
  (§2 gap 5). From the transcripts and `health_events.jsonl`: did the
  researcher emit any block at all; were the blocks well-formed
  (`cycle_plan_rejected` near zero); do the planned shapes look sensible
  against what the brief asked for; did the audit floor fire at cycle 3 as
  designed; and did the periodic reporter still flush a deliverable
  despite longer cycles. This read decides §9's planning kill criterion.
- **Prompt fit** (sanity check, not a risk on this model — see §3). One
  trim is allowed here, decided from transcripts and never from scores
  (§7.2), then frozen.

Estimate: **about one working week** before the first scored run.

---

## 5. The configuration

### Budget: main's defaults, unchanged

Every ceiling, timer and concurrency cap is whatever `main` ships. The
code is this branch (for the bug fixes and the two switches); the budget
is stock. That combination is deliberate and is disclosed as such, because
it is not a released configuration.

| Knob | Value | Source |
|---|---|---|
| `loop.max_cycles` | **`null` (unlimited)** | main's score |
| `loop.cycle_cooldown_seconds` | **400** | main's score |
| `loop.report_interval` | **3** | main's score |
| `loop.daily_sync_interval_hours` | **24** | main's score |
| `loop.min_clone_cycles_before_preempt` | 1 | main's score |
| `loop.barrier_preempt_timeout_seconds` | 3600 | main's score |
| `loop.max_cost_usd` | **absent → unlimited** | main has no such key |
| `loop.max_tool_calls` | **absent → unlimited** | main has no such key |
| `cli_timeout` | **0 (no per-call ceiling)** | main's config |
| `provider_idle_timeout_seconds` | **1800** | main's config |
| `provider_idle_poll_seconds` | 10 | main's config |
| `context_window` / `compact_threshold` | 1,000,000 / 0.90 | main's config |
| Fan-out branch cap | **3** (`FANOUT_MAX_BRANCHES`) | code constant, identical on both branches |
| Fan-out per-clone wall cap | **10 h** (`FANOUT_CAP_SECONDS`) | `fanout.py:98`, identical on both branches |
| End-of-run stage wall cap | **10 h** (`WALL_CAP_SECONDS` = 36,000 s) | `limits.py:11`, identical on both branches |

An earlier draft invented tighter values (`max_cycles: 12`,
`cli_timeout: 5400`, `max_cost_usd: 60`, `report_interval` forced to the
cycle cap). Withdrawn — they would have measured a budget-constrained
variant nobody runs.

**`max_cycles: null` means the run ends when the work does.** Termination
comes from the exhaustion detector — two consecutive cycles below 5 % of
peak observed output, floor 500 tokens
(`LOW_OUTPUT_FRACTION`/`LOW_OUTPUT_ABS_FLOOR`/`LOW_OUTPUT_CLOSURE_COUNT`,
`exploration.py:3907-3909`) — or from the auditor emitting
`[[BRANCH_COMPLETE]]`. That is the harness's actual operating point and
the thing worth characterising.

**Budget is tracked, not enforced.** With both caps absent,
`budget_exceeded()` never fires (it only triggers on a cap that parses as
a number > 0), so the usage ledger is pure observation: per-agent tokens,
cache reads/writes, tool calls, turns, wall time and notional cost, live
in `long-exposure status` and `long-exposure usage`, persisted in
`usage_summary.json`, merged from fan-out clones at collapse. Measure
everything, constrain nothing.

### The 10 h stance, inherited rather than invented

The harness already takes a position on how long one unit of work may
run: **10 hours.** It appears twice — `limits.WALL_CAP_SECONDS = 36_000`
caps an entire end-of-run synthesis pass (`auditing.py:814`,
`reporting.py:642`), and `fanout.FANOUT_CAP_SECONDS = 10*60*60` caps each
fan-out clone (`fanout.py:1729-1772`, which writes a "10h cap" merge
report and cleans up the clone's subprocess tree).

What the harness does **not** have is a wall cap on the *root cycle loop*:
grepping main's `exploration.py` for a run-level elapsed budget returns
nothing, so with `max_cycles: null` the root loop ends only on exhaustion,
`[[BRANCH_COMPLETE]]`, or an operator stop. Since both end-of-run stages
are off here, neither existing 10 h cap can bound a root run either.

So the adapter applies **the harness's own 10 h** at the root, through the
existing stop path, identical for every task. Not a new budget or a new
policy — the number the harness already uses for a clone and for a
synthesis pass, applied at the one place the code leaves open. **The
exhaustion-vs-10 h split is a reported result, not a footnote:** if a
large share of tasks hit the cap, the headline is "score after 10 h"
rather than "score at natural exhaustion", and the write-up must say so.

### The deliberate deviations from stock

| Deviation | Why |
|---|---|
| `model` / `agent_models` → `claude-opus-4-6` | The run's subject, and §1's comparability |
| `end_of_run.final_auditor: false`, `final_reporter: false` | Operator decision; see the deliverable note |
| `compact_db` → absolute, per task | Cross-task contamination (§4 item 1) |
| `working_directory` → the RCB task workspace | Required by the adapter contract |
| `loop.cycle_planning.enabled: true` | Operator decision; see §5.5 |
| `model_profiles.enabled: true` | Operator decision; see §5.5 — **inert on this model** |

Everything else — including `curator: true`, since "everything else on" —
stays as shipped. `usage_allowance` stays **off**: the budget is main's
stock, which is unlimited, and adding a cap would be a further deviation.
`startup_gate` stays **off**: the adapter drives runs non-interactively, and
a gate with no TTY and no flags exits 4 (see configuration-reference.md).

### 5.5 What "both features enabled" actually yields on Opus 4.6

This needs stating plainly, because the decision to enable both features and
the decision to run Opus 4.6 pull against each other.

**`loop.cycle_planning` is active and changes the run.** The researcher may
chain up to three workers in a cycle and may omit the auditor, bounded by an
audit floor of 2 consecutive audit-free cycles, with `[[REQUEST_AUDIT]]`
available to the worker. This is a real behavioural change from the fixed
`researcher → worker → auditor` flow and is the substance of what the run
measures beyond stock.

**`model_profiles` is inert.** The feature thins guidance only for models
listed in its `advanced` family, which ships as `[fable, astra]`. On
`claude-opus-4-6` it resolves to `standard` and sets no knobs — verified, not
assumed. So enabling it changes nothing about the assembled prompt for this
run.

That is not an accident of configuration; it is the feature working as
designed. The profiles exist because the full guidance stack was calibrated
for Opus 4.6 and 4.7 and suits them. Thinning it for the model it was tuned
for would be measuring a configuration nobody would deploy.

So this run measures: **stock harness + the bug fixes + agent-planned cycle
tails, on Opus 4.6.** It does *not* measure the thinned guidance stack. The
report must say exactly that rather than "both advanced-model features
enabled", which would imply a prompt change that did not happen.

If you want the thinned stack measured, one edit does it —
`model_profiles.families.advanced: [fable, astra, claude-opus-4-6]` — but it
changes the question from "does agent-planned scheduling help?" to "does
thinning guidance hurt a model that never needed it thinned?", and it forfeits
§1's comparability argument, which rests on Opus 4.6 running a
recognisable harness. Left off.

### 5.6 How cycle planning interacts with the rest of the plan

| Plan element | Interaction |
|---|---|
| One shot per task (below) | Unchanged. Planning shapes cycles *within* the single attempt. |
| Fan-out | Unchanged and still enabled. Fan-out already replaces worker and auditor for its cycle and takes precedence over a plan; a fan-out cycle runs no planned tail. |
| Clones | Planning is root-only (`allow_in_clones: false`, shipped default). Every branch runs the fixed flow, which is what keeps branches comparable at the merge. |
| The 10 h stance (§5.3) | Unchanged. Planning does not touch wall-clock limits. |
| Harness diagnostics (§7.6) | Gains three counters: `cycle_plan_rejected`, `cycle_plan_audit_forced`, `cycle_plan_audit_requested`, all in `health_events.jsonl`. |
| Reporting (§7.5) | Per task, record how many cycles were planned vs fixed, the flow each cycle ran, and the audit-free streak. A run where the researcher never plans is a null result for the feature and must be reported as one. |

### The deliverable, and one shot at it

**One attempt per task.** A single pass to present clear, concise
synthesized findings to the judge, then on to the next task. No repeat
runs, no second attempts, no re-rolls on a bad score. This matches the
protocol behind both published rows we compare to.

With the final reporter off, nothing else writes a report for the grader,
so the **periodic reporter is the graded artifact**. Main's
`report_interval: 3` handles this without special casing: a report flushes
every third cycle, and the stop signal flushes one too. Two consequences:

- The periodic reporter is *cumulative and process-oriented* by design,
  which is in tension with "clear and concise synthesized findings". §4
  item 4 checks the shape on a real task before anything is scored. If it
  reads as a process log, that is a reported property of running with the
  final reporter off — not something to tune away mid-campaign.
- No report means a zero, not a low score. The smoke test asserts report
  provenance because this is the most likely mechanical failure here.

### Runs

| Pass | Runs |
|---|---|
| Smoke | 1 |
| **Main** | 40 × 1 |

Forty-one runs. That is the whole campaign.

---

## 6. Cost accounting: notional, and labelled as such

The run bills against the Max plan through `claude -p`. **Nothing is
billed through the API, and no API key is used for agent calls.** Marginal
cash cost is zero; the subscription is the outlay.

The envelope's `total_cost_usd`, which the ledger records per call, is
therefore an **API-equivalent figure** — what these tokens would have cost
at list price. Every table reports it as "API-equivalent cost
(subscription-billed run)". It is the right number for comparison with
peers, whose published costs are API-priced, and the wrong number to call
spend.

Two consequences to report rather than hide:

- **No cost ceiling is in force.** With `max_cost_usd` absent, an
  expensive task runs to its own end. Monitoring is `long-exposure usage`;
  the response to an outlier is to report it, not to kill it, since a kill
  would be an undisclosed cap.
- **Max-plan rate limits are part of the experiment.** Rate-limit events
  and adaptive-cooldown time are recorded in health events; both go in
  `result.json` and in the results table.

---

## 7. What makes this honest

One arm and a shared model: the comparison is real, so the job is keeping
the number and its four condition gaps (§2) attached to each other.

### 7.1 Compute and condition disclosure

Report spend in the headline table, not an appendix: tokens in/out, calls,
cycles, wall time (raw *and* net of the 400 s/cycle cooldown, which is
dead time rather than compute), and notional cost, per task.
Score-per-notional-dollar and score-per-net-hour sit next to the raw
score — a reader comparing us to Claude Code's single call needs to see
what was spent to get there.

§1's bracket goes in the results section, not just the plan: **19.9 thin →
21.5 best agent harness, on this exact model.** A reader is entitled to
see how narrow the observed harness effect on this venue has been before
reading ours. The three pre-registered sentences from §2 go in the
conclusions.

### 7.2 Judge integrity

The judge is GPT-5.1 per the paper (`JUDGE_MODEL_NAME`), cross-family from
the agent, which avoids self-preference. Four controls:

- **Blinding.** Long-exposure reports carry harness fingerprints — "Cycle
  7", plan-of-record and ledger references, `STRUCTURE.md`, checkpoint
  residue — which cue a rubric judge toward "thorough process". Apply one
  deterministic, published neutralisation pass and **judge both the raw
  and the neutralised report**, reporting both scores. It measures how much
  of our score depends on process fingerprints rather than findings.
  Re-judging 40 reports is cheap, and a large gap would be one of the more
  interesting things this run could find — especially since the rows we
  compare to were single-call reports with no such fingerprints.
- **Verbosity.** LLM rubric judges reward length. Record report length,
  figure count and artifact count per task, and report score against
  length. Necessary context when the comparison rows produced one-call
  reports.
- **No tuning against the judge.** The §4 prompt-fit trim happens once,
  before any scored run, decided from transcripts. After the first scored
  run: no prompt, config or flow change without restarting the pass and
  saying so. Hill-climbing on rubric scores would make this an overfit,
  and with a 1.6-point band to beat, even mild overfitting would manufacture
  the entire result.
- **Drift.** Re-score a held-out sample of 10 runs at the end of the pass
  with the same judge config, and report the delta. This is also the only
  handle we have on §2's gap 1 — judge drift between the paper's grading
  run and ours — so it is doing double duty and is not optional.

### 7.3 Contamination: block the target, then own what remains

**Policy: no web retrieval of the target papers or their results.** Web
search and fetch stay enabled — the comparison rows had them — but the
specific leak is closed rather than merely measured. Implementation, in
the adapter:

1. **Build a per-task denylist** from the benchmark's own hidden target
   metadata: DOI, arXiv ID, exact title, and identifying venue/author
   strings. Confirming that this metadata is readable from the task files
   is a §4 build item; the fallback is title/DOI matching against the
   rubric text.
2. **Block at egress** where the fetch is client-side, via the container's
   HTTP proxy.
3. **Detect post hoc** for anything arriving through a server-side search
   path the proxy cannot see: log every query, retrieved URL and title,
   and match against the denylist.
4. **Disqualify and re-run once** any task where a denylisted identifier
   appears, logging both the disqualification and the replacement run.
   This is the one sanctioned exception to "one shot per task", and it is
   a contamination remedy, never a response to a low score.
5. **Report the counts**: hits blocked at egress, hits detected post hoc,
   tasks re-run.

**Memorisation is not probed.** The target papers are real and published,
so some may sit in the model's weights. With one arm there is no paired
difference for prior knowledge to cancel against, so it inflates the
absolute score directly and this design cannot detect it. Two things make
that more tolerable here than it was under Fable 5.1:

- **The comparison rows share the exposure.** Claude Code at 21.5 and
  ResearchHarness at 19.9 ran on the *same model weights*, so whatever
  prior knowledge Opus 4.6 has was equally available to them. For the
  comparison — which is the point of choosing 4.6 — memorisation is
  largely common-mode and cancels. It does not cancel for the absolute
  score.
- **An older model has an earlier cutoff**, so fewer of the hidden targets
  are likely to be in weights than with a current-generation model.

Still to be said in the results rather than implied: **absolute scores are
an upper bound on re-discovery-from-evidence, because prior knowledge of
the target literature was neither blocked nor measured.** One closed-book
call per task (40 calls, a few dollars) would turn that caveat into a
measured split and remains the cheapest available upgrade.

### 7.4 Statistics that match one arm, one shot

Our 40 task scores are a real sample over tasks, so task-level variation
is estimable. Run-to-run variation is not, and neither is the published
rows'. What that permits:

- **Report our mean ± SEM across the 40 tasks**, with the full
  distribution, and **21.5 and 19.9 as reference lines**. Count of tasks
  above 50 (the re-discovery line) and above 21.5.
- **Per-domain breakdown** across the 10 domains.
- **Rubric sub-scores** (§7.6).
- A one-sample Wilcoxon of our 40 scores against each constant is
  defensible **with the caveat stated next to it**: 21.5 and 19.9 are
  themselves single-run-per-task means carrying unmeasured noise, so
  treating them as exact constants understates the true uncertainty. The
  test is a directional aid, not a verdict.
- **No paired test against peers.** The leaderboard publishes aggregates
  only — no per-task breakdown, no downloadable results (checked) — so
  pairing on task difficulty is not available even though it would be the
  better analysis.
- Pre-register the primary metric (mean rubric score on raw reports)
  before the pass so there is no metric-shopping afterwards. Freeze this
  document's commit SHA and publish it with the results.

A final calibration point worth writing down before the run: **the band we
are trying to clear is 1.6 points wide on a 100-point scale, from one run
per task.** Any conclusion that rests on a gap of that size is fragile
regardless of how the arithmetic is presented, and the write-up should say
so plainly rather than leaning on a p-value.

### 7.5 Report every run, and every difference from stock

- **Every task reported, including failures.** A crashed or report-less run
  scores whatever the judge gives it — usually zero. No quiet exclusions.
  Any excluded task is pre-registered with a reason before the pass. With
  40 tasks, a single silently dropped zero moves the mean by roughly half
  a point — a third of the entire band in §1.
- **Retry policy, pre-registered:** two sanctioned reasons only —
  infrastructure failure (container death, network loss) and a §7.3
  contamination disqualification. At most one retry, always logged in
  `result.json` and counted in the write-up. Never retry a bad score.
- **Tool surface, disclosed.** Long-exposure brings the MCP session-search
  server, `figure`, `promise_check` and the workspace validators, where
  the published Claude Code row had the CLI's built-in tools only. This
  matters more than it looks because the rubric is multimodal — figures
  are graded. Say so plainly, and report figure counts.
- **Prompt difference vs main, disclosed.** This branch materially
  shortens the headless system prompt: interactive-only text (`/complete`,
  `/clear`, "ask the user") removed, Wolfram guidance gated off when no
  kernel is configured, MCP tools advertised only when actually launched,
  one operator's hard-coded paths replaced by the derived harness root. A
  run on this branch does not see the prompt main would send — a
  behavioural difference, not just a bug fix.
- **Which branch deltas can touch this run.** Three groups, and only the
  first changes behaviour a judge could see:
  - **Cycle planning (active).** The researcher shapes each cycle's tail.
    Report the per-task split of planned vs fixed cycles, the flow each
    cycle ran, worker-chain lengths, and how often the audit floor or a
    worker escalation forced an auditor. A task where the researcher never
    emitted a plan is a null result for the feature and is reported as one.
  - **Inert in this configuration.** `model_profiles` (resolves to
    `standard` on Opus 4.6, §5.5); the final-stage token threshold
    (20k → 100k) and the restored `_N_MAX` cap (the final auditor and
    reporter are off); the run memoir's fan-out shadow handling (no
    behavioural effect at the root). The spend limit and startup gate are
    off entirely.
  - **Bug fixes and observability.** Everything else, including the two
    switches and the usage ledger.

  Publish the diff so a reader need not take the classification on trust.

- **The run memoir is active and is a prompt difference.** `memoir.enabled`
  ships `true`, so the researcher and worker each receive a `run_memory`
  input of up to 3,000 tokens that main does not send, and the auditor is
  asked to maintain it. This is narrative memory the published Claude Code
  row did not have. Disclose it alongside the tool surface, and report the
  memoir's size trajectory per task.

### 7.6 Harness diagnostics

With one arm the descriptive record carries much of the weight, so collect
it properly. All of it is already instrumented:

- Cycles to termination per task, and the exhaustion-vs-10 h-cap split.
- Auditor verdict distribution per cycle, and score against how many
  cycles the auditor gated.
- **Cycle-planning counters**, from `health_events.jsonl`:
  `cycle_plan_rejected` (a block the parser refused — a high count means
  the guidance is not landing), `cycle_plan_audit_forced` (the floor
  overrode the plan), `cycle_plan_audit_requested` (a worker escalated).
  The last two together are the honest read on whether the researcher's
  scheduling judgement was any good: if the floor and the workers are
  supplying most of the audits, the planner is not earning its keep.
- Fan-out incidence: branches spawned per task, and score on tasks where
  fan-out fired against tasks where it did not. Observational, and
  labelled so — the researcher chose when to fan out.
- Rubric sub-scores separately. Long-exposure's reporter is an LLM
  summarising work it did not do, so if the scores concentrate in
  presentation-flavoured items rather than implementation, measurement and
  analysis, that is a finding about what the harness is actually adding —
  and directly relevant against a 21.5 row whose report was written by the
  agent that did the work.
- Termination reasons, rate-limit events, cooldown time, compaction count.
- `promise_check` green rate against score — does ledger discipline track
  research quality, or is it overhead?

---

## 8. Cost and calendar

Per-task notional cost: Opus 4.6 at $5/$25, unlimited cycles to natural
exhaustion, fan-out up to 3 branches, no cost ceiling, 10 h outer bound.
Modelling 6–15 cycles at 3–4 calls each, with a fan-out multiplier on the
cycles where it fires, gives roughly **$10–$75 per task** with a tail
bounded only by the 10 h stop — half the Fable estimate.

| Pass | Runs | Agent spend (notional) | Judge | Notes |
|---|---|---|---|---|
| Build (§4) | — | <$30 | — | ~1 week |
| Smoke | 1 | <$30 | <$5 | Gate |
| Main | 40 | $400–$3,000 | $150–$400 | |
| Re-judge (neutralised + drift) | — | — | $150–$400 | §7.2 |

Notional agent total **$430–$3,060**; judge $300–$800. The judge is the
only real-cash line and needs an OpenAI-compatible key.

### One measured cycle, and why it makes the table above look optimistic

The 2026-09-25 smoke run gives a real per-cycle figure for the first time.
**Caveat first: it ran on Opus 5.5, not Opus 4.6** — the smoke config used
the `opus` alias, which is exactly the trap §9 warns about, so the dollar
figures are not on this plan's pricing basis and must not be copied into the
table.

What does transfer is the *shape*:

| Observation | Measured | What the table above assumes |
|---|---|---|
| Calls per cycle | **3** (researcher, worker, one compaction) | 3–4 — confirmed |
| Cost per cycle | $7.81 (Opus 5.5 basis) | implies $10–$75 per task at 6–15 cycles |
| Wall clock per cycle | **990 s** with zero cooldown | not modelled per cycle |
| Worker turn | 760 s, 74,712 output tokens, 48 tool calls | — |

The wall-clock number is the one that should change expectations, because it
is pricing-independent. At 990 s per cycle plus main's 400 s cooldown —
about 23 minutes — a task that runs to the 10 h stop completes roughly **26
cycles**, not the 6–15 the table models. Whatever the per-cycle cost turns
out to be on Opus 4.6, the per-task multiplier is plausibly **2–4x** the
modelled range, and the total is bounded by the 10 h stop rather than by the
cycle count assumed here.

Two things follow, neither of which is "rewrite the numbers from one sample":

1. **Measure per-cycle cost on the actual benchmark model during the smoke
   test**, from `usage_summary.json`, and re-derive §8 from that before the
   main pass. The smoke test already has to run; it should now also produce
   this number.
2. **Decide whether the main pass needs a spend cap.** The plan currently
   keeps main's unlimited budget on the grounds that a cap is a deviation.
   If the re-derived estimate lands near the top of a 2–4x range, an
   explicit `usage_allowance` becomes the cheaper deviation to disclose —
   and it now has a live-verified kill path. Set it with the overshoot in
   mind: the smoke run's $6 cap stopped at $7.81.

**Wall clock is the binding constraint, not money.** With the 10 h
per-task stop, the pass is up to 400 hours serial — over two weeks
continuous. Parallel containers are what make it a few days, and per-task
DB isolation (§4 item 1) is the prerequisite. Decide the parallelism
factor before the main pass: it also determines whether Max-plan rate
limits become the binding constraint, which the smoke test cannot reveal.
Main's 400 s cooldown adds roughly 7 minutes of dead time per cycle — real
wall clock, zero compute — which is why §7.1 reports hours both raw and
net.

---

## 9. Risks and kill criteria

| Risk | Detection | Response |
|---|---|---|
| `claude-opus-4-6` no longer served | §4 item 0, one call | Pre-decided fallback ladder in §3; 4.7 is a weaker experiment, so decide before building |
| The `opus` alias left anywhere in the config | Smoke test asserts the served model | Exact IDs only — the alias would silently run a current model and void §1. **Observed live 2026-09-25:** a smoke config using `model: opus` served `claude-opus-5-5`, silently and with no warning. This row is not hypothetical; the assertion is load-bearing |
| No gradeable report (final reporter off) | Smoke test asserts provenance | Fix the fallback chain — highest-probability mechanical failure here |
| Report reads as a process log, not findings | §4 item 4 report-shape read | Report it as a property of this configuration; do not tune mid-campaign |
| Target paper retrieved despite the denylist | §7.3 egress block + post-hoc detection | Disqualify and re-run once; report the count |
| Target papers already in weights | Not detectable in this design | §7.3's upper-bound caveat; largely common-mode for the comparison |
| Judge drift between the paper's grading run and ours | §7.2 drift re-score | Report the delta; it is the only handle on §2's gap 1 |
| Judge rewards length or process fingerprints | §7.2 dual raw/neutralised scoring, length reporting | Report both scores; a large gap is itself a finding |
| A 1.6-point band decided by run noise | Acknowledged by design (§7.4) | Say plainly that a gap of that size from one run per task is fragile |
| Unlimited cycles hit the 10 h stop often | Exhaustion-vs-cap split per task | Re-frame the headline as "score after 10 h" |
| Max-plan rate limits throttle a parallel pass | Rate-limit events in `result.json` | Lower parallelism; report cooldown time |
| Fan-out clones cross-contaminate | §4 item 1 assertion per task | Isolation boundary is the task, not the clone |
| The model never emits a `<cycle_plan>` block | `cycle_plan_rejected` count and the per-cycle flow log | Report it as a null result for the feature. Do NOT rewrite the guidance mid-campaign — that is tuning against the benchmark |
| The model emits malformed blocks constantly | `cycle_plan_rejected` high on the smoke task | Decide before the full pass: run planning off (and say so), or fix the guidance and restart the pass. Never mid-campaign |
| Planning skips audits and the score collapses | `cycle_plan_audit_forced` / `cycle_plan_audit_requested` vs score | A finding, not a fault: report it. It is evidence the researcher's scheduling judgement is poor on this model |
| A planned worker chain starves the periodic reporter | `report_interval` is per cycle, and chaining makes cycles longer | Watch the smoke task's report count; if the deliverable never flushes, that is a kill criterion below |

Kill criteria:

- **If the smoke test cannot produce a gradeable `report/report.md` from
  the periodic reporter, the main pass does not start.** A mechanical
  failure worth 40 zeros, not a result.
- **If neither Opus 4.6 nor 4.7 is served, stop and re-decide** rather
  than substituting a current model, which would silently convert this
  back into the uncomparable experiment §1 exists to avoid.
- **If the smoke task shows cycle planning misbehaving, decide before the
  full pass, not during it.** Misbehaving means: the guidance is not
  landing (mostly rejected blocks), or a worker chain is long enough that
  the periodic reporter never flushes a gradeable deliverable. Either way
  the choice is to run with planning off and disclose it, or fix and
  restart the pass. Changing configuration mid-campaign forfeits the
  pre-registration.

---

## 10. Configuration appendix

### RCB agent registration (`evaluation/agents.json`)

```json
{
  "long_exposure": {
    "label": "Long-Exposure",
    "icon": "LE",
    "cmd": "bench/rcb_agent.sh --prompt-file <PROMPT> --workspace <WORKSPACE>"
  }
}
```

### Per-task bench config (generated by the adapter)

Only the model IDs and the two per-task paths differ from stock. Every
timer, ceiling and cap is main's.

```yaml
# EXACT model ID everywhere. The `opus` alias resolves to the CURRENT Opus
# and would void the same-model comparison in §1.
model_tier: claude-opus-4-6
model: claude-opus-4-6
agent_models:
  researcher:     { provider: claude, model: claude-opus-4-6, effort: high }
  worker:         { provider: claude, model: claude-opus-4-6, effort: high }
  auditor:        { provider: claude, model: claude-opus-4-6, effort: high }
  reporter:       { provider: claude, model: claude-opus-4-6, effort: medium }
  curator:        { provider: claude, model: claude-opus-4-6, effort: medium }

working_directory: <WORKSPACE>           # per task
compact_db: <INSTANCE_DIR>/sessions.db   # absolute, per task — isolation

# --- branch-only keys, set explicitly for the record ---
# Enabled per the operator decision, and INERT on this model: the advanced
# family ships as [fable, astra], so claude-opus-4-6 resolves to `standard`
# and no guidance knob is set. Verified, not assumed. See §5.5 — do not
# report this run as measuring thinned guidance.
model_profiles:
  enabled: true
  auto: true
  default: standard
  families:
    advanced: [fable, astra]             # deliberately NOT claude-opus-4-6
  overrides: {}

# Off: the adapter drives runs non-interactively, and a gate with no TTY and
# no flags exits 4.
startup_gate:
  enabled: false

# Off: the budget is main's stock, which is unlimited. A cap here would be a
# further deviation, and the ledger already records spend without gating.
usage_allowance:
  enabled: false

# --- everything below is main's default, reproduced for the record ---
cli_timeout: 0
provider_idle_timeout_seconds: 1800
provider_idle_poll_seconds: 10
context_window: 1000000
compact_threshold: 0.90
wolfram_path: ""                         # stock; no kernel in the container
```

The reporter stays at main's `medium` effort. It writes the graded
deliverable here, which is an argument for raising it — and exactly the
kind of tuning that would stop this being a stock-budget run, so it is
left alone and noted instead.

### Score `loop` block (main's values verbatim)

```yaml
loop:
  max_cycles: null               # unlimited — run to exhaustion
  cycle_cooldown_seconds: 400
  report_interval: 3             # flushes the graded report
  daily_sync_interval_hours: 24
  min_clone_cycles_before_preempt: 1
  barrier_preempt_timeout_seconds: 3600
  # No max_cost_usd / max_tool_calls: main has no such keys, so the ledger
  # tracks and never gates.
  fanout_enabled: true           # branch-only key; true == main's behaviour
  # Branch-only. The substance of what this run measures beyond stock:
  # the researcher may shape the rest of each cycle. Bounds are the shipped
  # defaults; see configuration-reference.md and §5.5/§5.6.
  cycle_planning:
    enabled: true
    max_worker_chain: 3
    max_turns_per_cycle: 4
    audit_floor_cycles: 2
    allow_in_clones: false       # root only — keeps branches comparable
    worker_may_request_audit: true
  end_of_run:                    # branch-only key
    enabled: true
    final_auditor: false         # operator decision
    final_reporter: false        # operator decision
    curator: true

flow: [researcher, worker, auditor]
```

The root 10 h stop is not a config key — it is the adapter writing
`long-exposure.stop`, using the harness's own `FANOUT_CAP_SECONDS` /
`WALL_CAP_SECONDS` value (§5).

### Environment

```bash
# Judge only — the one real-cash API key in the campaign
JUDGE_MODEL_NAME=gpt-5.1
JUDGE_API_BASE=https://api.openai.com/v1
JUDGE_API_KEY=...

# Agent calls use the Max plan via `claude -p`. No ANTHROPIC_API_KEY.
```

### Deliverable paths

| Artifact | Path |
|---|---|
| Graded report | `<WORKSPACE>/report/report.md` (RCB's contract) |
| Its source | newest `<WORKSPACE>/reports/report_cycles_*.md` |
| Run record | `<OUT_DIR>/result.json` |
| Usage ledger | `<INSTANCE_DIR>/output/usage_summary.json` |
| Telemetry | `<INSTANCE_DIR>/telemetry/events.jsonl` |
| Health events | `<INSTANCE_DIR>/health_events.jsonl` |
| Retrieval log | `<OUT_DIR>/retrieval.jsonl` (§7.3) |
| Judge output | `<WORKSPACE>/_score.json` |

---

## 11. Decisions taken

| Decision | Choice | Consequence carried in this plan |
|---|---|---|
| Model | **`claude-opus-4-6`**, `high` effort | Chosen for comparability: it is the only model with **two** published same-model harness rows — Claude Code 21.5 and ResearchHarness 19.9 — which brackets the observed harness effect at 1.6 points on our exact model. Opus 4.7 would give one thin-baseline row only. Fallback ladder in §3 |
| Arms | **One.** Long-exposure only; no baseline, no ablation grid | Model is controlled, conditions are not: §2's four gaps and three pre-registered sentences |
| Billing | Max plan via `claude -p`; no API key for agent calls | Cost is notional/API-equivalent and labelled as such (§6); the judge key is the only cash line |
| Budget | Every ceiling, timer and cap as `main` ships them | Unlimited cycles, no cost cap; the ledger tracks without gating |
| Outer bound | The harness's own 10 h, applied at the root | No new number invented; the root loop is the one place main leaves uncapped, and the exhaustion-vs-cap split is reported |
| Configuration | All features on except the final auditor and final reporter; full 40-task scope | The periodic reporter is the deliverable; mechanism evidence is the §7.6 diagnostics |
| Advanced-model features | **Both enabled.** `loop.cycle_planning` is active; `model_profiles` is enabled but **inert on Opus 4.6** | This run measures stock + the fixes + agent-planned cycle tails, NOT thinned guidance. §5.5 says why, and says what one edit would change it |
| Spend limit | **Off** | The budget is main's stock, which is unlimited; a cap would be a further deviation |
| Startup gate | **Off** | The adapter is non-interactive; the appendix records the configuration instead of `gate_answers.json` |
| Attempts | One shot per task, then move on | Mean ± SEM over tasks with reference lines; no paired peer test (§7.4) |
| Web access | Enabled, but no retrieval of the target papers or their results | Per-task denylist, egress block, post-hoc detection, disqualify-and-re-run |
| Memorisation | Not probed | Largely common-mode for the comparison since the peer rows share the weights; absolute scores still reported as an upper bound (§7.3) |
| Compute-matched control | Not run | Folded into §2's limitation sentences |

The two cheapest upgrades, if scope reopens, ranked by what they buy:

1. **An in-house baseline** — RCB's Claude Code preset on
   `claude-opus-4-6`, 40 runs, ~$80–$200 notional. Closes §2's condition
   gaps (same judge invocation, same CLI vintage, same task revision) and
   demotes the published rows to a cross-check.
2. **A closed-book memorisation probe** — 40 single calls, a few dollars.
   Turns §7.3's upper-bound caveat into a measured split.
