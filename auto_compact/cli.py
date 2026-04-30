"""CLI entry point for auto-compact."""

import argparse
import os
import sys
from pathlib import Path

import anthropic

from .compact import bootstrap_from_db, checkpoint, compact
from .db import (
    DEFAULT_DB_PATH,
    get_session_by_id,
    init_db,
    list_session_catalog,
    search_sessions,
)

DEFAULT_MODEL = os.environ.get("COMPACT_MODEL", "claude-sonnet-4-6")
DEFAULT_CONTEXT_WINDOW = 1_000_000
DEFAULT_THRESHOLD = 0.90

# Tools exposed to the model
SEARCH_TOOL = {
    "name": "search_sessions",
    "description": (
        "Search past session summaries for context. Use when the user references "
        "something not in the current summary, or asks 'do you remember...'"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query for past sessions",
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (default 5)",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}

SEARCH_BY_ID_TOOL = {
    "name": "search_sessions_by_id",
    "description": (
        "Retrieve a specific session's full summary by ID. Use this to get "
        "full context for a session found via context gems or the catalog."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "The session ID to retrieve.",
            },
        },
        "required": ["session_id"],
    },
}

LIST_CATALOG_TOOL = {
    "name": "list_session_catalog",
    "description": (
        "List all sessions with their catalog metadata (topic, subtopic, "
        "tools, keywords). Returns a compact table for browsing. Use this "
        "to find sessions beyond the pre-loaded context gems. For full "
        "session content, follow up with search_sessions_by_id."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "topic_filter": {
                "type": "string",
                "description": "Optional. Filter to sessions matching this topic.",
            },
            "tools_filter": {
                "type": "string",
                "description": "Optional. Filter to sessions that used this tool.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum rows to return (default 25, max 100).",
                "default": 25,
            },
        },
    },
}

ALL_TOOLS = [SEARCH_TOOL, SEARCH_BY_ID_TOOL, LIST_CATALOG_TOOL]

SYSTEM_PROMPT = """\
You are a helpful assistant with persistent memory across context boundaries. \
You have access to a search_sessions tool to look up past conversation context \
when needed. Use it when the user references something you don't have in your \
current context, or when you suspect relevant information exists from prior sessions.

If the user says /compact, immediately trigger a compaction cycle.
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Auto-Compact: Persistent Context Management")
    parser.add_argument(
        "--compact-threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Fraction of context window that triggers compaction (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        "--compact-db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite database path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=DEFAULT_CONTEXT_WINDOW,
        help=f"Context window size W (default: {DEFAULT_CONTEXT_WINDOW})",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=DEFAULT_MODEL,
        help=f"Anthropic model to use (default: {DEFAULT_MODEL})",
    )
    return parser.parse_args(argv)


def handle_tool_use(conn, response):
    """Process tool calls in the response. Returns tool results to feed back."""
    tool_results = []
    for block in response.content:
        if block.type != "tool_use":
            continue

        result_text = ""

        if block.name == "search_sessions":
            query = block.input.get("query", "")
            limit = block.input.get("limit", 5)
            results = search_sessions(conn, query, limit)
            if results:
                result_text = "\n\n---\n\n".join(
                    f"Session {r['id']} (depth {r['depth']}, {r['created_at']}):\n{r['summary_xml']}"
                    for r in results
                )
            else:
                result_text = "No matching sessions found."

        elif block.name == "search_sessions_by_id":
            sid = block.input.get("session_id", "")
            session = get_session_by_id(conn, sid)
            if session:
                result_text = (
                    f"Session {session['id']} (depth {session['depth']}, "
                    f"{session['created_at']}):\n{session['summary_xml']}"
                )
            else:
                result_text = f"No session found with ID: {sid}"

        elif block.name == "list_session_catalog":
            topic = block.input.get("topic_filter")
            tools = block.input.get("tools_filter")
            limit = min(block.input.get("limit", 25), 100)
            rows = list_session_catalog(conn, topic_filter=topic, tools_filter=tools, limit=limit)
            if rows:
                lines = []
                for r in rows:
                    lines.append(
                        f"{r['id'][:8]}... | {r.get('created_at', '')[:10]} | "
                        f"topic={r.get('topic', '')} subtopic={r.get('subtopic', '')} | "
                        f"tools={r.get('tools', '')} | keywords={r.get('keywords', '')}"
                    )
                result_text = "\n".join(lines)
            else:
                result_text = "No sessions found matching filters."

        else:
            result_text = f"Unknown tool: {block.name}"

        tool_results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": result_text,
        })
    return tool_results


def get_text_content(response) -> str:
    """Extract text content from a response."""
    parts = []
    for block in response.content:
        if block.type == "text":
            parts.append(block.text)
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    client = anthropic.Anthropic()
    conn = init_db(args.compact_db)

    # Bootstrap from last session if one exists
    conversation, parent_id, depth = bootstrap_from_db(conn)

    W = args.context_window
    threshold = args.compact_threshold
    model = args.model
    checkpoint_threshold = threshold / 2  # 50% of working context (e.g., 45% of W when threshold=0.90)
    checkpoint_logged = False  # Reset after each compaction

    print("Auto-Compact ready. Type your message (Ctrl+D or 'exit' to quit).")
    print(f"  Context: {W:,} tokens | Compact at {threshold:.0%} | Checkpoint at {checkpoint_threshold:.0%}")
    if parent_id:
        print(f"  Restored from session {parent_id} (depth {depth})")
    print()

    while True:
        # Get user input
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not user_input:
            continue
        if user_input.lower() == "exit":
            # Compact on exit so state is saved
            if conversation:
                print("[Compacting before exit...]")
                conversation, parent_id, depth = compact(
                    client, model, conn, conversation, parent_id, depth
                )
            print("Goodbye.")
            break

        # Handle manual /compact command
        force_compact = user_input.strip().lower() == "/compact"
        if force_compact and conversation:
            print("[Compacting...]")
            conversation, parent_id, depth = compact(
                client, model, conn, conversation, parent_id, depth
            )
            print(f"[Compacted. New depth: {depth}]")
            continue

        conversation.append({"role": "user", "content": user_input})

        # Call the model
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=conversation,
            tools=ALL_TOOLS,
        )

        # Handle tool use loop
        while response.stop_reason == "tool_use":
            # Add assistant message with tool calls
            conversation.append({"role": "assistant", "content": response.content})
            tool_results = handle_tool_use(conn, response)
            conversation.append({"role": "user", "content": tool_results})

            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=conversation,
                tools=ALL_TOOLS,
            )

        # Add final assistant response
        conversation.append({"role": "assistant", "content": response.content})

        # Print response
        text = get_text_content(response)
        if text:
            print(f"\nAssistant: {text}\n")

        # Check threshold
        used = response.usage.input_tokens + response.usage.output_tokens
        ratio = used / W
        if ratio >= threshold:
            print(f"[Context at {ratio:.0%} of window. Logging checkpoint and compacting...]")
            conversation, parent_id, depth = compact(
                client, model, conn, conversation, parent_id, depth
            )
            checkpoint_logged = False  # Reset for next cycle
            print(f"[Compacted. New depth: {depth}]")
        elif ratio >= checkpoint_threshold and not checkpoint_logged:
            print(f"[Context at {ratio:.0%} of window ({used:,} tokens). Logging mid-context checkpoint...]")
            cp_id = checkpoint(client, model, conn, conversation, parent_id, depth)
            checkpoint_logged = True
            print(f"[Checkpoint {cp_id[:8]}... logged. Continuing without compaction.]")

    conn.close()


if __name__ == "__main__":
    main()
