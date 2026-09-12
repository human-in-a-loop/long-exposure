# ResearchClawBench: execution plan for the bench-mode branch

Scope: one venue, one model family, one question. This is the runnable
plan for `claude/long-exposure-benchmarking-pikxm2` after the bench-mode
work landed (fan-out switch, end-of-run switches, usage ledger, budget
gates). The wider survey and the rejected venues stay in
`docs/benchmarking-plan.md`; this document supersedes its §5.2 with
concrete commands, arms, budgets and gates.

Status of this document: **pre-registration draft.** Nothing here has been
run. Numbers quoted for peers are from the published paper and repository,
not from our runs.

---

## 1. The venue, and why only this one

**ResearchClawBench** (InternScience, arXiv 2606.07591, MIT licence;
[repo](https://github.com/InternScience/ResearchClawBench),
[data](https://huggingface.co/datasets/InternScience/ResearchClawBench)).
40 tasks across 10 scientific domains. Each task is grounded in a real
published paper, ships `data/` and `related_work/`, hides the target
paper, and asks the agent to produce `report/report.md` plus code and
figures. An expert-curated multimodal rubric is scored by an LLM judge on
a 100-point scale where **50 = reference-level evidence (the target paper
re-discovered)** and >50 implies discovery beyond it.

It is the only venue on the shortlist that satisfies every selection
criterion at once:

| Criterion | ResearchClawBench |
|---|---|
| Unit of comparison is the *harness* | Yes — the published table is harness × model, seven autonomous agents under one protocol |
| Task shape matches a long-exposure directive | Yes — open-ended objective, supplied corpus, hours of work, report + code + figures graded by rubric |
| Public tasks, public grader, documented integration | Yes — `evaluation/agents.json` + judge configured by env |
| Contamination controllable | Partly — target papers are hidden but published; mitigations in §7 |
| Affordable | Yes — CPU only, no GPU, no training runs |

### The peer table we are joining

From the paper (280 runs = seven agents × 40 tasks, one run each):

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
| *ResearchHarness* (thin baseline) | Claude-Opus-4.7 | 20.7 |

Every system is far below 50. The headroom is the point: this is a
benchmark where a harness can still matter, unlike the saturated
coding boards.

Two facts shape everything below. First, **the best published harness
entry is Claude Code on Opus 4.6** — the direct comparison target, and a
model two generations old. Second, **the paper's protocol is one run per
task**, so the published numbers carry no variance estimate; the
leaderboard added a Pass@5 view later. Our protocol must therefore supply
its own error bars (§6) and cannot treat a single-run difference against
21.5 as a result.

---

## 2. The claim under test

> Long-exposure's deterministic researcher → worker → auditor cycle, with
> its durable plan/ledger surface, produces higher-rubric-score research
> artifacts than a single-agent harness given the same model and the same
> budget.

Three comparisons, in order of importance:

1. **Paired, on-model (the real result).** Long-exposure vs Claude Code
   `/goal`, both on the pinned model, same tools, same wall and dollar
   budget, same tasks, same seeds. This is the only comparison that
   isolates the harness, and it does not depend on the published board at
   all.
2. **Internal ablations (why it works, if it does).** Which mechanism
   earns its cost: cycles, the auditor gate, fan-out, the end-of-run
   pipeline. These are the arms the switch work exists to enable.
3. **Board tie-back (external credibility).** A small arm on the board's
   own model so our Claude Code baseline can be checked against the
   published 21.5. Without it, a reader cannot tell whether our A0 is a
   faithful reproduction or a mis-configured strawman.

Comparison 3 is a *calibration*, not the headline. If our Opus-4.6 Claude
Code arm lands far from 21.5 on the same tasks, the reproduction is wrong
and everything else is suspect — that is exactly what it is for.

---

## 3. Model pinning

### The decision

- **Primary model, every arm, every role: `claude-opus-4-8`** at `high`
  effort, via `claude -p --model claude-opus-4-8 --effort high`.
- **Calibration model: `claude-opus-4-6`**, A0 and A2 only, on the
  10-task validation subset, to tie our baseline to the published 21.5.
- **One cost point: `claude-opus-4-8` at `medium`** effort, A2 only, to
  give a score-per-dollar curve rather than a single point.

### Why Opus 4.8

| Candidate | Input/output per MTok | Argument for | Argument against |
|---|---|---|---|
| `claude-opus-4-8` | $5 / $25 | Current-generation Opus workhorse; one generation above the board's best entry, so results read against it cleanly; identical price to Opus 5, so no cost penalty; `--effort` supported across the range | Not the newest Opus |
| `claude-opus-5` | $5 / $25 | Newest and strongest Opus tier, same price; thinking on by default | Two generations above the board; the further the model is from the peer set, the less the published table tells a reader |
| `claude-opus-4-6` | $5 / $25 | Exact match to the Claude Code entry — a directly comparable board row | Two generations old; a harness result on a stale model ages badly and says little about today's deployments |
| `claude-fable-5-1` | $10 / $50 | Most capable model available | 2× the price, a different API surface (thinking always on, forced tool use rejected, refusal fallbacks), and no peer within reach — it would measure the model, not the harness |

The case for 4.8 over Opus 5 is about interpretability rather than
capability. A reader of the board has calibration for Opus 4.6 and 4.7
numbers; 4.8 is one step from that and needs no extra argument. Opus 5
would also be defensible and is a one-word config change (§11) — it is
the first question in §12 for that reason.

The case against 4.6-only is that a harness comparison on a two-generation-
old model answers a question nobody will ask in six months, and harness
scaffolding tends to help *more* on weaker models — so a 4.6-only result
would flatter long-exposure in a way that will not replicate.

### Constraints this creates

1. **The model string is passed verbatim** to the CLI
   (`orchestrator.py:3750`), and `claude --model` accepts full names as
   well as aliases. So pinning is a config edit, not code.
2. **Availability must be preflighted.** Whether the installed CLI on the
   Max plan still serves `claude-opus-4-6` is an empirical question, not a
   documented one. §5 item 0 is a one-call probe per candidate model; if
   4.6 is gone, the calibration arm degrades to 4.7 and, failing that, is
   dropped with that stated in the write-up.
3. **`model_tier: opus` and the `model: opus` aliases in `agent_models`
   must be replaced with the exact ID** in the bench config, or the run
   silently follows whatever "opus" resolves to that week — which would
   make the whole exercise unreproducible. This is the single most
   important line in the bench config.
4. **Cost is notional.** The run bills against the Max plan through
   `claude -p`, so marginal dollars are zero and the envelope's
   `total_cost_usd` is an API-equivalent figure. We report it as such,
   labelled "API-equivalent cost (subscription-billed run)". Peers' costs
   are API-priced, so the comparison is meaningful; pretending it is cash
   spend would not be. Wall-clock parity (§6) is the budget control that
   actually binds.

---

## 4. Peer parity: what A0 must be

The published entry is Claude Code invoked by RCB's own preset — one
`claude -p` call per task with the unified persona prompt from
`evaluation/instructions_tmpl.py`, the CLI's own tools, and no external
scaffolding. Our A0 reruns *that*, on the pinned model, from RCB's own
`agents.json` entry. We do not write our own baseline: using the
benchmark's preset is what makes A0 checkable against 21.5.

Long-exposure arms get the **same prompt text** (RCB injects it; our
adapter passes `<PROMPT>` through as the score directive verbatim) and the
same tool surface. Any difference beyond the harness is a confound to be
removed, not a design choice.

---

## 5. What must be built first

Phase-0 work that is already done (on this branch): fan-out switch,
end-of-run switches, usage ledger with tool counts and dollar capture,
budget gates, headless prompt hygiene, run-config threading. What remains
is the adapter surface and per-task isolation. Each item is small and
independently testable.

**0. Model-availability probe** (~20 lines, half a day). For each
candidate ID, one `claude -p --model <id> --effort high` call with a
trivial prompt; record success, the served model reported in the
envelope, and latency. Output: a table that decides §3's calibration arm.
Run this before anything else — it can invalidate the model plan.

**1. Per-task `sessions.db` isolation** (config-only, one day). `compact_db`
resolves relative to the config file (`orchestrator.py:1662-1666`) and the
MCP `search_sessions` tool is global with no run scoping. Forty tasks
against one DB would leak task A's outputs, lemmas and lessons into task
B's prompts — a contamination bug that would invalidate every number.
Fix without code change: the adapter writes a per-task config carrying an
**absolute** `compact_db` under the task's instance dir. Verify by
asserting the DB path in `result.json` is unique per run and that
`search_sessions` in task B returns nothing from task A.

**2. `long-exposure bench` subcommand** (~150 lines, three days). Inputs:
`--prompt-file`, `--workspace`, `--out-dir`, `--wall-budget`,
`--cost-budget`, `--max-cycles`, `--arm`. Behaviour:
   - generate the per-task config and score (directive = prompt file
     contents, verbatim);
   - run the loop as a subprocess (module import installs signal handlers
     and is one-run-per-process, `exploration.py:94-128`);
   - at `wall_budget − reserve`, write `long-exposure.stop` into the
     instance dir. A stop is a *graceful* end: the current agent finishes,
     a periodic report flushes, and the end-of-run pipeline runs when
     enabled (`_should_run_final_synthesis`, `exploration.py:946-965`).
     Reserve 25 min for arms with the pipeline off, 90 min with it on;
   - copy the deliverable to `<workspace>/report/report.md` — RCB's
     expected path — preferring `reports/final/final_report.md`, falling
     back to the newest `reports/report_cycles_*.md`;
   - write `result.json`: report path, artifact list, tokens, notional
     dollars, tool calls, turns, wall time, cycles run, termination
     reason, model ID actually served, arm label, commit SHA.

   Two footguns to encode rather than discover: `report_interval: 0` means
   *every cycle*, not *never* (`exploration.py:4980` tests `>=`), so a
   pipeline-off arm sets `report_interval: <max_cycles>` to get exactly
   one flush at the end plus one on stop; and `cli_timeout: 0` is the
   default, so the per-agent timeout must be set explicitly or only the
   1800 s idle watchdog protects a wedged agent.

**3. RCB adapter** (~40 lines, one day). `bench/rcb_agent.sh` plus the
`evaluation/agents.json` entry in §11. Thin by design: RCB owns the task
loop, we own one task.

**4. Smoke test** (one day). One validation task, `max_cycles: 1`,
`cycle_cooldown_seconds: 0`, pipeline off. Asserts `report/report.md` is
non-empty, `result.json` has non-zero cost and tool calls, the DB path is
task-local, and the served model equals the pinned model. This is the
gate for spending anything.

Estimate: **about one working week** before the first scored run.

---

## 6. Arms and protocol

### Arms

| Arm | Harness | Cycles | Fan-out | End-of-run | Flow | Purpose |
|---|---|---|---|---|---|---|
| **A0** | Claude Code preset | — | — | — | — | Peer-parity baseline (the comparison) |
| **A1** | long-exposure | 3 | off | off | r→w→a | Does the cycle help at all |
| **A2** | long-exposure | 6 | off | off | r→w→a | Primary long-exposure arm |
| **A3** | long-exposure | 6 | **on** (cap 3) | off | r→w→a | Fan-out's contribution |
| **A4** | long-exposure | 6 | off | **on** | r→w→a | End-of-run pipeline's contribution |
| **A5** | long-exposure | 6 | off | off | **r→w** | Auditor gate's contribution |
| **A2-med** | long-exposure | 6 | off | off | r→w→a | Cost point at `medium` effort |
| **A0-cal / A2-cal** | both | — / 6 | off | off | r→w→a | Board tie-back on `claude-opus-4-6` |

A3 runs on a single account with fan-out sharing the workspace — the pool
is deliberately not used (see `docs/benchmarking-plan.md` §3.4). A5 is the
cleanest test of the harness's central design claim, since the auditor
gate is what distinguishes it from a plan-and-execute loop.

### Staging gates

Money is spent in three tranches, each gated on the previous.

- **Gate 1 — smoke.** 1 task, 1 seed, A2 only. Cost: under $20. Passes
  when §5 item 4's assertions hold.
- **Gate 2 — validation subset.** 10 tasks × 3 seeds × {A0, A1, A2, A4} =
  120 runs. Passes when (a) A0 is within a defensible margin of the
  published Claude Code behaviour, (b) A2 − A0 has a consistent sign, and
  (c) per-run cost and wall time match the budget model. **If A2 − A0 is
  negative or indistinguishable from zero at this scale, stop and publish
  that** — a well-instrumented null result on 120 paired runs is a real
  finding and costs a tenth of the full matrix.
- **Gate 3 — full matrix.** 40 tasks × 3 seeds × {A0, A1, A2, A3, A4, A5}
  = 720 runs, plus A2-med (120) and the calibration pair (60).

### Budgets, per task per run

| Control | Value | Why |
|---|---|---|
| Wall budget | 6 h | Long enough for 6 cycles; the binding budget control |
| `cli_timeout` (per agent call) | 3600 s | Matches the one documented peer timeout (OpenClaw) |
| `loop.max_cost_usd` | $25 | A backstop, not the intended stop. Peers on the cost-reporting venue spend $3.50–$15 |
| `loop.max_tool_calls` | unset | Wall and cycles bind first |
| `cycle_cooldown_seconds` | 0 | Cooldown is an operator comfort, not a benchmark condition |

Equal-wall-clock is the primary parity condition; equal-notional-dollar is
reported alongside. A0 gets the same 6 h — a single `claude -p` call will
not use it, and that asymmetry is itself a finding to report rather than
hide.

### Seeds and statistics

Three seeds per arm per task. Report mean ± SEM and a **paired bootstrap
over per-task differences** A_i − A0 (10,000 resamples, task as the
resampling unit), which is the right test because the same 40 tasks appear
in every arm and per-task rubric difficulty dominates the variance. Report
the per-task difference distribution, not just the mean: a harness that
wins big on 8 tasks and loses on 30 is a different finding from one that
gains 2 points everywhere.

---

## 7. Contamination controls

The target papers are hidden by the benchmark but are real published
papers, and peers ran with web search enabled — so disabling it would
break parity while enabling it risks an agent simply retrieving the
answer.

Controls, all of which are cheap:

1. **Log every search and fetch.** Telemetry already records tool calls;
   the adapter additionally captures query strings and fetched URLs from
   the session transcript.
2. **Report the retrieval rate.** For each task, whether a URL or title
   matching the hidden target paper appeared in any run. This number goes
   in the results table next to the score. A result that only holds on
   tasks where the target was retrieved is not a result.
3. **A no-web arm on a 10-task subset** (A2 with WebSearch removed from
   `allowed_tools`) to bound the effect size.
4. **Scored-run freeze.** Pin the commit SHA, the RCB task set revision,
   the judge model, and both model IDs before Gate 2; record all of them
   in every `result.json`. No config edits mid-tranche.

---

## 8. Metrics

Primary:

- Rubric score per task (RCB's own `_score.json`), mean ± SEM per arm.
- Paired bootstrap of A_i − A0.

Cost and efficiency:

- Score per notional dollar, and score per wall-clock hour.
- Tokens in/out, cache read/write, tool calls, turns, cycles — all
  already in `usage_summary.json` per agent.

Harness-specific diagnostics (these are why we built the ledger, and they
are what a reader learns from even if the headline is null):

- Auditor decision distribution per cycle (CONTINUE / revise / complete),
  and score conditional on how many cycles the auditor gated.
- Termination-reason distribution: exhaustion vs cycle cap vs wall stop vs
  budget gate vs failure.
- `promise_check` green rate — does the ledger discipline correlate with
  rubric score, or is it overhead?
- Stage counts actually used by the final auditor and reporter (the N
  heuristic), to see whether the `_N_MAX` cap binds in practice.

Judge configuration must match the paper for comparability:
`JUDGE_MODEL_NAME=gpt-5.1` with the repo's default rubric mode selection.
This needs an OpenAI-compatible key and is a hard prerequisite — a
different judge produces numbers that cannot be placed next to 21.5.

---

## 9. Cost and calendar

| Tranche | Runs | Agent spend (notional, API-equivalent) | Judge spend | Calendar |
|---|---|---|---|---|
| Build (§5) | — | <$50 | — | 1 week |
| Gate 1 smoke | 1 | <$20 | <$5 | 1 day |
| Gate 2 validation | 120 | $600–$1,800 | $50–$150 | 1 week |
| Gate 3 full matrix | 900 | $4,500–$13,500 | $400–$1,200 | 2–3 weeks |

Notional agent spend assumes $5–$15 per long-exposure run at 6 cycles and
under $2 for an A0 run. Actual cash outlay against the Max plan is the
subscription, not these figures; they are the comparability numbers.
Wall-clock, not money, is the real constraint: 900 runs × up to 6 h needs
either serialised weeks or parallel containers, which is the reason for
per-task DB isolation in §5 item 1.

---

## 10. Risks and kill criteria

| Risk | Detection | Response |
|---|---|---|
| `claude-opus-4-6` no longer served | §5 item 0 probe | Calibration arm moves to 4.7, or is dropped and said so |
| Our A0 does not reproduce Claude Code's published behaviour | Gate 2 | Fix the baseline before believing any A_i − A0; this is the gate's main job |
| Rubric variance swamps the effect | Gate 2 SEM vs observed A2 − A0 | Raise seeds on a narrower arm set, or report the null |
| Target-paper retrieval drives scores | §7 retrieval rate | Report conditional results; lean on the no-web subset |
| Long-exposure wins only by spending more | Equal-dollar comparison | Report both parities; a win that exists only at 10× cost is reported as such |
| Judge drift over weeks of runs | Re-score a held-out 10-run sample at the end of Gate 3 | Report score delta; re-score all if it exceeds noise |
| Wall-clock, not money, becomes the limit | Runs-per-day tracking in Gate 2 | Cut arms rather than seeds — seeds are what make the claim |

Explicit kill criterion: **if Gate 2 shows A2 − A0 ≤ 0, the full matrix is
not funded.** The deliverable becomes a paired null result on 120 runs
with the ablation diagnostics, which is both honest and useful.

---

## 11. Configuration appendix

### RCB agent registration (`evaluation/agents.json`)

```json
{
  "long_exposure_a2": {
    "label": "Long-Exposure (6 cycles)",
    "icon": "LE",
    "cmd": "bench/rcb_agent.sh --prompt-file <PROMPT> --workspace <WORKSPACE> --arm a2"
  },
  "claude_code_pinned": {
    "label": "Claude Code (pinned model)",
    "icon": "CC",
    "cmd": "claude -p \"$(cat '<PROMPT>')\" --model claude-opus-4-8 --effort high --output-format json"
  }
}
```

### Per-task bench config (generated by the adapter)

```yaml
# EXACT model IDs, never the `opus` alias — the alias makes the run
# unreproducible the next time it is repointed.
model_tier: claude-opus-4-8
model: claude-opus-4-8
agent_models:
  researcher:     { provider: claude, model: claude-opus-4-8, effort: high }
  worker:         { provider: claude, model: claude-opus-4-8, effort: high }
  auditor:        { provider: claude, model: claude-opus-4-8, effort: high }
  reporter:       { provider: claude, model: claude-opus-4-8, effort: medium }
  final_auditor:  { provider: claude, model: claude-opus-4-8, effort: high }
  final_reporter: { provider: claude, model: claude-opus-4-8, effort: medium }
  curator:        { provider: claude, model: claude-opus-4-8, effort: medium }

working_directory: <WORKSPACE>
compact_db: <INSTANCE_DIR>/sessions.db     # absolute; per-task isolation
cli_timeout: 3600
context_window: 1000000
compact_threshold: 0.90
wolfram_path: ""                            # no kernel in the container
test_runner: ""
```

### Per-arm score `loop` block

```yaml
# A2 (primary): 6 cycles, no fan-out, no end-of-run pipeline.
loop:
  max_cycles: 6
  cycle_cooldown_seconds: 0
  report_interval: 6          # exactly one flush at the end (0 means EVERY cycle)
  daily_sync_interval_hours: 0
  fanout_enabled: false
  end_of_run: false
  max_cost_usd: 25
  max_tool_calls: null

# A3: fan-out on.        fanout_enabled: true
# A4: pipeline on.       end_of_run: {enabled: true, final_auditor: true,
#                                     final_reporter: true, curator: false}
# A5: auditor removed.   flow: [researcher, worker]
```

`curator: false` in A4 because the ZIP package is not graded and costs a
call; the auditor and reporter are the stages under test.

### Environment

```bash
# Judge — must match the paper for comparability
JUDGE_MODEL_NAME=gpt-5.1
JUDGE_API_BASE=https://api.openai.com/v1
JUDGE_API_KEY=...

# One-launch overrides, for sanity checks without editing a score
LONG_EXPOSURE_FANOUT=0
LONG_EXPOSURE_END_OF_RUN=0
```

### Deliverable paths

| Artifact | Path |
|---|---|
| Graded report | `<WORKSPACE>/report/report.md` (RCB's contract) |
| Our run record | `<OUT_DIR>/result.json` |
| Usage ledger | `<INSTANCE_DIR>/output/usage_summary.json` |
| Telemetry | `<INSTANCE_DIR>/telemetry/events.jsonl` |
| Judge output | `<WORKSPACE>/_score.json` |

---

## 12. Open decisions

Four choices change the shape of the run rather than its details: the
pinned model, how much to spend before deciding, which ablation arms to
fund, and how hard to push contamination control. They are put to the
operator before Gate 1.
