# ResearchClawBench: execution plan for the bench-mode branch

Scope: one venue, one model, one harness configuration, at the harness's
own default operating point. This is the runnable plan for
`claude/long-exposure-benchmarking-pikxm2`. The wider survey and the
rejected venues stay in `docs/benchmarking-plan.md`; this document
supersedes its §5.2.

Status: **pre-registration.** Nothing here has been run. Peer numbers are
from the published paper and repository, not from our runs.

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
criterion at once: the published unit of comparison is the harness, the
task shape is a long-exposure directive, the grader is public, and it
needs no GPU.

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

Everything is far below 50. The headroom is the point: a harness can
still matter here, unlike the saturated coding boards. Two facts from the
table shape the protocol — the best published harness entry is Claude Code
on Opus 4.6, and one run per task means the published numbers carry no
variance estimate.

---

## 2. The claim under test

> Long-exposure's deterministic researcher → worker → auditor cycle, with
> fan-out and its durable plan/ledger surface, produces higher-rubric-score
> research artifacts than a single-agent harness on the same model.

Note what that claim does **not** say: it does not say "at equal token
spend". Long-exposure at its default operating point runs until the topic
is exhausted and will spend one to two orders of magnitude more tokens
than one `claude -p` call. That is the harness working as designed, and
the honest way to handle it is to measure the spend, report it in the
headline, and add a control that lets the baseline use comparable compute
(§8). It is not honest to quietly compare an all-night run against a
single call and report only the score difference.

Arms:

| Arm | What it is | Tasks |
|---|---|---|
| **B** | Long-exposure, one configuration, everything on except the final auditor and final reporter (§6) | 40 |
| **A** | Claude Code via RCB's own preset, same model | 40 |
| **A+** | Claude Code, k sequential self-continuation rounds, k set from B's measured spend (§8.1) | 10 |

There is no feature-ablation grid. If B beats A we will know the harness
as a whole wins, not which of cycles / auditor gate / fan-out earned it.
The switches stay in the code, so ablations remain available as follow-up
on whichever subset looks most informative.

---

## 3. Model: `claude-fable-5-1`

Pinned for **every** arm and every role, at `high` effort, through
`claude -p --model claude-fable-5-1 --effort high`. Max plan, subscription
billing, nothing through the API.

### What this buys and what it costs

Fable 5.1 is Anthropic's most capable widely released model — the right
choice if the question is "what is the best research this harness can
produce". Four consequences, accepted deliberately:

1. **No board tie-back.** Every published peer runs Opus 4.6/4.7 or
   GPT-5.4. A Fable 5.1 number cannot be placed next to 21.5 and called a
   comparison. **This makes arm A load-bearing rather than optional** — it
   is the only same-model reference point the campaign has.
2. **2× the token price** ($10/$50 per MTok vs Opus-tier $5/$25) in the
   notional accounting of §9. No cash effect: see §7.
3. **Different model behaviour, in a direction that matters here.**
   Thinking is always on and cannot be disabled. More important: prompts
   written for earlier models are often *too prescriptive* for Fable 5.1
   and reduce output quality. Long-exposure's four-layer system prompt is
   exactly that shape — philosophy + framework + operating protocol +
   role, with checkpoint-block rules and a named anti-pattern list on
   every call. §5 item 4 is a one-time, pre-scored prompt-fit check for
   this; §8.2 explains why it must happen before any scored run and never
   after.
4. **Longer single turns.** Main's defaults are `cli_timeout: 0` (no
   per-call ceiling) and `provider_idle_timeout_seconds: 1800`. That pair
   turns out to be the right combination for this model: the idle watchdog
   checks process-tree CPU as well as file progress
   (`orchestrator.py:3163-3182`), so a turn that thinks for 40 minutes
   survives while a genuinely wedged process is still killed. Both stay at
   main's values (§6).

### Constraints

- **The model string is passed verbatim** to the CLI
  (`orchestrator.py:3750`); `claude --model` accepts full names. Pinning is
  a config edit.
- **Use the exact ID, never the `fable` alias.** `model_tier: opus` and
  every `model: opus` in `agent_models` becomes `claude-fable-5-1`. An
  alias resolves to whatever is current that week, which would make the
  run unreproducible. This is the single most important line in the bench
  config.
- Fable 5.1 is unavailable to zero-data-retention organisations unless
  expressly authorised; §5 item 0 catches that in one call.

---

## 4. Peer parity: what arm A must be

The published entry is Claude Code invoked by RCB's own preset — one
`claude -p` call per task with the unified persona prompt from
`evaluation/instructions_tmpl.py`, the CLI's own tools, no external
scaffolding. Arm A reruns *that*, on Fable 5.1, from RCB's own
`agents.json` entry, unmodified. We do not write our own baseline: using
the benchmark's preset is what makes arm A checkable rather than a
strawman we tuned down.

Arm B gets the **same prompt text** — the adapter passes `<PROMPT>`
through as the score directive verbatim — and the same task files.

---

## 5. What must be built first

Already done on this branch: fan-out switch, end-of-run switches, usage
ledger with tool counts and cost capture, budget gates (unused here — see
§6), headless prompt hygiene, run-config threading. What remains:

**0. Model-availability and retention probe** (~20 lines, half a day).
One `claude -p --model claude-fable-5-1 --effort high` call with a trivial
prompt. Record success, the served model from the envelope, latency, and
that no retention error comes back. Run this first — one call can
invalidate the model plan.

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
`--prompt-file`, `--workspace`, `--out-dir`, `--wall-stop`, `--no-web`.
Behaviour:

- generate the per-task config and score (directive = prompt file
  contents, verbatim);
- run the loop as a subprocess — importing the module installs signal
  handlers and is one-run-per-process (`exploration.py:94-128`);
- at `wall_stop − reserve`, write `long-exposure.stop` into the instance
  dir. A stop is a *graceful* end: the current agent finishes, a periodic
  report flushes, and the enabled end-of-run stages run
  (`_should_run_final_synthesis`, `exploration.py:946-965`). Reserve
  45 min;
- copy the deliverable to `<workspace>/report/report.md` — RCB's expected
  path — from the newest `reports/report_cycles_*.md` (see §6);
- write `result.json`: report path *and which file it came from*, artifact
  list, tokens, notional cost, tool calls, turns, wall time, cooldown
  seconds, cycles run, fan-out branches spawned, rate-limit events,
  termination reason, served model, and the commit SHA.

Encode two footguns rather than rediscover them: `report_interval: 0`
means *every cycle*, not *never* (`exploration.py:4980` tests `>=`); and
`cli_timeout: 0` means no per-call ceiling at all.

**3. RCB adapter** (~40 lines, one day). `bench/rcb_agent.sh` plus the
`agents.json` entries in §11.

**4. Smoke test and prompt-fit check** (one day). One validation task, the
real arm-B config, stopped after three cycles. Asserts: `report/report.md`
exists, is non-empty, and `result.json` records its source file;
`result.json` has non-zero cost, tool calls and turns; the `compact_db`
path is task-local; the served model is `claude-fable-5-1`; no call hit
the idle watchdog. Then the **prompt-fit check**: read the transcripts and
judge whether the operating protocol's scaffolding is crowding out the
work (short outputs, checkpoint ceremony dominating, the model arguing
with the protocol). One trim is allowed here, decided from transcripts and
never from scores (§8.2), then frozen.

Estimate: **about one working week** before the first scored run.

---

## 6. The configuration

### Budget: main's defaults, unchanged

Every ceiling, timer and concurrency cap is whatever `main` ships. The
code is this branch (for the bug fixes and the two switches); the budget
is stock. That combination is deliberate and must be disclosed as such,
because it is not a released configuration.

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
| End-of-run stage wall cap | 36,000 s (10 h) | `limits.WALL_CAP_SECONDS`, identical on both branches |

An earlier draft of this plan invented tighter values (`max_cycles: 12`,
`cli_timeout: 5400`, `max_cost_usd: 60`, `report_interval` forced to the
cycle cap). Those are withdrawn. They would have measured a
budget-constrained variant of the harness that nobody runs.

**`max_cycles: null` means the run ends when the work does.** Termination
comes from the exhaustion detector — two consecutive cycles below 5 % of
peak observed output, floor 500 tokens
(`LOW_OUTPUT_FRACTION`/`LOW_OUTPUT_ABS_FLOOR`/`LOW_OUTPUT_CLOSURE_COUNT`,
`exploration.py:3907-3909`) — or from the auditor emitting
`[[BRANCH_COMPLETE]]`. This is the harness's actual operating point and
the thing worth benchmarking.

**Budget is tracked, not enforced.** With both caps absent,
`budget_exceeded()` never fires (it only triggers on a cap that parses as
a number > 0), so the usage ledger is pure observation: per-agent tokens,
cache reads/writes, tool calls, turns, wall time and notional cost, live
in `long-exposure status` and `long-exposure usage`, persisted in
`usage_summary.json`, merged from fan-out clones at collapse. That is
exactly the intended arrangement: measure everything, constrain nothing.

### The four deliberate deviations from stock

Each one is a benchmark necessity, not a tuning choice:

| Deviation | Why |
|---|---|
| `model` / `agent_models` → `claude-fable-5-1` | The experiment's variable |
| `end_of_run.final_auditor: false`, `final_reporter: false` | Operator decision; see the deliverable note below |
| `compact_db` → absolute, per task | Cross-task contamination (§5 item 1) |
| `working_directory` → the RCB task workspace | Required by the adapter contract |

Everything else — including `curator: true`, since "everything else on" —
stays as shipped.

### One outer bound, imposed outside the harness

An unlimited-cycle run needs some bound or a single wedged task blocks the
pass forever. Rather than cap the harness internally (which would change
what is being measured), the adapter imposes **a 12 h per-task wall stop,
identical for every task and every arm, via the graceful stop signal.**

It is set high on purpose: most runs should exhaust naturally well inside
it, and **the exhaustion-vs-wall-stop split is a reported result, not a
footnote.** If a large share of tasks hit the wall, the headline becomes
"long-exposure's score after 12 h" rather than "after natural
exhaustion", and the write-up must say so.

### The deliverable

With the final reporter off, nothing else writes a report for the grader,
so the **periodic reporter is the graded artifact**. Main's
`report_interval: 3` handles this without special casing: a report flushes
every third cycle, and the graceful stop flushes one too. The smoke test
asserts report provenance because this is the most likely mechanical
failure in this configuration — no report means a zero, not a low score.

### Runs

| Pass | Runs | Purpose |
|---|---|---|
| Smoke | 1 | §5 item 4 gate |
| **Main (arm B)** | 40 × 1 | The full-scope result |
| **Baseline (arm A)** | 40 × 1 | Same-model reference |
| Compute control (arm A+) | 10 × 1 | §8.1 |
| No-web subset (arm B) | 10 × 1 | §8.3 |
| Memorisation probe | 40 × 1 call | §8.3 |
| Variance probe (arm B) | 10 × 3 | §8.4 — recommended |

One run per task matches the paper's protocol. It gives no error bars,
which is what §8.4 is about.

---

## 7. Cost accounting: notional, and labelled as such

The run bills against the Max plan through `claude -p`. **Nothing is
billed through the API, and no API key is used for agent calls.** Marginal
cash cost is zero; the subscription is the outlay.

The envelope's `total_cost_usd`, which the ledger records per call, is
therefore an **API-equivalent figure** — what these tokens would have cost
at list price. Every table reports it as "API-equivalent cost
(subscription-billed run)". It is the right number for comparison, because
peers' published costs are API-priced, and the wrong number to call spend.

Two consequences to report rather than hide:

- **No cost ceiling is in force.** With `max_cost_usd` absent, an
  expensive task runs to completion. Monitoring is `long-exposure usage`;
  the response to an outlier is to report it, not to kill it, since a
  kill would be an undisclosed cap.
- **Max-plan rate limits are part of the experiment.** Long-exposure makes
  many calls where arm A makes one, so throttling lands asymmetrically.
  Rate-limit events and adaptive-cooldown time are already recorded in
  health events; both go in `result.json` and in the results table.

---

## 8. What makes this honest

The model is the most capable available and the harness runs at its own
default operating point. That combination is the most flattering setup
long-exposure will ever get, which is precisely why the controls below are
not optional garnish — they are what separates a benchmark from a demo.
Each is cheap relative to the main pass.

### 8.1 Compute disclosure, and a control that uses the compute

Arm B will spend perhaps 20–50× arm A's tokens. The first objection any
reader will raise is "you just spent more". Two answers, both required:

- **Report spend in the headline table**, not an appendix: tokens in/out,
  calls, cycles, wall time (raw *and* net of the 400 s/cycle cooldown,
  which is dead time, not compute), and notional cost, per arm per task.
  Score-per-notional-dollar and score-per-net-hour sit next to the raw
  score.
- **Arm A+, the compute-aware baseline.** Claude Code run k times
  sequentially on the same task, each round fed its own previous report
  with "continue improving this", with k chosen so total token spend
  approximates arm B's measured median. This is the dumbest reasonable way
  to spend the same compute without any of long-exposure's structure, so
  B − A+ isolates **structure** from **spend** in a way B − A cannot. Ten
  tasks is enough to see whether the gap survives. If B beats A but not
  A+, the honest headline is "the tokens did the work, not the
  architecture" — and that result is worth publishing.

### 8.2 Judge integrity

The judge is GPT-5.1 per the paper (`JUDGE_MODEL_NAME`), cross-family from
the agent, which avoids self-preference. Four controls:

- **Blinding.** Long-exposure reports carry harness fingerprints — "Cycle
  7", plan-of-record and ledger references, `STRUCTURE.md`, checkpoint
  residue — that tell the judge which system wrote them and cue "thorough
  process". Apply one deterministic, published neutralisation pass to
  **both** arms' reports equally, and **judge both the raw and the
  neutralised report.** Report both scores. Silently judging either one
  alone is a choice a reader cannot check; judging both turns a confound
  into a measurement.
- **Verbosity.** LLM rubric judges reward length. Record report length,
  figure count and artifact count per run, and report score against length
  so a reader can see whether the advantage is substance or volume.
- **No tuning against the judge.** The §5 prompt-fit trim happens once,
  before any scored run, decided from transcripts. After the first scored
  run, no prompt, config or flow change without restarting the pass and
  saying so. Hill-climbing on rubric scores would convert this from a
  benchmark into an overfit.
- **Drift.** Re-score a held-out sample of 10 runs at the end of the pass
  with the same judge config. Report the delta; if it exceeds noise,
  re-score everything.

### 8.3 Contamination, in three layers

- **Web retrieval.** Keep WebSearch on for parity with peers, log every
  query and fetched URL, and report the **retrieval rate**: per task,
  whether a URL or title matching the hidden target paper appeared. A
  result that only holds where the target was retrieved is not a result.
  Bound the effect with the 10-task no-web subset.
- **Memorisation.** The target papers are real and published; a
  frontier model may simply know them. One closed-book probe per task —
  ask Fable 5.1 for the key finding with no data and no tools, 40 calls,
  a few dollars — tells us which tasks are memorised regardless of web
  access. Stratify every headline by that split. This is the layer that
  a web-only control misses entirely, and on a "re-discovery" benchmark
  it is the most serious threat to the whole exercise.
- **Cross-run leakage.** Per-task `compact_db` (§5 item 1), a fresh
  workspace per task, and a verification assertion per run. Fan-out clones
  share the parent's instance dir by design, so the isolation boundary is
  the task, not the clone — confirm that is what the assertion checks.

### 8.4 Statistics that match what we ran

One run per task cannot support a significance claim. The variance probe —
10 tasks × 3 runs of arm B, 30 runs — estimates within-task spread, which
is the denominator any claim about B − A needs. With it: paired bootstrap
over per-task differences (10,000 resamples, task as the unit), plus a
sign test over the 40 paired tasks. Without it: report the B − A
distribution descriptively and **make no significance claim** — a
difference of unknown noise is an observation.

Pre-register the primary metric (mean rubric score, raw reports) before
the pass so there is no metric-shopping afterwards. Freeze this document's
commit SHA and publish it alongside results.

### 8.5 Full accounting of what ran

- **Every task reported, including failures.** A crashed or report-less run
  scores whatever the judge gives it — usually zero. No quiet exclusions.
  If a task is excluded it is pre-registered with a reason before the pass.
- **Retry policy, pre-registered:** infrastructure failures only (container
  death, network loss), at most one retry, every retry logged in
  `result.json` and counted in the write-up. Never retry a bad score.
- **Tool-surface difference, disclosed.** Long-exposure adds the MCP
  session-search server, `figure`, `promise_check` and the workspace
  validators; arm A has the CLI's built-in tools only. This matters more
  than it looks because the rubric is multimodal — figures are graded. Say
  so plainly, and report figure counts per arm.
- **Prompt difference vs main, disclosed.** This branch materially
  shortens the headless system prompt: interactive-only text (`/complete`,
  `/clear`, "ask the user") removed, Wolfram guidance gated off when no
  kernel is configured, MCP tools advertised only when actually launched,
  one operator's hard-coded paths replaced by the derived harness root. A
  run on this branch does not see the prompt main would send, and that is
  a behavioural difference, not just a bug fix.
- **Which branch deltas can touch this run.** The only other behavioural
  change between main and this branch that the graded configuration could
  reach is the final-stage token threshold (20k → 100k) and the restored
  `_N_MAX` cap — and both are inert here, because the final auditor and
  reporter are off. Everything else is bug fixes, the two switches, and
  observability. State this, with the diff published, so a reader does not
  have to take it on trust.

### 8.6 Separating better research from better report-writing

Long-exposure's reporter is an LLM summarising work it did not do; arm A's
report is written by the agent that did the work. So part of any advantage
could be report craft rather than research quality. RCB's rubrics
decompose into weighted criteria, so **report the sub-scores separately.**
If B wins only on presentation-flavoured items and not on
implementation, measurement or analysis, that is the finding — and it is a
much weaker claim than the headline mean would suggest.

---

## 9. Cost and calendar

Per-task notional cost for arm B is the largest unknown in the plan:
Fable 5.1 at $10/$50, unlimited cycles to natural exhaustion, fan-out up
to 3 branches, no cost ceiling. Modelling 6–15 cycles at 3–4 calls each,
with a fan-out multiplier on the cycles where it fires, gives roughly
**$20–$150 per task** with an unbounded tail.

| Pass | Runs | Agent spend (notional) | Judge | Notes |
|---|---|---|---|---|
| Build (§5) | — | <$50 | — | 1 week |
| Smoke | 1 | <$50 | <$5 | Gate |
| Main (arm B) | 40 | $800–$6,000 | $150–$400 | Wide by construction |
| Baseline (arm A) | 40 | $150–$400 | $150–$400 | Parallel |
| Compute control (A+) | 10 | $200–$1,500 | $40–$100 | Matched to B's median |
| No-web subset | 10 | $200–$1,500 | $40–$100 | |
| Memorisation probe | 40 calls | <$10 | — | |
| Variance probe | 30 | $600–$4,500 | $120–$300 | Optional |

Notional agent total **$2k–$15k**; judge $0.5k–$1.3k. The judge is the
only line item that is real cash, and it needs an OpenAI-compatible key.

**Wall clock is the binding constraint, not money.** With a 12 h per-task
stop, arm B alone is up to 480 hours serial — three weeks of continuous
running. Parallel containers are what make this a few days instead, and
per-task DB isolation (§5 item 1) is the prerequisite. Decide the
parallelism factor before the main pass: it also determines whether
Max-plan rate limits become the binding constraint, which the smoke test
cannot reveal. Note that main's 400 s cooldown adds roughly 7 minutes of
dead time per cycle — real wall clock, zero compute — which is why §8.1
reports hours both raw and net.

---

## 10. Risks and kill criteria

| Risk | Detection | Response |
|---|---|---|
| Fable 5.1 unavailable on this account | §5 item 0, one call | Re-decide the model first |
| No gradeable report (final reporter off) | Smoke test asserts provenance | Fix the fallback chain — highest-probability mechanical failure here |
| Prescriptive prompt suppresses Fable 5.1 quality | §5 item 4, from transcripts | One pre-scored trim, then frozen (§8.2) |
| Target papers memorised | §8.3 probe | Stratify every headline; a fully memorised task set would invalidate the venue for this model |
| B wins on spend, not structure | Arm A+ | Report "the tokens did the work" if that is what the data says |
| Judge rewards length | §8.2 length reporting, dual raw/neutralised scoring | Report both; no silent choice |
| Rubric variance swamps the difference | Variance probe | Descriptive reporting, no significance claim |
| Unlimited cycles blow up wall clock | Exhaustion-vs-wall-stop split per task | Report the split; if most tasks hit 12 h, re-frame the headline |
| Max-plan rate limits throttle a parallel pass | Rate-limit events in `result.json` | Lower parallelism; report cooldown time |
| Fan-out clones cross-contaminate | §5 item 1 assertion per task | Isolation boundary is the task, not the clone |
| Arm A is a strawman | Use RCB's preset unmodified | Never hand-tune the baseline |

Kill criterion: **if the smoke test cannot produce a gradeable
`report/report.md` from the periodic reporter, the main pass does not
start.** That is a mechanical failure worth 40 zeros, not a result.

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

Only the model IDs and the two per-task paths differ from stock. Every
timer, ceiling and cap is main's.

```yaml
# EXACT model ID everywhere, never the `fable` alias.
model_tier: claude-fable-5-1
model: claude-fable-5-1
agent_models:
  researcher:     { provider: claude, model: claude-fable-5-1, effort: high }
  worker:         { provider: claude, model: claude-fable-5-1, effort: high }
  auditor:        { provider: claude, model: claude-fable-5-1, effort: high }
  reporter:       { provider: claude, model: claude-fable-5-1, effort: medium }
  curator:        { provider: claude, model: claude-fable-5-1, effort: medium }

working_directory: <WORKSPACE>           # per task
compact_db: <INSTANCE_DIR>/sessions.db   # absolute, per task — isolation

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
  end_of_run:                    # branch-only key
    enabled: true
    final_auditor: false         # operator decision
    final_reporter: false        # operator decision
    curator: true

flow: [researcher, worker, auditor]
```

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
| Judge output | `<WORKSPACE>/_score.json` |

---

## 12. Decisions taken

| Decision | Choice | Consequence carried in this plan |
|---|---|---|
| Model | `claude-fable-5-1`, every arm, `high` effort | No board tie-back, so arm A is load-bearing; 2× notional price; prompt-fit check added |
| Billing | Max plan via `claude -p`; no API key for agent calls | Cost is notional/API-equivalent and labelled as such (§7); the judge key is the only cash line |
| Budget | Every ceiling, timer and cap as `main` ships them | Unlimited cycles and no cost cap: the harness runs to exhaustion, the ledger tracks without gating, and a 12 h per-task wall stop is imposed *outside* the harness and reported |
| Configuration | One config, all features on except the final auditor and final reporter, full 40-task scope | No ablation grid; mechanism evidence is observational (§8.6); the periodic reporter is the deliverable |
| Web access | On and logged, plus a 10-task no-web subset | Peer parity kept; retrieval rate reported; memorisation probe added (§8.3) |

Decided by implication, and easy to reverse:

- **Arm A is included.** "One run with all features enabled" was read as
  one long-exposure *configuration* — no feature matrix — not as dropping
  the baseline. On Fable 5.1 it is the only same-model reference the
  campaign has.
- **Arm A+ and the variance probe are proposed, not assumed.** Together
  they are what make the result defensible rather than suggestive: A+
  answers "was it just more tokens", the probe answers "is the difference
  bigger than noise". Dropping either is survivable if the corresponding
  claim is dropped with it.
