# Closeout: the four advanced-model features

**Status: CLOSED 2026-09-25.** Everything in this plan was executed (§6).
Deeper live testing is deferred to a later, dedicated pass by operator
decision — this was judged rigorous enough for now.

Branch `claude/long-exposure-tiered-memory`, 650 tests passing, tree clean,
wheel builds. **Nothing on `main`, and no merge is proposed:** the standing
instruction that the branch needs live testing before it goes near `main`
still holds, and one smoke cycle is not that.

## 0. Where things actually stand

| | State |
|---|---|
| Code | Five stages implemented; seven post-implementation defects found and fixed |
| Tests | 650 pass under `uv run pytest`. Unittest discovery: 519 pass, 3 pre-existing `import pytest` loader errors (unchanged from baseline) |
| Docs | Four feature blocks in `configuration-reference.md`; thins/never-thins in `soft-guidance.md`; spend visibility in `parallelism.md`; benchmark plan revised |
| Defaults | All four features ship **off**. Prompt and flow are byte-identical to pre-feature with them off (measured against the pre-feature commit) |
| Branches | `main` = `89b2e8f`. `claude/long-exposure-benchmarking-pikxm2` = `3ebce66`, an **ancestor** of the work branch, so everything is on one branch |

## 1. Two documentation gaps I owe (no decisions needed)

Both are things I said were done and are not.

1. **`docs/usage-guide.md` has no gate walkthrough.** The plan's §5.3 says it
   gains one; it has zero mentions of `startup_gate`. An operator reading the
   usage guide currently cannot discover the feature at all. Fix: a short
   walkthrough — enabling it, what the four questions do, the flags for
   headless use, and that `resume` never re-asks.

2. **`docs/gaps.md` item 54(a) is now half-answered and does not say so.**
   That entry records that graceful preemption "writes the graceful-stop file
   but never escalates to SIGTERM/SIGKILL, so a clone wedged inside a
   provider call can hold the barrier toward the 10h cap". The spend-kill path
   now does exactly the bounded escalation that entry asks for
   (`SPEND_KILL_GRACE_SECONDS`, then SIGTERM, then SIGKILL). The remaining
   work is to apply the same pattern to preemption. Fix: record the precedent
   so the next person copies it rather than re-deriving it, and keep the entry
   open for the preemption path.

Also worth adding to `gaps.md` as explicit deferred entries, because they are
currently only recorded inside a plan doc:

- Cycle planning's live mileage (one cycle, researcher declined to plan).
- The interactive Claude transport has not been exercised with any of the
  four features.

## 2. The live smoke test — the one gap that blocks the benchmark

This is the highest-value remaining item, and it is now possible here:
`claude` **2.1.282** is installed in this container with credentials at
`/root/.claude`. Everything so far used a stubbed provider, so no real model
has ever emitted a `<cycle_plan>` block.

It needs approval because **it spends the operator's Max-plan quota** through
`claude -p`, which is the design's intended billing path but is not mine to
spend unasked.

### Proposed scope, deliberately small

| Setting | Value | Why |
|---|---|---|
| Task | one small, self-contained research directive in a scratch workspace | Not a benchmark task; this tests the harness, not the science |
| `loop.max_cycles` | **4** | The audit floor is 2, so cycle 3 is the earliest the floor can fire. Four cycles shows it firing and then recovering |
| `loop.cycle_planning.enabled` | **true** | The thing being tested |
| `loop.fanout_enabled` | **false** | Keeps it single-process and cheap. The fan-out kill path is already covered by the real-subprocess test |
| `loop.end_of_run` | **false** | No final auditor, reporter or curator — they are the expensive stages and are not what is under test |
| `usage_allowance` | **on, small cap** | A hard bound on the worst case, and it live-tests the kill path for free |
| `model_profiles` | **on** | Should resolve to `standard` on Opus; confirms the family logic against a real resolved model id |
| Wrapper | wall-clock watchdog + stack dump, as used in the stress runs | So a wedge cannot burn quota unattended |

### What it answers, and what counts as a pass

| Question | Pass looks like |
|---|---|
| Does a real model emit a `<cycle_plan>` block at all? | At least one cycle planned rather than fixed |
| Are the blocks well-formed? | `cycle_plan_rejected` at or near zero |
| Is the scheduling sensible? | Planned shapes match what the brief asked for, read from transcripts |
| Does the audit floor fire? | `cycle_plan_audit_forced` at cycle 3 |
| Does escalation work end to end? | `[[REQUEST_AUDIT]]` honoured if it appears (may not — not a failure) |
| Does the profile resolve correctly? | `standard` on Opus, no knobs set |
| Does the spend limit kill cleanly against a real provider? | If the cap is hit: marker written, end-of-run skipped, exit 3 |

A **null result is a real result**: if the model never emits a block, that is
the finding, and it goes into the benchmark plan as such rather than being
fixed by rewriting the guidance until it does. Rewriting guidance to get the
feature to trigger, then benchmarking it, would be tuning against the
measurement.

### What it does not cover

Fan-out with live clones, the interactive transport, account pooling, and
anything at benchmark scale. Those stay deferred and recorded.

## 3. Sequencing

1. The two doc fixes (§1) — small, no approval needed.
2. The live smoke test (§2) — **needs approval on scope and spend.**
3. Fold the smoke-test findings into `docs/rcb-benchmark-plan.md` §2 gap 5,
   §9's planning kill criterion, and `docs/advanced-model-modes-plan.md` §7's
   "what remains untested".
4. Final verification: full suite, unittest discovery, wheel, validators.
5. Commit and push. **Stop there** unless a PR or merge is asked for.

## 4. Deliberately out of scope

- **No merge to `main`.** The standing instruction is live testing first, and
  a four-cycle smoke test is not the live testing that justifies a merge of
  36 commits.
- **No pull request** unless asked.
- **No account-pool work.** Untouched by design, as instructed.
- **No fix for the 3 unittest-discovery errors.** Three modules `import
  pytest`, which the bare interpreter lacks. Pre-existing, cosmetic, and
  `uv run pytest` is the real runner. Fixing it means either adding skip
  guards to tests that legitimately need pytest, or adding pytest to the
  system interpreter — neither earns its keep.
- **No further feature work.** The four features are done; the next thing
  they need is mileage, not more surface.

## 6. Executed (2026-09-25)

| Step | Outcome |
|---|---|
| §1 doc gaps | Both closed: `usage-guide.md` now has the gate walkthrough; `gaps.md` records the escalation precedent against item 54(a) and carries the two untested seams as explicit entries |
| §2 live smoke test | Ran. One cycle in 990 s, then killed by the deliberate $6 cap. Four findings, recorded in `advanced-model-modes-plan.md` §7 |
| §3 fold findings | Done, into `advanced-model-modes-plan.md` §7 and `rcb-benchmark-plan.md` §2 gap 5, §4 item 4, §8 and §9 |
| Branches | Left as they are, per decision |
| PR | Not opened, per decision |
| Merge to main | Not done, and not proposed |

### What the smoke test bought

- **The spend limit is verified against a real provider**, not a stub:
  tripped, marker written, end-of-run skipped, exit 3, state resumable.
- **A measured overshoot**: $7.81 against a $6.00 cap, 130%, because the
  crossing turn was a $3.45 compaction call. Now in the operator docs.
- **Cycle planning's plumbing is confirmed and its judgement is not.** The
  guidance reached the researcher's prompt; the researcher emitted no block.
  Permitted, plausibly correct for a single-build-step brief, and only one
  sample.
- **A live reproduction of the benchmark plan's own alias trap**: `model:
  opus` served `claude-opus-5-5`. The plan's served-model assertion is now
  demonstrably load-bearing rather than precautionary.
- **A real per-cycle cost and wall-clock figure**, which suggests the
  benchmark plan's per-task cost range is optimistic by roughly 2–4x on
  wall-clock grounds (pricing-independent). §8 now says to re-derive it
  during the smoke test rather than trusting either number.

### Still open after this closeout

1. **The planner's judgement.** One live sample, a decline. Needs cycles
   2-4+ on a task with a genuine sequential dependency. Blocked on spend,
   not on code: ~$8/cycle on Opus 5.5 means the original 4-cycle scope costs
   roughly $30, not the $6 that was capped.
2. **The interactive transport** with any of these features.
3. **Live fan-out** with real clones (the kill path is verified against real
   subprocesses, but not against real clone *runs*).

## 5. Open questions for the operator

1. **Run the live smoke test here, on the Max plan?** If yes: which model,
   and what cap on the spend limit?
2. **Branch names.** Everything is on `claude/long-exposure-tiered-memory`;
   `claude/long-exposure-benchmarking-pikxm2` is an ancestor and now
   misleadingly named. Leave both, or fast-forward the benchmarking ref so
   the two agree?
3. **A PR against `main`** for review without merging — wanted, or not yet?
