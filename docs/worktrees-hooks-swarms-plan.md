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
| A cycle costs **$7.81** and **990 s** measured live (Opus 5.5, 3 calls) | `docs/advanced-model-modes-plan.md` §7 |

That last row is the one that decides the swarm question, and the fourth row
is the one that should make anyone cautious about worktrees as an isolation
primitive inside a vendor's agent runtime.

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

## 2. Worktrees: four candidate levels, three noes

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

**Verdict: yes, but only as part of a swarm build (§4), gated on a
threshold, and never for the 2–3 case.**

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

## 4. Swarms: the wall is cost, not engineering

Scaling `FANOUT_MAX_BRANCHES` from 3 to 20 is not primarily a parallelism
problem. Each branch is a **full long-exposure process** running the whole
researcher → worker → auditor cycle.

At the measured $7.81/cycle and 990 s/cycle, one clone allowed to run to the
10 h `FANOUT_CAP_SECONDS` completes roughly 36 cycles ≈ **$280**. So:

| Branches | Worst-case fan-out cost (one fork, 10 h) |
|---|---|
| 3 (today's cap) | ~$840 |
| 8 | ~$2,240 |
| 20 | ~$5,600 |

Those are Opus-5.5-basis numbers from a single measured cycle, so treat the
magnitude rather than the digits. The conclusion survives either way: **a
swarm is economically gated long before it is engineering-gated**, and the
enabling feature for swarms is therefore the spend limit that already
exists — not worktrees.

Four things would actually have to change, in dependency order:

1. **Per-member cost control.** The total spend limit exists but is
   root-enforced and root-only. A swarm needs the cap to bind *and* a cheaper
   member shape — most swarm members should not run a full three-role cycle.
   A "worker-only member" mode is the obvious primitive and does not exist.
2. **Hierarchical merge.** Today: concat below 4 branches, reporter synthesis
   at 4+. At 20, one synthesis turn reading 20 merge reports is itself a
   context problem. A swarm needs tree reduction — merge in groups, then
   merge the merges — which is a real design, not a parameter.
3. **Depth > 1.** The 1-level cap is deliberate and enforced in two places
   (`_parse_fanout_block`'s `_is_clone()` short-circuit, and
   `cycle_planning.allow_in_clones: false`). A swarm is naturally a tree, so
   this cap is the structural blocker. Lifting it re-opens every question the
   depth-1 decision closed — see `docs/parallelism.md` "Why depth=1".
4. **Worktree isolation per member** (§2.3), which is the *last* of the four,
   not the first.

**My recommendation: do not build swarms now.** Not because the engineering
is hard, but because the unit economics say a 20-branch fork costs thousands
of dollars per fork, and nothing in the current evidence says 20 branches
beats 3 on research yield. The cheap experiment that would justify it is
measuring whether 3 branches already beat 1 — which the harness can do today
and has not.

---

## 5. Hooks worth adopting, ranked by what they actually fix

Each is a hardening of guidance that already exists in prompt text, and each
maps to a problem this repo has already had.

### 5.1 `PreToolUse` on Bash → make the directory fence real (highest value)

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

This is the one I would build first of the hooks. The harness runs agents
with permission-skipping by design, for hours, unattended.

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

These two hooks make it observable for free: one `health_events` row per
provider compaction. Pure instrumentation, no behaviour change, and it feeds
the cost model §4 depends on.

### 5.5 `SubagentStart` / `SubagentStop` → per-teammate accounting

The agent-teams feature spawns teammates whose turns are opaque to the usage
ledger — the guidance even reasons about "halves per-teammate cost" without
being able to measure it. These hooks give a per-teammate record, which the
ledger could attribute rather than folding into the lead's row.

### 5.6 Deliberately **not** adopting

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
2. **`PreToolUse` path fence** (§5.1). Turns the harness's most important
   soft boundary into a hard one; identical on Claude and Codex.
3. **`PreCompact`/`PostCompact` observability** (§5.4). Nearly free, and it
   improves the cost model everything else is reasoned from.
4. **`Stop` output-envelope enforcement** (§5.2). Prevents a bug class with
   a known incident history.
5. **`PostToolUse` validators** (§5.3). Nice-to-have.
6. **Swarms** — not now (§4). The prerequisite experiment is measuring
   whether 3 branches beat 1.

Worktrees appear nowhere in that list, which is the honest answer to "where
should they be applied": **nowhere yet** — and inside a swarm build, if one
is ever justified.

---

## 7. Open questions

1. **Git sync cadence.** Commit every cycle (readable history, many commits)
   or every N cycles / on report boundaries (tidier history, coarser
   recovery)?
2. **Conflict posture.** Surface a conflict to the next researcher as an
   input and let the run reason about it, or hard-stop the run and wait for a
   human?
3. **Who may commit.** Harness-only (my recommendation), or allow an agent
   to commit within its leased subtree?
4. **Hook install surface.** Extend `long-exposure cli-install` to write hook
   configs, or ship them as a separate opt-in `long-exposure hooks-install`
   so an operator can adopt the harness without adopting hooks?
5. **Hook failure posture.** If a hook script is missing or errors, should
   the harness refuse to start a run (fail closed — the fence is why it
   exists) or warn and continue (fail open)?
