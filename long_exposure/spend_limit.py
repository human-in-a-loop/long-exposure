"""One total spend limit for a run, expressed as a delta percentage.

See docs/advanced-model-modes-plan.md (Feature 4).

## What this is, honestly

The harness CANNOT read real subscription usage. The `claude` CLI exposes no
`usage` subcommand, `/usage` is interactive-only, and the `-p` JSON envelope
carries `total_cost_usd`, token counts and `num_turns` — nothing about plan
consumption. So the denominator here is a number the OPERATOR declares:
`weekly_allowance_usd`, in API-equivalent dollars. Every surface that shows
the cap says so; a proxy presented as a meter reading would be a lie.

The delta semantics fall out for free. The ledger totals only *this* run's
spend, so a cap measured from run start already is a delta on top of whatever
was consumed before it, with no knowledge of prior usage required.

## One limit, and only one

There are no per-agent, per-role, per-cycle or per-clone sub-budgets. That is
a design constraint, not an omission: the run spends freely against a single
total until the total is gone.

## Hitting it KILLS the run

This is the part that does not reuse `loop.max_cost_usd`. That gate fires at
the cycle boundary and ends the run as a *natural* end-of-run, so the final
auditor, final reporter and curator all still run — and all still spend. A
total spend limit has the opposite contract:

  1. the check fires wherever spend is recorded, not at the next boundary;
  2. clone process groups are swept, so nothing outlives the root and keeps
     spending past the limit that just killed it;
  3. the end-of-run pipeline is skipped entirely — spending past the cap to
     write a report about hitting the cap is incoherent;
  4. a marker file is written and the process exits non-zero, so an operator
     or a wrapper can tell a kill from a clean finish.

## Enforcement lives at the ROOT

A fan-out clone cannot see the run total, only its own spend, so a clone that
enforced the cap itself would be wrong in both directions: it would self-kill
after spending the whole cap alone, yet three clones at 40% each (120% of the
cap) would each stay under it. So clones never trip; the root's barrier poll
sums its own ledger plus each live clone's incrementally written
`output/usage_summary.json` and checks the total.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from long_exposure.flags import truthy

MARKER_FILENAME = "killed_spend_limit.json"

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "weekly_allowance_usd": 0.0,
    "run_pct": 0.0,
}

# The single run-global tripwire. Once tripped it stays tripped: a kill is
# not a condition that can improve.
#
# Lock-protected for the same reason UsageLedger holds one: spend is recorded
# from the main cycle loop AND, under `launch --manager`, from the manager
# poller thread (manager.py -> _call_agent_with_rotation -> _record_usage ->
# check). Without the lock two threads can both pass the "already tripped?"
# test and both build a record, so the marker could name the later moment and
# the kill banner could print twice.
_TRIP: dict[str, Any] | None = None
_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def settings(config: dict | None) -> dict:
    """The `usage_allowance` block with defaults filled in."""
    raw = (config or {}).get("usage_allowance")
    if not isinstance(raw, dict):
        raw = {}
    out = dict(DEFAULTS)
    out.update(raw)
    return out


def _float(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    # NaN and infinity would both defeat the comparison silently.
    if out != out or out in (float("inf"), float("-inf")):
        return 0.0
    return max(0.0, out)


def cap_usd(config: dict | None) -> float | None:
    """The run's single total cap in USD, or None for unlimited.

    None is returned for `enabled: false`, a zero/absent allowance, and a
    zero/absent percentage — the three documented ways to say "no limit".
    """
    cfg = settings(config)
    if not truthy(cfg.get("enabled"), name="usage_allowance.enabled"):
        return None
    allowance = _float(cfg.get("weekly_allowance_usd"))
    pct = _float(cfg.get("run_pct"))
    if allowance <= 0 or pct <= 0:
        return None
    if pct > 100:
        print(
            f"[spend-limit] run_pct {pct:g} exceeds 100; clamping to 100 "
            "(the allowance is the ceiling).",
            flush=True,
        )
        pct = 100.0
    return allowance * pct / 100.0


def enabled(config: dict | None) -> bool:
    return cap_usd(config) is not None


# ---------------------------------------------------------------------------
# Live clone spend
# ---------------------------------------------------------------------------


def clone_spend_usd(clone_dirs) -> float:
    """Sum the cost each live clone has written to its own usage summary.

    Clone ledgers merge into the root only at barrier collapse, which is far
    too late to enforce a total — up to FANOUT_MAX_BRANCHES clones can each
    run for FANOUT_CAP_SECONDS (10 h) unseen. But every process writes an
    incrementally updated `output/usage_summary.json` on each status write,
    including clones in their own instance dirs, so the root can read them
    while they run.

    Degrades to zero per file, never raises: a clone that has not written a
    summary yet, or has written a partial one mid-flush, must not be able to
    crash the root's poll. An under-read delays the kill by one poll; an
    exception would lose the run.
    """
    total = 0.0
    for cd in clone_dirs or ():
        path = Path(cd) / "output" / "usage_summary.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        totals = data.get("totals")
        if isinstance(totals, dict):
            total += _float(totals.get("cost_usd"))
    return total


# ---------------------------------------------------------------------------
# Tripwire
# ---------------------------------------------------------------------------


def reset() -> None:
    """Clear the tripwire. Called once at run start (and by tests)."""
    global _TRIP
    with _LOCK:
        _TRIP = None


def tripped() -> dict | None:
    """The trip record, or None. Callers treat a non-None value as a kill."""
    with _LOCK:
        return dict(_TRIP) if _TRIP else None


def check(
    total_usd: float,
    config: dict | None,
    *,
    source: str,
    extra_usd: float = 0.0,
) -> dict | None:
    """Trip the wire if `total_usd + extra_usd` has reached the cap.

    `extra_usd` carries live clone spend at the barrier poll, where the root
    ledger alone understates the run total. `source` is recorded for the
    marker file so an operator can see which check caught it.

    Idempotent: once tripped the original record is kept, so the marker names
    the moment the limit was actually crossed rather than the last poll.
    """
    global _TRIP
    with _LOCK:
        if _TRIP is not None:
            return dict(_TRIP)
        cap = cap_usd(config)
        if cap is None:
            return None
        total = _float(total_usd) + _float(extra_usd)
        if total < cap:
            return None
        _TRIP = {
            "cap_usd": cap,
            "observed_usd": total,
            "root_usd": _float(total_usd),
            "clone_usd": _float(extra_usd),
            "source": source,
            "tripped_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "weekly_allowance_usd": _float(
                settings(config).get("weekly_allowance_usd")
            ),
            "run_pct": _float(settings(config).get("run_pct")),
        }
        record = dict(_TRIP)
    # Printed outside the lock: stdout can block, and holding the tripwire
    # while it does would stall every other thread recording spend.
    print(
        f"\n[spend-limit] TOTAL SPEND LIMIT REACHED — ${total:,.2f} of a "
        f"${cap:,.2f} cap (observed at {source}). Killing the run: clones "
        "will be terminated and the end-of-run pipeline skipped.",
        flush=True,
    )
    return record


# ---------------------------------------------------------------------------
# Marker + reporting
# ---------------------------------------------------------------------------


def marker_path(output_dir) -> Path:
    return Path(output_dir) / MARKER_FILENAME


def clear_marker(output_dir) -> bool:
    """Remove a marker left by an earlier run. Called once at run start.

    Without this, a run killed at the cap leaves the marker behind, and a
    later resume that finishes cleanly still looks killed to an operator or
    a wrapper script checking for the file — which is exactly the question
    the marker exists to answer. Best-effort: a marker we cannot delete is
    a stale-reporting problem, not a reason to refuse to start.
    """
    path = marker_path(output_dir)
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError as e:
        print(f"[spend-limit] Stale marker not cleared: {e}", flush=True)
    return False


def write_marker(output_dir, trip: dict | None = None, *, cycle: int | None = None):
    """Record the kill so a wrapper can distinguish it from a clean finish.

    Best-effort: a run that cannot write the marker is still killed. Losing
    the marker is a reporting loss; failing to stop would be a spending one.
    """
    record = dict(trip or tripped() or {})
    if not record:
        return None
    record["cycle"] = cycle
    record["killed"] = True
    path = marker_path(output_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        return path
    except OSError as e:
        print(f"[spend-limit] Marker write failed: {e}", flush=True)
        return None


def summary_lines(config: dict | None, total_usd: float) -> str:
    """Lines for usage_summary.md. Empty string when the feature is off.

    Always carries the disclaimer: the allowance is operator-declared, and
    naming it a proxy is the whole point.
    """
    cap = cap_usd(config)
    if cap is None:
        return ""
    cfg = settings(config)
    allowance = _float(cfg.get("weekly_allowance_usd"))
    pct = _float(cfg.get("run_pct"))
    spent = _float(total_usd)
    share = (spent / cap * 100.0) if cap > 0 else 0.0
    lines = [
        "## Run spend limit",
        "",
        f"**Run allowance:** {pct:g}% of a declared ${allowance:,.2f} weekly "
        f"allowance = ${cap:,.2f} cap",
        f"**Consumed:** ${spent:,.2f} ({share:.1f}% of the run allowance)",
    ]
    trip = tripped()
    if trip:
        lines.append(
            f"**Outcome:** KILLED at the limit — see `{MARKER_FILENAME}`. "
            "The end-of-run pipeline was skipped."
        )
    lines.extend(
        [
            "",
            "> Note: the weekly allowance is operator-declared. The harness "
            "cannot read subscription usage; this is an API-equivalent proxy, "
            "not a meter reading.",
        ]
    )
    return "\n".join(lines)


def describe(config: dict | None) -> str:
    """One line for the startup banner and the gate's confirmation table."""
    cap = cap_usd(config)
    if cap is None:
        return "unlimited (no spend limit)"
    cfg = settings(config)
    return (
        f"${cap:,.2f} total "
        f"({_float(cfg.get('run_pct')):g}% of a declared "
        f"${_float(cfg.get('weekly_allowance_usd')):,.2f} weekly allowance; "
        "operator-declared proxy)"
    )
