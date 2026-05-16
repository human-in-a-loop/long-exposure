"""Context proximity scoring and gem injection.

Ranks past sessions by relevance to the current task and formats
the top results as XML gems for injection into the agent's system prompt.
"""

import re
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape


def extract_catalog_from_xml(summary_xml: str | None) -> dict:
    """Extract catalog fields from session summary XML.

    Returns dict with keys: topic, subtopic, tools, keywords.
    Missing fields are omitted. Safe to call with None.
    """
    if not summary_xml:
        return {}
    catalog = {}
    for field in ("topic", "subtopic", "tools", "keywords"):
        match = re.search(rf"<{field}>(.*?)</{field}>", summary_xml, re.DOTALL)
        if match:
            val = match.group(1).strip()
            if val:
                catalog[field] = val
    return catalog


def score_session(
    session: dict,
    profile: dict,
    current_catalog: dict,
    now: datetime,
    ancestor_set: set[str] | None = None,
) -> float:
    """Score a session's relevance to the current context.

    Args:
        session: Dict with at least topic, subtopic, tools, keywords, created_at.
        profile: Relevance profile with topic_weights, tool_weights, keyword_weights.
        current_catalog: The current task's catalog (topic, subtopic, tools).
        now: Current UTC datetime for recency calculation.

    Returns a float score (higher = more relevant).
    """
    score = 0.0
    topic_weights = profile.get("topic_weights", {})
    tool_weights = profile.get("tool_weights", {})
    keyword_weights = profile.get("keyword_weights", {})

    # --- Topic matching ---
    current_topic = current_catalog.get("topic")
    if current_topic and session.get("topic") == current_topic:
        score += topic_weights.get("_same_topic", 0)

        current_subtopic = current_catalog.get("subtopic")
        if current_subtopic and session.get("subtopic") == current_subtopic:
            score += topic_weights.get("_same_subtopic", 0)

    # Base score for all sessions
    score += topic_weights.get("_any_topic", 0)

    # Named topic weights (boost or penalize specific topics)
    session_topic = session.get("topic") or ""
    if session_topic and session_topic in topic_weights:
        score += topic_weights[session_topic]

    # --- Tool matching ---
    session_tools = _split_csv(session.get("tools"))
    current_tools = _split_csv(current_catalog.get("tools"))
    shared = session_tools & current_tools
    score += len(shared) * tool_weights.get("_shared_tools", 0)

    # --- Keyword matching ---
    for kw in _split_csv(session.get("keywords")):
        if kw in keyword_weights:
            score += keyword_weights[kw]

    # --- Lesson boost (Plan 5 §2.3) ---
    # Lesson records are concentrated cross-cutting findings emitted by the
    # final auditor. They get a +0.3 base bonus and skip the standard recency
    # decay for their first 10 runs of life (durable wisdom shouldn't fade
    # just because work moves on). After that, a soft re-decay applies.
    is_lesson = session.get("record_type") == "lesson"
    if is_lesson:
        score += 0.3

    if ancestor_set and session.get("id") in ancestor_set:
        score += profile.get("topic_weights", {}).get("_ancestor", 0)

    # --- Recency decay ---
    age_days = _age_in_days(session.get("created_at"), now)
    half_life = 30.0
    if is_lesson:
        # Approximate "first 10 runs" by ~3-day-per-run cadence × 10 = 30 days.
        # Beyond that, apply a much gentler half-life of 365 days so durable
        # lessons remain ranked but not ossified (~0.05 / run after the
        # exemption period). This keeps stale lessons from dominating
        # indefinitely while preserving short-term immunity.
        if age_days <= 30:
            recency = 1.0
        else:
            recency = 1.0 / (1.0 + ((age_days - 30) / 365.0))
    else:
        recency = 1.0 / (1.0 + (age_days / half_life))
    score *= recency

    return score


def compute_ancestor_set(
    sessions: list[dict],
    start_id: str | None,
    max_depth: int = 20,
) -> set[str]:
    """Walk parent_id pointers backward from start_id."""
    if not start_id:
        return set()
    by_id = {s.get("id"): s for s in sessions if s.get("id")}
    ancestors: set[str] = set()
    visited: set[str] = set()
    current = by_id.get(start_id)
    for _ in range(max_depth):
        if not current:
            break
        parent_id = current.get("parent_id")
        if not parent_id or parent_id in visited:
            break
        visited.add(parent_id)
        ancestors.add(parent_id)
        current = by_id.get(parent_id)
    return ancestors


def extract_snippet(summary_xml: str | None, max_chars: int = 150) -> str:
    """Extract a one-line snippet from session summary XML.

    Priority order:
    1. First high-priority working_memory fact
    2. First active_thread description
    3. First decision chosen text
    4. First sentence of objective

    Safe to call with None.
    """
    if not summary_xml:
        return ""
    patterns = [
        r'<fact priority="high">(.*?)</fact>',
        r"<description>(.*?)</description>",
        r"<chosen>(.*?)</chosen>",
        r"<objective>(.*?)</objective>",
    ]

    for pattern in patterns:
        match = re.search(pattern, summary_xml, re.DOTALL)
        if match:
            text = match.group(1).strip()
            # Collapse whitespace
            text = re.sub(r"\s+", " ", text)
            if text:
                if len(text) > max_chars:
                    text = text[: max_chars - 3] + "..."
                return text

    return ""


def rank_sessions(
    sessions: list[dict],
    profile: dict,
    current_catalog: dict,
    max_gems: int = 7,
    min_score: float = 0.3,
    exclude_id: str | None = None,
    fork_scope: str = "all",
    current_fork_id: str | None = None,
    ancestor_anchor_id: str | None = None,
) -> list[dict]:
    """Rank sessions by relevance and return the top N.

    Args:
        sessions: List of session dicts (from get_all_sessions_with_catalog).
        profile: Relevance profile dict.
        current_catalog: Current task catalog dict.
        max_gems: Maximum sessions to return.
        min_score: Minimum score threshold.
        exclude_id: Session ID to exclude (e.g. the current session).
        fork_scope: How to filter sessions by fork_id. One of:
            "all"        — no filter (default; preserves prior behavior).
            "root_only"  — keep only sessions with fork_id IS NULL (root-authored).
            "same_fork"  — keep root-authored sessions plus sessions whose
                           fork_id matches current_fork_id (own branch +
                           pre-fork root context).
        current_fork_id: The caller's fork_id; used only when
            fork_scope == "same_fork". None means "treat caller as root".
        ancestor_anchor_id: Optional session ID whose parent chain receives
            the opt-in `_ancestor` score bonus.

    Returns list of dicts with added 'score' and 'snippet' keys, sorted by score desc.
    """
    if fork_scope not in ("all", "root_only", "same_fork"):
        raise ValueError(
            f"fork_scope must be one of 'all'|'root_only'|'same_fork', "
            f"got {fork_scope!r}"
        )

    now = datetime.now(timezone.utc)
    ancestor_set = compute_ancestor_set(sessions, ancestor_anchor_id)
    scored = []

    for session in sessions:
        if session.get("record_type") == "lemma":
            continue
        if exclude_id and session.get("id") == exclude_id:
            continue
        # Skip sessions with no catalog data
        if not session.get("topic"):
            continue

        # Fork-scope filter: additive, opt-in. Default ("all") is a no-op.
        session_fork = session.get("fork_id")
        if fork_scope == "root_only" and session_fork is not None:
            continue
        if fork_scope == "same_fork" and session_fork is not None \
                and session_fork != current_fork_id:
            continue

        s = score_session(session, profile, current_catalog, now, ancestor_set=ancestor_set)
        if s >= min_score:
            scored.append({
                "id": session["id"],
                "score": round(s, 3),
                "topic": session.get("topic") or "",
                "subtopic": session.get("subtopic") or "",
                "tools": session.get("tools") or "",
                "snippet": extract_snippet(session.get("summary_xml", "")),
            })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:max_gems]


def format_gems_xml(ranked: list[dict]) -> str:
    """Format ranked sessions as an XML gems block for system prompt injection.

    Returns empty string if no gems.
    """
    if not ranked:
        return ""

    lines = [
        f'<context_gems count="{len(ranked)}" note="Pre-ranked session pointers. '
        "Read these before searching. Use search_sessions_by_id(session_id) "
        'for full session content.">'
    ]

    for i, gem in enumerate(ranked, 1):
        # Escape XML-sensitive characters in catalog values
        esc = {k: xml_escape(str(gem.get(k, "")), {'"': "&quot;"}) for k in
               ("id", "topic", "subtopic", "tools")}
        lines.append(
            f'  <gem rank="{i}" session_id="{esc["id"]}" score="{gem["score"]}"'
            f' topic="{esc["topic"]}" subtopic="{esc["subtopic"]}"'
            f' tools="{esc["tools"]}">'
        )
        lines.append(f"    {xml_escape(gem.get('snippet', ''))}")
        lines.append("  </gem>")

    lines.append("</context_gems>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_csv(value: str | None) -> set[str]:
    """Split a comma-separated string into a set of stripped, non-empty tokens."""
    if not value:
        return set()
    return {t.strip() for t in value.split(",") if t.strip()}


def _age_in_days(iso_timestamp: str | None, now: datetime) -> int:
    """Calculate age in days from an ISO timestamp to now."""
    if not iso_timestamp:
        return 365  # Unknown age — treat as old
    try:
        created = datetime.fromisoformat(iso_timestamp)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0, (now - created).days)
    except (ValueError, TypeError):
        return 365
