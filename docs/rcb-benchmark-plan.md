# ResearchClawBench: execution plan for the bench-mode branch

Scope: one venue, one model, one harness configuration. This is the
runnable plan for `claude/long-exposure-benchmarking-pikxm2` after the
bench-mode work landed (fan-out switch, end-of-run switches, usage ledger,
budget gates). The wider survey and the rejected venues stay in
`docs/benchmarking-plan.md`; this document supersedes its §5.2.

Status: **pre-registration.** Nothing here has been run. Peer numbers are
from the published paper and repository, not from our runs. The four
operator decisions in §12 are settled and reflected throughout.

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
| Contamination controllable | Partly — target papers are hidden but published; controls in §7 |
| Affordable | Yes — CPU only, no GPU, no training runs |

### The peer table

From the paper (280 runs = seven agents × 40 tasks, one run each):

| Agent (harness) | Model | Avg score |
|---|---|---|
| **Claude Code** | Claude-Opus-4.6 | **21.5** |
| EvoScientist v0.1.1 | GPT-5.4 | 18.8 |
| Codex CLI | GPT-5.4 | 18.4 |
| OpenClaw | GPT-5.4 | 16.6 |
| ResearchClaw | GPT-5.4 | 16.3 |
| EvoScientist v0.0.4 | GPT-5.4 | 15.5 |
| ARIS Codex | Codex/GPT-5.4 | 13.6 |
| Nanobot | GPT-5.4 | 12.8 |
| *ResearchHarness* (thin baseline) | Claude-Opus-4.7 | 20.7 |

Every system is far below 50. The headroom is the point: this is a
benchmark where a harness can still matter, unlike the saturated coding
boards.

Two facts from that table shape the protocol. The best published harness
entry is **Claude Code on Opus 4.6**, and the paper's protocol is **one
run per task**, so the published numbers carry no variance estimate.

---

## 2. The claim under test

> Long-exposure's deterministic researcher → worker → auditor cycle, with
> fan-out and its durable plan/ledger surface, produces higher-rubric-score
> research artifacts than a single-agent harness given the same model and
> the same wall budget.

Two arms, and they are not a feature matrix:

- **B — long-exposure**, one configuration, everything on except the final
  auditor and final reporter (§6). All 40 tasks.
- **A — Claude Code**, RCB's own preset, same model, same tasks, same wall
  budget.

There is no ablation grid. The cost of that choice, stated plainly: if B
beats A we will know the harness as a whole wins, but not which of
cycles / auditor gate / fan-out earned it. The switches remain in the
code, so ablations stay available as a follow-up on whichever subset looks
most informative after the first pass.

---

## 3. Model: `claude-fable-5-1`

Pinned for **both** arms, every role, at `high` effort.

```
claude -p --model claude-fable-5-1 --effort high ...
```

### What this buys and what it costs

Fable 5.1 is Anthropic's most capable widely released model — the right
choice if the question is "what is the best research this harness can
produce". The consequences are real and are accepted deliberately:

1. **No board tie-back.** Every published peer runs Opus 4.6/4.7 or
   GPT-5.4. A Fable 5.1 number cannot be placed next to 21.5 and called a
   comparison. **This makes arm A load-bearing rather than optional** — it
   is the only same-model reference point the campaign will have. Without
   it there is no harness result, just a score.
2. **2× the token price.** $10/$50 per MTok against Opus-tier $5/$25.
   Combined with fan-out this roughly quadruples the per-task figure used
   in §9's budget model.
3. **Different model behaviour, in a direction that matters here.**
   Thinking is always on and cannot be disabled; effort spans `low`–`max`.
   More important for this harness: prompts written for earlier models are
   often *too prescriptive* for Fable 5.1 and reduce output quality.
   Long-exposure's four-layer system prompt is exactly that — philosophy +
   framework + operating protocol + role, with checkpoint-block rules and
   a named anti-pattern list on every call. The smoke test (§5 item 4)
   therefore includes a prompt-fit check, and a prescriptiveness trim is
   the first thing to try if output quality looks low.
4. **Longer single turns.** Hard tasks can run many minutes per request.
   Two timeouts must be raised from their defaults or runs will be killed
   mid-turn: `cli_timeout` (default `0` = unlimited, but we set a real
   value) and `provider_idle_timeout_seconds` (default 1800). The idle
   watchdog checks process-tree CPU as well as file progress
   (`orchestrator.py:3163-3182`), so a streaming turn is not spuriously
   killed — but the hard per-call timeout is the binding limit. §6 sets
   both.
5. **Data retention.** Fable 5.1 is unavailable to zero-data-retention
   organisations unless expressly authorised. A ZDR account gets a 400 on
   every call, which item 0 of §5 will catch immediately.

### Constraints this creates

- **The model string is passed verbatim** to the CLI
  (`orchestrator.py:3750`), and `claude --model` accepts full names as well
  as aliases (`fable` is a documented alias). Pinning is a config edit.
- **Use the exact ID, never the alias.** `model_tier: opus` and the
  `model: opus` entries in `agent_models` must all become
  `claude-fable-5-1`. An alias resolves to whatever is current that week,
  which would make the run unreproducible — this is the single most
  important line in the bench config.
- **Cost is notional.** The run bills against the Max plan through
  `claude -p`, so marginal dollars are zero and the envelope's
  `total_cost_usd` is an API-equivalent figure. We report it labelled
  "API-equivalent cost (subscription-billed run)". Peers' costs are
  API-priced, so the figure is comparable; calling it cash spend would not
  be. Wall-clock parity is the budget control that actually binds.

---

## 4. Peer parity: what arm A must be

The published entry is Claude Code invoked by RCB's own preset — one
`claude -p` call per task with the unified persona prompt from
`evaluation/instructions_tmpl.py`, the CLI's own tools, no external
scaffolding. Arm A reruns *that*, on Fable 5.1, from RCB's own
`agents.json` entry. We do not write our own baseline: using the
benchmark's preset is what makes A honest.

Arm B gets the **same prompt text** — the adapter passes `<PROMPT>`
through as the score directive verbatim — and the same tool surface. Any
difference beyond the harness is a confound to remove, not a design
choice.

---

## 5. What must be built first

Already done on this branch: fan-out switch, end-of-run switches, usage
ledger with tool counts and dollar capture, budget gates, headless prompt
hygiene, run-config threading. What remains is the adapter surface and
per-task isolation.

**0. Model-availability and retention probe** (~20 lines, half a day).
One `claude -p --model claude-fable-5-1 --effort high` call with a trivial
prompt. Record: success, the served model in the envelope, latency, and
that no retention error comes back. Run this first — it can invalidate the
whole model plan in one call.

**1. Per-task `sessions.db` isolation** (config-only, one day).
`compact_db` resolves relative to the config file
(`orchestrator.py:1662-1666`) and the MCP `search_sessions` tool is global
with no run scoping. Forty tasks against one DB would leak task A's
outputs, lemmas and lessons into task B's prompts — a contamination bug
that would invalidate every number, and one that matters *more* with
fan-out on, since clones share the parent's DB by design. Fix without a
code change: the adapter writes a per-task config carrying an **absolute**
`compact_db` under that task's instance dir. Verify by asserting the DB
path in `result.json` is unique per run, and that `search_sessions` in
task B returns nothing from task A.

**2. `long-exposure bench` subcommand** (~150 lines, three days). Inputs:
`--prompt-file`, `--workspace`, `--out-dir`, `--wall-budget`,
`--cost-budget`, `--max-cycles`, `--no-web`. Behaviour:

- generate the per-task config and score (directive = prompt file
  contents, verbatim);
- run the loop as a subprocess — importing the module installs signal
  handlers and is one-run-per-process (`exploration.py:94-128`);
- at `wall_budget − reserve`, write `long-exposure.stop` into the instance
  dir. A stop is a *graceful* end: the current agent finishes, a periodic
  report flushes, and the enabled end-of-run stages run
  (`_should_run_final_synthesis`, `exploration.py:946-965`). Reserve
  45 min, which must cover one periodic-reporter call on Fable 5.1 plus
  the curator;
- copy the deliverable to `<workspace>/report/report.md` — RCB's expected
  path. **With the final reporter off, the newest
  `reports/report_cycles_*.md` IS the deliverable** (see §6);
- write `result.json`: report path and provenance (which file it came
  from), artifact list, tokens, notional dollars, tool calls, turns, wall
  time, cycles run, fan-out branches spawned, termination reason, the
  model actually served, and the commit SHA.

Two footguns to encode rather than rediscover: `report_interval: 0` means
*every cycle*, not *never* (`exploration.py:4980` tests `>=`); and
`cli_timeout: 0` is the default, meaning no per-call ceiling at all.

**3. RCB adapter** (~40 lines, one day). `bench/rcb_agent.sh` plus the
`evaluation/agents.json` entries in §11. Thin by design: RCB owns the task
loop, we own one task.

**4. Smoke test and prompt-fit check** (one day). One validation task, the
real arm-B config but `max_cycles: 2`, `cycle_cooldown_seconds: 0`.
Asserts:

- `report/report.md` exists, is non-empty, and `result.json` records which
  file it came from — this is the mechanism most likely to break, because
  the final reporter is off;
- `result.json` has non-zero cost, tool calls and turns;
- the `compact_db` path is task-local;
- the served model is `claude-fable-5-1`;
- no call hit the per-call timeout or the idle watchdog;
- **prompt fit**: read the two agent transcripts and judge whether the
  operating protocol's prescriptive scaffolding is crowding out the work
  (short outputs, checkpoint ceremony dominating, the model arguing with
  the protocol). If so, trim prescriptiveness before spending anything —
  on this model that is a quality lever, not a style preference.

This is the gate for spending anything. Estimate: **about one working
week** before the first scored run.

---

## 6. The one configuration

### Feature switches

Everything on except the two stages you flipped off.

| Feature | Setting | Note |
|---|---|---|
| In-cycle auditor | **on** | `flow: [researcher, worker, auditor]` — the gate is the harness's central mechanism |
| Whole-cycle fan-out | **on**, cap 3 | Single account, shared workspace; the pool stays out (`benchmarking-plan.md` §3.4) |
| Periodic reporter | **on** | Now load-bearing — see below |
| Final auditor | **off** | `end_of_run.final_auditor: false` |
| Final reporter | **off** | `end_of_run.final_reporter: false` |
| Curator | **on** | One call; produces an ungraded ZIP. Kept because "everything else on"; the cheapest thing to drop if per-task cost runs hot |
| Auto-compaction | on (default) | At 0.90 × 1M context |
| Multi-account pool | **off** | Held out deliberately |

**The periodic reporter is the deliverable.** With the final reporter off,
nothing else writes a report for the grader. So `report_interval` is not a
convenience knob here — it is the thing that produces the graded artifact.
Set it to `max_cycles` so exactly one report flushes at the end, and rely
on the graceful-stop flush if the wall budget fires first. If both paths
somehow fail the run produces no gradeable output and scores zero, which
is why the smoke test asserts on report provenance specifically.

### Budgets, per task

| Control | Value | Why |
|---|---|---|
| `max_cycles` | 12 | Generous; the wall budget is meant to bind first, so the harness ends on exhaustion or wall rather than an arbitrary cycle count. A long-horizon harness should be allowed to run long |
| Wall budget | 6 h | The binding budget control; both arms get the same |
| `cli_timeout` | 5400 s | Raised for Fable 5.1's longer turns (default is `0` = no ceiling) |
| `provider_idle_timeout_seconds` | 3600 | Raised from 1800; belt-and-braces behind the CPU-activity check |
| `loop.max_cost_usd` | $60 | A backstop, not the intended stop. Raised from the Opus-tier $25 for 2× token price × fan-out |
| `loop.max_tool_calls` | unset | Wall and cycles bind first |
| `cycle_cooldown_seconds` | 0 | Cooldown is operator comfort, not a benchmark condition |

Arm A gets the same 6 h. A single `claude -p` call will not use it; that
asymmetry is a finding to report, not to hide.

### Runs

| Pass | Runs | Purpose |
|---|---|---|
| Smoke | 1 | §5 item 4 gate |
| **Main** | 40 tasks × 1 run, arm B | The full-scope result |
| **Baseline** | 40 tasks × 1 run, arm A | The same-model comparison |
| No-web subset | 10 tasks × 1 run, arm B | Contamination bound (§7) |
| Variance probe | 10 tasks × 3 runs, arm B | *Recommended.* See below |

One run per task matches the paper's own protocol, so the main pass is
directly shaped like the published table. But a single run per task gives
no error bars, and rubric variance on open-ended research tasks is
substantial — so a bare B-vs-A difference from the main pass is an
observation, not a measurement. The variance probe (30 extra runs on 10
tasks) is the cheapest thing that turns it into one: it estimates
within-task spread, which is what any claim about the difference needs. It
is listed separately so it can be dropped, and dropping it means reporting
the main-pass difference descriptively, without a significance claim.

### Analysis

- Per-task rubric score from RCB's `_score.json`; mean per arm.
- Per-task difference B − A, reported as a distribution, not just a mean.
  A harness that wins big on 8 tasks and loses on 30 is a different
  finding from one that gains 2 points everywhere.
- With the variance probe: paired bootstrap over per-task differences
  (10,000 resamples, task as the resampling unit), using the probe's
  within-task spread to say whether the main-pass difference is
  distinguishable from run-to-run noise.

---

## 7. Contamination controls

Target papers are hidden by the benchmark but are real published papers,
and peers ran with web search enabled — so disabling it would break
parity, while enabling it risks an agent simply retrieving the answer.

1. **Log every search and fetch.** Telemetry already records tool calls;
   the adapter additionally captures query strings and fetched URLs from
   the session transcript.
2. **Report the retrieval rate.** Per task, whether a URL or title
   matching the hidden target paper appeared in any run. This number goes
   in the results table next to the score. A result that only holds on
   tasks where the target was retrieved is not a result.
3. **No-web subset**: 10 tasks, arm B, WebSearch and WebFetch removed from
   `allowed_tools`, to bound the effect size.
4. **Scored-run freeze.** Pin the commit SHA, the RCB task-set revision,
   the judge model and the model ID before the main pass; record all of
   them in every `result.json`. No config edits mid-pass.

---

## 8. Metrics

Primary: rubric score per task per arm, and the B − A distribution.

Cost and efficiency: score per notional dollar; score per wall-clock hour;
tokens in/out, cache read/write, tool calls, turns, cycles — all already
in `usage_summary.json` per agent, with clone usage merged at fan-out
collapse.

Harness diagnostics — these are why the ledger exists, and with no
ablation grid they are the main source of *mechanism* evidence:

- Auditor decision distribution per cycle, and score conditional on how
  many cycles the auditor gated.
- Fan-out: branches spawned per task, and score on tasks where fan-out
  fired vs where it did not. This is observational rather than a
  controlled contrast, and must be reported as such — but it is free.
- Termination reasons: exhaustion vs cycle cap vs wall stop vs budget gate
  vs failure.
- Cycles completed per task against wall time, which is the closest thing
  the main pass has to a dose-response curve.
- `promise_check` green rate — does ledger discipline correlate with
  rubric score, or is it overhead?

Judge configuration must match the paper: `JUDGE_MODEL_NAME=gpt-5.1` with
the repo's default rubric-mode selection. This needs an OpenAI-compatible
key and is a hard prerequisite — a different judge produces numbers that
cannot sit beside the published ones.

---

## 9. Cost and calendar

Per-task notional cost for arm B is the big unknown: Fable 5.1 at $10/$50
with fan-out up to 3 branches and up to 12 cycles. Modelling it as 2× the
Opus-tier estimate for price and up to 2× again for fan-out gives
**$20–$60 per task**, with the $60 gate as the backstop.

| Pass | Runs | Agent spend (notional) | Judge | Calendar |
|---|---|---|---|---|
| Build (§5) | — | <$50 | — | 1 week |
| Smoke | 1 | <$60 | <$5 | 1 day |
| Main (arm B) | 40 | $800–$2,400 | $150–$400 | see below |
| Baseline (arm A) | 40 | $150–$300 | $150–$400 | parallel |
| No-web subset | 10 | $200–$600 | $40–$100 | parallel |
| Variance probe | 30 | $600–$1,800 | $120–$300 | optional |

Total notional agent spend **$1.8k–$5.2k**, judge $0.5k–$1.2k. Actual cash
outlay against the Max plan is the subscription; these are the
comparability figures.

**Wall clock is the real constraint, not money.** 40 arm-B tasks at up to
6 h each is up to 240 hours serial — ten days of continuous running before
the baseline, subset or probe. Running tasks in parallel containers is
what makes this a two-week pass instead of a six-week one, and per-task DB
isolation (§5 item 1) is the prerequisite for that. Decide the parallelism
factor before the main pass; it also determines whether Max-plan rate
limits become the binding constraint, which the smoke test cannot reveal.

---

## 10. Risks and kill criteria

| Risk | Detection | Response |
|---|---|---|
| Fable 5.1 unavailable on this account (retention, plan) | §5 item 0, one call | Re-decide the model before anything else |
| No gradeable report, because the final reporter is off | Smoke test asserts report provenance | Fix the fallback chain; this is the highest-probability mechanical failure in this configuration |
| Prescriptive prompt suppresses Fable 5.1 quality | §5 item 4 prompt-fit check | Trim the operating protocol's ceremony before the main pass |
| Long turns hit a timeout mid-work | `result.json` termination reasons; no call should hit the ceiling | Raise `cli_timeout`; already raised once for this model |
| Arm A is a strawman | Compare arm A's behaviour to the published Claude Code entry qualitatively (different model, so scores will differ) | Use RCB's own preset unmodified; do not hand-tune it |
| Rubric variance swamps the difference | Variance probe | Report the main-pass difference descriptively, without a significance claim |
| Target-paper retrieval drives scores | §7 retrieval rate, no-web subset | Report conditional results |
| B wins only by spending far more | Equal-wall and equal-notional-dollar views | Report both; a win that exists only at 10× cost is reported as such |
| Max-plan rate limits throttle a parallel pass | Runs-per-day tracking from the first parallel batch | Lower parallelism; the harness's adaptive cooldown handles the rest |
| Fan-out clones cross-contaminate through a shared DB | §5 item 1 verification, per task | Fan-out shares the parent instance dir by design — confirm the isolation boundary is the task, not the clone |

Kill criterion: **if the smoke test cannot produce a gradeable
`report/report.md` from the periodic reporter, the main pass does not
start.** That is a mechanical failure, not a result, and it would score 40
zeros.

---

## 11. Configuration appendix

### RCB agent registration (`evaluation/agents.json`)

```json
{
  "long_exposure": {
    "label": "Long-Exposure",
    "icon": "LE",
    "cmd": "bench/rcb_agent.sh --prompt-file <PROMPT> --workspace <WORKSPACE>"
  },
  "claude_code_pinned": {
    "label": "Claude Code (Fable 5.1)",
    "icon": "CC",
    "cmd": "claude -p \"$(cat '<PROMPT>')\" --model claude-fable-5-1 --effort high --output-format json"
  }
}
```

### Per-task bench config (generated by the adapter)

```yaml
# EXACT model ID everywhere, never the `fable` alias — an alias resolves to
# whatever is current that week, which makes the run unreproducible.
model_tier: claude-fable-5-1
model: claude-fable-5-1
agent_models:
  researcher:     { provider: claude, model: claude-fable-5-1, effort: high }
  worker:         { provider: claude, model: claude-fable-5-1, effort: high }
  auditor:        { provider: claude, model: claude-fable-5-1, effort: high }
  reporter:       { provider: claude, model: claude-fable-5-1, effort: high }
  curator:        { provider: claude, model: claude-fable-5-1, effort: medium }

working_directory: <WORKSPACE>
compact_db: <INSTANCE_DIR>/sessions.db   # absolute; per-task isolation
cli_timeout: 5400                        # Fable 5.1 turns run long
provider_idle_timeout_seconds: 3600      # raised from 1800
context_window: 1000000
compact_threshold: 0.90
wolfram_path: ""                         # no kernel in the container
test_runner: ""
```

The reporter is at `high`, not the usual `medium`: with the final reporter
off it writes the graded deliverable.

### Score `loop` block

```yaml
loop:
  max_cycles: 12
  cycle_cooldown_seconds: 0
  report_interval: 12        # one flush at the end (0 would mean EVERY cycle)
  daily_sync_interval_hours: 0
  fanout_enabled: true
  end_of_run:
    enabled: true
    final_auditor: false
    final_reporter: false
    curator: true
  max_cost_usd: 60
  max_tool_calls: null

flow: [researcher, worker, auditor]
```

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
| Source of that report | newest `<WORKSPACE>/reports/report_cycles_*.md` |
| Our run record | `<OUT_DIR>/result.json` |
| Usage ledger | `<INSTANCE_DIR>/output/usage_summary.json` |
| Telemetry | `<INSTANCE_DIR>/telemetry/events.jsonl` |
| Judge output | `<WORKSPACE>/_score.json` |

---

## 12. Decisions taken

| Decision | Choice | Consequence carried in this plan |
|---|---|---|
| Model | `claude-fable-5-1`, both arms, `high` effort | No board tie-back, so arm A is load-bearing; 2× token price; timeouts raised; prompt-fit check added to the smoke test |
| Staging | Smoke test, then the full pass | One gate, and it is mechanical: no gradeable report means no main pass |
| Configuration | One config, all features on except the final auditor and final reporter; full 40-task scope | No ablation grid — mechanism evidence comes from observational diagnostics (§8); the periodic reporter becomes the deliverable |
| Web access | On and logged, plus a 10-task no-web subset | Peer parity kept, retrieval rate reported, effect size bounded |

Two things were decided by implication rather than explicitly, and are
easy to change:

- **Arm A is included.** "One run with all features enabled" was read as
  one long-exposure *configuration* — no feature matrix — not as dropping
  the baseline. On Fable 5.1 the baseline is the only same-model reference
  the campaign has; without it there is a score but no harness comparison.
- **The variance probe is proposed, not assumed.** 30 extra runs to get
  error bars on the headline difference. Drop it and the main-pass
  difference is reported descriptively.
