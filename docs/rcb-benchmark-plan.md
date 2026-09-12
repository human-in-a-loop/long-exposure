# ResearchClawBench: execution plan for the bench-mode branch

Scope: one venue, one model, **one arm**, one shot per task. This is the
runnable plan for `claude/long-exposure-benchmarking-pikxm2`. The wider
survey and the rejected venues stay in `docs/benchmarking-plan.md`; this
document supersedes its §5.2.

Status: **pre-registration.** Nothing here has been run. Peer numbers are
from the published paper and repository, not from our runs.

---

## 1. The venue

**ResearchClawBench** (InternScience, arXiv 2606.07591, MIT licence;
[repo](https://github.com/InternScience/ResearchClawBench),
[data](https://huggingface.co/datasets/InternScience/ResearchClawBench)).
40 tasks across 10 scientific domains. Each task is grounded in a real
published paper, ships `data/` and `related_work/`, hides the target
paper, and asks the agent to produce `report/report.md` plus code and
figures. An expert-curated multimodal rubric is scored by an LLM judge on
a 100-point scale where **50 = reference-level evidence (the target paper
re-discovered)** and >50 implies discovery beyond it.

It was chosen because the published unit of comparison is the harness, the
task shape is a long-exposure directive, the grader is public, and it
needs no GPU.

### The published table

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

Everything is far below 50 — there is real headroom. But read the top two
rows together, because they set the interpretive frame for a single-arm
run: **a thin harness on Opus 4.7 scored 20.7 and the best agent harness
on Opus 4.6 scored 21.5.** On this venue, moving from almost no scaffold
to the best-performing agent harness bought under one point on comparable
models. Whatever we score, that gap is the scale on which harness effects
have so far been observed here.

---

## 2. What this run is, and what it is not

**One arm.** Long-exposure, one configuration, everything on except the
final auditor and final reporter (§6), on `claude-fable-5-1`, across all
40 tasks, one attempt each. No baseline arm, no ablation grid. 41 runs
including the smoke test.

### What it measures

- **A leaderboard row**: the rubric score of *this harness on this model*
  on a public benchmark with a public grader. Every entry on that board is
  a harness × model pair; ours would be a new one, honestly labelled.
- **Whether the pair crosses the benchmark's own threshold.** Fifty means
  the hidden target paper was re-discovered. No published system is close.
  "Does the most capable model inside a long-horizon harness get nearer to
  50, and on which domains" is a real question this run answers.
- **How the harness behaves on 40 real research tasks at its default
  operating point** — cycles to exhaustion, auditor verdict patterns,
  fan-out incidence, spend, failure modes. This is engineering evidence
  that does not need a comparison to be worth having, and it is what the
  §8 diagnostics are for.

### What it cannot establish

**It cannot attribute any difference from a published row to the
harness.** Every peer row runs a different, older model (Opus 4.6/4.7 or
GPT-5.4). Our row changes the model *and* the harness at once, so the two
are fully confounded. Given §1's observation — under one point between a
thin harness and the best agent harness on comparable models — the
prior should be that **most of any headline gain over 21.5 is the model,
not long-exposure.** A single-arm design cannot separate them, and no
analysis after the fact can rescue that.

Three sentences must therefore appear in the conclusions, not buried in a
limitations paragraph. Pre-registering them here is what keeps them from
being dropped when the numbers arrive:

1. This run had no same-model baseline, so it makes **no causal claim**
   about long-exposure's contribution.
2. The comparison to published peers is **confounded by model
   generation**, and the published table's own spread suggests the model
   dominates.
3. There was no compute-matched control, so any advantage is also
   confounded with **spending one to two orders of magnitude more tokens**
   than a single-call harness.

A same-model baseline (RCB's own Claude Code preset on `claude-fable-5-1`,
40 runs, roughly $150–$400 notional) is what would convert this from a
characterisation into a comparison. It is out of scope by decision, and
the decision is recorded in §12 so a reader knows it was a choice rather
than an oversight.

---

## 3. Model: `claude-fable-5-1`

Pinned for every role at `high` effort, through
`claude -p --model claude-fable-5-1 --effort high`. Max plan, subscription
billing, nothing through the API.

Fable 5.1 is Anthropic's most capable widely released model — the right
choice if the question is "what is the best research this harness can
produce". Four consequences, accepted deliberately:

1. **It is the confound.** No published peer runs this tier, so the model
   difference is doing unknown work in any comparison (§2).
2. **2× the token price** ($10/$50 per MTok vs Opus-tier $5/$25) in the
   notional accounting of §9. No cash effect: see §7.
3. **Different model behaviour, in a direction that matters here.**
   Thinking is always on and cannot be disabled. More important: prompts
   written for earlier models are often *too prescriptive* for Fable 5.1
   and reduce output quality. Long-exposure's four-layer system prompt is
   exactly that shape — philosophy + framework + operating protocol +
   role, with checkpoint-block rules and a named anti-pattern list on
   every call. §5 item 4 is a one-time, pre-scored prompt-fit check.
4. **Longer single turns.** Main's defaults are `cli_timeout: 0` (no
   per-call ceiling) and `provider_idle_timeout_seconds: 1800`. That pair
   turns out to be right for this model: the idle watchdog checks
   process-tree CPU as well as file progress
   (`orchestrator.py:3163-3182`), so a turn that thinks for 40 minutes
   survives while a genuinely wedged process is still killed. Both stay at
   main's values.

### Constraints

- **The model string is passed verbatim** to the CLI
  (`orchestrator.py:3750`); `claude --model` accepts full names.
- **Use the exact ID, never the `fable` alias.** `model_tier: opus` and
  every `model: opus` in `agent_models` becomes `claude-fable-5-1`. An
  alias resolves to whatever is current that week, which would make the
  run unreproducible. This is the single most important line in the bench
  config.
- Fable 5.1 is unavailable to zero-data-retention organisations unless
  expressly authorised; §5 item 0 catches that in one call.

---

## 4. What must be built first

Already done on this branch: fan-out switch, end-of-run switches, usage
ledger with tool counts and cost capture, budget gates (unused here — see
§5), headless prompt hygiene, run-config threading. What remains:

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

**4. Smoke test and two judgement checks** (one day). One validation task,
the real config, stopped after three cycles. Asserts: `report/report.md`
exists, is non-empty, and `result.json` records its source file;
`result.json` has non-zero cost, tool calls and turns; the `compact_db`
path is task-local; the served model is `claude-fable-5-1`; no call hit
the idle watchdog; the retrieval log is being captured.

Then, read by a human from the artifacts:

- **Report shape.** The deliverable must read as clear, concise
  synthesized findings, not a process log. The periodic reporter is
  cumulative by design, so this is the check most likely to fail.
- **Prompt fit.** Are the operating protocol's scaffolding and checkpoint
  ceremony crowding out the work? One trim is allowed here, decided from
  transcripts and never from scores (§7.2), then frozen.

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
existing stop path, identical for every task. This is not a new budget or
a new policy — it is the number the harness already uses for a clone and
for a synthesis pass, applied at the one place the code leaves open. **The
exhaustion-vs-10 h split is a reported result, not a footnote:** if a
large share of tasks hit the cap, the headline is "score after 10 h"
rather than "score at natural exhaustion", and the write-up must say so.

### The four deliberate deviations from stock

| Deviation | Why |
|---|---|
| `model` / `agent_models` → `claude-fable-5-1` | The run's subject |
| `end_of_run.final_auditor: false`, `final_reporter: false` | Operator decision; see the deliverable note |
| `compact_db` → absolute, per task | Cross-task contamination (§4 item 1) |
| `working_directory` → the RCB task workspace | Required by the adapter contract |

Everything else — including `curator: true`, since "everything else on" —
stays as shipped.

### The deliverable, and one shot at it

**One attempt per task.** A single pass to present clear, concise
synthesized findings to the judge, then on to the next task. No repeat
runs, no second attempts, no re-rolls on a bad score. This matches the
paper's own protocol.

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

With one arm, the honesty burden shifts. There is no comparison to keep
fair, so the whole job is making sure the number means what it says and
that its limits travel with it.

### 7.1 Compute and confound disclosure

Report spend in the headline table, not an appendix: tokens in/out, calls,
cycles, wall time (raw *and* net of the 400 s/cycle cooldown, which is
dead time rather than compute), and notional cost, per task.
Score-per-notional-dollar and score-per-net-hour sit next to the raw
score, because a reader comparing to a single-call peer needs to see the
scale of what was spent to get it.

The three pre-registered limitation sentences in §2 are part of this
control, not decoration. §1's 20.7-vs-21.5 observation goes in the results
section too: a reader deserves the benchmark's own evidence about how
little harness choice has moved this number so far.

### 7.2 Judge integrity

The judge is GPT-5.1 per the paper (`JUDGE_MODEL_NAME`), cross-family from
the agent, which avoids self-preference. Four controls:

- **Blinding.** Long-exposure reports carry harness fingerprints — "Cycle
  7", plan-of-record and ledger references, `STRUCTURE.md`, checkpoint
  residue — which cue a rubric judge toward "thorough process". Apply one
  deterministic, published neutralisation pass and **judge both the raw
  and the neutralised report**, reporting both scores. With one arm this
  is no longer about cross-arm fairness; it measures how much of our own
  score depends on process fingerprints rather than findings. Re-judging
  40 reports is cheap, and a large raw-vs-neutralised gap would be one of
  the more interesting things this run could find.
- **Verbosity.** LLM rubric judges reward length. Record report length,
  figure count and artifact count per task, and report score against
  length — necessary context when the peers being compared to produced
  one-call reports.
- **No tuning against the judge.** The §4 prompt-fit trim happens once,
  before any scored run, decided from transcripts. After the first scored
  run: no prompt, config or flow change without restarting the pass and
  saying so. Hill-climbing on rubric scores would make this an overfit.
  With one arm and no baseline, this is the control doing the most work —
  it is the only thing preventing the number from being tuned upward.
- **Drift.** Re-score a held-out sample of 10 runs at the end of the pass
  with the same judge config. Report the delta; if it exceeds noise,
  re-score everything.

### 7.3 Contamination: block the target, then own what remains

**Policy: no web retrieval of the target papers or their results.** Web
search and fetch stay enabled — peers had them — but the specific leak is
closed rather than merely measured. Implementation, in the adapter:

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

**Memorisation is not probed** — and with one arm that costs more than it
did with two, so the reasoning is worth stating precisely rather than
inheriting.

The target papers are real and published, so Fable 5.1 may already know
some of them. In a two-arm design prior knowledge would largely cancel in
the paired difference, because both arms share the model. **With one arm
there is nothing for it to cancel against: memorisation inflates the
absolute score directly, and this design cannot detect it.** The position
being taken is the second one — for a re-discovery benchmark, a frontier
model holding the target literature in weights is the venue's limitation
for models of this class, not the harness's, and it bounds how much any
absolute number here (ours *and* the board's) should be trusted for a
model of this generation.

That is defensible, but it must be said in the results, not implied:
**every absolute score in this run is an upper bound on
re-discovery-from-evidence, because prior knowledge of the target
literature was neither blocked nor measured.** One closed-book call per
task (40 calls, a few dollars) is what would turn that caveat into a
measured split, and it remains the cheapest available upgrade to this
plan.

### 7.4 Statistics that match one arm, one shot

With a single arm and one run per task there is no comparison to test and
no variance to estimate, so the analysis is **descriptive, and says so**:

- Mean, median and full distribution of the 40 task scores; count of tasks
  above 50 (the benchmark's re-discovery line) and above 21.5 (the best
  published row, with §2's confound attached wherever that number is
  quoted).
- Per-domain breakdown across the 10 domains.
- Rubric sub-scores (§7.5).
- **No significance test.** There is no second arm to pair against, and a
  paired comparison to published per-task peer scores is not available:
  the leaderboard publishes aggregates only, with no per-task breakdown or
  downloadable results (checked). Any test against 21.5 would be a
  one-sample test against a number produced by a different model, a
  different judge invocation, and a single run per task — arithmetic
  dressed as inference.
- Pre-register the primary metric (mean rubric score on raw reports)
  before the pass so there is no metric-shopping afterwards. Freeze this
  document's commit SHA and publish it with the results.

### 7.5 Report every run, and every difference from stock

- **Every task reported, including failures.** A crashed or report-less run
  scores whatever the judge gives it — usually zero. No quiet exclusions.
  Any excluded task is pre-registered with a reason before the pass. With
  40 tasks and one arm, a single silently dropped zero moves the mean by
  half a point, which is most of the entire observed harness effect in §1.
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
- **Which branch deltas can touch this run.** The only other behavioural
  change between main and this branch that the graded configuration could
  reach is the final-stage token threshold (20k → 100k) and the restored
  `_N_MAX` cap — both inert here, because the final auditor and reporter
  are off. Everything else is bug fixes, the two switches, and
  observability. State this, with the diff published, so a reader need not
  take it on trust.

### 7.6 Harness diagnostics: the evidence a single arm can actually carry

No comparison means the descriptive record *is* the result, so collect it
properly. All of it is already instrumented:

- Cycles to termination per task, and the exhaustion-vs-10 h-cap split.
- Auditor verdict distribution per cycle, and score against how many
  cycles the auditor gated.
- Fan-out incidence: branches spawned per task, and score on tasks where
  fan-out fired against tasks where it did not. Observational, and must be
  labelled so — the researcher chose when to fan out.
- Rubric sub-scores separately. Long-exposure's reporter is an LLM
  summarising work it did not do, so if the scores concentrate in
  presentation-flavoured items rather than implementation, measurement and
  analysis, that is a finding about what the harness is actually adding.
- Termination reasons, rate-limit events, cooldown time, compaction count.
- `promise_check` green rate against score — does ledger discipline track
  research quality, or is it overhead?

---

## 8. Cost and calendar

Per-task notional cost is the largest unknown: Fable 5.1 at $10/$50,
unlimited cycles to natural exhaustion, fan-out up to 3 branches, no cost
ceiling, 10 h outer bound. Modelling 6–15 cycles at 3–4 calls each, with a
fan-out multiplier on the cycles where it fires, gives roughly **$20–$150
per task** with a tail bounded only by the 10 h stop.

| Pass | Runs | Agent spend (notional) | Judge | Notes |
|---|---|---|---|---|
| Build (§4) | — | <$50 | — | ~1 week |
| Smoke | 1 | <$50 | <$5 | Gate |
| Main | 40 | $800–$6,000 | $150–$400 | Wide by construction |
| Re-judge (neutralised + drift) | — | — | $150–$400 | §7.2 |

Notional agent total **$850–$6,100**; judge $300–$800. The judge is the
only real-cash line and needs an OpenAI-compatible key.

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
| Fable 5.1 unavailable on this account | §4 item 0, one call | Re-decide the model first |
| No gradeable report (final reporter off) | Smoke test asserts provenance | Fix the fallback chain — highest-probability mechanical failure here |
| Report reads as a process log, not findings | §4 item 4 report-shape check | Report it as a property of this configuration; do not tune mid-campaign |
| Prescriptive prompt suppresses Fable 5.1 quality | §4 item 4, from transcripts | One pre-scored trim, then frozen (§7.2) |
| Target paper retrieved despite the denylist | §7.3 egress block + post-hoc detection | Disqualify and re-run once; report the count |
| Target papers already in weights | **Not detectable in this design** | §7.3's upper-bound caveat in the results |
| Score read as a harness result | — | §2's three pre-registered sentences in the conclusions |
| Judge rewards length or process fingerprints | §7.2 dual raw/neutralised scoring, length reporting | Report both scores; a large gap is itself a finding |
| Unlimited cycles hit the 10 h stop often | Exhaustion-vs-cap split per task | Re-frame the headline as "score after 10 h" |
| Max-plan rate limits throttle a parallel pass | Rate-limit events in `result.json` | Lower parallelism; report cooldown time |
| Fan-out clones cross-contaminate | §4 item 1 assertion per task | Isolation boundary is the task, not the clone |

Kill criterion: **if the smoke test cannot produce a gradeable
`report/report.md` from the periodic reporter, the main pass does not
start.** That is a mechanical failure worth 40 zeros, not a result.

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
| Arms | **One.** Long-exposure only; no baseline, no ablation grid | The run is a characterisation and a leaderboard row, not a comparison. No causal harness claim; §2's three sentences are pre-registered for the conclusions |
| Model | `claude-fable-5-1`, `high` effort | It is also the confound: no peer runs this tier, so model and harness differ together |
| Billing | Max plan via `claude -p`; no API key for agent calls | Cost is notional/API-equivalent and labelled as such (§6); the judge key is the only cash line |
| Budget | Every ceiling, timer and cap as `main` ships them | Unlimited cycles, no cost cap; the ledger tracks without gating |
| Outer bound | The harness's own 10 h, applied at the root | No new number invented; the root loop is the one place main leaves uncapped, and the exhaustion-vs-cap split is reported |
| Configuration | All features on except the final auditor and final reporter; full 40-task scope | The periodic reporter is the deliverable; mechanism evidence is the §7.6 diagnostics |
| Attempts | One shot per task, then move on | Descriptive statistics only; no significance test (§7.4) |
| Web access | Enabled, but no retrieval of the target papers or their results | Per-task denylist, egress block, post-hoc detection, disqualify-and-re-run |
| Memorisation | Not probed | With one arm it no longer cancels, so every absolute score is reported as an **upper bound** on re-discovery-from-evidence (§7.3) |
| Compute-matched control | Not run | Folded into §2's limitation sentences |

The two cheapest upgrades, if the scope ever reopens, in order of what
they buy per dollar:

1. **A same-model baseline** — RCB's Claude Code preset on
   `claude-fable-5-1`, 40 runs, ~$150–$400 notional. Converts the whole
   exercise from a characterisation into a harness comparison.
2. **A closed-book memorisation probe** — 40 single calls, a few dollars.
   Turns the §7.3 upper-bound caveat into a measured split.
