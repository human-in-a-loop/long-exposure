# Long-Exposure: Evaluation and Benchmarking Plan

**Date:** 2026-09-10
**Scope:** full read of the repository at commit `89b2e8f` (all source, docs,
score, tests), plus a survey of public agent benchmarks and open-source
harnesses as of September 2026.
**Audience:** the long-exposure maintainers and anyone deciding whether to
fund a benchmark campaign for it.

---

## 1. Summary

Long-exposure is a *research-campaign* harness, not a coding agent. It takes
an ambiguous directive, runs a researcher → worker → auditor loop over a
workspace for hours to days, and emits reports, figures, code, and an audit
trail. That shape has become a crowded category since May 2026, and, unlike
then, there are now public benchmarks whose unit of comparison is the
*harness* rather than the model. So an apples-to-apples campaign is possible.

The short version:

- **Primary venue: ResearchClawBench** (40 tasks, 10 scientific domains,
  rubric-judged, MIT). It already lists Claude Code, Codex CLI, OpenClaw,
  ResearchClaw, ARIS and others as "autonomous agents". Its task shape
  (data + literature + objective → code + figures + `report/report.md`)
  is exactly long-exposure's directive shape. Adding an agent is one entry
  in `evaluation/agents.json`.
- **Secondary venue: AstaBench E2E-Bench-Hard** (50 tasks, rubric-judged,
  cost-per-task reported, external submissions accepted). Requires an
  Inspect AI solver wrapper.
- **Third venue: the Harbor family** (AARRI-bench research-lifecycle tasks,
  Long-Horizon Terminal-Bench, Terminal-Bench 2.1). One `BaseInstalledAgent`
  adapter unlocks all three; TB 2.1 is a sanity check, not a target.
- **Optional GPU venue: PostTrainBench** (or the Prime Intellect nanoGPT
  speedrun) for the "runs for days and improves a number" claim.
- **Report-quality venue: DeepResearch Bench II** (132 tasks, 9,430 rubrics),
  with the contamination controls described in §5.

Before any of that can run, the harness needs a **bench mode**. As of
2026-09-11 three of its four pieces exist: `loop.fanout_enabled`,
`loop.end_of_run` stage switches, and a per-agent usage ledger (tokens,
dollars, tool calls) with `loop.max_cost_usd` / `loop.max_tool_calls`
budget gates surfaced by `long-exposure status`. Per-task `sessions.db`
isolation is still a config-file step. §3.3 lists the original blockers
with file:line references and marks what has landed; §5 Phase 0 lists the
remaining work.

Two features stay off for the benchmark: the multi-account pool (an
exploratory feature, last used to pair Claude and Codex accounts for a
mixed-model run, and left untouched here) and the interactive transport
(self-gated in the repo). Runs use `claude -p` on a Max subscription, which
Anthropic's Help Center confirms still draws from the subscription's usage
limits (the June 15, 2026 credit change was paused before taking effect).

---

## 2. What long-exposure is, and who it now competes with

### 2.1 The design in one paragraph

Deterministic Python owns control flow; the model owns only what to
investigate. Each cycle runs three `claude -p` (or `codex exec` / `gemini`)
turns with a four-layer system prompt (philosophy, framework, operating
protocol, role) and `[INPUT: …]`/`[OUTPUT: …]` block protocol. The auditor's
VALIDATED / CONTINUE / PIVOT / INVALIDATED / COMPLETE decision is free text
consumed by the next researcher; only `[[BRANCH_COMPLETE]]` is parsed. State
is a per-instance `exploration_state.json`, a shared SQLite `sessions.db`
(FTS5), and the workspace (`plan_of_record.md`, append-only
`promise_ledger.jsonl`, `STRUCTURE.md`, reports). Whole-cycle fan-out spawns
clone processes when the researcher emits `<parallel_cycle_fanout>`. An
end-of-run pipeline (staged final auditor → staged final reporter → curator →
ZIP) runs at stop, at `max_cycles`, and every 24 h.

### 2.2 The peer set (open source, September 2026)

| Harness | Loop shape | Backends | Publishes benchmark numbers? |
|---|---|---|---|
| Claude Code `/goal` + `/loop` (2.1.139+, May 2026) | single agent, completion condition, agent teams | Claude | Yes (TB 2.1, PostTrainBench, ResearchClawBench, nanoGPT speedrun) |
| Codex CLI goal mode | single agent, subagents | OpenAI | Yes (same venues) |
| LongHorizon-Harness (AMAP-ML, arXiv 2608.01964) | manager → executor (fresh context) → auditor | Claude Code, Codex, OpenCode, dsh | Yes: WeaveBench 51.8→80.7 %, TB 2.1 69.7→77.2 % |
| AutoResearchClaw (arXiv 2605.20025) | 23-stage pipeline, debate, self-healing executor | any | Yes: ARC-Bench, +54.7 % vs AI Scientist v2 |
| ResearchClaw / OpenClaw / ARIS / EvoScientist / Nanobot | various research loops | Claude Code / Codex | Yes: all on ResearchClawBench |
| Kosmos (open impl. `jimmc414/Kosmos`; commercial 12 h runs) | literature + data loop, world model | multi | Product claims only |
| Karpathy autoresearch / pi-autoresearch / Prime Agent | modify → measure → keep/discard | Claude Code, any | nanochat val_bpb; speedrun steps |
| Arbor (hypothesis-tree, arXiv 2606.11926; no code yet) | coordinator + worktree executors | GPT-5.5 | MLE-Bench Lite 86.4 % Any Medal |
| gpt-researcher, LangChain open_deep_research, Tongyi DeepResearch, Steel Atlas | deep-research report generators | any | DeepResearch Bench, BrowseComp |

LongHorizon-Harness is the closest architectural sibling (three roles, an
independent auditor, fresh-context executors). Claude Code `/goal` is the
null-harness baseline: the same model, the same tools, no long-exposure.

---

## 3. Evaluation of the harness

### 3.1 What holds up

- **Control-flow discipline.** Termination, compaction, rotation and
  fan-out are Python decisions, not model decisions. Compaction at
  `compact_threshold × context_window` resumes the agent's own session with
  a summary prompt and drops the UUID (`exploration.py:1751-1937`). This is
  the right instinct for multi-day runs and the reason resume works.
- **Output protocol robustness.** `parse_outputs` (`conductor.py:330-389`)
  takes the longest block per name and falls back to re-parsing the Claude
  session JSONL when the final message lacks the block
  (`exploration.py:1525-1551`). The incident that motivated this (a 29 KB
  report lost to a trailing cover note) is documented in `docs/gaps.md`.
- **Auditable workspace.** The plan-of-record, append-only ledger with a
  unified status vocabulary, `promise_check`/`org_check` validators, and the
  final auditor's reconciliation events give an external grader more
  structure than most peers produce. The ledger causal summary
  (`tools/ledger_graph.py`) is a genuine differentiator for rubric items
  about provenance.
- **Provider abstraction.** Claude, Codex and Gemini share one envelope and
  one continuity model (`orchestrator.py:3278-3469`); per-agent routing
  (`agent_routing.py`) lets a benchmark pin every role to one provider and
  effort, which is what a fair comparison needs.
- **Test suite.** 37 deterministic test files cover command construction,
  parsing, resume, fan-out, curator recovery and the one-cycle loop without
  credentials. (Results in §3.5.)
- **Honest gap register.** `docs/gaps.md` records incidents with root
  causes and deliberately deferred items. Few harnesses in the peer set do
  this.

### 3.2 Where the documentation and the code disagree

These matter because a benchmark report has to describe the system that
actually ran.

| Documented behaviour | What the code does | Evidence |
|---|---|---|
| Every call gets a four-layer prompt with proximity-ranked "gems" from `sessions.db` (`docs/architecture-overview.md`, `docs/persistence-and-gems.md`) | Cycle agents get layers 1–3 plus the role block only. `_compute_gems` is called solely from the standalone REPL and its compaction path. No `gems_xml` is ever passed on the harness path. | `exploration.py:1339` vs `orchestrator.py:3923-3980, 4223, 4563` |
| Compaction produces a depth-aware XML summary with a `<catalog>` and up to 5 retries on malformed XML | Harness compaction stores a plain-text summary with hard-coded `topic="Context Summary"`, and does not retry | `exploration.py:1880-1883, 1900-1914` |
| Branchial entropy and branch-novelty scoring rank recent topics | Both compute over rows whose topic is mostly "Context Summary" or a heading regex, so the signal is largely noise | `branchial.py:74-80`, `branchial_budget.py:58-64` |
| Preemption backup timer is the "last line of defense" when all clones are stuck | It is gated behind `pool.is_active()` and a cold account; unreachable on a single account | `fanout.py:1417-1426, 1454-1462`; acknowledged in `docs/gaps.md` |
| `disable_tools` maps to `--disallowedTools all` | Conductor uses `--tools ""`; the cycle path ignores the key | `orchestrator.py:3553-3571` |
| Health events log silent fallbacks for a run | Root runs drop them unless `AGENT_INSTANCE_DIR` is exported; only clones set it | `health_events.py:40-51`, `fanout.py:1075` |
| `agent_routing.py` docstring: template ships every agent at `xhigh` | `config.yaml` ships high/medium | `agent_routing.py:51-55`, `config.yaml:376-384` |
| Final auditor stage count is capped at N=5 (`_N_MAX`, score comment "capped at 5, range [4, 12]") | `n = max(1, input_tokens // 20_000)` with no cap; `_N_MAX` is dead. 1 M tokens of inputs → ~102 auditor stages | `auditing.py:84, 187-195` |
| End-of-run docs describe `_lookup_existing_lesson` cross-run lesson merging | Function does not exist; code marks merge as a future enhancement | `docs/end-of-run-pipeline.md:503-510`, `auditing.py:429-433` |
| Explore/document rescues overwrite the stage file | They append, so a re-run document stage with an unchanged file grows `final_audit_report.md` | `auditing.py:340-345` |
| `final_audit_summary.json` key `wall_cap_hit` | Code writes and reads `wall_cap_exceeded`; documented schema also omits `figure_coverage`, `lessons_emitted` | `auditing.py:1017`, `reporting.py:236` |
| `org_check` honours domain folders declared in `STRUCTURE.md` and detects stale files | `org_check.py` only checks that `STRUCTURE.md` exists; `promise_check` walks a fixed folder tuple; no stale-file detection exists | `org_check.py:149-151`, `promise_check.py:512` |

### 3.3 Blockers for plugging into a benchmark harness

1. **Fan-out had no off switch** (fixed 2026-09-11: `loop.fanout_enabled`,
   `LONG_EXPOSURE_FANOUT`). Previously the `<parallel_cycle_fanout>`
   guidance was always injected at root and any valid 2–3 branch block
   spawned child interpreters sharing the workspace and `sessions.db`, all
   on the same account when no pool is set (`fanout.py:1024-1025,
   1144-1162`; default cap `FANOUT_MAX_BRANCHES = 3`). Fan-out is now an
   ablation.
2. **No dollar cost, no tool-call counts** (fixed 2026-09-11: usage ledger,
   see `configuration-reference.md`). Previously the Claude envelope's
   `total_cost_usd` and `num_turns` were never read, telemetry recorded
   per-call `usage` and `duration_ms` only, and clone usage was never
   merged. Now every call is recorded per agent, clones are folded in at
   fan-out collapse, and `long-exposure usage` prints the run total.
   Codex/Gemini cost is an estimate from the `pricing:` table, so for
   AstaBench route model calls through Inspect's proxy as the authoritative
   cost source.
3. **Shared memory across tasks.** `compact_db` resolves relative to the
   config file, not the instance dir (`orchestrator.py:1659-1663`), and the
   MCP `search_sessions` tool is global with no run scoping. Running 40
   benchmark tasks against one DB leaks task A's outputs, lemmas and lessons
   into task B's prompts and search results. Isolation requires a per-task
   config with an absolute `compact_db`.
4. **No result API.** `run_exploration()` returns `None`. Results are the
   last `research_brief`/`work_output`/`audit_report` in
   `exploration_state.json["results"]`, plus `reports/`. Benchmarks that
   expect a single answer file (LiveDRBench, BrowseComp-Plus) need a
   post-processing step; ResearchClawBench and E2E-Bench expect a report
   path, which fits.
5. **Mandatory workspace side effects.** Fresh runs create eight folders,
   `plan_of_record.md`, `STRUCTURE.md` and a ledger event
   (`exploration.py:3451-3464`; `workspace_bootstrap.py:227-263`), and the
   role text tells agents to maintain them and run the validators. In a
   Harbor container this is fine; in AstaBench's sandbox it is tolerable;
   on BrowseComp-style short tasks it is pure overhead.
6. **Importing the module has side effects.** `long_exposure.exploration`
   installs SIGINT/SIGTERM handlers and probes `long_exposure/data` at
   import (`exploration.py:94-128, 239-240`); module-level `_stop_requested`
   makes it one run per process. Wrapping it in Inspect or Harbor means a
   subprocess, not an in-process call.
7. **Packaged config leakage.** `_invoke_claude`/`call_claude` call
   `load_config()` with no path for Gemini auth and Codex/Gemini/local flags
   (`orchestrator.py:3312, 3500, 3519, 3533`), so some settings come from
   the packaged `config.yaml` rather than the run's `--config`.
8. **Rate-limit detection is a substring match** on `"429"`, `"quota"`,
   `"limit reached"` etc. over stdout+stderr on any non-zero exit
   (`orchestrator.py:2533-2541, 2701-2729`). On a single account this
   falls through to adaptive cooldown, which is acceptable, but a tool that
   prints "quota" will be misclassified.
9. **Hard dependencies for a headless run.** `prompt_toolkit` is imported at
   module top (`orchestrator.py:35-37`) though it is only used by the REPL;
   `launch` requires `pandoc` and `tectonic` unless `--skip-doctor`
   (`cli.py:179-190`); PDF rendering failure is non-fatal.
10. **Default timeouts.** `cli_timeout: 0` by default; only the 1800 s idle
    watchdog protects a wedged researcher/auditor. Benchmarks need an
    explicit wall budget.
11. **Operating protocol leaks interactive-only text** into headless agents
    (`/complete`, `/clear`, "ask the user", hard-coded off-limits paths,
    Wolfram guidance always on:
    `templates/operating-protocol-template.md:140-154, 205-246`). This costs
    tokens on every call and may confuse non-Claude providers.
12. **The end-of-run pipeline is expensive** (switchable since 2026-09-11:
    `loop.end_of_run` per-stage switches, `LONG_EXPOSURE_END_OF_RUN`).
    Minimum cost at `max_cycles` or stop is 9 LLM calls (periodic-report
    flush 1, final auditor 4, final reporter 3, curator 1), growing
    linearly with input volume (about 23 calls at 120 k tokens of inputs),
    with two *independent* 10 h wall caps (`limits.py:11`,
    `auditing.py:806-822`, `reporting.py:632-641`) and a rerun every 24 h
    of run time (`exploration.py:4497-4552`) that honours the same
    switches. The uncapped stage count (`_N_MAX` dead) is still open. An
    undocumented `LE_FORCE_FINAL_REPORT` env switch jumps straight to
    synthesis (`exploration.py:3664-3673`).
13. **No single machine-readable answer.** The deliverable is
    `reports/final/final_report.md` (+ PDF), `audits/final/final_audit_report.md`,
    `audits/final/final_audit_summary.json` (agent-written, schema not
    validated) and `<slug>_package.zip`. A grader must read the report;
    for RCB-style venues that is the expected shape, for answer-file venues
    it is not.
14. **`lstrip("./")` path bug** in three places (`curator.py:161-162`,
    `promise_check.py:618`, `auditing.py:147`): it strips a character set,
    not a prefix, so `../x` passes the `".."` containment check after the
    dots are removed. Harmless in a sandbox, but it is in the packaging
    path that a benchmark would ship.

### 3.4 Features held out of the benchmark

- **Multi-account pool.** Exploratory and not exercised recently; when it
  was used, it combined a Claude account and a Codex account so two model
  families could share one campaign. It is not used in this benchmark and
  the code is left untouched: `CLAUDE_ACCOUNT_POOL` / `CODEX_ACCOUNT_POOL`
  stay unset, which makes the pool inert (§3.3 item 8 notes the rate-limit
  path that remains active on a single account).
- **Interactive transport.** Self-gated in the repo
  (`docs/gaps_interactive_mode.md`) and it disables pooling, fan-out and
  usage accounting. Keep `claude_transport: headless`.
- **Billing basis.** All benchmark runs use `claude -p` on a Max
  subscription. Anthropic's Help Center article "Use the Claude Agent SDK
  with your Claude plan" states that the June 15, 2026 move to a separate
  Agent SDK credit was paused and that `claude -p` usage "still draw[s] from
  your subscription's usage limits". Report the subscription tier with the
  results, since it bounds throughput.
- **`--yolo` / `dangerously_skip_all`.** Codex and Gemini run with approvals
  bypassed by default. All benchmark venues above run agents in containers,
  which is the sandbox the README asks for.

### 3.5 Test suite and tooling status

Run on this container at commit `89b2e8f` (Python 3.11, `uv sync` clean):

| Command | Result |
|---|---|
| `uv run pytest -q` | 37 collection errors (`ModuleNotFoundError: long_exposure`): `pytest` is not declared in `pyproject.toml`, so `uv run` falls through to the system pytest outside the venv |
| `uv run --with pytest pytest -q` | **322 passed, 7 subtests passed** in 35 s |
| `uv run python -m unittest discover -s tests` (the documented command, `docs/local-setup.md:176`) | 216 run, **2 import errors** (`test_agent_routing.py`, `test_interactive_transport.py` import `pytest`); it also silently skips ~108 pytest-style function tests, including all of `test_final_pipeline_gates.py`, `test_curator_recovery.py`, `test_output_block_recovery.py` and `test_clone_termination.py` |

The suite is entirely offline: every test patches `_invoke_claude`,
`_call_agent_with_rotation` or `render_pdf`. Nothing exercises a real
provider CLI, pandoc, tectonic, D2, graphviz or Wolfram; `figure.py`, the
three figure renderers, `wolfram_batch.py`, the curator agent path,
`_commit_reconciliation_events`, the auditor wall-cap path and
`run_final_reporter.py` have no tests. The one-cycle loop, resume, fan-out
parser, curator packaging, output-block recovery and provider envelope
parsing are well covered. Fix before benchmarking: declare `pytest` as a dev
dependency and change the documented command.

Tooling on this container: `claude` is present; `pandoc`, `tectonic`, `d2`,
`dot` and `wolfram` are absent. `long-exposure launch` would refuse to start
(`setup_env.py:27, 270-278`); `start` bypasses the doctor and PDF rendering
degrades to a logged failure.

### 3.6 Verdict

As a *harness*, long-exposure's differentiators are (a) the auditor as a
per-cycle gate with a structured verdict vocabulary, (b) a durable
plan/ledger surface that survives compaction and resume, (c) a staged
final-audit pass that reconciles claims against evidence before the final
report, and (d) provider portability. Its costs are (a) heavy fixed overhead
per cycle (three long system prompts, 400 s cooldown, a reporter every three
cycles, a 4–12 stage end-of-run pipeline), (b) several documented memory
features that do not actually run on the harness path, and (c) an
observability layer that cannot produce a cost-per-task number.

The benchmark question is therefore not "is it state of the art" but "does
the auditor gate plus ledger buy rubric points that Claude Code `/goal`
alone does not, at the same dollar and wall budget". That is a testable
hypothesis, and §5 is built around it.

---

## 4. Which real benchmarks apply

### 4.1 Selection criteria

1. The unit of comparison is the harness (the leaderboard labels harness ×
   model), or at minimum peers are other harnesses on the same model.
2. Task shape matches a directive: open-ended objective, hours of work,
   artifacts (report, code, figures) graded by rubric or executable metric.
3. Public tasks, public grader, published integration path, and a cost we
   can afford (judge cost plus agent cost plus compute).
4. Contamination can be controlled (closed corpus, internet off, or
   post-cutoff tasks).

### 4.2 Recommended venues

| Venue | Tasks | What is graded | Harness-labelled peers today | Integration | Fit |
|---|---|---|---|---|---|
| **ResearchClawBench** (InternScience, arXiv 2606.07591, MIT) | 40 tasks / 10 domains; `data/` + `related_work/` + prompt → `report/report.md`, code, figures | Multimodal LLM judge vs expert rubrics; 50 = rediscovers target paper, 70+ = surpasses | Claude Code 21.5 (best), Codex CLI, OpenClaw, ResearchClaw, ARIS Codex, EvoScientist, Nanobot; ResearchHarness baseline for 17 LLMs (Opus 4.7: 20.7) | `evaluation/agents.json` entry with `cmd` using `<PROMPT>` and `<WORKSPACE>`; `rcb-eval` CLI; judge via env | **Primary.** Exact task shape; exact peer set; cheap (no GPU) |
| **AstaBench E2E-Bench-Hard** (Ai2, ICLR 2026 oral) | 50 tasks (40 test / 10 val): name + description + hypothesis → full pipeline | Binary rubric items (implementation, data, measurement, stats, ablations, docs); LLM judge; **cost per task reported** | ReAct+Opus 4.7 65.5 % @ $11.45; Asta Panda 56.5 % @ $14.49; CodeScientist 55.8 % @ $3.55; Faker 25.4 %; Qiushi Engine 81.6 % @ $15.21 (Sept 2026) | Inspect AI solver in `agent-baselines` (`scripts/new_solver.sh`); cost via Inspect model API or `record_model_usage_with_inspect()`; leaderboard accepts external submissions with openness and tool-usage labels | **Secondary.** Cost axis is first-class; long-exposure will be labelled "Custom tools" because it drives `claude -p` |
| **AARRI-bench** (arXiv 2606.07462 "Act As a Real Researcher") | Research-lifecycle tasks: literature review, hypothesis/experiment design, implementation, paper writing | pytest-style verifiers per task | Opus 4.7, Sonnet 4.6, GPT-5.3 Codex, Qwen 3.6 Plus, Kimi K2-6, DeepSeek V4; Hermes Agent as open harness | Harbor: `harbor run -d aarr/aarri-bench -a <agent>`; tasks are `instruction.md` + `task.toml` + Dockerfile + tests | **Third.** Shares the Harbor adapter |
| **Long-Horizon Terminal-Bench** (LHTB, `zli12321/LHTB`) | 46 tasks, 90-min budget, hidden verifiers, continue-until-timeout | Mean reward; 29/46 never solved; best 0.505 (Grok 4.5) | 21 frontier models under modified Harbor | Same Harbor adapter, but must use the bundled modified Harbor | **Third.** Tests sustained work, which is the harness's claim |
| **Terminal-Bench 2.1** (tbench.ai) | 89 tasks, minutes each | Pass rate; verified trajectories public | Claude Code + Fable 5 83.8 %, Codex + GPT-5.5 83.1 %, Letta Code, Terminus 2, OpenCode | Harbor `BaseInstalledAgent` | **Sanity only.** Short tasks penalise the cycle overhead; a large regression here is a bug signal, not a research result |
| **PostTrainBench** (arXiv 2603.08640) | 28 configs (4 base models × 7 evals), 10 h on 1×H100 each | Post-trained model score vs official instruct baseline; LLM judge for reward hacking; 3 runs with std | Claude Code (Opus 4.6: 23.2 %), Codex CLI, Gemini CLI, OpenCode on identical models | Public code + leaderboard | **Optional GPU.** Cleanest "harness × same model" protocol with a numeric target |
| **Prime Intellect nanoGPT speedrun** (`PrimeIntellect-ai/frontier-automated-speedrun`) | 1 task: 124M GPT to val loss 3.28 in fewest steps; internet off; frozen `verify.py`, 8 fixed seeds, noise floor 3.27859 | Steps (baseline 3,290; human 2,600; Fable 5 · claude-code 2,726) | 153 runs, 18 models, harnesses labelled (claude-code, prime-agent, native CLIs) | Public program.md, traces | **Optional GPU, expensive** (8×H200 for days). Cheaper proxy: Karpathy autoresearch nanochat with the 24 h / 3-seed protocol from arXiv 2603.24647 |
| **DeepResearch Bench II** (arXiv 2601.08536) | 132 tasks, 22 domains → long report | 9,430 binary rubrics (recall / analysis / presentation); GPT-5.5 judge (91 % agreement with humans) | Product deep-research systems; open peers gpt-researcher, open_deep_research, Tongyi | `report/<model>/idx-N.md`, `uv run python run_evaluation.py` | **Report-quality axis.** Web-enabled; contamination controls required |
| **LiveDRBench** (Microsoft) | 100 tasks: SciFacts, NovelDatasets, PriorArt, Entities, incidents | LLM-judge precision/recall/F1 on structured predictions | No public harness leaderboard | `evaluate.py --preds_file` | Optional; scientific flavour suits the harness but needs an answer-extraction step |

### 4.3 Considered and rejected

| Benchmark | Why not |
|---|---|
| BrowseComp / BrowseComp-Plus, HLE, GAIA, xbench-DeepSearch | Short-answer QA; the leaderboard variable is the model or a search agent. Long-exposure's three-role cycle adds cost without a mechanism to add accuracy. BrowseComp-Plus is worth one small run only as a search-discipline diagnostic. |
| SWE-bench Verified / Pro, DeepSWE | Software-engineering tasks with a fixed harness convention (mini-swe-agent); not the directive shape. |
| PaperBench, MLE-bench full | 24 h GPU tasks at $1k+ per paper with judge; Arbor-class systems already report here. Defer until Phase 1–2 results justify it; MLE-bench Lite (22 comps) is the affordable entry if pursued. |
| Vending-Bench 2 | Closed; third parties cannot run it. |
| WeaveBench, OSWorld 2.0 | GUI computer-use; out of scope. |
| CyberGym, SEC-bench Pro | Matches the "audit a codebase for defects" directive but the leaderboard is model-centric and PoC-verified; keep as a possible Phase 6 if the codebase-audit use case matters to users. |
| METR RE-Bench / HCAST time-horizon suite | Public and Inspect-based, and METR already showed Claude Code adds nothing over ReAct on it. Valuable, but GPU-heavy and long; consider after PostTrainBench. |
| Original DeepResearch Bench (RACE/FACT) | Superseded by DRB-II for our purposes; keep only if a submission to the older board is wanted for visibility. |

---

## 5. The plan

### 5.0 Principles

- **One model, one effort, everywhere.** Pin every role to the same
  provider/model/effort via `agent_models`; run the null-harness baseline
  (Claude Code `/goal`, or `claude -p` with the same tools) on the identical
  model. The claim under test is the harness, so the model must not vary.
- **Budget parity, two ways.** Report both equal-wall-clock and
  equal-dollar comparisons. Long-exposure's cycle overhead means it will
  spend more per task unless capped; the cap is part of the result.
- **Three seeds minimum, mean ± SEM, paired bootstrap** on per-task
  differences against the baseline (MLE-bench and PostTrainBench
  conventions).
- **Pre-register** the hypotheses in §5.6 and the configs in §6 before the
  first scored run; publish configs, traces and judge outputs with results.
- **Contamination controls.** Internet off where the benchmark allows
  (Prime Intellect precedent); closed corpora where offered; for web-enabled
  report benchmarks, log every URL fetched and report the share of tasks
  whose target source was retrieved (arXiv 2606.05241 mitigation list).
- **Disclose what ran.** Fan-out state, cooldown, cycle cap, end-of-run
  pipeline on/off, `sessions.db` isolation, and the exact commit.

### 5.1 Phase 0: bench mode and instrumentation (about 2 weeks)

Work items in the repo, each small and independently testable. Items 1, 2,
3 and 5 landed on 2026-09-11 (see `configuration-reference.md`, "Loop
knobs" and "Usage ledger, cost, and tool counts"):

1. **Done.** `loop.fanout_enabled: false` skips guidance injection and
   block parsing at root; `LONG_EXPOSURE_FANOUT=0` is the one-launch
   override. Fan-out is now an ablation.
2. **Done.** Cost capture: Claude `total_cost_usd` and `num_turns` are read
   from the envelope; Codex and Gemini fall back to a `pricing:` estimate;
   every call lands in a per-agent usage ledger persisted in state, merged
   from clones at fan-out collapse, rendered in `long-exposure status` /
   `long-exposure usage`, and rolled up by `telemetry summarize`.
   `loop.max_cost_usd` and `loop.max_tool_calls` stop a run at the next
   cycle boundary. For AstaBench, still route model calls through
   Inspect's proxy so its cost logging is authoritative.
3. **Done.** Tool-call counts: Claude from `tool_use` blocks in the
   current turn of the session transcript, Codex from completed item
   events, Gemini from `stats.tools`.
4. Per-task `sessions.db` — accept `compact_db` on the CLI and resolve
   relative to `--instance-dir`, or make `--instance-dir` imply an
   instance-local DB in bench mode.
5. **Done.** `loop.end_of_run: {enabled, final_auditor, final_reporter,
   curator}` gates the end-of-run pipeline and the daily-sync re-run;
   `LONG_EXPOSURE_END_OF_RUN=0` is the one-launch override. Still open:
   restore the documented N=5 stage cap (`_N_MAX`) so the A4 arm has a
   bounded cost, and `report_interval: 0` semantics.
6. A `long-exposure bench` subcommand: takes `--task-dir`, `--out-dir`,
   `--wall-budget`, `--cost-budget`, `--max-cycles`; writes a `result.json`
   with the report path, artifacts list, tokens, dollars, tool calls, wall
   time, cycles run, termination reason. This is the adapter surface for
   every venue below.
7. Headless hygiene — lazy-import `prompt_toolkit`; make the
   operating-protocol interactive text conditional on the transport; pass
   the run config path through `_invoke_claude`/`call_claude`; declare
   `pytest` as a dev dependency and fix the documented test command; set
   `AGENT_INSTANCE_DIR` for root runs so health events are written.
8. Adapters (thin, ~100 lines each):
   - `bench/rcb_agent.sh` for `evaluation/agents.json`
     (`cmd: "long-exposure bench --prompt-file <PROMPT> --workspace <WORKSPACE> ..."`).
   - `bench/harbor_agent.py` implementing `BaseInstalledAgent`
     (`install()` = `uv sync` + provider CLI; `run()` = the bench
     subcommand; `populate_context_post_run()` = read `result.json`).
   - `bench/asta_solver/` Inspect solver that runs the bench subcommand in
     the sample sandbox via `sandbox_agent_bridge()` (as `inspect_swe`
     does for `claude_code()`), so tool constraints and cost are recorded.
9. A smoke test: one ResearchClawBench task, one LHTB task, `max_cycles: 1`,
   no cooldown, asserting `result.json` exists with non-zero cost.

### 5.2 Phase 1: ResearchClawBench (primary, about 3 weeks)

- **Configs (arms):**
  - A0 Claude Code `/goal` baseline, same model, same tool set, same wall
    budget (the benchmark's own Claude Code entry is the reference point;
    rerun it on the pinned model rather than trusting the board).
  - A1 long-exposure bench mode, `max_cycles: 3`, fan-out off, end-of-run
    off (final report from the last cycle's reporter call).
  - A2 as A1 with `max_cycles: 6`.
  - A3 as A2 with fan-out on (cap 3, single account, shared workspace).
  - A4 as A2 with the full end-of-run pipeline (final auditor + reporter).
  - A5 as A2 with the auditor removed (`flow: [researcher, worker]`) to
    isolate the gate's contribution.
  - Optional: Codex arms (A0c, A2c) to test provider portability.
- **Model:** the current default routing (`agent_models` all Claude Opus at
  `high`), also run once with the same model at `medium` to give a cost
  point. Peers' scores on the board are on older models; the paired
  comparison is A1–A5 vs A0 on the pinned model.
- **Budget:** `cli_timeout` per agent 1 h; wall budget per task 6 h;
  cost budget $25/task (E2E-Bench-Hard peers spend $3.5–$15). Record
  termination reason.
- **Seeds:** 3 per arm per task → 40 × 3 × 6 arms = 720 runs. At an
  observed $5–$15 per run that is roughly $4k–$11k in agent spend plus
  judge cost (40 × 3 × 6 × rubric items at GPT-5.1 rates, likely <$1k).
  Start with the 10-task validation subset to calibrate before the full 40.
- **Contamination:** the benchmark's target papers are hidden but
  published; keep WebSearch enabled (peers have it) but log every search
  and report the fraction of runs in which a target-paper URL appears.
- **Analysis:** per-task rubric score; paired bootstrap of A_i − A0;
  score per dollar and per hour; termination-reason distribution; auditor
  decision distribution per cycle; ledger validity (`promise_check` green
  rate) as a secondary, harness-specific metric.

### 5.3 Phase 2: AstaBench E2E-Bench-Hard (secondary, about 3 weeks)

- Run through the Inspect solver on the 10-task validation split first,
  then the 40-task test split, 3 seeds, arms A0/A2/A4.
- Use AstaBench's standard tools where the sandbox provides them; label the
  submission "Custom" tool usage honestly since the provider CLI brings its
  own tools.
- Report score and cost per task on the same axes as the board (ReAct+Opus
  4.7 65.5 % @ $11.45 is the bar to beat on Opus-class models).
- Submit to the leaderboard with the openness label "open source" and the
  commit hash.

### 5.4 Phase 3: Harbor family (about 2 weeks, mostly compute)

- LHTB: all 46 tasks, 3 seeds, arms A0/A2, continue-until-timeout on
  (90 min). This is the "does it keep making progress for the whole budget"
  test.
- AARRI-bench: full set, 3 seeds, arms A0/A2/A5.
- Terminal-Bench 2.1: one seed, arms A0/A1 as a regression check; expect
  long-exposure to trail Claude Code; report it anyway.

### 5.5 Phase 4 (optional, GPU): a days-long numeric target

- PostTrainBench 7-config subset (one base model × 7 evals), 3 runs, arms
  A0/A2, 10 h on one H100 each ≈ 420 GPU-hours plus agent cost. Compare to
  the published Claude Code row on the same model.
- Or the nanochat autoresearch protocol (24 h single GPU, 3 seeds, val_bpb)
  as a cheaper stand-in; the Prime Intellect 8×H200 speedrun is the
  headline venue but only if a sponsor covers compute.

### 5.6 Phase 5: report quality (about 2 weeks)

- DeepResearch Bench II, 132 tasks, arms A0/A2, one seed (rubric count is
  high enough that per-task variance is lower than in Phase 1).
- Contamination: log fetched URLs; report the share of tasks where the
  source report's venue was retrieved; run a 20-task subset with WebSearch
  disabled to bound the effect.
- Present recall / analysis / presentation separately: the hypothesis is
  that the auditor and ledger help *analysis* and *presentation* more than
  *recall*.

### 5.7 Hypotheses to pre-register

- H1 The auditor gate (A2 vs A5) raises rubric score at equal wall budget.
- H2 Multi-cycle (A2 vs A1) raises score with sub-linear cost growth.
- H3 Fan-out (A3 vs A2) helps only on tasks the researcher decomposes; on
  the rest it costs more with no gain.
- H4 The end-of-run pipeline (A4 vs A2) raises presentation/provenance
  rubric items but not recall or implementation items.
- H5 Long-exposure beats Claude Code `/goal` (A2 vs A0) on
  ResearchClawBench and E2E-Bench-Hard at equal dollars; it does not on
  Terminal-Bench 2.1.
- H6 Provider portability: the A2c − A0c gap has the same sign as A2 − A0.

### 5.8 Timeline and cost

| Phase | Calendar | Agent + judge spend (rough) | Compute |
|---|---|---|---|
| 0 Bench mode | 2 weeks | — | dev laptop |
| 1 ResearchClawBench | 3 weeks | $5k–$12k | CPU containers |
| 2 E2E-Bench-Hard | 3 weeks (overlaps 1) | $3k–$8k | CPU containers |
| 3 Harbor family | 2 weeks | $2k–$5k | CPU containers |
| 4 GPU target | 3 weeks | $1k–$3k | ~420 H100-hours |
| 5 DRB-II | 2 weeks | $2k–$4k | CPU |

Total: about three months elapsed with overlap, on the order of $15k–$30k
in model spend excluding GPU rental. Phase 1 alone answers the core
question and should be funded first.

### 5.9 Deliverables

- `bench/` adapters and the `long-exposure bench` subcommand (Phase 0).
- A results repository with configs, `result.json` per run, judge outputs,
  telemetry, and the analysis notebook.
- Leaderboard submissions: ResearchClawBench PR, AstaBench upload, Harbor
  leaderboard PR for LHTB/TB 2.1.
- A short report structured around H1–H6 with the paired-bootstrap tables
  and cost curves.

---

## 6. Bench-mode configuration (reference)

Score overrides (`bench-score.yaml`, derived from `exploration-score.yaml`):

```yaml
loop:
  max_cycles: 3                 # A1; 6 for A2+
  cycle_cooldown_seconds: 0
  report_interval: 100          # effectively off; Phase 0 item 5 residual
  daily_sync_interval_hours: 0
  fanout_enabled: false         # true for A3
  end_of_run:                   # all true for A4
    final_auditor: false
    final_reporter: false
    curator: false
  max_cost_usd: 25              # per-task cost cap (checked at cycle boundaries)
flow: [researcher, worker, auditor]   # [researcher, worker] for A5
```

Config overrides (`bench-config.yaml`):

```yaml
llm_provider: claude
agent_models:
  researcher: { provider: claude, model: <pinned>, effort: high }
  worker:     { provider: claude, model: <pinned>, effort: high }
  auditor:    { provider: claude, model: <pinned>, effort: high }
  reporter:   { provider: claude, model: <pinned>, effort: medium }
cli_timeout: 3600
provider_idle_timeout_seconds: 900
claude_transport: headless
agent_teams_defaults: { enabled: false }
compact_db: /abs/path/<task>/<seed>/sessions.db
telemetry: { enabled: true, level: standard }
working_directory: /abs/path/<task>/<seed>/workspace
wolfram_path: ""
```

Environment: `CLAUDE_ACCOUNT_POOL` and `CODEX_ACCOUNT_POOL` unset; a
logged-in Claude Code CLI on the Max subscription (`claude -p` draws from
the subscription's limits); `LONG_EXPOSURE_TELEMETRY=1`;
`AGENT_INSTANCE_DIR=<instance>` (so health events are written).

Invocation (today, before the bench subcommand exists):

```bash
long-exposure --score bench-score.yaml --config bench-config.yaml \
  --instance-dir /abs/path/<task>/<seed>/instance \
  start "$(cat task_prompt.md)"
# result: <instance>/exploration_state.json["results"], <workspace>/reports/
```

---

## 7. Sources

- ResearchClawBench: arXiv 2606.07591; github.com/InternScience/ResearchClawBench
- AstaBench: arXiv 2510.21652; allenai.org/blog/astabench-update-spring-2026; github.com/allenai/asta-bench; github.com/allenai/agent-baselines; Qiushi Engine on E2E-Bench-Hard, arXiv 2609.08196
- AARRI-bench: arXiv 2606.07462; github.com/AARR-bench/AARRI-bench
- Harbor / Terminal-Bench 2.x: tbench.ai; harborframework.com/docs/agents; LHTB github.com/zli12321/LHTB
- PostTrainBench: arXiv 2603.08640
- Prime Intellect: primeintellect.ai/blog/measuring-autonomous-research; github.com/PrimeIntellect-ai/frontier-automated-speedrun
- METR: metr.org/notes/2026-02-13-measuring-time-horizon-using-claude-code-and-codex; metr.org/blog/2026-1-29-time-horizon-1-1
- DeepResearch Bench II: arXiv 2601.08536; github.com/imlrz/DeepResearch-Bench-II
- Search-time contamination: arXiv 2606.05241
- LongHorizon-Harness: arXiv 2608.01964; AutoResearchClaw: arXiv 2605.20025; Arbor: arXiv 2606.11926; autoresearch HPO study: arXiv 2603.24647
- Anthropic Agent SDK billing change paused: support.claude.com/en/articles/15036540
