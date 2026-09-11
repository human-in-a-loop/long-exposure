"""Unit tests for long_exposure.usage_ledger."""

from long_exposure.usage_ledger import UsageLedger, estimate_cost_usd


def _result(**over):
    base = {
        "status": "ok",
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_input_tokens": 5000,
            "cache_creation_input_tokens": 100,
        },
        "duration_ms": 1500,
        "cost_usd": None,
        "num_turns": 3,
        "tool_calls": 4,
    }
    base.update(over)
    return base


def test_record_provider_reported_cost_wins():
    ledger = UsageLedger()
    added = ledger.record(
        "worker", _result(cost_usd=0.42),
        provider="claude", model="opus",
        config={"pricing": {"claude": {"opus": {"input": 5, "output": 25}}}},
    )
    assert added["cost_source"] == "provider"
    assert added["cost_usd"] == 0.42
    assert added["cost_estimated_usd"] == 0.0
    row = ledger.rows()["worker"]
    assert row["calls"] == 1 and row["ok_calls"] == 1
    assert row["tool_calls"] == 4 and row["turns"] == 3
    assert row["input_tokens"] == 1000 and row["cache_read_input_tokens"] == 5000
    assert row["cost_sources"] == ["provider"]


def test_record_estimates_from_pricing_when_unreported():
    pricing = {"codex": {"gpt-5.5": {"input": 1.0, "output": 10.0, "cache_read": 0.1, "cache_write": 2.0}}}
    ledger = UsageLedger()
    added = ledger.record(
        "worker", _result(), provider="codex", model="gpt-5.5", config={"pricing": pricing},
    )
    # 1000*1 + 200*10 + 5000*0.1 + 100*2 = 1000 + 2000 + 500 + 200 = 3700 per-M units
    assert added["cost_source"] == "estimated"
    assert abs(added["cost_estimated_usd"] - 0.0037) < 1e-9
    assert ledger.total_cost_usd() == 0.0037


def test_pricing_prefix_and_default_lookup():
    pricing = {"claude": {"claude-opus": {"input": 5, "output": 25}, "_default": {"input": 1, "output": 1}}}
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    assert estimate_cost_usd(usage, provider="claude", model="claude-opus-5", config={"pricing": pricing}) == 5.0
    assert estimate_cost_usd(usage, provider="claude", model="claude-haiku-4-5", config={"pricing": pricing}) == 1.0
    assert estimate_cost_usd(usage, provider="gemini", model="x", config={"pricing": pricing}) is None


def test_unavailable_cost_does_not_count_toward_budget():
    ledger = UsageLedger()
    added = ledger.record("worker", _result(), provider="gemini", model="g", config={})
    assert added["cost_source"] == "unavailable"
    assert ledger.total_cost_usd() == 0.0
    assert ledger.budget_exceeded({"max_cost_usd": 1}) is None
    assert "n/a" in ledger.render_markdown()


def test_rate_limit_and_empty_results_are_ignored_by_caller_contract():
    # The ledger itself records whatever it is given; the harness filters
    # rate-limit results before calling record(). Verify a zero-usage call
    # still increments calls (compaction with no usage would be visible).
    ledger = UsageLedger()
    ledger.record("x", {"status": "error", "usage": {}}, provider="local", model="m", config={})
    assert ledger.rows()["x"]["calls"] == 1
    assert ledger.rows()["x"]["ok_calls"] == 0


def test_merge_clone_ledgers_and_fork_counts():
    root = UsageLedger()
    root.record("researcher", _result(cost_usd=1.0), provider="claude", model="opus", config={})
    clone_a = UsageLedger()
    clone_a.record("worker", _result(cost_usd=2.0, tool_calls=10), provider="claude", model="opus", config={})
    clone_b = UsageLedger()
    clone_b.record("worker", _result(cost_usd=3.0, tool_calls=1), provider="claude", model="opus", config={})
    root.note_fork()
    root.merge(clone_a.to_dict())
    root.merge(clone_b.to_dict())
    totals = root.totals()
    assert totals["cost_usd"] == 6.0
    assert totals["tool_calls"] == 15
    assert totals["forks"] == 1 and totals["clones"] == 2
    assert root.rows()["worker"]["calls"] == 2
    assert "2 fan-out clone run(s) across 1 fork(s)" in root.render_markdown()


def test_budget_gates():
    ledger = UsageLedger()
    for _ in range(3):
        ledger.record("worker", _result(cost_usd=4.0, tool_calls=5), provider="claude", model="opus", config={})
    assert ledger.budget_exceeded({}) is None
    assert ledger.budget_exceeded({"max_cost_usd": 12}) is not None
    assert ledger.budget_exceeded({"max_cost_usd": 12.01}) is None
    assert ledger.budget_exceeded({"max_tool_calls": 15}) is not None
    assert ledger.budget_exceeded({"max_tool_calls": 16}) is None
    # Unlimited / malformed caps never trip.
    assert ledger.budget_exceeded({"max_cost_usd": None, "max_tool_calls": "abc"}) is None


def test_roundtrip_to_dict_load():
    ledger = UsageLedger()
    ledger.record("a", _result(cost_usd=1.5), provider="claude", model="opus", config={})
    ledger.note_fork()
    restored = UsageLedger(ledger.to_dict())
    assert restored.to_dict() == ledger.to_dict()
    assert restored.totals()["forks"] == 1


def test_render_markdown_has_total_row_and_budget_line():
    ledger = UsageLedger()
    ledger.record("researcher", _result(cost_usd=1.25), provider="claude", model="opus", config={})
    md = ledger.render_markdown(loop_cfg={"max_cost_usd": 10, "max_tool_calls": 100})
    assert "| **Total** |" in md
    assert "$1.25 of $10.00" in md
    assert "4 of 100 tool calls" in md
    assert "provider-reported $1.25" in md
