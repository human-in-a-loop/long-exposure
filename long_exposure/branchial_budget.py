"""Branchial novelty annotations for fan-out proposals.

Report-only signal. Scores proposed branch objectives against recent
sessions.db catalog tuples and returns annotations; it never gates dispatch.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path


DEFAULT_RECENT_N = 50
NOVEL_THRESHOLD = 0.8
PARTIAL_THRESHOLD = 0.5

_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "in", "is", "it", "its", "of", "on", "or", "that",
    "the", "this", "to", "was", "were", "will", "with", "we", "our",
    "these", "those", "any", "some", "do", "does", "via", "into",
    "across", "between", "over", "under", "than", "then", "if", "else",
    "when", "where", "what", "which", "how",
})
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_]{2,}")


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return {tok for tok in _TOKEN_RE.findall(text.lower()) if tok not in _STOPWORDS}


def _classify(score: float) -> str:
    if score >= NOVEL_THRESHOLD:
        return "novel"
    if score >= PARTIAL_THRESHOLD:
        return "partial-retread"
    return "likely-retread"


def _unknown() -> dict:
    return {
        "novelty_score": None,
        "novelty_class": "unknown",
        "matched_session_ids": [],
        "tokens_considered": 0,
    }


def _recent_catalog_tuples(db_path: Path, recent_n: int) -> list[dict]:
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, topic, subtopic, keywords FROM sessions "
            "WHERE record_type IN ('compaction', 'exploration') "
            "AND topic IS NOT NULL AND topic != '' "
            "ORDER BY created_at DESC LIMIT ?",
            (recent_n,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return [dict(row) for row in rows]


def score_branches(
    branches: list[dict],
    db_path: Path | str | None,
    recent_n: int = DEFAULT_RECENT_N,
) -> list[dict]:
    """Return one novelty annotation per branch. Never raises."""
    if not db_path:
        return [_unknown() for _ in branches]
    try:
        catalog = _recent_catalog_tuples(Path(db_path), max(1, int(recent_n)))
    except Exception:
        return [_unknown() for _ in branches]
    if not catalog:
        return [_unknown() for _ in branches]

    tuple_tokens = []
    for row in catalog:
        toks = _tokens(
            f"{row.get('topic') or ''} "
            f"{row.get('subtopic') or ''} "
            f"{row.get('keywords') or ''}"
        )
        if toks:
            tuple_tokens.append((row.get("id"), toks))
    if not tuple_tokens:
        return [_unknown() for _ in branches]

    annotations: list[dict] = []
    for branch in branches:
        obj_tokens = _tokens(branch.get("objective"))
        if not obj_tokens:
            annotations.append(_unknown())
            continue
        scored = []
        for sid, toks in tuple_tokens:
            jacc = len(obj_tokens & toks) / max(1, len(obj_tokens | toks))
            scored.append((sid, jacc))
        scored.sort(key=lambda item: item[1], reverse=True)
        max_jacc = scored[0][1] if scored else 0.0
        novelty = 1.0 - max_jacc
        annotations.append({
            "novelty_score": round(novelty, 3),
            "novelty_class": _classify(novelty),
            "matched_session_ids": [
                sid for sid, jacc in scored[:3] if sid and jacc >= 0.3
            ],
            "tokens_considered": len(obj_tokens),
        })
    return annotations
