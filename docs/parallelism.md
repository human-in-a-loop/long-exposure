# Parallelism

Long-exposure has two parallelism mechanisms operating at different
granularity. Both are **opt-in by signal from the agent**, not
scheduled by the conductor.

| Mechanism | Granularity | Who decides | Cost posture |
|---|---|---|---|
| Parallel-cycle fan-out | Whole cycle (researcher → worker → auditor in parallel branches) | Researcher emits `<parallel_cycle_fanout>` block | Up to N× quota where N = branch count, capped by pool capacity |
| Agent-teams | Within a single turn | Worker / auditor decides per-turn | ~4× baseline for a 2-teammate task with shared state |

Both fire only when the structure of the work makes them worthwhile.
This doc covers both, plus the tradeoff analysis explaining why
fan-out is depth-1 (flat, not recursive).

---

## Parallel-cycle fan-out

When the researcher detects genuinely independent sub-problems each
needing its own full researcher → worker → auditor loop, it can emit a
block at the end of `research_brief`:

```xml
<parallel_cycle_fanout>
  <branch>
    <objective>one paragraph</objective>
    <output_artifact>distinct/relative/path.md</output_artifact>
  </branch>
  <branch>
    <objective>...</objective>
    <output_artifact>different/path.md</output_artifact>
  </branch>
  ...
</parallel_cycle_fanout>
```

The conductor parses, validates, dispatches one clone per branch,
waits for them at a barrier, optionally synthesises their outputs,
then runs a single post-merge worker turn that integrates the
fan-out's findings.

### The independence gate (researcher self-check)

Before emitting the block, the researcher is told (via `live_guidance`
in every cycle's brief) to self-check three criteria. **All three must
hold; if uncertain on any, stay linear.**

| Gate | Question | If no |
|---|---|---|
| **(a) Independence** | Does any branch consume another branch's output? | Stay linear. |
| **(b) Own audit** | Does each branch's findings need its own auditor gate? | Delegate to worker for intra-cycle agent-teams. |
| **(c) Iteration** | Does each branch need at least one build → test → refine round? | Delegate to worker (one-shot tasks don't need a full sub-cycle). |

The Python parser cannot enforce (a)–(c) directly — they require
semantic judgment. What the parser **does** enforce structurally:

1. **K must be in [2, dynamic_cap]**, where `dynamic_cap =
   pool.fanout_cap()` (= `available_slots() - 1`) when the pool is
   active, else `FANOUT_MAX_BRANCHES = 3`. K > cap is tail-clamped
   and the dropped branches are surfaced to the next cycle's
   `live_guidance`. K < 2 rejects the whole block.
2. **Output paths must be distinct** under `os.path.normpath`
   (catches `./a.md` vs `a.md`) AND must not be in ancestor/descendant
   relation (catches `dir` vs `dir/file.md`).
3. **Paths must be relative**, no `..` segments, no absolute paths.
4. **No recursion.** A clone that emits its own fanout block has the
   block ignored by the parser (gated on `AGENT_FORK_ID` env var).
   See "Why depth=1" below for the tradeoff.

### Pool-aware capacity

Fan-out is bounded by the multi-account pool, not by user
configuration. The researcher sees the current cap each cycle:

```
Cap: up to N branches (set by current account-pool capacity, not by you).
```

`N = available_slots() - 1`, with one slot reserved for sequential
root calls. With 5 accounts × 3 slots/account = 15 slots → cap 14.
With 2 accounts → cap 5. Cooling accounts contribute 0; cold
accounts contribute their full capacity.

### Clone lifecycle

When the parser accepts a block:

1. **Generate a fork id** (12-char UUID hex) and a per-clone instance
   directory at `<root_workspace>/fork-<id>/clone-<k>/`.
2. **Write per-clone assignment** to `clone-<k>/fanout_assignment.json`
   with the branch's objective, output path, and inherited context
   note.
3. **Seed the clone's state file** at `clone-<k>/exploration_state.json`
   from the parent's `agent_sessions`, `agent_summaries`, and
   `results` (filtered to drop `live_guidance`). Propagates
   `parent_run_id` so clone-emitted ledger events share the run's
   counters.
4. **Acquire a pool slot** for the clone (via `pool.acquire_slot`).
   Set `CLAUDE_FORCE_ACCOUNT` in the clone's env to the slot's
   account dir. Strip `CLAUDE_ACCOUNTS` from the clone's env — clones
   never rotate; they're pinned at spawn.
5. **Popen the clone** as `python -m long_exposure.exploration ...
   resume` with `--instance-dir clone-<k>`. The clone is started in
   its own process group (`start_new_session=True`) so the parent can
   send a hard signal to the whole group on wall-cap exhaustion.
6. **Re-tag the slot** with the clone's PID (parent post-Popen,
   idempotent fallback). The clone itself re-tags via
   `pool.update_slot_pid` early in its bootstrap and registers an
   `atexit` handler for `release_slot_by_branch`. See
   `multi-account-pool.md` for the three-path slot lifecycle.

The clone runs its own cycle loop normally. It compacts independently,
hits its own rate-limits (which terminate it because it can't
rotate), and writes a `merge_report.md` at exit.

### Barrier and graceful preemption

The parent polls each clone's `merge_report.md` until either:

- All clones have finished (organic termination, low-output exhaustion,
  rate-limit on pinned account).
- Wall-clock cap hit: `FANOUT_CAP_SECONDS = 36000` (10h) per clone.
- **Graceful barrier preemption** (Stage 9): a clone has done
  ≥ `min_clone_cycles_before_preempt` cycles AND any other clone has
  already exited AND there is cold pool capacity sitting idle.

Preemption sends `long-exposure.graceful-stop` to the laggard clone.
The clone observes the signal at its next cycle boundary, finishes
the current cycle, writes `merge_report.md`, releases its slot, and
exits cleanly. This frees pool capacity that would otherwise sit idle
behind a single slow branch — verified to save multi-hour barriers in
production.

The backup timer `barrier_preempt_timeout_seconds` (default 3600s)
fires preemption when *all* clones are slow and no organic exit has
happened yet. Both knobs are in `exploration-score.yaml:loop`.

Hard-kill escalation if the clone ignores the graceful-stop signal:
SIGTERM after 120s grace → SIGKILL after a further 10s, sent to the
clone's process group.

### Merge synthesis (Stage 2)

After the barrier collapses, the parent:

1. Reads each clone's `merge_report.md`.
2. Verifies each branch's declared `output_artifact` exists on disk
   (annotated as `present` / `missing` in the fork manifest).
3. **Optional: synthesise** if K ≥ `merge_synthesis_min_branches`
   (default 4). Invokes the existing **reporter** agent (no new agent
   role per principle 4) with a synthesis prompt that compresses N
   merge_reports → one bounded `merge_synthesis.md` (~15–30k tokens
   independent of N). Below the threshold, raw concatenation is the
   substrate.
4. **Run a single post-merge worker turn** with the synthesis (or raw
   concat) as the merge content. The cycle loop continues normally
   from the next cycle, with the post-merge worker's output rolled
   forward.

Why the threshold: at K=3 the raw concat is ~30k tokens (within budget
for the post-merge worker); at K=10 it's ~100k; at K=30 it's ~300k.
Synthesis at K ≥ 4 keeps post-merge worker context bounded
regardless of fan-out width.

The reporter is reused (not a new "merge synthesizer" agent) per the
"reuse existing agents" principle. It's the same reporter that runs
periodic cycle reports; the prompt is templated for synthesis context.
File-gate rescue applies if the reporter's output doesn't land on
disk.

### What survives a clone

| Artifact | Where | Survives clone exit |
|---|---|---|
| Workspace files (output_artifact, side-effect data) | shared workspace root | yes — clones share workspace |
| Clone's exploration_state.json | `clone-<k>/exploration_state.json` | optional — rotting fork dirs can be archived |
| Clone's merge_report.md | `clone-<k>/merge_report.md` | yes — read by post-merge worker |
| Clone's promise_ledger shadow file | `clone-<k>/promise_ledger.jsonl` | yes — concatenated to root ledger by `concat_clone_ledgers` after barrier |
| `sessions.db` writes | shared `sessions.db` (auto_compact path) | yes — single shared DB; WAL serialises writes |
| Pool slot | reclaimed via clone-side atexit / parent barrier release / heartbeat sweep | three independent paths, no leaks |

---

## Agent-teams (intra-cycle)

When a worker or auditor faces embarrassingly-parallel sub-work
within a single turn (parameter sweeps, cross-comparisons, batch
post-processing) it can spawn up to 3 teammates **for that turn
only**.

### Why only worker and auditor

Researcher / reporter / curator are team-disabled by design. Their
work isn't shaped like parameter sweeps — researcher brief-authoring
is single-threaded reasoning, reporter synthesis is composition,
curator is selection. Teammates would add coordination cost without
work to parallelize.

### How to enable

Two gates must both be true:

1. **Master switch** in `long_exposure/config.yaml`:
   ```yaml
   agent_teams_defaults:
     enabled: true
   ```
2. **Per-agent flag** in score YAML:
   ```yaml
   agents:
     worker:
       agent_teams: true
   ```

Master switch is a kill-switch: flipping it to `false` disables teams
for every agent regardless of per-agent flags.

Configuration knobs (all under `agent_teams_defaults` in config.yaml):

| Key | Meaning | Default |
|---|---|---|
| `enabled` | Master switch | `true` |
| `max_teammates` | Hard cap per lead-spawn turn (advisory in template) | `3` |
| `allow_peer_messages` | Allow teammate-to-teammate `SendMessage` | `true` |
| `cleanup_residue` | Per-turn sweep of `tasks/<team>/` mailbox dirs | `true` |
| `teammate_response_budget_tokens` | Hard response cap per teammate | `20000` |

### Inheritance — what propagates verbatim

Three values are injected from the agent's runtime config and the
lead is required to pass them through unchanged on every `Agent`,
`TeamCreate`, and prompt construction:

- **Model** — same `--model` as the lead (no downgrade).
- **Effort** — verbatim posture line in every teammate prompt.
- **Budget** — context window, compaction threshold, and per-teammate
  response cap, all in tokens.

Everything else (philosophy, framework, role) is inherited by
*composition*: the lead has internalised those layers and writes each
teammate prompt out of that internalisation. The team block itself
does not restate them.

### Lifecycle

- Team is created at the start of a turn, delegated to, aggregated,
  and deleted at end-of-turn.
- No cross-cycle persistence. Each turn that needs teammates creates
  a fresh team.
- Teammate transcripts at
  `$CLAUDE_CONFIG_DIR/projects/<cwd>/<session>/subagents/agent-*.jsonl`
  accumulate (not auto-cleaned).
- `$CLAUDE_CONFIG_DIR/teams/<team-name>/config.json` is small and
  persistent; not cleaned up by this integration.
- `$CLAUDE_CONFIG_DIR/tasks/<team-name>/` mailbox state is swept
  per-turn (mtime ≥ subprocess start time) and again at exploration
  start.

### Session lifecycle and config changes

`--system-prompt` is sent only on the first call of a fresh session.
On `--resume` the CLI retains the original prompt, so the team block
stays in context across cycles without re-injection. After
auto-compact, the lead starts a fresh session and the team block is
re-rendered with **current** config values.

Mid-run flag toggles take effect at the next compaction. Flipping
`agent_teams: true` on a live session does nothing until that agent
crosses the compaction threshold. The master switch under
`agent_teams_defaults` is the cleanest way to kill the feature
quickly — it stops env-var injection on the next subprocess call, so
team tools disappear from the lead's inventory immediately.

### Cost posture

Empirical baseline: ~4× quota for a 2-teammate shared-state task
with no tuning. The integration applies two cost-containment levers
in the template:

1. Skips `shutdown_request` on `TeamDelete` (halves per-teammate wake
   cost).
2. Hard per-teammate response-token cap (default 20k) that the lead
   must pass to each teammate prompt verbatim.

`allow_peer_messages: true` is the default. Workloads where teammates
exchange messages cost more; the template still warns the lead to
batch peer sends. Flip to `false` for genuinely embarrassingly-parallel
work to reclaim that overhead.

### Observability

When teams fire on a turn, a second log line is printed after the
agent-completion line:

```
[exploration]   worker team: teammates=2 wall=87.3s
```

`teammates` is the count of `agent-*.jsonl` transcripts produced
during the turn. `wall` is the subprocess duration. No per-teammate
token accounting — the headline `usage` line on the preceding log
already includes teammate inference costs rolled into the lead's
input tokens (via `SendMessage` replies delivered to the lead).

### Pool slots

Teammates inherit the lead's `CLAUDE_FORCE_ACCOUNT`. They do **not**
consume separate pool slots. From the pool's perspective, the lead's
turn is one slot.

---

## Why depth=1 (flat fan-out, no recursion)

The fan-out parser blocks recursion: a clone that tries to emit its
own `<parallel_cycle_fanout>` block has it ignored. This is
deliberate. Two scaling strategies were analysed:

| Axis | Depth=1 (flat) — current | Recursive depth |
|---|---|---|
| Researcher cognitive load | One soft-guidance block, one cap | Depth-tier variants needed |
| Slot accounting | Linear: root allocates once | Exponential; clones acquire dynamically |
| Failure blast radius | Per-branch isolation | Subtree failure propagates |
| Communication topology | Star (root ↔ clones) | Tree + cross-cutting findings channel |
| Gem scoping | One env var (`AGENT_FORK_ID`) | Path-based fork scope |
| Reaching n=100 | Linear pool growth (33 accounts × 3 slots) | Exponential paths but doesn't actually save pool size |
| Soft-guidance complexity | One template block | Multiple per-depth variants |

Flat fan-out wins on every operational axis at current scale.
Recursive depth is deferred until: (a) pool reaches ~50 accounts,
(b) the problem domain is genuinely hierarchical, (c) very long runs
where re-evaluating mid-run matters. For 1-level expressiveness within
a flat fan-out, the workaround is to name branches with subgroups in
the objective (e.g., "Group A — sub-task 1", "Group A — sub-task 2",
"Group B — sub-task 1") — the synthesis step recovers the structure.

---

## Operational rules summary

1. Fan-out is opt-in. The researcher proposes; the parser validates;
   the conductor dispatches.
2. Parser enforces structure (path collisions, K bounds, recursion);
   the model is trusted on independence semantics.
3. Pool capacity dictates parallelism. There is no user-facing
   branch-count knob. `fanout_cap = pool.available_slots() - 1`.
4. Clones are pinned at spawn via `CLAUDE_FORCE_ACCOUNT`. They never
   rotate; rate-limit on a pinned account exits the clone.
5. Clones share workspace and `sessions.db`. Output_artifact paths
   must be disjoint (parser enforces).
6. Wall-cap is 10h per clone (safety floor). Graceful preemption
   fires earlier when cold capacity is idle.
7. Merge synthesis at K ≥ 4 reuses the existing reporter agent. No
   new "merge synthesizer" role.
8. Agent-teams are intra-turn only. They inherit the lead's account
   pin and consume no separate pool slots.
9. Teammate spawn is decided per-turn by the lead. No conductor
   logic schedules teammates.

---

## Interaction with multi-account rotation (Plan B)

The cycle loop has two rotation triggers, both of which clear the
parent's `agent_sessions` (Claude session UUIDs are per-account):

- **Rate-limit-driven** — fires when the active primary returns 429.
  The handler marks the old primary cooling, calls `pool.promote_fresh`,
  hot-swaps `CLAUDE_FORCE_ACCOUNT`, and clears `agent_sessions`. The
  current cycle is restarted from the top.
- **Planned 24h** (Plan B) — fires after the daily-sync block iff no
  rotation has happened in the prior 24h. Calls `pool.promote_fresh`,
  hot-swaps `CLAUDE_FORCE_ACCOUNT`, and clears `agent_sessions`.
  Pre-emptive (no `mark_rate_limited`); the old primary stays
  callable as overflow. Pre-emptive rotation is important for
  $200 / mo Max plans where a primary may not naturally hit a 429
  in a 24h window.

Clones are unaffected by either rotation event — they're pinned at
spawn and never migrate. A planned rotation while clones are active
just means the parent's next cycle uses a new primary; clones
continue on their original pinned accounts. See
[`multi-account-pool.md`](multi-account-pool.md) for the full
mechanics.

---

## Figures from inside a fan-out (Plan C)

Worker / auditor agents (including those running inside fan-out
clones) generate figures through the unified `figure` CLI:
`figure plot` (matplotlib), `figure flow` (D2; ELK layout default,
PNG output), `figure arch` (mingrammer/diagrams). Single allowlist
entry `Bash(figure *)`; raw `d2`, `dot`, `mmdc`, `plantuml`, `tikz`
binaries are NOT on the allowlist.

For fan-out clones, figure outputs live in the shared workspace
(co-located with their data per `<figure-discipline>`). Output paths
must follow the same disjoint-paths rule that the fan-out parser
enforces for `output_artifact` declarations — clones writing to the
same figure path is a last-writer-wins collision that the curator's
post-hoc collision check surfaces.

See [`figures.md`](figures.md) for the full
figure CLI reference.

---

## Code citations

- Parallel-cycle fan-out parser and gates: `long_exposure/fanout.py:282–424`.
- Clone spawn / lifecycle: `fanout.py:574–842`.
- Pool-aware fan-out cap: `fanout.py:92–106`; `pool.py:300–319`.
- Barrier and graceful preemption: `fanout.py:1021–1291` (eligibility
  check at `_should_preempt_barrier`).
- Merge synthesis (Stage 2): `fanout.py:1372–1471`; `reporting.py`
  (reporter reuse).
- Post-merge worker brief: `exploration.py:2235–2262`.
- Clone bootstrap (slot re-tag, atexit): `exploration.py` clone
  bootstrap block (look for `_is_clone()` block).
- Agent-teams runtime: lead's `claude -p` env contains
  `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` when active. Template
  block injected by `orchestrator.py:1734–1740`.
