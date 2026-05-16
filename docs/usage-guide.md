# Long-Exposure: Usage Guide

Long-exposure is an autonomous research conductor. It runs a researcher → worker
→ auditor loop indefinitely, with persistent context across sessions, until you
stop it or the topic is exhausted.

For the conceptual map and how the subsystems compose, see
[`architecture-overview.md`](architecture-overview.md). For environment setup,
see [`local-setup.md`](local-setup.md). This guide focuses on day-to-day use.

There are four ways to drive it, in order of preference:

| Interface | When to use | Form |
|---|---|---|
| **`long-exposure launch`** | Provider-neutral day-to-day use from any terminal or LLM CLI shell | `long-exposure launch "<directive>"` |
| **CLI adapter / slash command** | Claude/Codex/Gemini session convenience | route to `long-exposure launch "<directive>"` |
| **`long-exposure start`** | Lower-level scripted conductor call | `long-exposure start "<directive>"` |
| **`python3 -m long_exposure.exploration`** | Fallback when the console script isn't installed | `python3 -m long_exposure.exploration start ...` |

The launcher and adapters call the same conductor code. The launcher adds
preflight checks, status visibility, and manager-notice awareness; `start` is
kept as a stable low-level command for scripts.

---

## Quick Start

**Provider-neutral launcher:**

```bash
long-exposure launch "Explore the foundations of microlocal analysis"
```

**From within Claude Code, when the adapter is installed:**

```
/long-exposure Explore the foundations of microlocal analysis
```

CLI adapters should be thin routing layers. They should invoke
`long-exposure launch "<directive>"` rather than duplicating launch logic in
prompts or shell fragments.

To install project-local adapter files where supported:

```bash
long-exposure cli-install --target all --directory .
```

This writes deterministic routing files for Claude, Codex, and Gemini with
overwrite safeguards. Use `--force` only when you want backups plus overwrite.

The run goes until you stop it (Ctrl+C on the tail, stop signal, or topic
exhaustion).
Each cycle pauses for the configured cooldown (default 400s) before the next
cycle starts. A reporter agent runs every 3 cycles to consolidate results;
a final auditor, final reporter, and curator run once at the end to audit,
synthesize, and package the output.

**Equivalent from a terminal:**

```bash
long-exposure start "Explore the foundations of microlocal analysis"
```

**Or, if the console script isn't installed:**

```bash
python3 -m long_exposure.exploration start "Explore the foundations of microlocal analysis"
```

A directive is required. Running `/long-exposure` or `long-exposure start`
with no directive falls back to the `task:` field in
`long_exposure/exploration-score.yaml` — useful for repeat runs of a
pre-edited score, but you'll usually want to pass the directive inline.

---

## Daily Controls

### Start

| Interface | Command |
|---|---|
| Provider-neutral launcher | `long-exposure launch "<directive>"` |
| Claude/Codex/Gemini adapter | route to `long-exposure launch "<directive>"` |
| Low-level CLI | `long-exposure start "<directive>"` |
| Module | `python3 -m long_exposure.exploration start "<directive>"` |

Starting with a directive **archives and clears** any existing state file
before launching — a new directive implies a new run.

Starting with **no** directive (`long-exposure start`) continues from saved
state if one exists, or falls back to the score YAML's `task:` field. In
practice, prefer `resume` for the "continue where I left off" case; it makes
intent explicit.

### Stop

Any of these gracefully stop the exploration. The current in-flight agent turn
is allowed to finish; the run then exits through the final auditor, final
reporter, and curator before writing a completed status. Use `clear` only when
you explicitly want to archive state and skip end-of-run synthesis.

| Interface | Command |
|---|---|
| Keyboard | `Ctrl+C` (in the terminal watching the run) |
| Claude Code | `/long-exposure stop` |
| CLI | `long-exposure stop` |
| Signal file | `touch long_exposure/data/long-exposure.stop` |

For a named instance, scope the stop signal to its workspace:

```bash
long-exposure --instance-dir ~/agent-instances/<name> stop
```

### Resume

Resume from where you left off. State is read from
`long_exposure/data/exploration_state.json` (or the instance's equivalent).

| Intent | Command |
|---|---|
| Continue with saved directive | `/long-exposure resume` |
| Redirect to a new directive, keep accumulated context | `/long-exposure resume "<new directive>"` |
| Restore a specific archived state file | `/long-exposure resume --from-archive <path>` |

The same commands work under the CLI (`long-exposure resume ...`) and module
forms.

**Agent sessions are persistent.** On resume, each agent picks up with its
full context from the last cycle. If a session was compacted before the
stop, the agent starts a fresh session bootstrapped from the compaction
summary.

**Task resolution on resume.** Resume uses the directive that was saved when
the exploration was last stopped — it does **not** silently re-read the
`task:` field in `exploration-score.yaml`. Editing the score file between
stop and resume does nothing unless you also pass a new directive on the
command line.

Priority order, when the exploration starts:

1. **Explicit override on the command line.** `resume "<new directive>"`
   redirects the run and logs the change.
2. **Saved directive from state.** No argument → continue with the task
   that was running at stop time.
3. **Score YAML `task:` field.** Consulted only when no state exists
   (fresh start with no override and no state file).

To switch to a new topic *without* carrying over old context, clear state
first:

```
/long-exposure clear
/long-exposure <new directive>
```

### Clear

Archive the current state file and reset for a new topic. Session history
in `sessions.db` is preserved (it accumulates across all runs and is the
source of cross-run context gems).

| Interface | Command |
|---|---|
| Claude Code | `/long-exposure clear` |
| CLI | `long-exposure clear` |
| Signal file | `touch long_exposure/data/long-exposure.clear` |

This moves the current `exploration_state.json` to a timestamped archive
(e.g., `exploration_state_20260424T1700.json`) and signals the running
exploration to stop. You can later restore it with `resume --from-archive`.

### Inject live guidance (mid-run)

Send guidance that will be read by agents at the start of the next cycle —
without stopping the run:

```bash
long-exposure guide "Focus on convergence proofs for the heat equation"
```

Equivalent signal-file form:

```bash
echo "Focus on convergence proofs for the heat equation" \
     > long_exposure/data/long-exposure.guide
```

The file is consumed once and deleted. You can write to it at any time,
including while a cycle is in flight — it's picked up at the next cycle
boundary.

### Status and manager notices

Print the latest deterministic status file and the newest manager poll notice:

```bash
long-exposure status
```

For named instances:

```bash
long-exposure --instance-dir ~/agent-instances/<name> status
```

Manager polls append structured notices to
`<instance-dir>/manager_notifications.jsonl`. Cron jobs and interactive
launchers read the same file, so manager awareness does not depend on which
LLM CLI launched the run.

For a **permanent** change of direction, use `resume "<new directive>"`
instead (stops the run cleanly, saves state, then reruns with the new
directive).

---

## Working with Local Files

Agents read, write, and execute files in the **working directory**, set by
`working_directory` in `long_exposure/config.yaml`. Set this to the
absolute path of the project directory you want agents to read and
write before launching a run, e.g.:

```
/path/to/your/project/workspace
```

Place any files you want agents to use here. Subdirectories are fine.
Examples:

- Drop a `.wls` script in the working dir and the worker can run it with
  `wolfram -script your_script.wls`.
- Drop a `.py` script and the worker can run it with `python3 your_file.py`.
- Agents can create new files, edit existing ones, and organize into
  subdirectories as the exploration progresses.

### What agents can do

Tool permissions are declared in `exploration-score.yaml` and enforced by
Claude Code at the CLI layer. File tools are auto-scoped to the working
directory; Bash is restricted to a pattern allowlist.

| Tool | Scope |
|---|---|
| `Read`, `Write`, `Edit`, `Glob`, `Grep` | Files under the working directory |
| `wolfram`, `wolframscript` | Run Wolfram scripts |
| `python`, `python3` | Run Python scripts |
| `pip`, `pip3` | Install Python packages |
| `pandoc` | Render reports to PDF (reporter agents) |
| `figure` | Unified figure CLI. Subcommands: `plot` (matplotlib), `flow` (D2 structural diagrams), `arch` (mingrammer/diagrams architecture diagrams), `check`, `list`. See [`figures.md`](figures.md). |
| `WebSearch` | Search the web |

Agents cannot run arbitrary shell commands.

---

## Emergent Parallelism

Long-exposure has two parallelism mechanisms that fire automatically when
the problem structure warrants. You don't schedule them — the agent
conditioning decides.

- **Parallel-cycle fan-out** (coarse-grained). The researcher emits a
  `<parallel_cycle_fanout>` block when 2–N genuinely independent
  sub-problems each need their own full Researcher → Worker → Auditor
  loop. The conductor spawns clones, waits at a merge barrier, and
  reuses the reporter at the merge point. Branch count is bounded by
  pool capacity (`fanout_cap = available_slots − 1`). Depth is fixed at
  1 — clones cannot recursively fan out.
- **Agent-teams fan-out** (fine-grained). Within a single turn, worker
  and auditor can spawn up to 3 teammate agents for embarrassingly-
  parallel sub-work. Teammates inherit the lead's model, effort, and
  token-budget verbatim; the team is created and deleted within the
  turn. Researcher / reporter / curator are team-disabled by design.

What this looks like in logs:

```
[long-exposure] Cycle 7: researcher emitted <parallel_cycle_fanout>
  branch 0: "Characterize convergence in regime A" → fork-abc/clone-0
  branch 1: "Characterize convergence in regime B" → fork-abc/clone-1
[long-exposure]   Spawning 2 clones...
[long-exposure]   Barrier: waiting for merge_report.md from 2 clones...
[long-exposure]   All clones merged. Running reporter in merge mode.
```

```
[long-exposure]   worker: ok (87.3s, ctx:52,100tok, out:8,900tok)
[long-exposure]   worker team: teammates=2 wall=87.3s
```

Cost posture: cycle fan-out roughly multiplies quota by branch count;
agent-teams adds ≈4× baseline for a 2-teammate shared-state task. Both
fire only when the structure of the work warrants — they're not
scheduled by the conductor.

For the full mechanics (clone lifecycle, barrier and graceful preemption,
merge synthesis, depth=1 rationale, agent-team configuration), see
[`parallelism.md`](parallelism.md).

---

## Concurrent Sessions

Multiple `long-exposure` processes can run on the same machine as long as
each has its own **instance directory**. An instance directory is the
per-session workspace that holds the state file, output folder, and MCP
config file. Instances share `sessions.db` (gem store) and global account
rotation state; everything else is isolated.

> **Each concurrent session must point at a distinct `--instance-dir`.**
> Two processes started with the same `--instance-dir` will contend on the
> state and signal files. A unique descriptive name per topic is the
> simplest convention (e.g. `~/agent-instances/microlocal`,
> `~/agent-instances/chern-simons`). The default single-session paths
> (`long_exposure/data/...`) count as one implicit instance — don't launch
> a second process with `--instance-dir long_exposure/data` while your
> default run is active.

### Starting a second exploration

While your default run continues at the stock paths:

```bash
long-exposure resume    # continues on default instance
```

Launch a second exploration on a different topic alongside it:

```bash
long-exposure --instance-dir ~/agent-instances/chern-simons \
    start "Explore Chern-Simons theory foundations"
```

Stop, clear, or resume a named instance by passing the same `--instance-dir`:

```bash
long-exposure --instance-dir ~/agent-instances/chern-simons stop
long-exposure --instance-dir ~/agent-instances/chern-simons resume
```

`AGENT_INSTANCE_DIR` is accepted as an environment-variable equivalent.

### What's shared vs. isolated

| Resource | Scope | Notes |
|---|---|---|
| `sessions.db` (gems, compaction history) | **Shared** | Gems written by any live session are immediately visible to every other session's MCP search. WAL mode + 5s busy timeout. |
| `~/.claude-pool-state.json` | **Shared, locked** | Multi-account pool state. When one session hits a rate limit and rotates, peers pick up the new primary on their next call. fcntl-locked. See [`multi-account-pool.md`](multi-account-pool.md). |
| `~/.claude-accounts-state.json` | **Shared, locked (legacy)** | Single-account rotation index for the legacy non-pool path. |
| `exploration_state.json` | **Per-instance** | Cycle counter, per-agent session UUIDs, failure counters, saved directive. |
| `output/` (status, reports) | **Per-instance** | |
| `mcp_config.json` | **Per-instance** | Its contents still point at the shared `sessions.db`, so gems cross-pollinate. |

### Overrides

`--instance-dir` sets defaults only. Explicit `--state` / `--output` flags
still win when you need more control (e.g. pointing two instances at the
same output dir for a comparison run).

To give a concurrent session its own isolated gem store (discouraged — you
lose cross-session memory), pass a `--config` file that sets `compact_db`
to an instance-local path.

---

## Auxiliary entry points

These are lower-level and rarely needed day-to-day.

### Interactive single-agent session (orchestrator)

One-off interactive use, not the multi-agent loop:

```bash
python3 -m long_exposure.orchestrator
```

Optional flags: `--config path/to/config.yaml`,
`--instance-dir ~/agent-instances/scratch`.

Starts an interactive conversation loop with persistent context. Type
messages, get responses. Context survives compaction events automatically.

### Running a score once (conductor)

Execute the multi-role flow once (not looping) and save a run log:

```bash
python3 -m long_exposure.conductor long_exposure/exploration-score.yaml \
    --output run_log.json
```

Print a specific agent's output:

```bash
python3 -m long_exposure.conductor long_exposure/exploration-score.yaml \
    --print-result research_brief
```

Run a score concurrently with another exploration by giving it its own
instance dir:

```bash
python3 -m long_exposure.conductor long_exposure/exploration-score.yaml \
    --instance-dir ~/agent-instances/score-run
```

### Re-run just the final reporter + curator

If an exploration stopped before completing its end-of-run packaging
(e.g., crashed during the final report, or you want to regenerate the
PDF and skill bundle against updated sources), run the standalone
final-reporter entry:

```bash
python3 run_final_reporter.py --state long_exposure/data/exploration_state.json
```

Optional flags: `--score`, `--config`, `--instance-dir`. The script
loads saved state, runs `_run_final_reporter` followed by `_run_curator`
exactly as the main loop would, and re-saves state with the outputs.
The final_reporter and curator sessions are cleared on entry so they
start fresh rather than resuming a stale session.

---

## Monitoring

Agent output prints to the terminal as the exploration runs. Key log lines:

```
[long-exposure] Cycle 3 starting...
[long-exposure]   researcher: ok (12.4s, ctx:45,230tok, out:3,200tok)
[long-exposure]   worker: ok (28.1s, ctx:52,100tok, out:8,900tok)
[long-exposure]   auditor: ok (9.7s, ctx:61,000tok, out:2,100tok)
[long-exposure]   Cooldown: 400s
[long-exposure]   worker compacted (~12,000 tokens summary). Fresh session on next cycle.
```

State is saved to `exploration_state.json` after every cycle. If the
process crashes, `resume` picks up from the last saved state.

### Multi-account observability

When a provider pool is active (for example ≥2 accounts in
`CLAUDE_ACCOUNT_POOL`, or a unified Claude+Codex pool), additional log
surfaces appear:

```
# format_pool_summary — printed alongside pool state changes:
pool: acct-prim=prim(1/3) tokens(in=1.2M cr=4.2M cc=120K out=210K),
      acct2=over(0/3) tokens(in=0 cr=0 cc=0 out=0) — 5 free slots

# Daily-sync boundary print (every loop.daily_sync_interval_hours):
[long-exposure] === Daily sync (<TIMESTAMP>) ===
... (final auditor / final reporter / curator log lines)
[long-exposure] Account usage delta since last sync (<TIMESTAMP>):
  acct-prim     in:  1.2M cr:  4.2M cc: 120K out: 210K  (share: 71.3%)
  acct2         in:  0.4M cr:  1.5M cc:  40K out:  72K  (share: 22.1%)
  acct3         in:  0.1M cr:  0.5M cc:  15K out:  24K  (share:  6.6%)
[long-exposure] === Daily sync done (<TIMESTAMP>) ===
[long-exposure] Planned rotation: acct-prim -> acct2 (no rotations in last 24.0h)
```

Token totals are cumulative since each account's `tokens_since`
timestamp; the share % is a quota-burn proxy. The **planned rotation**
fires only when no rotation has happened in the previous 24h —
otherwise the rate-limit-driven rotation handler has already done the
work and the pre-emptive rotation skips. Configurable via
`loop.planned_rotation_min_age_hours` (defaults to
`daily_sync_interval_hours`). See
[`multi-account-pool.md`](multi-account-pool.md) and
[`end-of-run-pipeline.md`](end-of-run-pipeline.md) for details.

If you launched in the foreground with `long-exposure launch`, these lines
stream in the same terminal or CLI transcript. If a CLI adapter chooses to
detach the process, follow the status with `long-exposure status` or tail the
adapter's log path directly.

---

## Key Files

Default single-session layout:

| File | Purpose |
|---|---|
| `long_exposure/config.yaml` | Model, thresholds, paths, tool permissions |
| `long_exposure/exploration-score.yaml` | Task, agent roles, flow, loop settings |
| `long_exposure/data/sessions.db` | Session history (SQLite, accumulates; shared across concurrent sessions) |
| `long_exposure/data/exploration_state.json` | Current cycle state (overwritten each cycle) |
| `long_exposure/data/mcp_config.json` | MCP server config (pointed at `sessions.db`) |
| `long_exposure/data/long-exposure.stop` | Signal file: stop after current agent |
| `long_exposure/data/long-exposure.clear` | Signal file: stop + archive state |
| `long_exposure/data/long-exposure.guide` | Guidance text injected into next cycle |
| `long_exposure/data/manager_notifications.jsonl` | Structured manager poll notices |
| `long_exposure/data/health_events.jsonl` | Off-nominal events log (silent fallbacks, rescues, retries) — `tail -n 50` to surface what went wrong silently |
| `~/.claude-pool-state.json` | Multi-account pool state (slot holders, rate-limit timestamps) |
| `~/.claude-accounts-state.json` | Legacy single-account rotation index |
| `~/.claude-pool-state.lock`, `~/.claude-accounts-state.lock` | Advisory fcntl locks guarding RMW |

When `--instance-dir DIR` is set, the per-instance equivalents live under
`DIR/`: `DIR/exploration_state.json`, `DIR/output/`, `DIR/mcp_config.json`,
and `DIR/long-exposure.stop` / `.clear` / `.guide` /
`manager_notifications.jsonl`. `sessions.db` and the account-state files stay
at their shared locations.

---

## Configuration Quick Reference

Settings in `long_exposure/config.yaml` worth knowing:

| Setting | Default | Notes |
|---|---|---|
| `model` | `opus` | Also supports `sonnet`, `haiku` |
| `compact_threshold` | `0.90` | Lower = more frequent compaction |
| `context_proximity.max_gems` | `7` | Past sessions injected as context |
| `cli_timeout` | `0` | Per-agent timeout in seconds (0 = none) |

Settings in `long_exposure/exploration-score.yaml`:

| Setting | Default | Notes |
|---|---|---|
| `loop.max_cycles` | `null` | null = unlimited; set a number to auto-stop |
| `loop.cycle_cooldown_seconds` | `400` | Pause between cycles |
| `loop.report_interval` | `3` | Reporter runs every N cycles |

For the full schema reference (every config + score key), see
[`configuration-reference.md`](configuration-reference.md). For
deployment / installation detail, see [`local-setup.md`](local-setup.md).

---

## Editing the Exploration Task (via score YAML)

Editing the score file's `task:` field affects only *fresh* starts with no
override — it does NOT redirect an in-flight or resumed exploration. To
change direction on an existing run, prefer one of:

- `long-exposure resume "<new directive>"` — redirect, keep context.
- `long-exposure clear` + `long-exposure launch "<new directive>"` — fresh start,
  preserved sessions.db gems.

If you do want the score YAML to be the source of truth, clear state first:

```
long-exposure clear
long-exposure launch
```

(A no-arg `launch` or `start` with state absent falls through to the score
YAML's `task:` field.)
