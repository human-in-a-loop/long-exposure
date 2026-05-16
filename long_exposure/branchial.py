"""Branchial entropy signal over the sessions.db catalog.

Read-only helper for the manager sidecar. It summarizes whether recent
compaction/exploration catalog tuples have collapsed onto a narrow topic
region. The signal is watch-only; callers decide what to do with it.
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from pathlib import Path


DEFAULT_WINDOW = 20
DEFAULT_MIN_CYCLES = 10
DEFAULT_OVERALL_CAP = 500

COLLAPSED_RECENT_H = 0.5
COLLAPSED_RATIO = 0.5
NARROWING_RECENT_H = 0.8
NARROWING_RATIO = 0.7


def _shannon_entropy_nats(counts: Counter[tuple[str, str]]) -> float:
    total = sum(counts.values())
    if total <= 0:
        return 0.0
    h = 0.0
    for count in counts.values():
        if count <= 0:
            continue
        p = count / total
        h -= p * math.log(p)
    return h


def _classify(recent_h: float, overall_h: float, total: int, min_cycles: int) -> str:
    if total < min_cycles:
        return "cold"
    ratio = recent_h / max(overall_h, 0.1)
    if recent_h < COLLAPSED_RECENT_H and ratio < COLLAPSED_RATIO:
        return "collapsed"
    if recent_h < NARROWING_RECENT_H and ratio < NARROWING_RATIO:
        return "narrowing"
    return "exploring"


def compute_branchial_signal(
    db_path: Path | str,
    window: int = DEFAULT_WINDOW,
    min_cycles: int = DEFAULT_MIN_CYCLES,
    overall_cap: int = DEFAULT_OVERALL_CAP,
) -> dict | None:
    """Return recent catalog entropy summary, or None when no signal exists.

    Missing DBs, unreadable DBs, cold starts, and malformed parameters all
    collapse to None so the manager can degrade to its prior behavior.
    """
    try:
        db_path = Path(db_path)
        window = max(1, int(window))
        min_cycles = max(1, int(min_cycles))
        overall_cap = max(window, int(overall_cap))
    except Exception:
        return None
    if not db_path.exists():
        return None

    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT topic, subtopic FROM sessions "
            "WHERE record_type IN ('compaction', 'exploration') "
            "AND topic IS NOT NULL AND topic != '' "
            "ORDER BY created_at DESC LIMIT ?",
            (overall_cap,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass

    total = len(rows)
    if total < min_cycles:
        return None

    tuples = [
        (str(row["topic"] or ""), str(row["subtopic"] or ""))
        for row in rows
    ]
    recent_tuples = tuples[:window]
    overall_counts: Counter[tuple[str, str]] = Counter(tuples)
    recent_counts: Counter[tuple[str, str]] = Counter(recent_tuples)
    recent_h = _shannon_entropy_nats(recent_counts)
    overall_h = _shannon_entropy_nats(overall_counts)
    ratio = recent_h / max(overall_h, 0.1)
    classification = _classify(recent_h, overall_h, total, min_cycles)
    if classification == "cold":
        return None

    return {
        "recent_h": round(recent_h, 3),
        "overall_h": round(overall_h, 3),
        "ratio": round(ratio, 3),
        "classification": classification,
        "window_size": len(recent_tuples),
        "total": total,
        "top_recent_tuples": [
            {"topic": topic, "subtopic": subtopic, "count": count}
            for (topic, subtopic), count in recent_counts.most_common(3)
        ],
    }
