"""Researcher-planned cycle tails.

See docs/advanced-model-modes-plan.md (Feature 1).

## What the researcher plans

The researcher is agent 0 of every cycle, so it does **not** plan a cycle it
might be scheduled out of — it plans the *remainder* of the cycle it has just
opened. Its `<cycle_plan>` block replaces `flow_this_cycle[1:]`, never index
0. That framing removes three problems at once: no re-entry (the planner has
already run when its plan is read), no staleness (the plan governs the very
next agent turn), and no new `results` key (the block lives inside
`research_brief`, which is already stored, archived and compacted).

It mirrors the `<parallel_cycle_fanout>` precedent exactly: parsed out of
researcher output, validated, clamped, and silently ignored when malformed,
falling back to the fixed flow. It never raises — a long autonomous run must
not die over a model's formatting slip.

## Bounds the agent cannot waive

Two floors, enforced by the harness after parsing:

- `max_worker_chain` caps consecutive worker turns, so a plan cannot turn a
  cycle into an unbounded worker loop that starves the memoir, the reporter
  cadence and the exhaustion detector.
- `audit_floor_cycles` caps consecutive cycles ending without an auditor.
  This is what keeps the researcher's self-interest bounded — it is the role
  whose brief the auditor checks, so letting it say "no audit needed"
  indefinitely would be self-exculpating — and it keeps
  `[[BRANCH_COMPLETE]]` reachable, since only the auditor can emit it.

## The researcher's blind spot, and who covers it

The researcher plans before seeing this cycle's work, so it must predict
whether an audit will be needed rather than observe it. The worker covers
that: a worker that finds something surprising emits `[[REQUEST_AUDIT]]`,
and the harness re-inserts the auditor even if the plan omitted it, resets
the audit-floor counter, and emits a health event. The event is the point as
much as the fix — a run where escalation fires every cycle is telling you the
planner is not earning its keep.

## Root only

Clones keep the fixed flow, exactly as they already sit out the fan-out
decision. A clone with no auditor never emits `[[BRANCH_COMPLETE]]` and
would burn to the 10 h `FANOUT_CAP_SECONDS` wall with nothing to show; and
branches that ran different shapes are not comparable, which is what would
make the merge's divergence table meaningless.
"""

from __future__ import annotations

import re

from long_exposure import health_events as _health

# Defaults for `loop.cycle_planning`. Off by default: a run that does not opt
# in keeps the fixed flow and a byte-identical researcher prompt.
DEFAULTS: dict = {
    "enabled": False,
    "max_worker_chain": 3,
    "max_turns_per_cycle": 4,
    "audit_floor_cycles": 2,
    "allow_in_clones": False,
    "worker_may_request_audit": True,
}

PLANNER = "researcher"
AUDITOR = "auditor"
WORKER = "worker"

_BLOCK_RE = re.compile(
    r"<cycle_plan>(.*?)</cycle_plan>", re.DOTALL | re.IGNORECASE,
)
_TURN_RE = re.compile(
    r'<turn\s+agent\s*=\s*["\']?([A-Za-z_][A-Za-z0-9_]*)["\']?\s*>(.*?)</turn>',
    re.DOTALL | re.IGNORECASE,
)
_RATIONALE_RE = re.compile(
    r"<rationale>(.*?)</rationale>", re.DOTALL | re.IGNORECASE,
)

# Emitted by a worker that wants the auditor re-inserted. Anchored to its own
# line, like BRANCH_COMPLETE_RE, so a worker merely DISCUSSING the token
# ("I did not emit [[REQUEST_AUDIT]] because...") does not trigger it.
REQUEST_AUDIT_SIGNAL = "[[REQUEST_AUDIT]]"
REQUEST_AUDIT_RE = re.compile(
    r"^\s*" + re.escape(REQUEST_AUDIT_SIGNAL) + r"\s*$", re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def settings(loop_cfg: dict | None) -> dict:
    raw = (loop_cfg or {}).get("cycle_planning")
    if not isinstance(raw, dict):
        raw = {}
    out = dict(DEFAULTS)
    out.update(raw)
    return out


def enabled(loop_cfg: dict | None, *, is_clone: bool = False) -> bool:
    cfg = settings(loop_cfg)
    if not cfg.get("enabled"):
        return False
    if is_clone and not cfg.get("allow_in_clones"):
        return False
    return True


def _int(value, fallback: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return fallback
    return out if out >= 0 else fallback


# ---------------------------------------------------------------------------
# Guidance
# ---------------------------------------------------------------------------


def guidance(loop_cfg: dict | None) -> str:
    """The researcher-facing block. Injected only when planning is active."""
    cfg = settings(loop_cfg)
    max_workers = _int(cfg.get("max_worker_chain"), 3)
    max_turns = _int(cfg.get("max_turns_per_cycle"), 4)
    floor = _int(cfg.get("audit_floor_cycles"), 2)
    return (
        "<cycle_plan_guidance>\n"
        "You may shape the REST of this cycle. You have already run; the plan\n"
        "covers the turns after you, and you cannot schedule yourself.\n"
        "\n"
        "Emit this block only when the default shape (one worker, then one\n"
        "auditor) is wrong for the work you just briefed. Omitting it is\n"
        "always valid and keeps the default.\n"
        "\n"
        "  <cycle_plan>\n"
        "    <turn agent=\"worker\">What this worker turn should do.</turn>\n"
        "    <turn agent=\"worker\">What the second turn should do, given\n"
        "    the first finished.</turn>\n"
        "    <turn agent=\"auditor\">What to check.</turn>\n"
        "    <rationale>Why this shape, in one or two sentences.</rationale>\n"
        "  </cycle_plan>\n"
        "\n"
        "Rules the harness enforces, so do not spend tokens on them:\n"
        f"  - at most {max_workers} worker turns, at most one auditor turn,\n"
        f"    at most {max_turns} turns in total;\n"
        "  - `researcher` is rejected (you have already run);\n"
        "  - an auditor turn is always moved last;\n"
        f"  - after {floor} consecutive cycles with no auditor, one is added\n"
        "    regardless of your plan;\n"
        "  - a malformed block is ignored and the default shape runs.\n"
        "\n"
        "Chain workers when a second turn genuinely depends on the first\n"
        "finishing. Drop the auditor when this cycle produces nothing to\n"
        "audit — setup, a long build, data collection — not merely to move\n"
        "faster. <rationale> is recorded, and it is what makes a skipped\n"
        "audit reviewable afterwards.\n"
        "</cycle_plan_guidance>"
    )


def worker_escalation_guidance() -> str:
    """The worker-facing block for `[[REQUEST_AUDIT]]`."""
    return (
        "<audit_escalation>\n"
        "The researcher may have planned this cycle without an auditor. You\n"
        f"are the only role that has seen the work, so if you hit something\n"
        f"that genuinely needs checking — a surprising result, a claim you\n"
        f"cannot substantiate, a contradiction with the plan of record —\n"
        f"emit {REQUEST_AUDIT_SIGNAL} on a line of its own and the auditor\n"
        "will be added to this cycle.\n"
        "\n"
        "Use it for a real finding, not as a habit: it is recorded, and a\n"
        "run that escalates every cycle is a signal that the planning is\n"
        "wrong rather than that the work is surprising.\n"
        "</audit_escalation>"
    )


def wants_audit(text: str | None) -> bool:
    """True iff a worker emitted the escalation token on its own line."""
    return bool(text) and bool(REQUEST_AUDIT_RE.search(str(text)))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse(
    text: str | None,
    loop_cfg: dict | None,
    known_agents,
    *,
    is_clone: bool = False,
) -> list[str] | None:
    """Parse a `<cycle_plan>` into an ordered list of agent names.

    Returns None when the block is absent, malformed, or planning is not
    active — in every one of those cases the caller keeps the fixed flow.
    Never raises.
    """
    if not enabled(loop_cfg, is_clone=is_clone):
        if text and _BLOCK_RE.search(str(text)):
            _reject(
                "cycle planning is not active for this process; "
                "ignoring the block"
            )
        return None
    if not text:
        return None

    match = _BLOCK_RE.search(str(text))
    if not match:
        return None

    cfg = settings(loop_cfg)
    max_workers = _int(cfg.get("max_worker_chain"), 3)
    max_turns = _int(cfg.get("max_turns_per_cycle"), 4)

    body = match.group(1)
    turns = [name.strip().lower() for name, _ in _TURN_RE.findall(body)]
    if not turns:
        return _reject("block has no <turn> entries")

    known = set(known_agents or ())
    for name in turns:
        if name not in known:
            return _reject(f"unknown agent {name!r} — not defined in the score")
        if name == PLANNER:
            return _reject(
                "the researcher cannot schedule itself; it has already run"
            )

    workers = [t for t in turns if t == WORKER]
    auditors = [t for t in turns if t == AUDITOR]
    if len(auditors) > 1:
        return _reject(f"{len(auditors)} auditor turns; at most one is allowed")
    if len(workers) > max_workers:
        return _reject(
            f"{len(workers)} worker turns exceeds max_worker_chain "
            f"({max_workers})"
        )
    if len(turns) > max_turns:
        return _reject(
            f"{len(turns)} turns exceeds max_turns_per_cycle ({max_turns})"
        )

    # An auditor before the work it audits is a parse error in disguise, so
    # move it last rather than rejecting an otherwise usable plan.
    if auditors and turns[-1] != AUDITOR:
        turns = [t for t in turns if t != AUDITOR] + [AUDITOR]

    return turns


def rationale(text: str | None) -> str:
    match = _RATIONALE_RE.search(str(text or ""))
    return match.group(1).strip() if match else ""


def turn_directives(text: str | None) -> list[tuple[str, str]]:
    """(agent, directive) pairs, for handing each turn its own instruction."""
    match = _BLOCK_RE.search(str(text or ""))
    if not match:
        return []
    return [
        (name.strip().lower(), directive.strip())
        for name, directive in _TURN_RE.findall(match.group(1))
    ]


def _reject(reason: str) -> None:
    print(f"[long-exposure] cycle_plan rejected: {reason}.", flush=True)
    _health.append_event("cycle_plan_rejected", reason)
    return None


# ---------------------------------------------------------------------------
# Applying the plan, with the floors
# ---------------------------------------------------------------------------


def apply_floor(
    tail: list[str],
    loop_cfg: dict | None,
    audit_free_streak: int,
) -> tuple[list[str], bool]:
    """Force an auditor onto the tail if the audit floor would break.

    Returns (tail, forced). `audit_free_streak` counts consecutive COMPLETED
    cycles that ended without an auditor, so a floor of 2 permits two
    audit-free cycles and adds an auditor to the third.
    """
    cfg = settings(loop_cfg)
    floor = _int(cfg.get("audit_floor_cycles"), 2)
    if AUDITOR in tail:
        return tail, False
    if floor <= 0 or audit_free_streak < floor:
        return tail, False
    print(
        f"[long-exposure] cycle_plan: audit floor reached "
        f"({audit_free_streak} consecutive cycles without an auditor); "
        "adding one regardless of the plan.",
        flush=True,
    )
    _health.append_event(
        "cycle_plan_audit_forced",
        f"audit_free_streak={audit_free_streak} floor={floor}",
    )
    return list(tail) + [AUDITOR], True


def insert_requested_audit(flow: list[str], position: int) -> list[str]:
    """Add an auditor after `position` because a worker escalated."""
    if AUDITOR in flow:
        return list(flow)
    print(
        f"[long-exposure] cycle_plan: worker emitted {REQUEST_AUDIT_SIGNAL}; "
        "adding the auditor to this cycle.",
        flush=True,
    )
    _health.append_event(
        "cycle_plan_audit_requested", "worker escalation honoured", agent=WORKER,
    )
    out = list(flow)
    out.insert(position + 1, AUDITOR)
    return out


def build_flow(
    planned_tail: list[str] | None,
    fixed_flow: list[str],
    loop_cfg: dict | None,
    audit_free_streak: int,
) -> tuple[list[str], bool, bool]:
    """Compose this cycle's flow from the plan (or the fixed flow).

    Returns (flow, planned, audit_forced). The planner itself is always the
    first entry: the plan covers the tail only.
    """
    if not planned_tail:
        return list(fixed_flow), False, False
    tail, forced = apply_floor(planned_tail, loop_cfg, audit_free_streak)
    return [fixed_flow[0]] + tail, True, forced
