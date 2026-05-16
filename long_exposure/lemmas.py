"""Shared infrastructure lemma channel.

Parses <lemma_proposal> blocks from agent output and stores them as
record_type='lemma' rows in sessions.db. The channel is intentionally narrow:
only infrastructure facts from a frozen category enum are accepted.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr

from auto_compact.db import store_session


VALID_CATEGORIES = frozenset({
    "tool_invocation",
    "env_quirk",
    "data_format",
    "failed_attempt",
})
MAX_LEMMAS_PER_OUTPUT = 10

_LEMMA_BLOCK_RE = re.compile(
    r"<lemma_proposal\b([^>]*)>(.*?)</lemma_proposal>",
    re.DOTALL | re.IGNORECASE,
)
_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
_FIELD_RES = {
    "claim": re.compile(r"<claim>(.*?)</claim>", re.DOTALL | re.IGNORECASE),
    "evidence": re.compile(r"<evidence>(.*?)</evidence>", re.DOTALL | re.IGNORECASE),
    "confidence": re.compile(r"<confidence>(.*?)</confidence>", re.DOTALL | re.IGNORECASE),
}


def parse_lemma_blocks(text: str | None) -> list[dict]:
    if not text:
        return []
    matches = _LEMMA_BLOCK_RE.findall(text)
    if len(matches) > MAX_LEMMAS_PER_OUTPUT:
        print(
            f"[lemmas] capping at {MAX_LEMMAS_PER_OUTPUT}; "
            f"agent emitted {len(matches)} lemma blocks",
            flush=True,
        )
        matches = matches[:MAX_LEMMAS_PER_OUTPUT]

    parsed: list[dict] = []
    for attrs_raw, body in matches:
        attrs = dict(_ATTR_RE.findall(attrs_raw))
        category = attrs.get("category", "").strip().lower()
        label = attrs.get("label", "").strip()
        if category not in VALID_CATEGORIES:
            print(f"[lemmas] dropping invalid category {category!r}", flush=True)
            continue
        if not label:
            print("[lemmas] dropping block with missing label", flush=True)
            continue
        fields = {}
        for name, rx in _FIELD_RES.items():
            match = rx.search(body)
            fields[name] = match.group(1).strip() if match else None
        if not fields.get("claim"):
            print(f"[lemmas] dropping {label!r}: missing claim", flush=True)
            continue
        parsed.append({
            "category": category,
            "label": label,
            "claim": fields["claim"],
            "evidence": fields.get("evidence"),
            "confidence": (fields.get("confidence") or "medium").strip().lower(),
        })
    return parsed


def _build_summary_xml(lemma: dict) -> str:
    lines = [
        (
            f"<lemma_proposal category={quoteattr(str(lemma['category']))} "
            f"label={quoteattr(str(lemma['label']))}>"
        ),
        f"  <claim>{escape(str(lemma['claim']))}</claim>",
    ]
    if lemma.get("evidence"):
        lines.append(f"  <evidence>{escape(str(lemma['evidence']))}</evidence>")
    lines.append(f"  <confidence>{escape(str(lemma['confidence']))}</confidence>")
    lines.append("</lemma_proposal>")
    return "\n".join(lines)


def store_lemmas(conn, lemmas: list[dict]) -> int:
    stored = 0
    for lemma in lemmas:
        try:
            store_session(
                conn,
                session_id=str(uuid.uuid4()),
                parent_id=None,
                depth=0,
                timestamp=datetime.now(timezone.utc).isoformat(),
                summary_xml=_build_summary_xml(lemma),
                record_type="lemma",
                topic=f"lemma_{lemma['category']}",
                subtopic=lemma["label"],
                keywords=None,
                fork_id=None,
            )
            stored += 1
        except Exception as exc:
            print(f"[lemmas] store failed for {lemma['label']!r}: {exc!r}", flush=True)
    return stored


def extract_and_store_lemmas(text: str | None, conn) -> int:
    parsed = parse_lemma_blocks(text)
    if not parsed:
        return 0
    stored = store_lemmas(conn, parsed)
    if stored:
        labels = ", ".join(item["label"] for item in parsed[:5])
        print(f"[lemmas] stored {stored} lemma(s): {labels}", flush=True)
    return stored
