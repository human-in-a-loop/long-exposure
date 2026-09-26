# Git federation: many long-exposure instances, one repository

**Status: the sync layer is not built. Two pieces of it are.**

| Piece | State |
|---|---|
| Operator identity on the ledger (§7.1) | **Built.** `long_exposure/federation.py` |
| Conflict radar (§4.3) | **Built**, off by default. `long_exposure/conflict_radar.py` — **read-only git** |
| Slice-name canonicalisation (§6.2) | **Built**, no consumer yet — the claims registry it is for is not built |
| Fetch / rebase / commit / push at cycle boundaries (§4.1, §4.2) | **Not built.** No design change, just unwritten |
| Claims registry (§6) | **Not built**, deliberately — §8 says run the experiment first |
| Per-operator memoirs (§5.2) | **Not built** |

So the harness now runs git, and it only ever **reads**: `rev-parse`,
`merge-base`, `merge-tree`, `diff`, `status`, plus `fetch`, which updates
remote-tracking refs and no working file. It does not commit, push, rebase,
checkout, reset or stash, and
`tests/test_git_federation_doc.py::GitIsReadOnlyTests` fails the build if a
write ever appears. That invariant matters because the radar runs on the cycle
path against the operator's live workspace.

Sections describing unbuilt parts say so inline. The git mechanics in §9 were
verified against real repositories rather than reasoned about.

The decisions this rests on were taken in
`docs/worktrees-hooks-swarms-plan.md` §3, §4 and §7. This document is the
walkthrough: what the repository looks like, what each operator's cycle does,
which files converge and how, where the lock is, and what breaks.

---

## 1. The scenario, precisely

Operator A on machine A and operator B on machine B each have long-exposure
installed. Both point `working_directory` at their own clone of the same
GitHub repository. Both run ordinary three-role cycles — including a local
2–3 branch fan-out when their researcher asks for one. Neither knows the
other's process exists; they share nothing but the remote.

Scale comes from *N operators × their small fan-outs*. Nothing local gets
bigger: `FANOUT_MAX_BRANCHES` stays at 3, depth stays capped at 1, and no
worktrees are involved at any level (plan §4.1).

What is new is a **git sync layer**: a cycle-boundary seam that fetches,
rebases, commits and pushes, and turns what it finds into an input for the
next researcher.

---

## 2. What actually lives where, today

This is the part that decides how hard federation is, and it is already
mostly favourable. Three zones, and only one of them is shared.

### Zone 1 — the workspace (`config.working_directory`) → **in the repo**

Everything `long_exposure/paths.py` routes. Verified against
`paths.py` and `workspace_bootstrap.py`:

| Path | Writer | Shape |
|---|---|---|
| `plan_of_record.md` | researcher, via bootstrap template | one file, edited |
| `STRUCTURE.md` | bootstrap | one file, rarely edited |
| `promise_ledger.jsonl` | every role, `append_ledger_event` | **append-only JSONL** |
| `MEMOIR.md` | auditor | **one file, rewritten every cycle** |
| `memoir/history/` | harness | one new file per changed cycle |
| `reports/cycles/`, `reports/final/` | reporter, curator | new files |
| `audits/`, `audits/final/` | auditor, final auditor | new files |
| `data/`, `scripts/`, `tests/`, `tools/`, `docs/`, `stale/` | worker | ordinary source |

### Zone 2 — the instance dir (`--instance-dir` / `AGENT_INSTANCE_DIR`) → **local, never committed**

`exploration_state.json`, `health_events.jsonl`, `branchial_budget.jsonl`,
`fanout_assignment.json`, `manager.lock`, `manager_assessments/`,
`clone_local.log`, the `long-exposure.stop` / `.clear` / `.guide` control
files, and `output/`. This is per-process run state. Committing it would
make two operators fight over a file that describes one machine's progress.

The harness's own `.gitignore` already excludes `instances/`.

### Zone 3 — machine-global → **outside any repo**

`~/.long-exposure/runs.jsonl` (the run registry the startup gate reads) and
`~/.local/share/auto-compact/sessions.db` (SQLite + WAL, per operator).

Worth stating because plan §4.2 called for "an explicit `.gitignore` entry"
for `sessions.db`: it does not need one. It lives under `~/.local/share`,
never inside `working_directory`, so it cannot be committed by accident.

**The consequence:** federation only has to make Zone 1 converge. Zones 2
and 3 are already private by construction, which is a piece of luck the
design should not spend.

---

## 3. Branch topology

```
main                         ← the shared trunk; nobody's run commits here directly
long-exposure/alice/run-2026-09-26T140312Z
long-exposure/bob/run-2026-09-26T141055Z
long-exposure/bob/run-2026-09-27T090001Z
```

One branch per run, namespaced by operator. `run_id` already exists in the
state file, the run registry and every telemetry event, so the branch name
needs no new identifier.

**The operator segment is load-bearing, not cosmetic.**
`workspace_bootstrap.derive_run_id` is:

```python
return f"run-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ')}"
```

A bare UTC timestamp to the second, with no host component and no
randomness. Two operators who start within the same second get the *same*
`run_id`. Namespacing the branch by operator makes that collision harmless
at the git level — but see §7.1, because the collision does not stay
harmless inside the promise ledger.

Convergence to `main` is a pull request, reviewed by a person. That is
deliberate: `git merge` plus review is a tool built for N-way convergence
between parties who are not in the same process, and the federation should
use it rather than reinvent a reduction step (plan §4.1).

---

## 4. One operator's cycle, end to end

Two new seams. Both sit exactly where the harness already has a transaction
point, so neither invents a lifecycle stage.

### 4.1 Before the roles run — `exploration.py` ~4251, after `cycle += 1`

*Steps 1–3 are **not built**. Step 4's seam exists and the radar already uses it.*

```
1.  git fetch origin
2.  git rebase origin/<shared-branch>       # or merge; never --strategy=ours
3.  read .long-exposure/claims/*.json
4.  build a <federation> block
```

The block joins the list already assembled at `exploration.py:4358`:

```python
parts = [p for p in (fanout_guide, sibling_block, anti_patterns_block,
                     conflict_block, guidance) if p]
base_live_guidance = "\n\n".join(parts) if parts else "[No live guidance this cycle.]"
```

`conflict_block` is the radar of §4.3, already in that list. A claims block
would slot in beside it for the same reason: pointer-shaped cross-process
visibility, gated to the cycles where it can be acted on. The researcher reads
that another run holds `spectral-theory` with a claim 40 minutes old, and picks
something else.

A rebase conflict is surfaced *here*, as an input, not raised as an error
(plan §7). The harness never auto-resolves it. Conflicts on generated
artifacts are usually "both are fine, keep both"; conflicts in source are a
research disagreement between two runs, and a strategy flag is the wrong
instrument for a disagreement.

### 4.2 After the roles run — `exploration.py` ~5270, the "Status file + state" block

***Not built.** The seam is identified; nothing writes to it.*

```
5.  git add <workspace paths only>
6.  git commit -m "cycle <n>: <topic>  [run_id, operator, harness commit]"
7.  git push origin HEAD:long-exposure/<operator>/<run_id>
8.  on rejection: fetch, and let the NEXT cycle's step 1–4 see the new state
```

This lands immediately after `save_state(...)` and before the `cycle_end`
telemetry event, which is where the cycle already becomes durable: state
written, status file updated, memoir archived. Committing there means every
commit is one coherent cycle and `git log` is a readable run history.

**The harness commits; the agent does not** (plan §7). Deterministic,
harness-authored commits at cycle boundaries are auditable and reproducible.
Letting agents run `git commit` would put provenance under model control and
make history a function of prompt adherence. This is carried by prompt
guidance and by the harness being the thing that runs `git` — not by a hook
that blocks `git commit`, which went when the `PreToolUse` fence did.

A rejected push is not an error either. It means someone else got there
first; the next cycle's fetch will see it.

---

### 4.3 The conflict radar — BUILT, read-only

The one part of the cycle that exists. Off by default
(`federation.conflict_radar.enabled`), root process only, researcher cycles
only, and it emits nothing when there is no overlap.

**Why it is worth building before the sync layer.** Across 33,596
agent-authored pull requests in 2,807 repositories, cross-agent PR pairs
conflict at **41.7%**, against 19.8% for pairs from the same agent. 40.2% of
repositories had agent PR pairs overlapping in time, and those pairs were
79.4% of all agent PRs. Of the conflicts, 57.6% are content, 26.8%
modify/delete and 15.1% add/add — so roughly **42% are structural**, the kind
no merge driver resolves because they are a disagreement about whether a
component should exist. All of it is normally discovered at merge time, which
is the most expensive moment to discover it.

Those figures are from ["When Agents
Collide"](https://codex.danielvaughan.com/2026/07/28/agent-pr-merge-conflicts-concurrent-coding-agents-codex-cli-worktree-isolation-coordination-defence/)
(2026-07-28), corroborated there against a larger set of 142,000+ agent PRs
across 59,000+ repositories at a 27.67% overall conflict rate. They are that
study's measurements, **not** this repository's — unlike every other number in
this document, which was measured here.

**The correction this implementation forced.** The plan was a `git merge-tree`
pre-flight, and `merge-tree --write-tree` is the right instrument: it predicts
a three-way merge without touching the working tree, the index or HEAD. On its
own it is nearly useless *here*, and testing it said so immediately:

```
remote moved data/spectral.py; workspace holds an uncommitted edit to it
merge-tree HEAD origin/main   ->  CLEAN
```

Because the harness does not commit, HEAD sits wherever a human last left it
while all of the cycle's work is uncommitted. So there are two signals:

| Signal | Question | Fires today? |
|---|---|---|
| `merge_tree_conflicts` | Would the *committed* state conflict with the shared branch? | Only once something commits — forward-looking, for when §4.2 lands |
| `dirty_overlap` | Which paths has the shared branch changed since the merge base that are **also** locally modified, staged or untracked? | **Yes.** This is the one that works on a normal run |

The block names the overlapping paths, groups them into regions via §6.2's
canonicaliser, and says plainly that it is a forecast and nothing is blocked:

```
<shared_branch_overlap>
  Another operator has changed work you are also touching. This is a
  FORECAST against origin/main, not an error, and nothing is
  blocked. Treat it as a reason to choose differently this cycle, or
  to reconcile deliberately rather than at merge time.
  regions: data
  the shared branch changed these, and you have uncommitted changes in them:
    data/spectral.py
</shared_branch_overlap>
```

**Failure posture: advisory, never a gate.** Every git failure degrades to "no
opinion" and the cycle proceeds — a forecast that could halt a run would be
worse than the conflicts it predicts. Verified for: not a git repository, a
missing directory, an empty repository, no remote, a non-existent shared
branch, unrelated histories, a detached HEAD, a junk timeout value, and an
unreachable remote. The last one is the interesting case: it still forecasts,
from whatever refs are local, and marks the block stale — yesterday's refs beat
nothing.

**Read-only is tested, not asserted.** One test snapshots HEAD, the current
branch, `git status`, the stash list and two file bodies, runs a full scan, and
compares. `fetch` is the only side effect and `fetch: false` removes it. A
second test pins that `conflict_radar._git` is the *only* place in the package
that invokes git at all — one chokepoint, one timeout, one never-raises
contract.

**One judgment call worth recording.** The block reaches the researcher as part
of `[INPUT: live_guidance]`, the same path `<sibling_reports>` uses, and unlike
`<sibling_reports>` it gets **no explanatory text in the score**. The house
style is to explain a conditional block in the role, and that was considered:
`<sibling_reports>` needs it because "do not read these by default" is not
guessable. `<shared_branch_overlap>` carries its own instruction inline, so
adding role text would tax the prompt on every run — including every
single-operator run, where the block never appears — to restate what the block
already says.

## 5. How each file class converges

Four classes, and the class decides the mechanism. This is the whole of
§4.2's third primitive, worked out.

### 5.1 `promise_ledger.jsonl` — union merge, and it is correct

One line in `.gitattributes`:

```
promise_ledger.jsonl merge=union
```

Union merge concatenates both sides' added hunks instead of conflicting. For
this file that is not a convenient approximation, it is *right*, and three
properties of the existing code are why:

- **Every line is newline-terminated.** `append_ledger_event` writes
  `json.dumps(event, ...) + "\n"`, so a union merge can never join two JSON
  objects into one unparseable line.
- **`event_id` is a UUID4** (`exploration.py:3034`, `manager.py:481`), so it
  is unique across machines with no coordination. Duplicate lines dedupe;
  distinct lines cannot collide.
- **The reader sorts.** `summarize_ledger` groups by `milestone_id`, sorts
  each group by `ts`, and re-sorts the selection by `(ts, milestone_id)`.
  Byte order in the file is irrelevant — which matters, because union merge
  produces *insertion* order (all of A's lines, then all of B's), not
  chronological order. Verified in §9.

This is the harness's single biggest existing asset for federation, and it
has the right shape by accident rather than by design.

### 5.2 `MEMOIR.md` — never merged; the shadow pattern, one level up

A single file rewritten every cycle is the worst possible merge shape. It
would conflict on essentially every sync, and a conflict in a memoir is not
resolvable by a machine: it is two different narratives of the same run.

The harness already solved this exact problem for fan-out. Clones write
**shadow memoirs** to their own instance dir and the conductor folds them via
`branch_memoirs` at the barrier (`memoir.py:107`, `memoir.py:395`). The same
pattern rescopes one level up:

- `MEMOIR.<operator>.md`, one per operator, each written only by its owner.
- Folded for *reading* — concatenated into the memoir input — never merged.
- `memoir/history/` is already one new file per changed cycle, so it
  federates without any change at all.

The work is a rename and a re-scope of a mechanism that exists and is
tested, which is why this is the cheapest hard problem on the list.

### 5.3 `plan_of_record.md` — the genuine contention point

Neither append-only nor per-operator. Both operators' researchers want to
edit it, and it is the file where slice boundaries would naturally be
recorded (plan §4.4).

No mechanism fixes this; it needs a convention. The least-bad one: the plan
of record is **claimed like any other slice**, so exactly one run holds the
write lease at a time, and everyone else reads it. That keeps the contention
visible in the claims registry instead of discovered at rebase time.

### 5.4 Ordinary source and artifacts — plain git

`data/`, `scripts/`, `tests/`, `reports/`, `audits/`. New files never
conflict. Edits to the same file conflict, and should: that is two runs
working the same thing, which the claims registry is meant to prevent and
§4.1 surfaces when it fails.

---

## 6. The lock: git's push rejection, and its exact limit

A lease file on operator A's disk is invisible to operator B, so the claim
registry has to be committed:

```
.long-exposure/claims/<slice>.json      →  {slice, operator, run_id, claimed_at, expires_at}
```

### 6.1 What git gives you for free

Two operators claim the same slice. Both commit
`.long-exposure/claims/spectral-theory.json` with their own contents. Both
push. **A git remote accepts exactly one of two conflicting pushes** — the
second is rejected as non-fast-forward. The loser fetches, hits an
add/add conflict on that exact path, sees the existing claim, and picks
something else.

No server, no daemon, no consensus protocol, no lease-renewal heartbeat.
Verified end to end in §9.2: the push is rejected, and the rebase reports
`AA .long-exposure/claims/spectral-theory.json`.

This is the design point that makes federation tractable where a local swarm
scheduler was not.

### 6.2 Where it stops working — stated plainly

The lock is on the **path**, not on the meaning. §9.2 also verified the
failure: operator B claiming `spectral_theory.json` while A holds
`spectral-theory.json` is accepted with no rejection, no conflict, and two
live claims on one topic.

So the registry detects collisions only between operators who already agree
on the slice's name. Three options, none free:

| Option | Cost |
|---|---|
| Canonicalise slice names (slugify, lowercase, strip separators) | Catches near-misses, not synonyms. Cheap, partial. **Built** — `federation.canonical_slice` |
| One shared `claims.json` index instead of a file per slice | *Every* claim now contends on one path, so every double-claim is detected — and so is every concurrent claim of different slices. Detected, but noisy |
| Nothing; let the auditor notice duplicated work | Honest, and cheapest, and what §8 argues for first |

`canonical_slice` folds the near-miss half: separators, case and simple
plurals collapse to one spelling, so `spectral-theory`, `spectral_theory`,
`Spectral Theory` and `SPECTRAL--THEORY` are one name. It does **not** fold
synonyms — `spectral-theory` and `eigenvalue-bounds` stay distinct — and a test
asserts that, so the limit is not quietly forgotten.

Two things went wrong writing it, both worth recording because they are the
shape of the risk in this kind of helper. A first depluralisation rule folded
`bias` to `bia`; the fix is an explicit list of Latin/Greek endings that look
plural (`-ss`, `-is`, `-us`, `-as`, `-os`). And a `-ses` rule guessed wrong
about half the time, because `analyses`→`analysis` and `bases`→`base` share an
ending and differ in the answer, so it is replaced by a short explicit map. The
governing principle: **over-stemming is worse than under-stemming**. Two
distinct slices folding together means one operator's claim silently matches
another's and neither is told, where a surviving near-miss is merely the status
quo.

It has no consumer yet beyond grouping radar paths into regions. The claims
registry that needs it is not built, per §8.

Slice granularity is the whole ballgame (plan §4.4), and it is the question
the first experiment should answer with data rather than with a design.

---

## 7. What breaks, and what the harness does about it

| Failure | What happens | Harness response |
|---|---|---|
| **Push rejected** | Someone pushed first | Not an error. Fetch; next cycle's step 1 sees it |
| **Rebase conflict, generated artifact** | Two runs wrote the same report path | Surface to the next researcher. Never auto-resolve — "keep both" is usually right but it is not the harness's call |
| **Rebase conflict, source** | Two runs edited the same code | Surface. This is a research disagreement; it wants a person or an auditor |
| **Same-second `run_id`** | Two operators, identical `run_id` | Branch names stay distinct via the operator segment; the ledger now carries `operator` (§7.1, closed). `run_id` itself is deliberately unchanged |
| **Offline operator** | Fetch fails | The cycle must run anyway. Local commits queue; the next successful fetch produces a bigger rebase. A run that halts because GitHub is unreachable is worse than a run that diverges for an hour |
| **Divergent harness versions** | A on this branch, B on `main` — different prompt text, different state fields | Record the harness commit in the commit trailer and the run registry, so a mismatch is visible in the artifacts rather than inferred from behaviour (plan §4.4) |
| **Operator B invalidates A's premise** | A's last four cycles rest on something B just disproved | **Unsolved.** A claims registry stops them editing the same files; it does nothing about this. The shared promise ledger is the only channel, and surfacing a conflicting *finding* is strictly harder than surfacing a file conflict |

### 7.1 The identity gap — CLOSED

**What was wrong.** `promise_ledger.jsonl` events carried `run_id`, `cycle`,
`agent` and `milestone_id`, and nothing that distinguished two operators.
`summarize_ledger` keeps the *latest event per `milestone_id`*, so when two
operators reached the same milestone the later event hid the earlier one in the
summary injected into the next cycle. Measured before the fix, on a merged
ledger with two `validated`/`high` events on `spectral/bound`:

```
Total events: 2, distinct milestones: 1, shown: 1
- [spectral/bound] validated/high (cycle 3, auditor, 2026-01-03T00:00:00Z)
    BOB bound is 7.9
```

Alice's contradicting result was not flagged, not merged, and not shown. It was
simply absent — the two operators disagree, and the mechanism meant to carry
the disagreement dropped one side silently.

Two backfills softened this and it is worth being precise about which. Events
with `status: in-progress`, and `validated`/`superseded` events at `low` or
`provisional` confidence, are always included regardless of recency — so a
duplicated `_run/start` survived, and so did a hedged finding. It was
specifically the **confident, settled** events, the ones most worth
reconciling, that collided.

**What was built.** `long_exposure/federation.py`, plus wiring:

- `operator_name` resolves from `LONG_EXPOSURE_OPERATOR`, then
  `federation.operator` in config, then the slugified hostname, then `local`.
  The hostname default is deliberate: the point of the field is distinguishing
  two machines, and requiring configuration would leave the common case — two
  people who each just installed the harness — with no identity at all.
- `append_ledger_event` stamps `operator` when an event lacks one. It is the
  single chokepoint every writer already goes through: the cycle loop, the
  manager, the bootstrap event, and the agent-facing `ledger_append` tool. So
  agents never have to know or supply it, and an existing value is never
  overwritten — a clone's shadow ledger is concatenated in by the fan-out
  conductor, and re-stamping there would relabel another process's events.
- `summarize_ledger` groups by `(operator, milestone_id)`.
- `orchestrator._add_federation_env` exports the resolved name to agent
  subprocesses. This is not decoration: without it the `ledger_append` tool
  would fall back to the hostname while the harness used config, and one
  machine would write under two operator names with every shared milestone
  looking contested.

**A second defect the same key change closed.** `anti_patterns._select`
surfaces a milestone whose *latest* event is `invalidated`. Keyed on
`milestone_id` alone, another operator's later non-invalidated event silently
stopped the warning being surfaced at all. Verified: Alice's "monte-carlo
diverges" now survives Bob's later attempt at it, while Alice's *own* later
validation still clears it.

**Two things this section originally got wrong.**

1. It proposed namespacing `milestone_id` as `<operator>/<milestone>`.
   Implementing it showed why a separate field is right:
   `promise_check.RESERVED_NAMESPACES` matches milestone **prefixes** (`_run/`,
   `_plan/`, `_archive/`, ...), which an operator segment defeats — and agents
   write `milestone_id` themselves, so it would put identity under prompt
   adherence.
2. It listed `cycle` being per-operator as part of the gap. That is true and
   is **not** fixed: two operators both have a "cycle 3". It is now visible
   rather than invisible, because the operator appears on the line next to it,
   which is as far as a schema change can take it. A reader that genuinely
   needs a merged timeline should sort by `ts`.

**What was deliberately not changed.** `run_id` is still a bare UTC timestamp
with no host component. §7.1 listed three symptoms of one cause, and the cause
is the missing operator field; a new `run_id` format would invalidate the run
registry, the state file and every telemetry row for no additional benefit.

**Backward compatibility.** A missing `operator` reads as the **local**
operator, not as a distinct empty one. Without that rule, resuming a
pre-upgrade ledger would split every milestone into a legacy group and a new
group and report two operators where there is one. A single-operator ledger
also renders no operator column at all, so the summary text injected into the
prompt is byte-identical to before.

Also checked: `ledger_graph` keeps the whole event chain per milestone rather
than selecting a latest, so it has no masking bug and needed no change; and
`promise_check` has no strict-field validation, so a stamped ledger passes it
clean.

---

## 8. Build order

1. ~~**Ledger identity** (§7.1)~~ — **DONE.** Small, no git involved, and
   required for anything downstream to be readable.
2. ~~**Conflict radar** (§4.3)~~ — **DONE**, off by default. Read-only, so it
   could ship ahead of the sync layer, and it is the piece that pays for itself
   soonest: it converts a 41.7% cross-agent conflict rate from a merge-time
   surprise into a pre-cycle input.
3. **The sync layer**: one module, one config block, the two cycle-boundary
   seams of §4.1 and §4.2. Branch-per-run and cycle-boundary commits only — no
   claims, no leases. **This is the next thing.**
4. **The experiment** (plan §4.5). Two operators, one repo, one small shared
   directive, both on the same harness commit, *no claims registry*. Count: how
   many pushes are rejected, how many rebases conflict, what they conflict on,
   and whether the two runs produce complementary or duplicated work. The radar
   makes this experiment much better instrumented than it would have been —
   every predicted overlap can be compared against what actually conflicted.
5. **Then** decide the claims registry's shape from §6.2 with data, and
   `MEMOIR.<operator>.md` if the experiment shows memoir conflicts actually
   dominate.

Steps 4 and 5 are in that order on purpose. The claims registry is the part of
this design with the most guesswork in it, and the experiment is cheap.

The config block for what is built (`long_exposure/config.yaml`), which is real
and read, unlike the `git_sync:` sketch this section used to carry:

```yaml
federation:
  operator: ""              # blank derives from the hostname
  conflict_radar:
    enabled: false
    remote: "origin"
    shared_branch: "main"
    fetch: true             # false = forecast from local refs, no network
    timeout_seconds: 30
    max_paths: 20
```

`federation.operator` applies whether or not the radar is on: the ledger stamp
is unconditional, because a ledger that is federation-ready by default costs
nothing on a single-operator run.

## 9. The git mechanics, verified

Run against real repositories rather than reasoned about, because the whole
design rests on two claims about git's behaviour.

### 9.1 Union merge on the ledger

Two clones append two events each to a `merge=union` JSONL file, A pushes
first, B's push is rejected, B rebases:

```
rebase clean
{"event_id":"base","ts":"2026-01-01T00:00:00Z"}
{"event_id":"a1","ts":"2026-01-02T00:00:00Z"}
{"event_id":"a2","ts":"2026-01-04T00:00:00Z"}
{"event_id":"b1","ts":"2026-01-03T00:00:00Z"}
{"event_id":"b2","ts":"2026-01-05T00:00:00Z"}
5 lines, 0 duplicates
```

No conflict, nothing lost, nothing duplicated — and the order is
A-then-B, **not** chronological (`a2` at 01-04 precedes `b1` at 01-03). That
is exactly why §5.1 depends on `summarize_ledger` sorting by `ts`.

### 9.2 The claim race, both outcomes

Same slice path, both operators:

```
PUSH REJECTED (good)
CONFLICT on the same slice -> B is told
AA .long-exposure/claims/spectral-theory.json
```

Different slice name, same topic:

```
ACCEPTED: two claims on the same topic, no git-level collision
.long-exposure/claims/spectral-theory.json
.long-exposure/claims/spectral_theory.json
```

Both halves of §6 hold: the lock is real, and it is a lock on the path.
