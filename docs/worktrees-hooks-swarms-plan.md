# Git worktrees, vendor-neutral hooks, and swarms

Brainstorming and design. **No code.** Status: proposal.

Three questions, which turn out to have less to do with each other than they
first appear:

1. Where, if anywhere, do git worktrees belong in long-exposure?
2. How do two operators on two machines work the same repo concurrently?
3. What would agent swarms — many more than three parallel branches — need?

Plus a fourth that cuts across all of them: which CLI lifecycle hooks are
worth adopting, given that the harness must not become Claude-only.

---

## 0. What was verified, not assumed

| Fact | Source |
|---|---|
| The harness has **no git awareness at all** today. One mention of the word "worktree" in the whole package, and it is a warning | `grep worktree long_exposure/` → 1 hit |
| Fan-out clones get their own `--instance-dir` (state, output, logs) but **inherit the same `config.working_directory`** — one shared workspace | `fanout.py:1157-1164` (spawn command passes only `--instance-dir`), `fanout.py:566` |
| The fan-out contract is explicitly shared-workspace: *"The fan-out design lets clones share the workspace freely; the only contract is that no two clones write to the same file"* | `fanout.py:566` |
| **Worktree isolation has already been tried here and silently failed.** The agent-teams guidance tells the lead: *"Do not rely on isolation:worktree (silently broken in team mode); scope each teammate's writes via an explicit subtree path in its prompt"* | `orchestrator.py:2040` |
| A cycle takes **990 s** and 3 calls measured live (Opus 5.5) | `docs/advanced-model-modes-plan.md` §7 |
| `branchial_budget.score_branches` already scores each fan-out branch's novelty, but only **advisorily** — it prints and logs | `fanout.py` branchial-budget block |

The **990 s** matters for the swarm question (§4) because wall clock, not
money, is what a barrier across many members pessimises — the harness bills
to a fixed-cost subscription, so per-run dollars are an accounting figure
rather than a constraint. The **worktree** row is the one that should make
anyone cautious about worktrees as an isolation primitive inside a vendor's
agent runtime.

---

## 1. The vendor hook surfaces, from primary sources

All three CLIs the harness supports now have lifecycle hooks. The shapes are
much closer than expected between two of them, and different in the third.

### The intersection

| Event | Claude Code | Codex | Gemini CLI |
|---|:--:|:--:|:--:|
| SessionStart | ✓ | ✓ | ✓ |
| SessionEnd | ✓ | ✓ | ✓ |
| UserPromptSubmit | ✓ | ✓ | — |
| **PreToolUse** | ✓ | ✓ | `BeforeTool` |
| **PostToolUse** | ✓ | ✓ | `AfterTool` |
| PermissionRequest | ✓ | ✓ | — |
| PreCompact / PostCompact | ✓ | ✓ | — |
| SubagentStart / SubagentStop | ✓ | ✓ | `BeforeAgent` / `AfterAgent` |
| **Stop** | ✓ | ✓ | — |
| Interrupt | — | ✓ | — |
| Worktree{Create,Remove} | ✓ | — | — |
| ~18 others | ✓ | — | — |

### The finding that makes this cheap

**Claude and Codex are wire-compatible for the shared events.** Same stdin
JSON (`session_id`, `cwd`, `hook_event_name`, `model`, plus event fields like
`tool_name` / `tool_input`), the same `hookSpecificOutput` /
`additionalContext` / `permissionDecision` output convention, the same
exit-code-2-blocks-with-stderr convention, and the same
`{matcher, hooks:[{type:"command", command, timeout, async}]}` config shape —
Codex accepting it as either `hooks.json` or a `[[hooks.X]]` TOML table.

So the architecture is: **one hook script per concern, three thin config
files.** Not a hook abstraction layer — the scripts themselves are portable.

Gemini needs a **name map only** (`PreToolUse`→`BeforeTool`,
`PostToolUse`→`AfterTool`, `SubagentStart`→`BeforeAgent`), and does not have
`Stop`, `UserPromptSubmit`, `PermissionRequest` or the compaction pair, so
any hook depending on those degrades to "not installed" under Gemini rather
than breaking.

The harness already has the right home for this: `long-exposure cli-install
--target claude|codex|gemini` already writes per-vendor adapter files
(`.codex/skills/long-exposure/SKILL.md` and siblings). Hook configs go
through the same command, so installation stays one operator action.

### The rule this implies

> A hook may **harden** a rule the prompt already states. It must never be
> the *only* place a rule exists.

Because Gemini lacks four of the events, and because a hook can be
`--bare`'d away (Claude) or refused by the trust model (Codex, which requires
user review of non-managed hooks), any behaviour that exists only in a hook
silently vanishes on the vendor or invocation that lacks it. Every hook below
is a hardening of existing prompt guidance, never a replacement for it.

---

## 2. Worktrees: four candidate levels, four noes

### 2.1 Fan-out clones (2–3 branches) — **no, and not merely as overkill**

The operator's instinct is right, and the reason is stronger than cost. The
fan-out design *depends* on shared-workspace visibility:

- sibling awareness reads each clone's `latest_report_pointer.md`;
- the merge reads every branch's declared `output_artifact`;
- `concat_clone_ledgers(workspace, fork_dir)` aggregates clone ledgers from
  the shared tree;
- the memoir's per-clone shadows and the `branch_memoirs` fold assume one
  workspace with several shadow files in it.

Worktrees would isolate exactly what these mechanisms read. The divergence
table, sibling pointers and ledger concat would all need to become git
operations across branches — a rewrite of the merge, to solve a collision
problem that is **already solved deterministically**: the parser rejects a
fan-out block whose branches declare colliding `output_artifact` paths, so
two clones cannot be scheduled onto the same file.

And there is direct local evidence: the one place worktree isolation was
tried in this project — Claude's agent-teams `isolation:worktree` — was
*silently broken*, and the shipped workaround is explicit subtree scoping in
the prompt. Adopting worktrees at the clone level would be re-buying a
failure the repo already documents.

**Verdict: no. Keep the shared workspace and the distinct-path contract.**

### 2.2 Two operators, two machines, one repo — **no, wrong tool entirely**

A worktree is multiple checkouts of **one local repository on one
filesystem**. It does nothing across machines. What the described scenario
needs is not worktrees but a **git sync discipline** the harness does not
have at all today (§3).

**Verdict: no. Worktrees are irrelevant to this case.**

### 2.3 Swarm members on one machine (N ≫ 3) — **yes, conditionally**

This is the one place worktrees genuinely earn their keep, and only above a
threshold. At 2–3 clones the shared workspace works because the distinct-path
contract is enough. At 10–20 the failure modes change in kind, not degree:

- concurrent `pytest` runs in one tree stomping `__pycache__` and fixtures;
- build artifacts and `data/` outputs colliding beyond what per-file
  declarations can express;
- any member running `git` commands seeing a tree 19 others are mutating;
- one member's broken intermediate state breaking every other member's read.

A worktree per member gives each a private checkout sharing one object store
(cheap — no re-clone, no duplicated history) and moves integration from file
conventions to `git merge`, which is a tool actually designed for N-way
convergence.

**Verdict: superseded — no.** This was written when scaling meant many
members on one machine. Under the federation model (§4) each operator runs
their own 2–3 branch fan-out on their own machine, so the local tree never
has 10–20 writers and the contention this section describes never arises.
Worktrees have no place in long-exposure at any level.

### 2.4 The harness's own repository — **no**

Agents are already fenced off the harness root (`== DIRECTORY BOUNDARIES ==`
names `SCRIPT_DIR.parent` explicitly). A worktree there would be a second
copy of the thing they must not touch.

---

## 3. The real multi-operator case: a git sync layer

The scenario — operator A on machine A and operator B on machine B, both
running long-exposure against the same repo — needs four things, none of
which is a worktree.

| Need | Shape |
|---|---|
| **Identity** | One branch per run: `long-exposure/<run_id>`. `run_id` already exists and is already in the state file, the registry and telemetry |
| **Cadence** | Commit at cycle boundaries, not mid-cycle. The cycle boundary is already the harness's transaction point — where state is saved, the status file written, the memoir archived. Committing there means every commit is a coherent cycle, and `git log` becomes a readable run history |
| **Convergence** | `fetch` + `rebase` (or merge) before a cycle starts; push after the commit. A conflict is a *first-class run event*, not an error: surface it to the next researcher as an input, the way `live_guidance` already works |
| **Ownership** | A lease file (`.long-exposure/leases/<subtree>.json`) claiming a subtree for a run_id with a timestamp. Advisory, checked at cycle start, surfaced in the prompt. Two runs on the same repo should be working different subtrees; a lease makes the overlap visible instead of discovered at merge time |

Two design positions worth stating up front:

- **The harness commits; the agent does not.** Deterministic, harness-driven
  commits at cycle boundaries are auditable and reproducible. Letting agents
  run `git commit` puts history under model control and makes the run's
  provenance a function of prompt adherence. The existing `promise_ledger`
  and memoir precedent both point the same way: the harness owns the
  bookkeeping, the agent owns the content.
- **Never auto-resolve a conflict.** Rebase conflicts on generated artifacts
  (reports, data, figures) are usually "both are fine, keep both"; conflicts
  in source are a research disagreement between two runs and want a human or
  an auditor, not a strategy flag.

This is the item I would build first: it unlocks the scenario actually
described, it is pure git (so vendor-neutral by construction), and it is
modest — one module, one config block, one new cycle-boundary seam of the
kind the harness already has four of.

---

## 4. Swarms, reframed twice: a federation over the repo, not a local swarm

This section has been wrong twice, and the operator corrected it both times.
Worth recording, because the third framing is much better than the first two
and it removes most of the work the earlier ones implied.

- **Draft 1** argued swarms were cost-gated: ~$280 per clone at the 10 h cap,
  so 20 branches ran to thousands of dollars. Wrong premise — the harness
  bills to a fixed-cost subscription, so those dollars are an accounting
  figure, not an invoice.
- **Draft 2** removed cost and concluded the binding constraint was
  *redundancy*: twenty agents on one directive duplicate each other, so a
  swarm is a coverage problem needing a tree, a queue, depth > 1 and rolling
  collapse.
- **Draft 3 (this one).** The operator's actual shape: *"swarming on a GitHub
  repo where multiple independent users with independent computers have
  long-exposure installed and they are concurrently working on the same
  project — maybe each independent user has a few agents working on a
  fan-out branch, but they do not necessarily have local swarms."*

That is a **federation**, not a swarm, and it changes the answer completely.

### 4.1 What the federation model deletes

Scale comes from *N operators × their small fan-outs*, not from raising
`FANOUT_MAX_BRANCHES`. Each operator runs the harness they already have —
2–3 branches, one machine, one plan. Nothing local gets bigger.

So from draft 2's seven-item build list:

| Draft-2 item | Under federation |
|---|---|
| Concurrency governor | **Already exists, per operator.** `_fanout_branch_cap()` clamps to that operator's own pool capacity. No global governor, because there is no global scheduler |
| Cheap worker-only member shapes | **Not needed.** Each operator runs normal three-role cycles |
| Rolling collapse instead of a barrier | **Not needed.** There is no cross-operator barrier to pessimise |
| Tree reduction | **Not needed.** Reduction is `git merge` and pull requests — a tool built for N-way convergence by people who are not in the same process |
| Depth > 1 | **Not needed.** The depth-1 cap can stay exactly as it is, and every question `docs/parallelism.md` "Why depth=1" settled stays settled |
| Worktree isolation per member | **Not needed.** Each operator has their own machine, so the local tree never has 20 writers. This makes §2.3's "conditionally yes" a **definitive no** — worktrees have no place in long-exposure at any level |
| Novelty-gated partitioning | **Still needed, but relocated.** The question is no longer "do these 3 branches overlap" but "are operator A and operator B working the same thing" — which is a claim problem, not a scoring problem |

The whole of draft 2's architecture collapses into one thing the harness
already lacks and needs anyway: **the git sync layer of §3.** The federation
*is* that layer, used by more than one operator.

### 4.2 Git as the coordination substrate

The repo becomes the blackboard, and each operator's long-exposure is an
autonomous participant working a claimed slice of it. Four primitives, three
of which reuse machinery that already exists.

**1. Branch per run, namespaced by operator.**
`long-exposure/<operator>/<run_id>`. `run_id` already exists in state, the
run registry and telemetry. Namespacing by operator keeps two runs from
colliding on a branch name without any central allocator.

**2. Claims live IN the repo, and git's push rejection is the lock.**
A lease on operator A's local disk is invisible to operator B, so the claim
registry has to be committed: `.long-exposure/claims/<slice>.json`, holding
slice, operator, run_id, timestamp, expiry.

The race resolves itself for free. Two operators claiming the same slice both
commit and push; the second push is rejected as non-fast-forward; the loser
re-fetches, sees the existing claim, and picks a different slice. **No
server, no daemon, no consensus protocol** — just the property that a git
remote accepts exactly one of two conflicting pushes. That is the single most
important design point here, and it is why this shape is tractable where a
local swarm scheduler was not.

**3. The promise ledger federates for free; the memoir needs the shadow
pattern.** These are opposite cases and the difference matters:

- The **promise ledger is append-only JSONL**. Two operators appending
  different events produce a textbook union merge. One line in
  `.gitattributes` — `promise_ledger.jsonl merge=union` — and ledger
  convergence is automatic and correct. This is the harness's biggest
  existing asset for federation and it is already the right shape by
  accident.
- The **memoir is a single file rewritten every cycle** — the worst possible
  merge shape, and it would conflict on essentially every sync. But the
  harness already solved this exact problem for fan-out: per-clone **shadow
  memoirs** plus a `branch_memoirs` fold at the collapse. The same pattern
  applies one level up: `MEMOIR.<operator>.md` per operator, folded for
  reading, never merged. The work is a rename and a re-scope of a mechanism
  that already exists and is already tested.

**4. `sessions.db` stays local and must never be committed.** SQLite with
WAL, per-operator, binary — nothing good comes of putting it in git. Each
operator's session history is theirs; the shared surfaces are the ledger,
the claims, the memoirs and the artifacts. Worth an explicit `.gitignore`
entry and a doc line, because committing it once would be unpleasant to
undo.

### 4.3 What a participating operator's cycle looks like

Every one of these is at a cycle boundary, which is already the harness's
transaction point:

1. `fetch`; `rebase` onto the shared branch. A conflict is surfaced to the
   next researcher as an input (operator decision, §7).
2. Read the claims registry. If this run's slice is claimed by someone else
   with a live lease, the researcher is told so and picks differently.
3. Run the cycle — unchanged, including a local 2–3 branch fan-out if the
   researcher asks for one.
4. Commit (harness-authored, never agent-authored — operator decision, §7),
   push. A rejected push means someone else got there first: re-fetch and
   let the next cycle see the new state.

### 4.4 What is genuinely hard about this

Not the mechanics — the *research* coordination:

- **Two operators can invalidate each other's premises.** A claim registry
  stops them editing the same files; it does not stop operator B proving
  false the assumption operator A's last four cycles were built on. The
  honest answer is that this is what the shared promise ledger is for, and
  that surfacing a conflicting *finding* to the other operator's researcher
  is a strictly harder problem than surfacing a file conflict. Worth naming
  now rather than discovering later.
- **Slice granularity is the whole ballgame.** Too coarse and operators
  block each other; too fine and the claims registry churns. It probably
  wants to be a directive-level or topic-level concept rather than a path,
  which means it belongs in `plan_of_record.md` — the one shared file that
  is neither append-only nor per-operator.
- **Divergent harness versions.** Operator A on this branch and operator B
  on `main` write different prompt text and different state-file fields. A
  federated run needs a version handshake, or at minimum the run registry to
  record the harness commit so a mismatch is visible in the artifacts.

### 4.5 The experiment that should still come first

Two operators, one repo, one small shared directive, both on this branch, no
claims registry — just branch-per-run and cycle-boundary commits. Run it and
count: how many pushes are rejected, how many rebases conflict, what
conflicts on, and whether the two runs produce complementary or duplicated
work.

That answers the slice-granularity question with data instead of design, and
it needs only §3's git sync layer, which is the first thing on the build list
anyway. The K = 1/3/8 branch-count experiment from draft 2 is **withdrawn**:
under federation, local branch count is not the scaling dial.

## 5. Hooks — BUILT (2026-09-25)

Two hooks ship — the `Stop` envelope check and `PreCompact`/`PostCompact`
observability — with the installer, the shim smoke check and 41 tests, all
verified against a live `claude -p`. A third, the `PreToolUse` fence, was
built and then removed (§5.0.1). What follows is the design rationale for each
candidate, with the implementation notes folded in.

### 5.0 What shipped

| Piece | Where |
|---|---|
| Shared stdin/stdout protocol | `long_exposure/hooks/_io.py` |
| `Stop` output-envelope check | `long_exposure/hooks/envelope.py` |
| `PreCompact`/`PostCompact` observability | `long_exposure/hooks/compaction.py` |
| Per-vendor installer + shim smoke check | `long_exposure/hooks_install.py` |
| CLI | `long-exposure hooks-install [--target …] [--verify] [--uninstall] [--dry-run]` |
| Config | `hooks:` block in config.yaml — schema and env mapping in `long_exposure/hooks/__init__.py` (`DEFAULTS`, `ENV_BY_KEY`) |
| Harness wiring | `orchestrator._add_hook_env`, called on every agent turn — sets the identity vars and translates the `hooks:` block into the disable/tuning vars |

One thing the live testing changed, worth recording because it was not
visible from the design:

- **The envelope hook is gated on being inside a harness
  turn** (`LONG_EXPOSURE_HOOK_ACTIVE`, set by `_add_hook_env`). An ungated
  envelope hook nudged a bare `claude -p` turn into inventing an
  `[OUTPUT: result]` label it was never asked for — the model said so
  itself: *"no output type was ever declared to me in this conversation,
  I'll use a generic label"*. Installed in an operator's home config, that
  would tax every unrelated turn they take.

Verified live: the envelope hook stays silent outside a harness turn, and
inside one it makes the agent emit the correctly-named
`[OUTPUT: work_output]` block.

### 5.0.1 What was removed again

**The `PreToolUse` path fence was built, verified live, and then removed by
operator decision.** The reasoning, which I agree with: long-exposure treats
the model as a faithful collaborator. A fence that only stops honest mistakes
duplicates guidance the system prompt already carries, and a fence meant to
stop an adversarial model could be circumvented anyway — my own threat-model
note conceded it can only match literal command text, so a path built from
shell variables or encoded passes straight through. Building enforcement that
works against neither threat is machinery for its own sake. This is a
harness, not a safety net; real isolation is a container boundary.

What that decision also removes: the fail-closed startup check, the
`hooks.fence` config block, the `scope: turn|always` question, and the
`git_sync.harness_commits_only` enforcement. The *decision* that the harness
authors commits still stands (§7) — it is now carried by prompt guidance and
by the harness simply being the thing that runs `git`, which is where it
always actually lived.

### 5.0.2 The bug the tests caught

The installer's ownership marker was the module name
(`long_exposure.hooks`), which appears only inside the shim's text — while
the vendor config stores the shim's **path** (`long-exposure-envelope.sh`).
So
`_is_ours` never matched: uninstall silently left our entries behind and a
re-install would have duplicated them. Matched on the shim basename now,
against the known hook names, so an operator's own script living under a
path that happens to contain "long-exposure" is never removed.

### 5.0.3 The candidate hooks

The sections below walk the events considered, in the order they were
evaluated. Each maps to a problem this repo has already had. Two shipped
(§5.2 `Stop`, §5.4 `PreCompact`/`PostCompact`), one was built and withdrawn
(§5.1 `PreToolUse`), and two are deferred (§5.3 `PostToolUse`, §5.5
`SubagentStart`/`SubagentStop`).

### 5.1 `PreToolUse` on Bash → the fence, and why it was withdrawn

Today `== DIRECTORY BOUNDARIES ==` is **prose**. The operating protocol says
so itself: file tools are scoped to the workspace, but *"Bash is NOT
path-restricted by this setting — see operating protocol for soft directory
boundaries enforced via instructions."* The prompt then asks the agent not to
touch `~/.claude`, `~/.ssh`, `~/.env`, or the harness root — which is on the
agent's own `PYTHONPATH` and therefore writable.

A `PreToolUse` hook matching `Bash` that parses the command for paths and
returns `permissionDecision: deny` converts the harness's most important soft
fence into a hard one. Both Claude and Codex support it identically; Gemini
gets it as `BeforeTool`.

This was built and then withdrawn (§5.0.1). The argument that beat it: the
harness runs agents it trusts, the prompt already states the boundary, and an
enforcement layer that cannot stop a determined model while duplicating
guidance for an honest one is not worth its weight.

### 5.2 `Stop` → enforce the `[OUTPUT: x]` envelope

`gaps.md` records a High-severity incident: *"Periodic/merge reports captured
the reporter's trailing cover-note instead of the report body"*, because the
agent emitted the deliverable mid-turn and then trailed a checkpoint, so
`envelope["result"]` held the cover note. The fix was a transcript re-parse —
a recovery, after the fact.

A `Stop` hook can check the final message for a well-formed
`[OUTPUT: name] … [END OUTPUT: name]` and return `{"decision": "block"}` —
which on **both** Claude and Codex means *continue the turn* — with a reason
telling the agent to emit the block properly. That turns a recovery path into
a prevention path, for the exact bug class that has already cost this project
an incident.

Not available on Gemini; degrades to today's recovery behaviour there.

### 5.3 `PostToolUse` on Write|Edit → validators at the point of violation

`org_check` and `promise_check` exist and run out of band, so an agent learns
it put a file in the wrong place at audit time — a cycle later. A
`PostToolUse` hook can run the same validator on the written path and return
`additionalContext` naming the violation while the agent is still in the turn
that caused it.

Cheap, useful, and it reuses validators that already exist rather than adding
a new rule surface. Worth capping: run it on workspace-root writes, not every
edit, or it becomes a tax on a 48-tool-call worker turn.

### 5.4 `PreCompact` / `PostCompact` → see the provider's own compaction

The harness does its own compaction at `compact_threshold`. The CLI *also*
compacts internally on its own schedule, and the harness cannot currently see
it. The live smoke run produced exactly one health event —
`compaction_xml_invalid` — and a $3.45 compaction call was the turn that
crossed the spend cap. Provider-side compaction is both expensive and
invisible.

These two hooks make it observable for free. Both events are installed, so
each provider compaction writes two `health_events` rows —
`provider_compaction_started` and `provider_compaction_finished` — which is
what lets a reader tell a completed compaction from one that began and
vanished. Pure instrumentation, no behaviour change, and it feeds the cost
model §4 depends on.

### 5.5 `SubagentStart` / `SubagentStop` → per-teammate accounting

The agent-teams feature spawns teammates whose turns are opaque to the usage
ledger — the guidance even reasons about "halves per-teammate cost" without
being able to measure it. These hooks give a per-teammate record, which the
ledger could attribute rather than folding into the lead's row.

### 5.6 The stance, stated positively

Long-exposure has **no enforcement layer, by design**. It treats the model as
a faithful collaborator, so hooks here exist for *correctness* (helping a
cooperating agent satisfy the harness's own output contract) and
*observability* (recording what the harness cannot otherwise see). Nothing
polices.

That is why the fence went. An enforcement hook sits in a bad middle: against
an honest agent it duplicates the system prompt, and against a dishonest one
it fails, since it can only match literal command text. Real isolation is a
container boundary — a deployment decision, not a hook.

### 5.7 Deliberately **not** adopting

- **`WorktreeCreate` / `WorktreeRemove`** — Claude-only, and the repo already
  documents worktree isolation silently failing in team mode. If worktrees
  ever arrive (§2.3) the harness should drive `git worktree` itself in Python,
  where it is vendor-neutral and testable, rather than reacting to one
  vendor's lifecycle event.
- **`UserPromptSubmit`** — the harness *is* the prompt author. A hook to
  modify a prompt the harness just wrote is indirection with no gain.
- **`PermissionRequest`** — genuinely useful in principle, but the harness
  deliberately runs permission-skipping for unattended autonomy. Adopting it
  means redesigning the permission posture, which is a bigger decision than a
  hook.
- **Anything Claude-only** as a behaviour dependency, per §1's rule.

---

## 6. What I would do, in order

1. **Git sync layer** (§3). Unlocks the scenario actually described, pure
   git, vendor-neutral, modest scope.
2. ~~**`PreToolUse` path fence**~~ — **built, then REMOVED** by decision (§5.0.1).
3. ~~**`PreCompact`/`PostCompact` observability**~~ — **DONE** (§5.0).
4. ~~**`Stop` output-envelope enforcement**~~ — **DONE** (§5.0).
5. **`PostToolUse` validators** (§5.3). Not in the first cut, by decision.
6. **The two-operator federation experiment** (§4.5) — which needs only
   item 1, and answers the slice-granularity question with data.
7. **Claims registry, union-merged ledger, per-operator memoirs** (§4.2),
   once that experiment says what a slice should be.

Worktrees appear nowhere in that list, and under the federation model they
never will. The honest answer to "where should they be applied" is
**nowhere**: the scaling unit is an operator with their own machine, not a
member with a checkout.

---

## 7. Decisions taken

| Question | Decision |
|---|---|
| **Which hooks** | The `Stop` **`[OUTPUT: x]` envelope** check and **`PreCompact`/`PostCompact`** observability. The `PreToolUse` **path fence was built and then removed** — the harness treats the model as faithful and has no enforcement layer (§5.0.1, §5.6). `PostToolUse` validators are not in the first cut |
| **Hook install surface** | A **separate `long-exposure hooks-install`**. Adopting the harness must never silently edit an operator's `~/.claude/settings.json` or `~/.codex/hooks.json` |
| **Hook failure posture** | Moot once the fence went: both remaining hooks are non-blocking, so `--verify` is a smoke check and nothing fails closed |
| **Git commit cadence** | **Every cycle boundary** — already the harness's transaction point, so every commit is a coherent cycle and `git log` reads as run history |
| **Conflict posture** | **Surface it to the next researcher as an input**, the way `live_guidance` works. A conflict is a run event, not an error; keeps an autonomous run autonomous |
| **Commit authority** | **Harness only**, carried by prompt guidance and by the harness being the thing that runs `git` — not by a hook that blocks `git commit`, which went with the fence |
| **Swarm shape** | **A federation over a GitHub repo** (§4): independent operators on independent machines, each with a small local fan-out. Not a local swarm |
| **Governor** | **A concurrency semaphore sized to plan capacity** — which under federation is per operator, and already half-exists as `_fanout_branch_cap()` |
| **Worktrees** | **Nowhere.** Four candidate levels, four noes (§2) |

### Still open, deliberately

- **Slice granularity** for the claims registry — path, topic or directive.
  §4.5's two-operator experiment is meant to answer this with data rather
  than design.
- **Cross-operator finding conflicts** (§4.4): a claims registry stops two
  operators editing the same file, not one invalidating the other's
  premise. The shared promise ledger is the likely home; the mechanism is
  not designed.
- **Harness version skew** between federated operators (§4.4).
