# Multi-Account Pool

Long-exposure can run a single research campaign across multiple provider
config directories ("accounts") so a rate-limit on one account rotates work
to another without interrupting the cycle. Claude and Codex use the same
per-provider pool state machine; an optional unified selector can route a root
run and fan-out clones across both configured pools. This doc covers the pool
state machine, slot lifecycle, rate-limit detection, unified provider mode,
and operational rules.

**Sources of truth:** `long_exposure/pool.py`, `long_exposure/unified_pool.py`,
`long_exposure/orchestrator.py`, `long_exposure/exploration.py`
(cycle-level rotation), `long_exposure/fanout.py` (per-clone slot acquisition).

---

## What an "account" is

An account is a Claude Code config directory: a path containing its own
`.credentials.json`. Each account corresponds to one Claude Max plan
seat with its own 5-hour rolling quota window. Naming is convention
only (`~/.claude`, `~/.claude-acctA`, `~/.claude-acctB`, …); the pool
refers to accounts by full path.

To create one:

```bash
mkdir -p ~/.claude-acctN
CLAUDE_CONFIG_DIR=~/.claude-acctN claude
# inside the interactive session, /login → /exit
CLAUDE_CONFIG_DIR=~/.claude-acctN claude -p "say ok" --output-format json
# verify non-interactive mode works (this is what long-exposure uses)
```

A working account produces a JSON envelope with `is_error: false`. If
that fails, the credentials are bad — re-run the login.

---

## Configuring a Claude pool

The pool reads two environment variables:

```bash
CLAUDE_ACCOUNT_POOL=/path/to/.claude,/path/to/.claude-acctA,/path/to/.claude-acctB
```

(or the legacy synonym `CLAUDE_ACCOUNTS` — same parsing).

Order matters on **first init**: position 0 becomes the primary;
positions 1+ start cold. On subsequent runs, the persisted
`~/.claude-pool-state.json` is reused, and `_ensure_account_entries`
just refreshes the account list to match the current env-var list
(adds new accounts as cold, drops removed ones).

To re-seed the primary, archive both state files first:

```bash
mv ~/.claude-pool-state.json ~/.claude-pool-state.json.bak.$(date +%Y%m%dT%H%M%S) 2>/dev/null
mv ~/.claude-accounts-state.json ~/.claude-accounts-state.json.bak.$(date +%Y%m%dT%H%M%S) 2>/dev/null
```

The next run seeds primary from the current `CLAUDE_ACCOUNT_POOL`
ordering.

### CLAUDE_FORCE_ACCOUNT (debug pin)

Set to an index (`0`, `1`, `2`) into the pool, or to a full directory
path. Pins every `claude -p` call to that one account, bypassing
rotation. Used by clones (set automatically by the parent at spawn
time) and by operators debugging a specific account.

Setting `CLAUDE_FORCE_ACCOUNT` does **not** disable pool semantics.
`pool.is_active()` returns True whenever ≥2 accounts are configured;
the force-pin only restricts which account each call uses, not whether
the pool state machine runs. (This is deliberate — see the docstring
in `pool.py` for why short-circuiting on the force pin caused
the recent silent no-rotation failure.)

To debug-pin without engaging pool semantics, leave
`CLAUDE_ACCOUNT_POOL` unset and only set `CLAUDE_FORCE_ACCOUNT`.

Codex uses the same semantics with Codex-specific names:

| Purpose | Claude | Codex |
|---|---|---|
| Pool env | `CLAUDE_ACCOUNT_POOL` / `CLAUDE_ACCOUNTS` | `CODEX_ACCOUNT_POOL` / `CODEX_HOMES` |
| Force pin | `CLAUDE_FORCE_ACCOUNT` | `CODEX_FORCE_ACCOUNT` |
| Child config/auth dir | `CLAUDE_CONFIG_DIR` | `CODEX_HOME` |
| Pool state file | `~/.claude-pool-state.json` | `~/.codex-pool-state.json` |

Each `CODEX_HOME` directory must be authenticated for the Codex CLI and
verified with `codex exec` in a trusted workspace before adding it to
`CODEX_ACCOUNT_POOL`.

---

## State machine

Each account is in exactly one of four states:

```
  cold       never used in this run, or rate-limit cleared via cooldown
  primary    hosts root agents and the first slots of any fan-out
  overflow   hosts leaf slots beyond primary's capacity
  cooling    rate-limited recently; not callable yet
```

Transitions:

```
  start                       →  first account = primary, others = cold
  primary  --rate-limit-->    →  cooling
                                  (caller invokes promote_fresh →
                                   new primary, demoted to overflow if
                                   not already cooling)
  cold|overflow --acquire-->  →  overflow (or stays primary if it was)
  overflow --rate-limit-->    →  cooling
  cooling  --thaw elapsed-->  →  cold
```

`PER_ACCOUNT_SLOT_CAP = 3` (empirical ceiling on simultaneous in-flight
provider CLI calls per account). `available_slots()` sums free capacity
across primary + overflow + cold accounts; cooling accounts contribute
zero. `fanout_cap()` returns `available_slots()` — the root's sequential
calls are covered by the ledger slot it acquires at startup (already
excluded from `available_slots()`), so no extra reserve is subtracted.

### Freshness-based promotion

When the primary rate-limits, `promote_fresh()` picks the *coldest*
available account. Order:

1. Coldest cold account (lowest `last_active_at`; never-used ranks
   freshest of all).
2. Overflow account with longest time idle.
3. None — all cooling. Caller falls back to adaptive cooldown.

One promotion per primary rate-limit; no cascading reshuffles. The
old primary is demoted to overflow (or stays cooling if the rate-limit
event marked it so first).

### Cooldown timers

Each rate-limited account records `rate_limited_at` (ISO timestamp).
At every cycle boundary `pool.thaw_eligible(cooldown_seconds=800)`
moves any cooling account back to cold once `(now - rate_limited_at)
≥ 800s` (default `2 × cycle_cooldown_seconds = 2 × 400`).

A monthly-exhausted account is just an account that re-cools each
thaw. Per-minute vs monthly distinction is deferred (see


---

## Slot lifecycle

A "slot" is one PID's reservation of one account's per-account slot
budget. Slots are tracked in `~/.claude-pool-state.json` under each
account's `slot_holders` list.

```python
holder = {
    "pid": int,         # owning process PID
    "role": "root" | "clone" | "agent",
    "since": ISO8601,
    "fork_id": str,     # only for clone slots
    "clone_k": int,     # only for clone slots
}
```

Three independent release paths protect against leaks:

1. **Clone-side `atexit`.** When a clone bootstraps, it re-tags its
   slot via `pool.update_slot_pid(fork_id, clone_k, os.getpid())`
   (early in `exploration.run_exploration` before the cycle loop) and
   registers an `atexit` handler that calls
   `release_slot_by_branch(fork_id, clone_k)` on clean exit.
   Authoritative path; runs on 99% of normal terminations.

2. **Conductor barrier release.** When the parent's barrier loop
   collapses (all clones finished or wall-cap hit), the conductor
   calls `release_slot_by_branch(fork_id, clone_k)` for every clone.
   Idempotent. Catches SIGTERM / SIGKILL / atexit-bypassing exits.

3. **Heartbeat sweep.** At every root cycle boundary,
   `pool.heartbeat_sweep()` walks all holders and removes any whose
   PID is no longer alive. Recovers slots from clones that died
   without anyone calling release (e.g., parent crashed too, both
   atexit and barrier-release missed).

This means slot leakage requires **all three** paths to fail
simultaneously — effectively impossible in practice. See
the os._exit case (atexit bypassed but
heartbeat recovers next cycle).

### PID race avoidance

The parent acquires a clone's slot **before** Popen, because
`CLAUDE_FORCE_ACCOUNT` must be set in the clone's env before Popen.
At acquire time the holder is tagged with the parent's PID. The clone
re-tags the slot with its own PID at startup (in
`exploration.run_exploration` clone bootstrap block); the parent also
calls `update_slot_pid` post-Popen as an idempotent fallback to close
the small window before the clone interpreter starts. The clone-side
re-tag is the authoritative path that survives parent crashes.

---

## Rate-limit detection

`orchestrator._is_rate_limit` covers three signalling paths from the
Claude CLI. **All three are load-bearing**; missing any one causes
silent misbehavior on a known CLI shape.

1. **Non-zero exit + RL text in stderr/stdout.** Standard CLI failure
   path. Matched against `_RATE_LIMIT_SIGNATURES` (broad on purpose:
   `"429"`, `"rate limit"`, `"rate-limit"`, `"rate_limit"`,
   `"usage limit"`, `"quota"`, `"limit reached"`).

2. **`api_error_status == 429` in the JSON envelope.** Authoritative
   structured signal. Can occur even with exit 0.

3. **`is_error: true` + RL text in `result`.** Catches the case where
   the CLI returns an exit-0 envelope marked as an error with rate-
   limit text in the result body. Without this layer, a rate-limit
   would silently come back as a low-output "successful" cycle that
   would spuriously trip the topic-exhaustion heuristic.

Detection is intentionally permissive on the text-match side. False
positives (rotating on a non-RL error) are self-healing — the next
account works. False negatives (treating a real RL as success) cause
silent quota burn. Tightening the signature list would risk false
negatives on new 429 wordings; the broad list is the conservative
choice.

### Rotation safety net

When the cycle loop rotates on rate-limit, it tracks
`rotation_attempts` in memory and breaks at `>= len(accounts)`
regardless of whether the legacy `~/.claude-accounts-state.json` write
succeeded. This protects against the scenario where a disk-full
condition silently corrupts the state file and the rotation
otherwise spins forever against a stale `active_index`.

---

## Two state files

| File | Schema | Written by | Read by |
|---|---|---|---|
| `~/.claude-pool-state.json` | `{schema_version, last_rotation_at, accounts: [{dir, state, slots_used, slot_holders, rate_limited_at, last_active_at, tokens_input, tokens_output, tokens_cache_read, tokens_cache_creation, tokens_since}]}` | slot lifecycle (`acquire_slot` / `release_slot` / `update_slot_pid` / `release_slot_by_branch` / `heartbeat_sweep`); rate-limit (`mark_rate_limited`, `thaw_eligible`, `promote_fresh`); usage (`record_usage`); rotation (`record_rotation`, `promote_fresh`) | `pool.pool_state`, `primary_dir`, `available_slots`, `fanout_cap`, `last_rotation_age_hours`, `get_usage_snapshot`, `format_pool_summary` |
| `~/.claude-accounts-state.json` | `{active_index: int}` | `orchestrator._save_account_state` | `orchestrator._parse_accounts`, `pool.init_pool` (first-run seed) |

The pool-state schema grew with two additions:
**top-level `last_rotation_at`** and **per-account
`tokens_*` + `tokens_since`**. Both are migrated lazily —
`record_usage` and `record_rotation` use defensive `.get(field, 0)`
on read, so old-schema state files continue to work and upgrade in
place on the first call that touches an account.

The legacy `accounts-state.json` is single-account-rotation bookkeeping
from the pre-pool era. It still exists for the cycle loop's legacy
rotation path (when `pool.is_active()` is False) and is consulted once
on first-run pool init to seed the primary so a Stage-0 deployment
upgrades cleanly.

Both writes use `os.replace()` on a temp file (atomic). Both reads
are guarded by `fcntl` advisory locks via `pool._pool_lock()` and
`orchestrator._account_state_lock()`.

**Neither file is archived on `clear`.** The pool is advisory; cooling
timers self-heal at the next cycle boundary. If you want a clean
restart, archive the files manually.

---

## Unified Claude+Codex pool

If both Claude and Codex pools are configured, unified mode is active by
default:

```bash
export CLAUDE_ACCOUNT_POOL="$HOME/.claude-a,$HOME/.claude-b"
export CODEX_ACCOUNT_POOL="$HOME/.codex-a,$HOME/.codex-b"
```

`long_exposure/unified_pool.py` does not replace `pool.py`. It is a thin
selector over the existing per-provider pools:

- initializes every configured provider pool,
- picks a provider with free capacity,
- acquires a normal provider-local slot,
- pins the process via `LONG_EXPOSURE_LLM_PROVIDER` plus the provider's
  force-account env,
- routes release, PID updates, heartbeat sweeps, thawing, and usage writes
  back to the provider that owns the slot.

Set `LONG_EXPOSURE_UNIFIED_POOL=disabled` to force normal single-provider
behavior even when both pools exist. Clone processes are launched with unified
mode disabled so they cannot recursively select providers after the parent has
assigned a slot.

### Unified root rotation

Rate-limit rotation in unified mode is provider-agnostic. The root process:

1. Marks the current unified root holder's account cooling in its provider's
   pool.
2. Releases the old root slot.
3. Acquires a fresh root slot from the other provider first, falling back to
   the current provider if needed.
4. Pins `LONG_EXPOSURE_LLM_PROVIDER` and the matching force-account env to the
   new holder.
5. Clears native provider session IDs before retrying the cycle or
   out-of-cycle agent.

This prevents passing a Claude session UUID to Codex or vice versa. The
tradeoff is that unified rotation does not preserve provider-native session
continuity; durable continuity remains in `sessions.db`, workspace files, and
agent summaries.

### Unified fan-out capacity

`unified_pool.fanout_cap()` sums free slots across configured Claude and Codex
pools, then applies the same root-slot reserve as normal fan-out. Clones are
pinned to the provider/account selected by the parent and release through the
origin provider. Gemini is intentionally excluded from unified pooling until
multi-account Gemini OAuth homes are validated.

### Known limitation

Planned daily rotation remains provider-local. Rate-limit rotation is
provider-agnostic; pre-emptive daily rotation may rotate within the active
provider. This is documented in `docs/gaps.md` because it is an efficiency
limitation, not a correctness bug.

---

## Interaction with per-agent providers

The `agent_models` block in `config.yaml` (see
`docs/configuration-reference.md` — Per-agent LLM routing) can assign a
different provider to each agent type. When agents resolve to **more than one
distinct provider**, the run enters **per-agent-pinned mode** and multi-account
pooling is **deferred** for that run — the same posture the interactive
transport already takes. On startup you'll see:

```
[long-exposure] Per-agent providers active (claude, codex): each agent is
pinned to its configured provider; multi-account pooling is deferred for this run.
```

Why this is a hard XOR rather than a blend: the unified pool's whole model is
**one active provider per root process**, chosen by capacity and rotated across
providers on a 429. That is fundamentally incompatible with a *fixed* per-agent
provider assignment — the pool would rotate the researcher off Claude onto
Codex mid-run, silently overriding the template. Rather than let the two
mechanisms fight, per-agent-pinned mode pins each agent's turn to its configured
provider (via the same `LONG_EXPOSURE_LLM_PROVIDER` swap the unified pool uses
internally) and skips pool init, pool-aware rotation, and cross-provider unified
rotation for the run. Per-agent **model** and **effort** still apply in full.

What still holds in per-agent-pinned mode:

- Each agent runs on its configured provider's **default account** (or the
  single account you have logged in for that provider).
- Compaction of an agent's session runs under that agent's provider (it must, to
  resume a provider-native session).
- A rate limit on a pinned agent surfaces through the normal adaptive-cooldown
  path (no account rotation), because rotation across heterogeneous providers is
  undefined.

What is **not** supported (deferred, see `docs/gaps.md`): heterogeneous per-agent
providers **and** simultaneous multi-account rotation. If you need many accounts
of one provider rotated for rate-limit resilience, keep the providers
homogeneous (the default) and use the pool; if you need deterministic per-agent
providers, use `agent_models` and rely on single accounts per provider.

Homogeneous routing — every agent on the same provider, including the shipped
default (all Claude) — is **not** pinned mode: pooling (single-provider and
unified) works exactly as before.

---

## Per-account usage tracking

Cumulative four-field token counters per account, hooked at the
single API chokepoint (`orchestrator._invoke_claude` after envelope
return). Helps the operator see whether usage is balanced across the
pool without reading individual cycle logs.

### What's tracked

Per account in pool state:

| Field | Source |
|---|---|
| `tokens_input` | `usage.input_tokens` |
| `tokens_output` | `usage.output_tokens` |
| `tokens_cache_read` | `usage.cache_read_input_tokens` |
| `tokens_cache_creation` | `usage.cache_creation_input_tokens` |
| `tokens_since` | ISO timestamp set lazily on first observation |

### How it's surfaced

1. **`pool.format_pool_summary()`** — extends the existing one-liner
   with K / M / G / T-suffixed token totals per account:
   ```
   pool: acct-prim=prim(1/3) tokens(in=1.2M cr=4.2M cc=120K out=210K),
         acct2=over(0/3) tokens(in=0 cr=0 cc=0 out=0) — 5 free slots
   ```
2. **Daily-sync boundary print** — `_print_account_usage_delta` in
   `exploration.py` snapshots usage at sync entry and prints
   per-account delta + share % at sync exit:
   ```
   [long-exposure] Account usage delta since last sync (<TIMESTAMP>):
     acct-prim     in:  1.2M cr:  4.2M cc: 120K out: 210K  (share: 71.3%)
     acct2         in:  0.4M cr:  1.5M cc:  40K out:  72K  (share: 22.1%)
     acct3         in:  0.1M cr:  0.5M cc:  15K out:  24K  (share:  6.6%)
   ```
   The share% uses a fixed quota-burn proxy weighting:
   `input + cache_read·0.1 + cache_creation·1.25 + output·5.0`.
   Raw four-field values are also surfaced for the audit trail.
3. **`pool.get_usage_snapshot()`** — programmatic read for tooling.
4. **`pool.reset_usage_counters()`** — manual reset gesture (operator
   one-liner; never auto-called).

### Operational rules

- Hook fires on the **success path only** of `_invoke_claude`. Failed
  calls (rate-limit, CLI error) raise before the hook and don't count.
- Skipped when `pool.is_active() == False` (single-account /
  pinned-without-pool modes don't track).
- Concurrent writes are serialized via the existing `_pool_lock` fcntl
  lock. Verified at 250 concurrent writes from 5 processes — zero
  write loss.
- Cumulative-only by design (no rolling window) — rate is computable
  from `tokens_since` if needed.

---

## Planned 24h rotation

Pre-emptively rotates the primary after each daily sync IF no
rotation has happened in the previous 24 hours. Spreads usage across
the pool when a primary doesn't naturally rate-limit within the
window. Important for paid-plan accounts where a primary may not
naturally hit its cap day-over-day.

### How it works

A single new top-level field `last_rotation_at` in pool state. Stamped
at pool init time (warmup window) and refreshed by `pool.promote_fresh()`
on every successful rotation (rate-limit-driven OR planned —
`promote_fresh` filters PRIMARY out of candidates so a non-None
return is guaranteed to be a real rotation).

The planned-rotation block lives in the cycle loop's daily-sync
`finally` clause (`exploration.py`, immediately after the daily-sync
state save). Gates:

1. `pool.is_active()` — only fires when ≥2 accounts are configured.
2. `not _is_clone()` — root only (clones don't rotate; they're pinned).
3. `not post_merge_pending` — defer during fan-out collapse.
4. `not _stop_requested` — skip if shutting down.
5. `pool.last_rotation_age_hours() >= planned_rotation_min_age_hours`
   (default: same as `daily_sync_interval_hours`, i.e. 24h) — or None
   meaning "never rotated, treat as eligible."

When the gates pass, the block calls `pool.promote_fresh()`. On a
non-None return it does THREE things — all load-bearing:

1. **Set the provider-specific force-account env to `new_primary`** —
   without this, the parent's active-account helper reads the old pinned
   value and continues sending calls to the old primary, leaving the
   rotation observable in pool state but invisible to running agents.
2. **`agent_sessions.clear()`** — provider-native session IDs are
   per-account; resuming an old account's session on the new account can
   fail with "session not found." Clearing forces fresh sessions on the
   next cycle.
3. **`health_events.append_event("planned_rotation", ...)`** —
   informational event for operator observability.

### Configuration

| Knob | Default | Meaning |
|---|---|---|
| `loop.planned_rotation_min_age_hours` | `loop.daily_sync_interval_hours` (24) | Minimum age of last rotation before a planned rotation fires. Set higher to space planned rotations more sparsely than syncs. |

### What gets demoted, what gets cooled

- The new primary is promoted to PRIMARY state.
- The old primary is demoted to OVERFLOW (still callable for clone
  slots). **NOT to COOLING** — planned rotation is pre-emptive, not
  punitive. Contrast with rate-limit rotation, which marks the old
  primary as COOLING because it just hit a 429.

### Edge cases

- All accounts cooling: `promote_fresh` returns None; the block
  records a `planned_rotation_skipped` event and falls through. The
  existing adaptive-cooldown path drives recovery on the next cycle.
- First-time behavior: `init_pool` stamps `last_rotation_at = now()`,
  so the first planned rotation can fire 24h after deployment.

### Interaction with rate-limit rotation

`promote_fresh` records the rotation regardless of cause. So:
- Rate-limit happens at hour 18 → planned rotation timer resets.
- Next daily-sync at hour 24 sees `age = 6h < 24h` → SKIPS planned
  rotation. No over-rotation.
- If no rate-limit happens, daily-sync at hour 24 fires planned
  rotation. Pre-emptive spread.

---

## Operational rules

1. All root agents (researcher, post-merge worker, auditor, reporter)
   run sequentially on primary; only fan-out clones use overflow.
2. The cycle loop clamps `<parallel_cycle_fanout>` proposals to
   `pool.fanout_cap()` and informs the researcher of the clamp via
   `live_guidance` on the next cycle.
3. Per-account cooldown is account-specific and clock-based. Different
   accounts thaw at different times — the pool naturally handles
   per-minute vs monthly mixed timelines.
4. Agent-teams (intra-cycle teammates) inherit the parent's
   `CLAUDE_FORCE_ACCOUNT`; they do **not** consume separate pool slots.
5. If `pool.promote_fresh()` returns None (all accounts cooling), the
   cycle loop falls back to adaptive cooldown (existing behavior:
   `2 × cycle_cooldown_seconds` then retry).
6. Compaction and checkpoint paths in the orchestrator are
   pool-aware: a primary rate-limit during compaction promotes a
   fresh primary and retries once before raising.
7. Per-account usage is recorded after every successful `_invoke_claude`
   call when the pool is active. Failed calls don't count.
8. Planned rotation fires after each daily sync iff no rotation
   happened in the prior `planned_rotation_min_age_hours` window.

---

## Quota-overlap caveat

If you also drive a Claude Code session yourself from one of these
directories (e.g., for ad-hoc inspection of the live run), that
session and long-exposure compete for the same Max plan quota.
Symptom: unexpected 429s on an account that should be fresh.

Mitigation: dedicate at least one directory exclusively to
long-exposure, or create a fresh debug account that's NOT in the
pool. The pool does not track usage source; it assumes one agent per
slot.

---

## Adding an account

1. `mkdir -p ~/.claude-acctN`
2. `CLAUDE_CONFIG_DIR=~/.claude-acctN claude` → `/login` → `/exit`
3. `CLAUDE_CONFIG_DIR=~/.claude-acctN claude -p "say ok" --output-format json` to verify non-interactive mode works
4. Append the path to `CLAUDE_ACCOUNT_POOL` and start (or resume).
   `pool.init_pool()` adds it as a cold entry on first observation.

To verify the pool sees it:

```bash
python3 -c "
import os
os.environ['CLAUDE_ACCOUNT_POOL']='\$HOME/.claude,...'
from long_exposure import pool
print(pool.format_pool_summary())
"
```

## Removing an account

Drop the directory from `CLAUDE_ACCOUNT_POOL` and restart.

## Gemini Provider Pool

Gemini multi-account pooling is disabled for now. Gemini CLI supports
`GEMINI_CLI_HOME`, but separate Google-account/free-tier homes require
operator-managed OAuth setup and have not been live-validated under the
long-exposure pool state machine.

A Gemini run ignores `GEMINI_ACCOUNT_POOL` and `GEMINI_HOMES`; use a
single authenticated Gemini CLI account. Parallel fan-out is still supported
on that account because fan-out creates concurrent Gemini sessions, not
separate account pins.

---

## Code references

- Pool state machine: `long_exposure/pool.py` (states, transitions,
  acquire/release/sweep, promotion, usage counters).
- Unified selector: `long_exposure/unified_pool.py`.
- Freshness promotion + rotation recording: `long_exposure/pool.py:promote_fresh`
  (records `last_rotation_at` on success).
- Slot lifecycle: `pool.py` (acquire/release/sweep);
  `fanout.py` (parent-side acquire + post-Popen re-tag);
  `exploration.py` clone bootstrap block (clone-side re-tag + atexit).
- Rate-limit detection: `orchestrator.py` (`_is_rate_limit`,
  `_RATE_LIMIT_SIGNATURES`, `_format_cli_failure_context`).
- Pool-aware compaction/checkpoint: `orchestrator.py`
  (`call_claude_pool_aware`).
- Rotation safety net and root rotation: `exploration.py` (cycle loop,
  `_rotate_unified_root_after_rate_limit`).
- **Per-account usage tracking:** `pool.record_usage`,
  `pool.get_usage_snapshot`, `pool.reset_usage_counters`,
  `pool._human_tokens`, `pool.format_pool_summary` (extended).
  Hook site: `orchestrator._invoke_claude` (post-envelope, success
  path). Daily-sync print: `exploration._snapshot_account_usage`,
  `exploration._print_account_usage_delta`.
- **Planned 24h rotation:** `pool.record_rotation`,
  `pool.last_rotation_age_hours`. Hook site: cycle loop's daily-sync
  `finally` clause in `exploration.py`. The force-account env hot-swap
  and `agent_sessions.clear()` in that block are load-bearing.
