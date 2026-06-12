"""Account pool with pinned overflow (Stage 1).

State machine over a small ordered list of Claude config dirs ("accounts").
Each account is in exactly one of: cold, primary, overflow, cooling.

  cold     never used in this run, or rate-limit cleared
  primary  hosts root agents and the first slots of any fan-out
  overflow hosts leaf slots beyond primary's capacity
  cooling  rate-limited recently; not callable yet

Transitions:
  start                        -> first account = primary, others = cold
  primary  --rate-limit-->     cooling
                               (caller invokes promote_fresh -> new primary)
  cold|overflow --acquire-->   overflow (or stays primary if it already is)
  overflow --rate-limit-->     cooling
  cooling  --thaw-elapsed-->   cold

Slot tracking: each account holds up to PER_ACCOUNT_SLOT_CAP simultaneous
in-flight slots. Slots are file-recorded with PID+role for orphan recovery.

The pool ledger lives at ~/.claude-pool-state.json and is updated under
fcntl-locked RMW. Operations are safe across concurrent root + clone
processes.

Backward compatibility:
  - If CLAUDE_ACCOUNT_POOL is set, parse it as a comma-separated list.
  - Else if CLAUDE_ACCOUNTS is set, treat it as the pool (same data, old
    name; existing deployments seamlessly upgrade).
  - Else if LONG_EXPOSURE_CLONE_POOL_CONFIG is set, parse it the same way.
    The fan-out conductor pops the primary pool env vars from a pinned
    clone's env (so the clone never runs the orchestrator's multi-account
    rotation loop) and stashes the original value under this name so
    clone-side pool semantics (slot re-tag + atexit release, §6.2
    rate-limit mark+release) keep working.
  - Else single-account mode (callers see is_active()==False).

CLAUDE_FORCE_ACCOUNT does NOT bypass the pool. is_active() deliberately
ignores the force pin: the pool itself sets CLAUDE_FORCE_ACCOUNT on the
parent (to pin the active primary) and on each clone (to pin the assigned
dir), so short-circuiting on the pin would disable every pool operation
right after startup pinning — the failure mode behind the 9h46m
no-rotation incident (see is_active's docstring). The pin only restricts
which account each call uses (orchestrator._resolve_force_account); the
pool state machine still runs. To debug-pin WITHOUT pool semantics, set
CLAUDE_FORCE_ACCOUNT while leaving CLAUDE_ACCOUNT_POOL / CLAUDE_ACCOUNTS
unset — parse_pool_config returns [] and is_active() returns False.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from long_exposure import provider as _provider

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PER_ACCOUNT_SLOT_CAP = 3  # default empirical per-account in-flight CLI ceiling

# Default cooldown matches the score's `cycle_cooldown_seconds` (400s) doubled
# — same value the existing adaptive_cooldown uses for failure cycles. A
# monthly-exhausted account simply re-cools each thaw; per-minute vs monthly
# distinction is deferred (98_DEFERRED.md D1).
DEFAULT_COOLDOWN_SECONDS = 800

_POOL_STATE_PATH = Path.home() / ".claude-pool-state.json"
_POOL_LOCK_PATH = Path.home() / ".claude-pool-state.lock"


def _pool_state_path() -> Path:
    return _provider.pool_state_path()


def _pool_lock_path() -> Path:
    return _provider.pool_lock_path()


def _slot_cap() -> int:
    """Return the provider-local in-flight slot cap.

    Defaults to the conservative historical cap. Operators can raise or lower
    the cap per provider for a specific live run with
    LONG_EXPOSURE_<PROVIDER>_SLOT_CAP, e.g. LONG_EXPOSURE_CODEX_SLOT_CAP=5.
    Invalid values fall back to the default rather than blocking startup.
    """
    provider = _provider.current_provider().upper()
    for name in (f"LONG_EXPOSURE_{provider}_SLOT_CAP", "LONG_EXPOSURE_POOL_SLOT_CAP"):
        raw = os.environ.get(name)
        if not raw:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return PER_ACCOUNT_SLOT_CAP

# Account states.
COLD = "cold"
PRIMARY = "primary"
OVERFLOW = "overflow"
COOLING = "cooling"


# ---------------------------------------------------------------------------
# Pool config parsing (env-var driven)
# ---------------------------------------------------------------------------


# Fallback env var for pinned clones. The fan-out conductor (_spawn_clone)
# pops the provider pool env vars (CLAUDE_ACCOUNT_POOL etc.) from the clone's
# env so the clone never runs the orchestrator's multi-account rotation loop
# (orchestrator._parse_accounts reads only the primary names). It stashes the
# original value here so clone-side pool state functions — is_active,
# update_slot_pid, release_slot_by_branch, mark_rate_limited — still see the
# pool. Without this, a pinned clone saw is_active()==False, never re-tagged
# or released its slot, and a rate-limited clone could not mark its account
# cooling or exit the §6.2 path.
CLONE_POOL_CONFIG_ENV = "LONG_EXPOSURE_CLONE_POOL_CONFIG"


def parse_pool_config() -> list[str]:
    """Return the ordered list of account config dirs.

    CLAUDE_ACCOUNT_POOL takes precedence; otherwise CLAUDE_ACCOUNTS (legacy);
    otherwise LONG_EXPOSURE_CLONE_POOL_CONFIG (set by the fan-out conductor
    in pinned clones — see CLONE_POOL_CONFIG_ENV above).
    Returns [] when none is set — single-account mode.
    Empty entries are filtered.
    """
    dirs = _provider.parse_pool_env()
    if dirs:
        return dirs
    raw = os.environ.get(CLONE_POOL_CONFIG_ENV, "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return []


def is_active() -> bool:
    """True iff pool logic should engage (≥2 accounts in pool config).

    Engages whenever CLAUDE_ACCOUNT_POOL (or legacy CLAUDE_ACCOUNTS) names
    ≥2 accounts. CLAUDE_FORCE_ACCOUNT is NOT consulted here — the pool
    itself sets that env var on the parent (to pin the active primary)
    and on each clone (to pin the assigned dir). Checking CLAUDE_FORCE_ACCOUNT
    would short-circuit every pool operation AFTER startup pinning,
    silently disabling per-clone slot acquisition, pool-aware rotation,
    cycle-boundary heartbeat sweeps, dynamic fanout cap, and
    call_claude_pool_aware retry — exactly the failure mode observed in
    the recent live test (no rotation in 9h46m; clones stuck on
    inherited parent's primary; root barrier-stuck).

    To debug-pin without pool semantics, set CLAUDE_FORCE_ACCOUNT WITHOUT
    setting CLAUDE_ACCOUNT_POOL/CLAUDE_ACCOUNTS — parse_pool_config
    returns [] and is_active returns False. Setting both is a no-op for
    this function (pool engages); CLAUDE_FORCE_ACCOUNT still pins
    individual call_claude attempts via _resolve_force_account.
    """
    return len(parse_pool_config()) >= 2


# ---------------------------------------------------------------------------
# Cross-process lock + atomic state I/O
# ---------------------------------------------------------------------------


@contextmanager
def _pool_lock():
    """Exclusive advisory lock for pool-state RMW.

    Mirrors orchestrator._account_state_lock — degrades silently if flock is
    unavailable so concurrency degrades to last-writer-wins rather than
    crashing.
    """
    try:
        fh = open(_pool_lock_path(), "a+")
    except OSError:
        yield
        return
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except OSError:
            yield
            return
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _empty_state() -> dict:
    return {"schema_version": 1, "accounts": []}


def _load_state_unlocked() -> dict:
    try:
        return json.loads(_pool_state_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _empty_state()


def _save_state_unlocked(state: dict) -> None:
    """Atomic write. Silent on failure — state is advisory."""
    try:
        state_path = _pool_state_path()
        tmp = state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str))
        os.replace(tmp, state_path)
    except OSError:
        pass


def _ensure_account_entries(state: dict, dirs: list[str]) -> dict:
    """Ensure every dir in `dirs` has an entry in state['accounts'], in the
    given order. Adds missing accounts as cold. Removes accounts not in
    `dirs` (they were dropped from the pool config). Idempotent.
    """
    by_dir = {a["dir"]: a for a in state.get("accounts", [])}
    new_accounts = []
    for i, d in enumerate(dirs):
        if d in by_dir:
            new_accounts.append(by_dir[d])
        else:
            # First account in fresh state seeds as primary; rest cold.
            initial_state = PRIMARY if (i == 0 and not by_dir) else COLD
            new_accounts.append({
                "dir": d,
                "state": initial_state,
                "slots_used": 0,
                "slot_holders": [],
                "rate_limited_at": None,
                "last_active_at": None,
            })
    # Promote first cold to primary if no primary exists (e.g., after every
    # account was dropped + repopulated). This keeps the invariant
    # "exactly one primary when pool is active" lazily satisfied.
    if new_accounts and not any(a["state"] == PRIMARY for a in new_accounts):
        for a in new_accounts:
            if a["state"] == COLD:
                a["state"] = PRIMARY
                break
    state["accounts"] = new_accounts
    return state


def _load_legacy_active_index() -> int:
    """Read the legacy ~/.claude-accounts-state.json active_index, if any."""
    legacy = _provider.accounts_state_path()
    try:
        return int(json.loads(legacy.read_text()).get("active_index", 0))
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# Public init + read
# ---------------------------------------------------------------------------


def init_pool() -> dict:
    """Initialize the pool ledger from CLAUDE_ACCOUNT_POOL / CLAUDE_ACCOUNTS.

    On first run (no pool-state file), seeds the primary using the legacy
    `~/.claude-accounts-state.json` active_index (if present) so a Stage-0
    deployment upgrades cleanly. Returns the loaded state dict.

    Idempotent: re-runs on existing state just refresh the account list to
    match current config (adding cold entries for new accounts, dropping
    entries for accounts removed from config).
    """
    dirs = parse_pool_config()
    if not dirs:
        return _empty_state()

    with _pool_lock():
        state = _load_state_unlocked()
        first_run = not state.get("accounts")

        if first_run:
            legacy_idx = _load_legacy_active_index()
            if 0 <= legacy_idx < len(dirs):
                # Seed primary from legacy active_index so a running
                # workspace continues on its current account.
                state["accounts"] = [
                    {
                        "dir": d,
                        "state": PRIMARY if i == legacy_idx else COLD,
                        "slots_used": 0,
                        "slot_holders": [],
                        "rate_limited_at": None,
                        "last_active_at": _now_iso() if i == legacy_idx else None,
                    }
                    for i, d in enumerate(dirs)
                ]
            else:
                state["accounts"] = []
            # Plan B: anchor last_rotation_at to pool init time so the
            # first 24h after deployment is the warmup window. The first
            # planned rotation will fire after the first daily-sync that
            # happens > 24h after init (assuming no rate-limit-driven
            # rotation has reset the timer in the meantime).
            state["last_rotation_at"] = _now_iso()

        state = _ensure_account_entries(state, dirs)
        _save_state_unlocked(state)
        return state


def pool_state() -> dict:
    """Read-only snapshot of pool state. Locks briefly for a consistent read."""
    with _pool_lock():
        return _load_state_unlocked()


def primary_dir() -> str | None:
    """Return the current primary account dir, or None if pool inactive."""
    if not is_active():
        return None
    state = pool_state()
    for a in state.get("accounts", []):
        if a["state"] == PRIMARY:
            return a["dir"]
    return None


def account_state(account_dir: str) -> str | None:
    """Return the pool state of `account_dir`, or None if pool inactive
    or dir not registered. Best-effort; never raises."""
    if not is_active():
        return None
    try:
        state = pool_state()
    except Exception:
        return None
    for a in state.get("accounts", []):
        if a.get("dir") == account_dir:
            return a.get("state")
    return None


def is_cooling(account_dir: str) -> bool:
    """Convenience: True iff `account_dir` is currently in cooling state.
    False when pool inactive or account not found. Best-effort."""
    return account_state(account_dir) == COOLING


# ---------------------------------------------------------------------------
# Slot accounting
# ---------------------------------------------------------------------------


def _free_slots(account: dict) -> int:
    if account["state"] == COOLING:
        return 0
    return max(0, _slot_cap() - account.get("slots_used", 0))


def available_slots() -> int:
    """Count of free slots across primary + overflow + cold (callable accounts).

    Cooling accounts contribute 0. Cold accounts contribute their full
    capacity; the first acquire on a cold account flips it to overflow.
    """
    state = pool_state()
    total = 0
    for a in state.get("accounts", []):
        if a["state"] in (PRIMARY, OVERFLOW, COLD):
            total += _free_slots(a)
    return total


def fanout_cap() -> int:
    """Maximum branches the researcher may propose this cycle.

    Equal to available_slots(). The reserve for sequential root calls is
    the root's own ledger slot, acquired at startup (exploration pool-init
    block, acquire_slot(role="root")) and therefore already excluded from
    available_slots() — subtracting an extra 1 here double-reserved the
    root and shrank the documented cap (2 accounts × 3 slots → cap 5,
    docs/multi-account-pool.md and docs/parallelism.md) to 4. Mirrors the
    unified_pool.fanout_cap() rationale, which sums provider-local
    available_slots() for the same reason.
    """
    return max(0, available_slots())


# ---------------------------------------------------------------------------
# Slot acquire / release
# ---------------------------------------------------------------------------


class PoolExhausted(RuntimeError):
    """Raised by acquire_slot when no callable account has a free slot."""


def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID exists but is owned by another user; treat as alive (we
        # shouldn't see this in practice on a single-user box).
        return True
    except OSError:
        return False


def acquire_slot(
    role: str,
    fork_id: str | None = None,
    clone_k: int | None = None,
    pid: int | None = None,
) -> str:
    """Reserve a slot and return the assigned account dir.

    Allocation order:
      1. primary (if any free slot)
      2. overflow accounts (if any free slot)
      3. cold accounts (first acquire flips cold -> overflow)

    Caller is responsible for pinning the spawned process via
    `CLAUDE_FORCE_ACCOUNT=<returned dir>`.

    Raises PoolExhausted if no callable account has a free slot. Caller
    decides whether to back off, clamp fan-out, or fall through to legacy
    behavior.
    """
    pid = pid or os.getpid()
    with _pool_lock():
        state = _load_state_unlocked()
        accounts = state.get("accounts", [])
        if not accounts:
            raise PoolExhausted("Pool not initialized")

        # Try primary, then overflow, then cold (in pool order within each).
        for desired in (PRIMARY, OVERFLOW, COLD):
            for a in accounts:
                if a["state"] != desired:
                    continue
                if _free_slots(a) <= 0:
                    continue
                # Acquire.
                if a["state"] == COLD:
                    a["state"] = OVERFLOW
                a["slots_used"] = a.get("slots_used", 0) + 1
                holder = {
                    "pid": pid,
                    "role": role,
                    "since": _now_iso(),
                }
                if fork_id is not None:
                    holder["fork_id"] = fork_id
                if clone_k is not None:
                    holder["clone_k"] = clone_k
                a.setdefault("slot_holders", []).append(holder)
                a["last_active_at"] = _now_iso()
                _save_state_unlocked(state)
                return a["dir"]

        raise PoolExhausted(
            f"All {len(accounts)} accounts have no free slots "
            f"(states: {[a['state'] for a in accounts]})"
        )


def release_slot(pid: int | None = None) -> None:
    """Remove the slot reserved for `pid`. Idempotent — releasing a non-existent
    slot is a no-op.

    Note: for clone slots acquired via the spawn path, prefer
    `release_slot_by_branch(fork_id, clone_k)`. The clone's runtime PID
    differs from the PID that called `acquire_slot` (parent acquires the
    slot before subprocess.Popen), so PID-based release would not match
    until `update_slot_pid` re-tags the holder. release_slot remains the
    correct API for root and any caller that owns its own slot.
    """
    pid = pid or os.getpid()
    with _pool_lock():
        state = _load_state_unlocked()
        changed = False
        for a in state.get("accounts", []):
            holders = a.get("slot_holders", [])
            new_holders = [h for h in holders if h.get("pid") != pid]
            if len(new_holders) != len(holders):
                a["slot_holders"] = new_holders
                a["slots_used"] = max(0, len(new_holders))
                changed = True
        if changed:
            _save_state_unlocked(state)


def update_slot_pid(fork_id: str, clone_k: int, new_pid: int) -> bool:
    """Update the PID of a clone slot identified by (fork_id, clone_k).

    Required because `acquire_slot` is called by the parent BEFORE
    `subprocess.Popen` for the clone (the parent must know which dir to
    pin via CLAUDE_FORCE_ACCOUNT in the clone's env, which has to be set
    before Popen). At that point os.getpid() is the parent's PID and the
    clone has none. After Popen returns, the parent calls this function
    to re-tag the holder with the clone's actual PID so that:

      - heartbeat_sweep() correctly reclaims the slot when the clone dies
      - release_slot(os.getpid()) from inside the clone matches the holder

    Returns True iff a matching holder was found and updated. Idempotent
    (safe to retry); a False return means the slot wasn't acquired or was
    already released.
    """
    with _pool_lock():
        state = _load_state_unlocked()
        for a in state.get("accounts", []):
            for h in a.get("slot_holders", []):
                if h.get("fork_id") == fork_id and h.get("clone_k") == clone_k:
                    h["pid"] = new_pid
                    _save_state_unlocked(state)
                    return True
        return False


def release_slot_by_branch(fork_id: str, clone_k: int) -> bool:
    """Release a clone slot identified by (fork_id, clone_k).

    Survives PID changes (e.g., if the clone was killed before
    update_slot_pid ran, leaving a stale parent-PID tag). Returns True iff
    a matching holder was found and removed. Idempotent.
    """
    with _pool_lock():
        state = _load_state_unlocked()
        changed = False
        for a in state.get("accounts", []):
            holders = a.get("slot_holders", [])
            new_holders = [
                h for h in holders
                if not (h.get("fork_id") == fork_id and h.get("clone_k") == clone_k)
            ]
            if len(new_holders) != len(holders):
                a["slot_holders"] = new_holders
                a["slots_used"] = max(0, len(new_holders))
                changed = True
        if changed:
            _save_state_unlocked(state)
        return changed


def heartbeat_sweep() -> int:
    """Remove slot holders whose PID is no longer running. Returns count removed.

    Called by root at every cycle boundary. PID-based recovery is enough —
    no explicit heartbeats from clones needed. Linux PID reuse is rare in
    the time window between a clone's death and the next sweep, so the
    risk of evicting a live unrelated process is negligible.
    """
    with _pool_lock():
        state = _load_state_unlocked()
        removed = 0
        for a in state.get("accounts", []):
            holders = a.get("slot_holders", [])
            kept = []
            for h in holders:
                if _pid_alive(h.get("pid", 0)):
                    kept.append(h)
                else:
                    removed += 1
            if removed and len(kept) != len(holders):
                a["slot_holders"] = kept
                a["slots_used"] = len(kept)
        if removed:
            _save_state_unlocked(state)
        return removed


# ---------------------------------------------------------------------------
# Cooling / thaw / promotion
# ---------------------------------------------------------------------------


def mark_rate_limited(account_dir: str) -> None:
    """Transition account to cooling and record rate_limited_at.

    Caller is responsible for invoking promote_fresh() if the affected
    account was primary — this function does NOT promote, so the caller
    can decide whether to keep its current process pinned (it's about
    to die anyway in clone case) or hot-swap (root case).
    """
    with _pool_lock():
        state = _load_state_unlocked()
        for a in state.get("accounts", []):
            if a["dir"] == account_dir:
                a["state"] = COOLING
                a["rate_limited_at"] = _now_iso()
                _save_state_unlocked(state)
                return
        # Account not in pool — silently no-op. Pool is advisory.


def thaw_eligible(cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS) -> list[str]:
    """Move cooling accounts back to cold once the cooldown has elapsed.

    Returns list of dirs that were thawed this call. Called at every cycle
    boundary by root.
    """
    now = time.time()
    with _pool_lock():
        state = _load_state_unlocked()
        thawed = []
        for a in state.get("accounts", []):
            if a["state"] != COOLING:
                continue
            rl = _parse_iso(a.get("rate_limited_at"))
            if rl is None:
                # Missing timestamp — thaw conservatively to avoid stuck state.
                a["state"] = COLD
                a["rate_limited_at"] = None
                thawed.append(a["dir"])
                continue
            if (now - rl) >= cooldown_seconds:
                a["state"] = COLD
                a["rate_limited_at"] = None
                thawed.append(a["dir"])
        if thawed:
            _save_state_unlocked(state)
        return thawed


def promote_fresh() -> str | None:
    """Pick the freshest available account as new primary.

    Order:
      1. coldest cold account (lowest last_active_at; never-active first)
      2. overflow account with longest time since rate-limit (or oldest
         last_active_at)
      3. None if no callable account exists

    The previous primary, if any, is demoted to overflow (or stays cooling
    if the caller already marked it). One promotion per call.

    Plan B: on success (returning a non-None new primary
    dir), records a rotation event by stamping `last_rotation_at` at the
    top level of pool state. This timestamp is consulted by the planned-
    rotation gate in the cycle loop's daily-sync block — if no rotation
    has happened in the last 24h, a planned rotation fires after the
    next daily sync.

    The candidate selection is constrained to COLD or OVERFLOW accounts
    (never PRIMARY by construction), so a non-None return is GUARANTEED
    to be a real rotation.
    """
    with _pool_lock():
        state = _load_state_unlocked()
        accounts = state.get("accounts", [])
        if not accounts:
            return None

        def freshness_key(a: dict) -> tuple[int, float]:
            # Primary key: never-used (last_active_at IS NULL) ranks freshest.
            la = _parse_iso(a.get("last_active_at"))
            never_used = 0 if la is None else 1
            return (never_used, la or 0.0)

        # 1. coldest cold
        cold = [a for a in accounts if a["state"] == COLD]
        candidate = None
        if cold:
            cold.sort(key=freshness_key)
            candidate = cold[0]
        else:
            # 2. overflow with longest time idle
            overflow = [a for a in accounts if a["state"] == OVERFLOW]
            if overflow:
                overflow.sort(key=freshness_key)
                candidate = overflow[0]

        if candidate is None:
            return None  # all cooling — no rotation, no recording

        # Demote any current primary that isn't already cooling. Multiple
        # primaries shouldn't exist, but iterate defensively.
        for a in accounts:
            if a["state"] == PRIMARY and a is not candidate:
                a["state"] = OVERFLOW

        candidate["state"] = PRIMARY
        candidate["last_active_at"] = _now_iso()
        # Plan B: record the rotation. Since candidate is from COLD or
        # OVERFLOW (never the previous primary), this return value is
        # guaranteed to be a different account than the prior primary.
        state["last_rotation_at"] = _now_iso()
        _save_state_unlocked(state)
        return candidate["dir"]


def record_rotation(timestamp: str | None = None) -> None:
    """Stamp `last_rotation_at` at the top of pool state (Plan B).

    Used as the public entry-point for explicit rotation events. The
    typical path is via `promote_fresh` which records internally.
    Callers that perform an external rotation can use this helper.
    Best-effort; never raises.
    """
    try:
        with _pool_lock():
            state = _load_state_unlocked()
            state["last_rotation_at"] = timestamp or _now_iso()
            _save_state_unlocked(state)
    except Exception:
        pass


def last_rotation_age_hours() -> float | None:
    """Return hours since the last rotation, or None if never recorded.

    Used by the planned-rotation gate. Returns None when:
      - the pool has no `last_rotation_at` field yet (fresh pool, or
        a state file written by pre-Plan-B code), OR
      - the timestamp is unparseable.
    The caller treats None as "rotation eligible" (warmup period
    expired or never set).
    """
    state = pool_state()
    raw = state.get("last_rotation_at")
    ts = _parse_iso(raw)
    if ts is None:
        return None
    delta = time.time() - ts
    return delta / 3600.0


# ---------------------------------------------------------------------------
# Per-account usage tracking (Plan A)
# ---------------------------------------------------------------------------
#
# Cumulative four-field counters per account, all writes funneled through
# the single API chokepoint (`orchestrator._invoke_claude`). Defensive
# against schema drift: read sites use `.get(field, 0)` and writes
# upgrade the entry in place. `tokens_since` is stamped lazily on first
# observation so old-schema entries (no `tokens_since`) get a sensible
# anchor without requiring an explicit migration step.
#
# Pool state is the storage. Same fcntl lock as slot acquire/release.
# Per-call write rate at peak fan-out (~14 clones × 3 agents per cycle =
# ~42 writes/cycle) is well within the proven envelope of the existing
# slot-lifecycle write rate.

_USAGE_FIELDS = (
    "tokens_input",
    "tokens_output",
    "tokens_cache_read",
    "tokens_cache_creation",
)


def record_usage(account_dir: str, usage: dict) -> None:
    """Increment per-account cumulative token counters from a usage envelope.

    Best-effort: never raises. Failure to record is itself silent — we
    don't want a logging fault to mask the underlying API call's success.

    `usage` is the dict returned in the `claude -p` envelope's `usage`
    field. We extract four canonical fields with defaults so missing
    fields don't propagate as None or KeyError. `tokens_since` is set
    lazily on first observation per account.
    """
    if not account_dir:
        return
    try:
        with _pool_lock():
            state = _load_state_unlocked()
            for a in state.get("accounts", []):
                if a.get("dir") != account_dir:
                    continue
                a["tokens_input"] = (
                    int(a.get("tokens_input", 0))
                    + int(usage.get("input_tokens", 0) or 0)
                )
                a["tokens_output"] = (
                    int(a.get("tokens_output", 0))
                    + int(usage.get("output_tokens", 0) or 0)
                )
                a["tokens_cache_read"] = (
                    int(a.get("tokens_cache_read", 0))
                    + int(usage.get("cache_read_input_tokens", 0) or 0)
                )
                a["tokens_cache_creation"] = (
                    int(a.get("tokens_cache_creation", 0))
                    + int(usage.get("cache_creation_input_tokens", 0) or 0)
                )
                if not a.get("tokens_since"):
                    a["tokens_since"] = _now_iso()
                _save_state_unlocked(state)
                return
    except Exception as _e:
        try:
            from long_exposure import health_events as _he
            _he.append_event(
                "usage_recording_failed",
                detail=f"acct={account_dir} err={type(_e).__name__}: {_e}",
            )
        except Exception:
            pass


def reset_usage_counters() -> None:
    """Zero all per-account token counters and refresh tokens_since.

    Manual reset gesture; not called automatically. The cumulative count
    is part of the run history. Operator can call this from a Python
    one-liner if they want fresh counters (e.g. between distinct
    campaigns on the same pool).
    """
    try:
        with _pool_lock():
            state = _load_state_unlocked()
            now = _now_iso()
            for a in state.get("accounts", []):
                for field in _USAGE_FIELDS:
                    a[field] = 0
                a["tokens_since"] = now
            _save_state_unlocked(state)
    except Exception:
        pass


def _human_tokens(n: int) -> str:
    """Format a token count with K/M/G/T suffix for readable summaries.

    Handles unrealistically-large values gracefully (1T = 1 trillion).
    Negative values are formatted with their sign — they shouldn't occur
    from the real API but are surfaced clearly if a bug ever produces
    one.
    """
    n = int(n or 0)
    if abs(n) < 1000:
        return f"{n}"
    if abs(n) < 1_000_000:
        return f"{n / 1000:.1f}K"
    if abs(n) < 1_000_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) < 1_000_000_000_000:
        return f"{n / 1_000_000_000:.2f}G"
    return f"{n / 1_000_000_000_000:.2f}T"


def get_usage_snapshot() -> list[dict]:
    """Return a read-only snapshot of per-account usage suitable for printing.

    Each entry: {dir, name, state, tokens_input, tokens_output,
    tokens_cache_read, tokens_cache_creation, tokens_since}.
    """
    state = pool_state()
    out = []
    for a in state.get("accounts", []):
        out.append({
            "dir": a.get("dir", ""),
            "name": Path(a.get("dir", "")).name or a.get("dir", ""),
            "state": a.get("state", "?"),
            "tokens_input": int(a.get("tokens_input", 0)),
            "tokens_output": int(a.get("tokens_output", 0)),
            "tokens_cache_read": int(a.get("tokens_cache_read", 0)),
            "tokens_cache_creation": int(a.get("tokens_cache_creation", 0)),
            "tokens_since": a.get("tokens_since"),
        })
    return out


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


def format_pool_summary() -> str:
    """One-line summary of pool state for logging.

    Includes per-account token totals (Plan A). Missing fields default
    to 0 so old-schema entries display as `tokens(in=0 ...)` until the
    first call lands and stamps them.
    """
    state = pool_state()
    accounts = state.get("accounts", [])
    if not accounts:
        return "pool: empty (single-account mode)"
    parts = []
    for a in accounts:
        label = Path(a["dir"]).name or a["dir"]
        used = a.get("slots_used", 0)
        tin = _human_tokens(a.get("tokens_input", 0))
        tcr = _human_tokens(a.get("tokens_cache_read", 0))
        tcc = _human_tokens(a.get("tokens_cache_creation", 0))
        tout = _human_tokens(a.get("tokens_output", 0))
        cap = _slot_cap()
        parts.append(
            f"{label}={a['state'][:4]}({used}/{cap}) "
            f"tokens(in={tin} cr={tcr} cc={tcc} out={tout})"
        )
    free = available_slots()
    return "pool: " + ", ".join(parts) + f" — {free} free slots"
