"""Regression tests for clone-termination remediation (RCA items 1 & 3).

Item 3 — the low-output backstop is RELATIVE to the run's own peak output, so a
verbose-but-idle clone still terminates. Replays the real clone-0 and clone-1
per-cycle token sequences from the incident and asserts both now reach closure.

Item 1 — the auditor's explicit BRANCH_COMPLETE_SIGNAL ends the loop. Tests the
detection predicate (auditor-only, fresh-output scoped).

These mirror the loop logic in exploration.py rather than driving the full
conductor, which would require a live provider. The constants and predicate
under test are kept in lock-step with that logic.
"""

from long_exposure.conductor import BRANCH_COMPLETE_RE, BRANCH_COMPLETE_SIGNAL

# Mirror of the loop constants (exploration.py).
LOW_OUTPUT_FRACTION = 0.05
LOW_OUTPUT_ABS_FLOOR = 500
LOW_OUTPUT_CLOSURE_COUNT = 2


def _streak_to_closure(cycle_totals, forced_substantive=None):
    """Replay the relative low-output detector; return the cycle index (1-based)
    at which closure fires, or None if it never does."""
    forced_substantive = forced_substantive or set()
    peak = 0
    streak = 0
    for i, total in enumerate(cycle_totals, start=1):
        peak = max(peak, total)
        threshold = max(LOW_OUTPUT_ABS_FLOOR, int(LOW_OUTPUT_FRACTION * peak))
        if i not in forced_substantive and total < threshold:
            streak += 1
        else:
            streak = 0
        if streak >= LOW_OUTPUT_CLOSURE_COUNT:
            return i
    return None


# Real per-cycle output totals from the incident (sum across researcher+worker+auditor).
CLONE0_TOTALS = [112929, 14728, 5232, 3201, 2707, 2456, 2456, 2456]  # asymptotes ~2456
CLONE1_TOTALS = [66727, 8648, 2847, 1488, 1304]                      # exited at cycle 5


def test_clone0_now_terminates_under_relative_threshold():
    # Under the old fixed 2000 floor, clone-0 (~2456/cycle) NEVER closed.
    # Relative: 5% of ~112929 peak = ~5646, so ~2456 cycles are "low".
    closed_at = _streak_to_closure(CLONE0_TOTALS)
    assert closed_at is not None, "clone-0 must now reach closure"
    assert closed_at <= 4  # two consecutive low cycles early on


def test_clone1_still_terminates():
    # Relative threshold = 5% of ~66727 peak = ~3336. Cycles 3 (2847) and
    # 4 (1488) are both below it, so closure fires at cycle 4 — one cycle
    # EARLIER than the old fixed-2000 floor (which closed at cycle 5, since
    # 2847 > 2000). Earlier termination of an idle branch is the goal.
    closed_at = _streak_to_closure(CLONE1_TOTALS)
    assert closed_at == 4


def test_substantive_run_does_not_falsely_close():
    # A run whose cycles stay well above 5% of peak must not close.
    steady = [50000, 48000, 52000, 47000, 49000]
    assert _streak_to_closure(steady) is None


def test_single_low_cycle_does_not_close():
    # One dip then recovery: closure needs TWO consecutive lows.
    seq = [100000, 1000, 60000, 1000, 70000]
    assert _streak_to_closure(seq) is None


def test_forced_substantive_cycles_never_count_low():
    # Post-merge / fan-out cycles are exempt even if their token count is small.
    totals = [100000, 100, 100]  # cycles 2 & 3 are tiny...
    assert _streak_to_closure(totals, forced_substantive={2, 3}) is None  # ...but exempt
    assert _streak_to_closure(totals) == 3  # without exemption they would close


# --- Item 1: auditor signal detection predicate -------------------------------

def _detects_signal(agent_name, outputs):
    """Mirror of the in-loop predicate at exploration.py."""
    return agent_name == "auditor" and any(
        BRANCH_COMPLETE_RE.search(str(v)) for v in outputs.values()
    )


def test_auditor_signal_detected():
    outputs = {"audit_report": f"VALIDATED. Scope exhausted.\n{BRANCH_COMPLETE_SIGNAL}"}
    assert _detects_signal("auditor", outputs)


def test_signal_on_own_line_with_surrounding_whitespace_detected():
    outputs = {"audit_report": f"Done.\n  {BRANCH_COMPLETE_SIGNAL}  \nEnd."}
    assert _detects_signal("auditor", outputs)


def test_no_signal_when_absent():
    assert not _detects_signal("auditor", {"audit_report": "VALIDATED. More work remains."})


def test_auditor_discussing_token_inline_does_not_trigger():
    # The score instructs "on its own line"; an auditor merely DISCUSSING
    # the token must NOT terminate the run.
    outputs = {
        "audit_report": (
            f"I am NOT emitting {BRANCH_COMPLETE_SIGNAL} because open "
            f"questions remain."
        )
    }
    assert not _detects_signal("auditor", outputs)


def test_non_auditor_echo_does_not_trigger():
    # A researcher quoting the token (even line-anchored) must NOT end the loop.
    outputs = {"research_brief": f"The auditor may emit:\n{BRANCH_COMPLETE_SIGNAL}\nwhen done."}
    assert not _detects_signal("researcher", outputs)
