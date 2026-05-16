"""Read-only graph view of promise_ledger.jsonl for final stages."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_RESERVED_NAMESPACES = ("_plan/", "_run/", "_archive/", "_orphan/", "_manager/")


def _is_reserved(mid: str) -> bool:
    return any(mid.startswith(prefix) for prefix in _RESERVED_NAMESPACES)


def _safe_read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events: list[dict] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


class LedgerGraph:
    def __init__(self, events: list[dict]) -> None:
        self.events = events
        self.by_event_id = {e["event_id"]: e for e in events if e.get("event_id")}
        self.supersedes: dict[str, str] = {}
        self.cites: dict[str, set[str]] = defaultdict(set)
        self.milestone_events: dict[str, list[dict]] = defaultdict(list)
        self.depends_on: dict[str, set[str]] = defaultdict(set)
        self.file_citations: dict[str, int] = defaultdict(int)
        self.event_citations: dict[str, int] = defaultdict(int)

        for event in events:
            eid = event.get("event_id")
            if not eid:
                continue
            mid = event.get("milestone_id") or ""
            if mid:
                self.milestone_events[mid].append(event)
            sup = event.get("supersedes")
            if sup and sup in self.by_event_id:
                self.supersedes[eid] = sup
            for item in event.get("evidence") or []:
                if not isinstance(item, str):
                    continue
                if _UUID_RE.match(item):
                    if item in self.by_event_id:
                        self.cites[eid].add(item)
                        self.event_citations[item] += 1
                else:
                    self.file_citations[item] += 1
            for dep in event.get("dependencies") or []:
                if isinstance(dep, str) and dep and mid:
                    self.depends_on[mid].add(dep)

        for events_for_mid in self.milestone_events.values():
            events_for_mid.sort(key=lambda e: str(e.get("ts") or ""))

    def chain_to(self, event_id: str, max_depth: int = 20) -> list[dict]:
        if event_id not in self.by_event_id:
            return []
        visited: list[str] = []
        seen: set[str] = set()

        def visit(eid: str, depth: int) -> None:
            if depth > max_depth or eid in seen or eid not in self.by_event_id:
                return
            seen.add(eid)
            sup = self.supersedes.get(eid)
            if sup:
                visit(sup, depth + 1)
            for cited in sorted(self.cites.get(eid, ())):
                visit(cited, depth + 1)
            event = self.by_event_id[eid]
            mid = event.get("milestone_id") or ""
            events_for_mid = self.milestone_events.get(mid) or []
            idx = next(
                (i for i, item in enumerate(events_for_mid) if item.get("event_id") == eid),
                -1,
            )
            if idx > 0:
                prev = events_for_mid[idx - 1].get("event_id")
                if prev:
                    visit(prev, depth + 1)
            visited.append(eid)

        visit(event_id, 0)
        return [self.by_event_id[eid] for eid in visited]

    def contradiction_clusters(self) -> list[tuple[str, list[str]]]:
        clusters: list[tuple[str, list[str]]] = []
        for mid, events_for_mid in self.milestone_events.items():
            if _is_reserved(mid):
                continue
            statuses = [(e.get("event_id"), e.get("status")) for e in events_for_mid]
            validated = [i for i, (_, status) in enumerate(statuses) if status == "validated"]
            invalidated = [i for i, (_, status) in enumerate(statuses) if status == "invalidated"]
            if not validated or not invalidated:
                continue
            problem: list[str] = []
            for v_idx in validated:
                for i_idx in invalidated:
                    if i_idx <= v_idx:
                        continue
                    between = [status for _, status in statuses[v_idx + 1:i_idx]]
                    if "reopened" not in between:
                        problem.extend([statuses[v_idx][0], statuses[i_idx][0]])
            if problem:
                clusters.append((mid, list(dict.fromkeys(eid for eid in problem if eid))))
        return clusters

    def evidence_density(self, top_k: int = 10) -> list[tuple[str, int]]:
        merged = list(self.file_citations.items())
        merged.extend((f"event:{eid[:8]}...", n) for eid, n in self.event_citations.items())
        merged.sort(key=lambda item: item[1], reverse=True)
        return merged[:top_k]

    def stats(self) -> dict:
        return {
            "events": len(self.events),
            "milestones": len(self.milestone_events),
            "supersedes_edges": len(self.supersedes),
            "cites_edges": sum(len(items) for items in self.cites.values()),
            "depends_on_edges": sum(len(items) for items in self.depends_on.values()),
        }


def build(workspace: Path) -> LedgerGraph | None:
    events = _safe_read(Path(workspace) / "promise_ledger.jsonl")
    if not events:
        return None
    return LedgerGraph(events)


def render_summary(
    graph: LedgerGraph | None,
    max_chains: int = 6,
    max_chars: int = 16_000,
) -> str:
    if graph is None:
        return ""
    stats = graph.stats()
    lines = [
        "# Ledger Causal Summary",
        "",
        (
            f"Events: {stats['events']}, milestones: {stats['milestones']}, "
            f"supersedes edges: {stats['supersedes_edges']}, citation edges: "
            f"{stats['cites_edges']}, dependency edges: {stats['depends_on_edges']}."
        ),
        "",
        "## Causal chains to validated milestones",
        "",
    ]

    targets = [
        (mid, events[-1])
        for mid, events in graph.milestone_events.items()
        if events and events[-1].get("status") == "validated" and not mid.startswith("_")
    ]
    targets.sort(key=lambda item: str(item[1].get("ts") or ""), reverse=True)
    for mid, event in targets[:max_chains]:
        chain = graph.chain_to(event.get("event_id") or "", max_depth=10)
        conf = event.get("confidence") or {}
        lines.append(f"### {mid} ({event.get('status', '?')}, {conf.get('level', '?')})")
        for item in chain:
            item_conf = item.get("confidence") or {}
            artifacts = item.get("artifacts") or []
            artifact_suffix = ""
            if artifacts:
                rendered_artifacts = [str(artifact) for artifact in artifacts[:2]]
                artifact_suffix = f" (artifacts={', '.join(rendered_artifacts)}"
                if len(artifacts) > 2:
                    artifact_suffix += "..."
                artifact_suffix += ")"
            lines.append(
                f"  - cycle {item.get('cycle', '?')}: "
                f"{item.get('milestone_id', '?')} {item.get('status', '?')}/"
                f"{item_conf.get('level', '?')}{artifact_suffix}"
            )
        lines.append("")

    contradictions = graph.contradiction_clusters()
    if contradictions:
        lines += ["## Contradictions", ""]
        for mid, event_ids in contradictions:
            short_ids = ", ".join(f"{eid[:8]}..." for eid in event_ids)
            lines.append(
                f"  - {mid}: contradicting events without intervening "
                f"`reopened` -- events {short_ids}. INVESTIGATE."
            )
        lines.append("")

    density = graph.evidence_density(top_k=10)
    if density:
        lines += ["## Top evidence", ""]
        for idx, (item, count) in enumerate(density, 1):
            lines.append(f"  {idx}. {item} -- cited {count} time(s)")
        lines.append("")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... [truncated ledger causal summary]"
    return text
