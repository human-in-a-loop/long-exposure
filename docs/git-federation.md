# Git federation: many long-exposure instances, one repository

**Status: designed, not built. Zero lines of it exist.**

A grep for `git` across `long_exposure/` returns no subprocess call, no
module, no config block. The harness has never run a git command. Everything
below is a design walkthrough with the git mechanics verified out-of-band
(§9), not a description of behaviour you can invoke today.

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

```
1.  git fetch origin
2.  git rebase origin/<shared-branch>       # or merge; never --strategy=ours
3.  read .long-exposure/claims/*.json
4.  build a <federation> block
```

The block joins the list already assembled at `exploration.py:4358`:

```python
parts = [p for p in (fanout_guide, sibling_block, anti_patterns_block, guidance) if p]
base_live_guidance = "\n\n".join(parts) if parts else "[No live guidance this cycle.]"
```

A `federation_block` slots in beside `sibling_block`, and for the same
reason: it is pointer-shaped cross-process visibility, gated to the cycles
where it can be acted on. The researcher reads that another run holds
`spectral-theory` with a claim 40 minutes old, and picks something else.

A rebase conflict is surfaced *here*, as an input, not raised as an error
(plan §7). The harness never auto-resolves it. Conflicts on generated
artifacts are usually "both are fine, keep both"; conflicts in source are a
research disagreement between two runs, and a strategy flag is the wrong
instrument for a disagreement.

### 4.2 After the roles run — `exploration.py` ~5270, the "Status file + state" block

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
| Canonicalise slice names (slugify, lowercase, strip separators) | Catches near-misses, not synonyms. Cheap, partial |
| One shared `claims.json` index instead of a file per slice | *Every* claim now contends on one path, so every double-claim is detected — and so is every concurrent claim of different slices. Detected, but noisy |
| Nothing; let the auditor notice duplicated work | Honest, and cheapest, and what §8 argues for first |

Slice granularity is the whole ballgame (plan §4.4), and it is the question
the first experiment should answer with data rather than with a design.

---

## 7. What breaks, and what the harness does about it

| Failure | What happens | Harness response |
|---|---|---|
| **Push rejected** | Someone pushed first | Not an error. Fetch; next cycle's step 1 sees it |
| **Rebase conflict, generated artifact** | Two runs wrote the same report path | Surface to the next researcher. Never auto-resolve — "keep both" is usually right but it is not the harness's call |
| **Rebase conflict, source** | Two runs edited the same code | Surface. This is a research disagreement; it wants a person or an auditor |
| **Same-second `run_id`** | Two operators, identical `run_id` | Branch names stay distinct via the operator segment. The **ledger does not** — see §7.1 |
| **Offline operator** | Fetch fails | The cycle must run anyway. Local commits queue; the next successful fetch produces a bigger rebase. A run that halts because GitHub is unreachable is worse than a run that diverges for an hour |
| **Divergent harness versions** | A on this branch, B on `main` — different prompt text, different state fields | Record the harness commit in the commit trailer and the run registry, so a mismatch is visible in the artifacts rather than inferred from behaviour (plan §4.4) |
| **Operator B invalidates A's premise** | A's last four cycles rest on something B just disproved | **Unsolved.** A claims registry stops them editing the same files; it does nothing about this. The shared promise ledger is the only channel, and surfacing a conflicting *finding* is strictly harder than surfacing a file conflict |

### 7.1 The one concrete gap this walkthrough found

`promise_ledger.jsonl` events carry `run_id`, `cycle`, `agent` and
`milestone_id`. Under federation:

- **`cycle` is per-operator.** Two operators both have a "cycle 3". Any
  reader that groups or sorts by cycle across a merged ledger is reading two
  timelines as one.
- **`milestone_id` is a shared namespace with no operator segment**, and
  `summarize_ledger` keeps the *latest event per `milestone_id`*. So when two
  operators reach the same milestone, the later event hides the earlier one in
  the summary injected into the next cycle. Measured on a merged ledger with
  two `validated`/`high` events on `spectral/bound`:

  ```
  Total events: 2, distinct milestones: 1, shown: 1
  - [spectral/bound] validated/high (cycle 3, auditor, 2026-01-03T00:00:00Z)
      BOB bound is 7.9
  ```

  Alice's contradicting result — a different bound on the same milestone — is
  not flagged, not merged, and not shown. It is simply absent. That is the
  worst available outcome: the two operators disagree, and the mechanism
  meant to carry the disagreement drops one side of it silently.

  Two backfills soften this and it is worth being precise about which. Events
  with `status: in-progress`, and `validated`/`superseded` events at `low` or
  `provisional` confidence, are always included regardless of recency — so a
  duplicated `_run/start` (in-progress) survives, and so does a
  hedged finding. It is specifically the **confident, settled** events, the
  ones most worth reconciling, that collide.
- **No `operator` field exists**, and `run_id` cannot substitute for one
  because it is a bare timestamp.

The fix is small and belongs *before* any git code: add an `operator` field
to ledger events, namespace `milestone_id` (`<operator>/<milestone>`), and
make `summarize_ledger` group by `(operator, milestone_id)`. Doing it after
the sync layer ships means a merged ledger that has already lost events.

---

## 8. Build order

1. **Ledger identity** (§7.1). Small, no git involved, and required for
   anything downstream to be readable. Do this first.
2. **The sync layer**: one module, one config block, the two cycle-boundary
   seams of §4. Branch-per-run and cycle-boundary commits only — no claims,
   no leases.
3. **The experiment** (plan §4.5). Two operators, one repo, one small shared
   directive, both on the same harness commit, *no claims registry*. Count:
   how many pushes are rejected, how many rebases conflict, what they
   conflict on, and whether the two runs produce complementary or duplicated
   work.
4. **Then** decide the claims registry's shape from §6.2 with data, and
   `MEMOIR.<operator>.md` if the experiment shows memoir conflicts actually
   dominate.

Steps 3 and 4 are in that order on purpose. The claims registry is the part
of this design with the most guesswork in it, and the experiment is cheap.

A sketch of the config block, for orientation only — nothing reads this:

```yaml
# NOT IMPLEMENTED — design sketch (docs/git-federation.md)
git_sync:
  enabled: false
  operator: ""                 # required when enabled; the branch namespace
  remote: origin
  shared_branch: main
  branch_template: "long-exposure/{operator}/{run_id}"
  on_conflict: surface         # surface | halt. Never "resolve"
  push_failure: continue       # a rejected push is the next cycle's input
```

---

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
