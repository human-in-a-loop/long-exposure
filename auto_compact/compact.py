"""Compaction logic: summary generation, storage, and bootstrap."""

import uuid
from datetime import datetime, timezone

import anthropic

from .db import count_sessions, get_latest_session, store_session
from .proximity import extract_catalog_from_xml

SUMMARY_SYSTEM_PROMPT = """\
Generate a session summary in the XML schema below. Capture everything needed \
to continue this work seamlessly. Be thorough but ruthless about what matters.

<schema>
<session_summary>
  <meta>
    <session_id>{session_id}</session_id>
    <parent_id>{parent_id}</parent_id>
    <depth>{depth}</depth>
    <timestamp>{timestamp}</timestamp>
  </meta>

  <context>
    <objective>{{top-level goal}}</objective>
    <background>{{key facts, constraints, preferences discovered}}</background>
  </context>

  <state>
    <artifacts>
      <artifact path="{{path}}" status="created|modified|deleted">{{description}}</artifact>
    </artifacts>
    <decisions>
      <decision topic="{{what}}">
        <chosen>{{what was decided}}</chosen>
        <rationale>{{why, including rejected alternatives if non-obvious}}</rationale>
      </decision>
    </decisions>
    <working_memory>
      <fact>{{fact}}</fact>
    </working_memory>
  </state>

  <plan>
    <active_threads>
      <thread priority="high|medium|low" status="active|blocked">
        {{description and next concrete step}}
      </thread>
    </active_threads>
    <completed>
      <item>{{what was done}}</item>
    </completed>
  </plan>

  <catalog>
    <topic>{{broad domain area}}</topic>
    <subtopic>{{specific focus within the topic}}</subtopic>
    <tools>{{tools, libraries, APIs, or system components central to this session}}</tools>
    <keywords>{{3-5 freeform terms for search matching}}</keywords>
  </catalog>
</session_summary>
</schema>

Rules:
- Use the provided session_id, parent_id, depth, and timestamp in <meta>.
- At depth >= 2, aggressively drop <completed> items and compress <decisions> \
to outcomes only (no rationale for old settled decisions).
- <working_memory> facts survive across all depths unless explicitly invalidated.
- Keep the summary concise. It must not exceed ~15% of the context window.
- Do not use emoji characters. Use plain text for status indicators (e.g. ACTIVE, DONE).
- Output ONLY the XML. No preamble, no explanation.

Catalog instructions:
- topic: The broad domain area you worked in. Use lowercase, underscore-separated. \
Choose the single most accurate term. Examples: api_client, auth, database, \
deployment, ui, testing, data_pipeline, infrastructure, documentation.
- subtopic: The specific focus within that domain. Same format. \
Examples: retry_logic, oauth_flow, connection_pooling, schema_design.
- tools: Comma-separated list of tools, libraries, frameworks, APIs, or system \
components that were central to this session's work. Use canonical names. \
Only include tools you actually used or investigated, not mentioned in passing.
- keywords: 3-5 freeform terms capturing the nature of the work. These should \
be terms someone would search for when looking for this session.
- Be consistent. Use the same topic/subtopic terms across sessions when working \
in the same area. Do not invent new terms when an existing one fits.
"""


def generate_summary(
    client: anthropic.Anthropic,
    model: str,
    conversation: list[dict],
    parent_id: str | None,
    depth: int,
    max_tokens: int = 4096,
    system_prompt_override: str | None = None,
) -> tuple[str, str, str, int, int]:
    """Generate a session summary from the current conversation.

    Args:
        system_prompt_override: If provided, used as the system prompt instead
            of the default. Must be a fully formatted string (no further
            interpolation). The caller is responsible for embedding session_id,
            parent_id, depth, and timestamp.

    Returns (session_id, timestamp, summary_xml, new_depth, token_estimate).
    token_estimate is the output token count of the summary as reported by
    the API (size of what gets stored and later injected on bootstrap).
    """
    session_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()
    new_depth = depth + 1

    if system_prompt_override is not None:
        system = system_prompt_override
    else:
        system = SUMMARY_SYSTEM_PROMPT.format(
            session_id=session_id,
            parent_id=parent_id or "null",
            depth=new_depth,
            timestamp=timestamp,
        )

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=conversation,
    )

    if not response.content or not hasattr(response.content[0], 'text'):
        raise ValueError("Summary generation returned no text content")
    summary_xml = response.content[0].text
    token_estimate = response.usage.output_tokens
    return session_id, timestamp, summary_xml, new_depth, token_estimate


def compact(
    client: anthropic.Anthropic,
    model: str,
    conn,
    conversation: list[dict],
    parent_id: str | None,
    depth: int,
) -> tuple[list[dict], str, int]:
    """Run the full compact cycle: summarize, store, bootstrap.

    Returns (new_conversation, new_session_id, new_depth).
    """
    # Step 1: Generate summary
    session_id, timestamp, summary_xml, new_depth, token_estimate = generate_summary(
        client, model, conversation, parent_id, depth
    )

    # Step 2: Extract catalog from generated summary
    catalog = extract_catalog_from_xml(summary_xml)

    # Step 3: Store
    store_session(conn, session_id, parent_id, new_depth, timestamp, summary_xml,
                  token_estimate=token_estimate, record_type="compaction", **catalog)

    # Step 4: Bootstrap new conversation
    new_conversation = bootstrap_conversation(conn, session_id, summary_xml, new_depth, timestamp)

    return new_conversation, session_id, new_depth


def checkpoint(
    client: anthropic.Anthropic,
    model: str,
    conn,
    conversation: list[dict],
    parent_id: str | None,
    depth: int,
) -> str:
    """Log a mid-context checkpoint to sessions.db WITHOUT resetting conversation.

    Generates a summary snapshot at the current point and stores it as a
    checkpoint record. The conversation continues unchanged — no compaction,
    no context reset.

    Returns the checkpoint session_id.
    """
    session_id, timestamp, summary_xml, _, token_estimate = generate_summary(
        client, model, conversation, parent_id, depth
    )

    # Extract catalog from generated summary
    catalog = extract_catalog_from_xml(summary_xml)

    # Store as checkpoint (same depth — no compaction happened)
    store_session(conn, session_id, parent_id, depth, timestamp, summary_xml,
                  token_estimate=token_estimate, record_type="checkpoint", **catalog)

    return session_id


def bootstrap_conversation(
    conn, session_id: str, summary_xml: str, depth: int, timestamp: str
) -> list[dict]:
    """Create the bootstrap preamble as the first message in a new conversation."""
    n = count_sessions(conn)
    preamble = (
        f"[RESTORED SESSION — depth {depth}, from {timestamp}]\n\n"
        f"{summary_xml}\n\n"
        f"[SESSION HISTORY]\n"
        f"You have {n} previous sessions stored and searchable.\n"
        f"Use search_sessions(query) to find past context when needed.\n"
        f"Use search_sessions_by_id(session_id) to fetch a specific session.\n"
        f"Use list_session_catalog() to browse session metadata.\n\n"
        f"[INSTRUCTIONS]\n"
        f"Continue seamlessly. Do not announce the compaction to the user\n"
        f"unless they ask. You have full continuity."
    )
    return [{"role": "user", "content": preamble}, {"role": "assistant", "content": "Understood. I have full continuity and am ready to continue."}]


def bootstrap_from_db(conn) -> tuple[list[dict], str | None, int]:
    """Load the latest session from the DB and bootstrap, or start fresh.

    Returns (conversation, parent_id, depth).
    """
    latest = get_latest_session(conn)
    if latest is None:
        return [], None, 0

    conversation = bootstrap_conversation(
        conn,
        latest["id"],
        latest["summary_xml"],
        latest["depth"],
        latest["created_at"],
    )
    return conversation, latest["id"], latest["depth"]
