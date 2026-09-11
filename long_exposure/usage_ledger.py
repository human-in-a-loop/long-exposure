"""Per-agent usage ledger: tokens, cost, tool calls, turns, wall time.

One `UsageLedger` per run accumulates every provider call the harness makes
(cycle agents, reporter, final auditor/reporter, curator, merge synthesis,
compaction). It is persisted in `exploration_state.json` under
`usage_totals`, merged from fan-out clones at barrier collapse, rendered into
`output/exploration_status.md` and `output/usage_summary.json`, and consulted
by the cycle loop for the `loop.max_cost_usd` / `loop.max_tool_calls` budget
gates.

Cost has two sources:

* `cost_usd` — reported by the provider CLI itself (Claude's JSON envelope
  carries `total_cost_usd` per invocation). Authoritative when present.
* `cost_estimated_usd` — computed from token usage and a `pricing:` table in
  config (USD per million tokens) when the provider reports no cost (Codex,
  Gemini, local). Absent pricing means the estimate stays at zero and the
  summary marks the cost source as `unavailable`.

Nothing here raises into the cycle loop: every public entry point is
best-effort and degrades to "no data".
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

COUNTER_FIELDS: tuple[str, ...] = (
    "calls",
    "ok_calls",
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "tool_calls",
    "turns",
    "duration_ms",
)
FLOAT_FIELDS: tuple[str, ...] = ("cost_usd", "cost_estimated_usd")
_CLONES_KEY = "_clones"


def _empty_row() -> dict[str, Any]:
    row: dict[str, Any] = {k: 0 for k in COUNTER_FIELDS}
    for k in FLOAT_FIELDS:
        row[k] = 0.0
    # Which cost sources fed this row: "provider", "estimated", or none.
    row["cost_sources"] = []
    return row


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Pricing / estimation
# ---------------------------------------------------------------------------


def _lookup_pricing(config: dict | None, provider: str | None, model: str | None) -> dict | None:
    """Find a pricing row for (provider, model) in config['pricing'].

    Accepted shapes (USD per 1M tokens, all keys optional):

        pricing:
          claude:
            opus: {input: 5, output: 25, cache_read: 0.5, cache_write: 6.25}
          codex:
            gpt-5.5: {input: ..., output: ...}
            _default: {...}          # per-provider fallback

    Lookup order: exact model, then a prefix match on the model id (so
    `claude-opus-5` matches a row named `claude-opus`), then `_default`.
    """
    table = (config or {}).get("pricing")
    if not isinstance(table, dict) or not provider:
        return None
    rows = table.get(provider)
    if not isinstance(rows, dict):
        return None
    model = str(model or "")
    if model and isinstance(rows.get(model), dict):
        return rows[model]
    best = None
    for name, row in rows.items():
        if not isinstance(row, dict) or name.startswith("_"):
            continue
        if model and (model.startswith(str(name)) or str(name).startswith(model)):
            if best is None or len(str(name)) > len(str(best[0])):
                best = (name, row)
    if best:
        return best[1]
    default = rows.get("_default")
    return default if isinstance(default, dict) else None


def estimate_cost_usd(
    usage: dict | None,
    *,
    provider: str | None,
    model: str | None,
    config: dict | None,
) -> float | None:
    """Estimate USD cost from token usage and the config pricing table.

    Returns None when no pricing row applies (caller records the cost source
    as unavailable).
    """
    row = _lookup_pricing(config, provider, model)
    if not row or not usage:
        return None
    per_m = 1_000_000.0
    inp = _int(usage.get("input_tokens"))
    out = _int(usage.get("output_tokens"))
    cache_read = _int(usage.get("cache_read_input_tokens") or usage.get("cached_input_tokens"))
    cache_write = _int(usage.get("cache_creation_input_tokens"))
    cost = (
        inp * _float(row.get("input")) / per_m
        + out * _float(row.get("output")) / per_m
        + cache_read * _float(row.get("cache_read", row.get("input"))) / per_m
        + cache_write * _float(row.get("cache_write", row.get("input"))) / per_m
    )
    return round(cost, 6)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class UsageLedger:
    """Accumulates per-agent usage. Keys are agent names; `_clones` holds a
    roll-up of merged fan-out clone ledgers."""

    def __init__(self, data: dict | None = None):
        self._rows: dict[str, dict[str, Any]] = {}
        self._clones: dict[str, Any] = {"forks": 0, "clones": 0}
        if data:
            self.load(data)

    # -- persistence ------------------------------------------------------

    def load(self, data: dict) -> None:
        self._rows = {}
        for name, raw in (data or {}).items():
            if name == _CLONES_KEY:
                if isinstance(raw, dict):
                    self._clones = {
                        "forks": _int(raw.get("forks")),
                        "clones": _int(raw.get("clones")),
                    }
                continue
            if not isinstance(raw, dict):
                continue
            row = _empty_row()
            for k in COUNTER_FIELDS:
                row[k] = _int(raw.get(k))
            for k in FLOAT_FIELDS:
                row[k] = _float(raw.get(k))
            sources = raw.get("cost_sources")
            if isinstance(sources, list):
                row["cost_sources"] = sorted({str(s) for s in sources})
            self._rows[str(name)] = row

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {name: dict(row) for name, row in self._rows.items()}
        out[_CLONES_KEY] = dict(self._clones)
        return out

    # -- recording --------------------------------------------------------

    def record(
        self,
        agent_name: str,
        result: dict | None,
        *,
        provider: str | None = None,
        model: str | None = None,
        config: dict | None = None,
    ) -> dict[str, Any]:
        """Add one provider call. `result` is the harness result dict
        (`usage`, `duration_ms`, `status`, and optionally `cost_usd`,
        `num_turns`, `tool_calls`). Returns the per-call breakdown that was
        added, for telemetry."""
        result = result or {}
        usage = result.get("usage") or {}
        row = self._rows.setdefault(agent_name, _empty_row())
        reported = result.get("cost_usd")
        estimated = None
        source = "unavailable"
        if reported is not None:
            source = "provider"
        elif usage:
            estimated = estimate_cost_usd(
                usage, provider=provider, model=model, config=config
            )
            if estimated is not None:
                source = "estimated"
        added = {
            "calls": 1,
            "ok_calls": 1 if result.get("status") == "ok" else 0,
            "input_tokens": _int(usage.get("input_tokens")),
            "output_tokens": _int(usage.get("output_tokens")),
            "cache_read_input_tokens": _int(
                usage.get("cache_read_input_tokens") or usage.get("cached_input_tokens")
            ),
            "cache_creation_input_tokens": _int(usage.get("cache_creation_input_tokens")),
            "tool_calls": _int(result.get("tool_calls")),
            "turns": _int(result.get("num_turns")),
            "duration_ms": _int(result.get("duration_ms")),
            "cost_usd": _float(reported),
            "cost_estimated_usd": _float(estimated),
            "cost_source": source,
        }
        for k in COUNTER_FIELDS:
            row[k] += added[k]
        for k in FLOAT_FIELDS:
            row[k] = round(row[k] + added[k], 6)
        if source != "unavailable" and source not in row["cost_sources"]:
            row["cost_sources"] = sorted({*row["cost_sources"], source})
        return added

    def merge(self, other: dict | None, *, is_clone: bool = True) -> None:
        """Fold another ledger's totals into this one (fan-out clones)."""
        if not other:
            return
        if is_clone:
            self._clones["clones"] += 1
        for name, raw in other.items():
            if name == _CLONES_KEY:
                if isinstance(raw, dict):
                    # Nested clones do not exist (depth=1) but keep the sum honest.
                    self._clones["clones"] += _int(raw.get("clones"))
                continue
            if not isinstance(raw, dict):
                continue
            row = self._rows.setdefault(str(name), _empty_row())
            for k in COUNTER_FIELDS:
                row[k] += _int(raw.get(k))
            for k in FLOAT_FIELDS:
                row[k] = round(row[k] + _float(raw.get(k)), 6)
            sources = raw.get("cost_sources")
            if isinstance(sources, list):
                row["cost_sources"] = sorted({*row["cost_sources"], *map(str, sources)})

    def note_fork(self) -> None:
        self._clones["forks"] += 1

    # -- queries ----------------------------------------------------------

    def rows(self) -> dict[str, dict[str, Any]]:
        return {name: dict(row) for name, row in sorted(self._rows.items())}

    def totals(self) -> dict[str, Any]:
        total = _empty_row()
        sources: set[str] = set()
        for row in self._rows.values():
            for k in COUNTER_FIELDS:
                total[k] += row[k]
            for k in FLOAT_FIELDS:
                total[k] = round(total[k] + row[k], 6)
            sources.update(row.get("cost_sources") or [])
        total["cost_sources"] = sorted(sources)
        total["cost_known_usd"] = round(total["cost_usd"] + total["cost_estimated_usd"], 6)
        total["forks"] = self._clones["forks"]
        total["clones"] = self._clones["clones"]
        return total

    def total_cost_usd(self) -> float:
        """Reported + estimated dollars across every agent."""
        return self.totals()["cost_known_usd"]

    def total_tool_calls(self) -> int:
        return self.totals()["tool_calls"]

    def budget_exceeded(self, loop_cfg: dict | None) -> str | None:
        """Return a human-readable reason when a `loop` budget cap is hit."""
        loop_cfg = loop_cfg or {}
        max_cost = loop_cfg.get("max_cost_usd")
        if max_cost is not None:
            try:
                cap = float(max_cost)
            except (TypeError, ValueError):
                cap = 0.0
            if cap > 0:
                spent = self.total_cost_usd()
                if spent >= cap:
                    return f"cost budget reached (${spent:,.2f} >= ${cap:,.2f})"
        max_tools = loop_cfg.get("max_tool_calls")
        if max_tools is not None:
            cap_t = _int(max_tools)
            if cap_t > 0:
                used = self.total_tool_calls()
                if used >= cap_t:
                    return f"tool-call budget reached ({used:,} >= {cap_t:,})"
        return None

    # -- rendering --------------------------------------------------------

    def summary_dict(self, *, loop_cfg: dict | None = None) -> dict[str, Any]:
        totals = self.totals()
        return {
            "agents": self.rows(),
            "totals": totals,
            "budget": {
                "max_cost_usd": (loop_cfg or {}).get("max_cost_usd"),
                "max_tool_calls": (loop_cfg or {}).get("max_tool_calls"),
                "exceeded": self.budget_exceeded(loop_cfg),
            },
        }

    def render_markdown(self, *, loop_cfg: dict | None = None) -> str:
        rows = self.rows()
        if not rows:
            return "## Usage\nNo provider calls recorded yet.\n"
        totals = self.totals()
        lines = [
            "## Usage",
            "",
            "| Agent | Calls | Tool calls | Turns | Input tok | Output tok | Cache read | Cache write | Wall (s) | Cost (USD) |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for name, row in rows.items():
            lines.append(
                f"| {name} | {row['calls']} | {row['tool_calls']:,} | {row['turns']:,} "
                f"| {row['input_tokens']:,} | {row['output_tokens']:,} "
                f"| {row['cache_read_input_tokens']:,} | {row['cache_creation_input_tokens']:,} "
                f"| {row['duration_ms'] / 1000:,.0f} | {_cost_cell(row)} |"
            )
        lines.append(
            f"| **Total** | {totals['calls']} | {totals['tool_calls']:,} | {totals['turns']:,} "
            f"| {totals['input_tokens']:,} | {totals['output_tokens']:,} "
            f"| {totals['cache_read_input_tokens']:,} | {totals['cache_creation_input_tokens']:,} "
            f"| {totals['duration_ms'] / 1000:,.0f} | {_cost_cell(totals)} |"
        )
        lines.append("")
        source_note = {
            (): "Cost: no provider reported a cost and no `pricing:` table is configured.",
        }.get(tuple(totals["cost_sources"]))
        if source_note is None:
            parts = []
            if totals["cost_usd"]:
                parts.append(f"provider-reported ${totals['cost_usd']:,.2f}")
            if totals["cost_estimated_usd"]:
                parts.append(f"estimated from `pricing:` ${totals['cost_estimated_usd']:,.2f}")
            source_note = "Cost: " + ", ".join(parts) + "."
        lines.append(source_note)
        if totals["clones"]:
            lines.append(
                f"Includes {totals['clones']} fan-out clone run(s) across "
                f"{totals['forks']} fork(s)."
            )
        cap = (loop_cfg or {}).get("max_cost_usd")
        cap_t = (loop_cfg or {}).get("max_tool_calls")
        if cap or cap_t:
            budget_bits = []
            if cap:
                budget_bits.append(f"${totals['cost_known_usd']:,.2f} of ${_float(cap):,.2f}")
            if cap_t:
                budget_bits.append(f"{totals['tool_calls']:,} of {_int(cap_t):,} tool calls")
            lines.append("Budget: " + "; ".join(budget_bits) + ".")
        return "\n".join(lines) + "\n"

    def write_summary(self, output_dir: Path, *, loop_cfg: dict | None = None) -> None:
        """Atomically write output/usage_summary.json. Best-effort."""
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            target = output_dir / "usage_summary.json"
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.summary_dict(loop_cfg=loop_cfg), indent=2, sort_keys=True))
            os.replace(tmp, target)
        except OSError:
            return


def _cost_cell(row: dict[str, Any]) -> str:
    reported = _float(row.get("cost_usd"))
    estimated = _float(row.get("cost_estimated_usd"))
    if reported and estimated:
        return f"{reported + estimated:,.2f} ({estimated:,.2f} est.)"
    if reported:
        return f"{reported:,.2f}"
    if estimated:
        return f"{estimated:,.2f} est."
    return "n/a"


def load_summary(output_dir: Path) -> dict | None:
    """Read output/usage_summary.json if present (used by `long-exposure usage`)."""
    try:
        return json.loads((output_dir / "usage_summary.json").read_text())
    except (OSError, ValueError):
        return None
