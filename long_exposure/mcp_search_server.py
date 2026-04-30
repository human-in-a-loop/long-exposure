#!/usr/bin/env python3
"""Minimal MCP server exposing session tools over stdio.

Launched as a subprocess by Claude Code via --mcp-config.
Reads the SQLite database path from the SESSIONS_DB environment variable.

Protocol: JSON-RPC 2.0 over stdio with Content-Length framing (MCP standard).
"""

import json
import os
import sys
from pathlib import Path

from auto_compact.db import (
    get_session_by_id,
    init_db,
    list_session_catalog,
    search_sessions,
)


def read_message():
    """Read a JSON-RPC message with Content-Length header from stdin."""
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        decoded = line.decode("utf-8")
        if decoded.strip() == "":
            break
        if ":" in decoded:
            key, value = decoded.split(":", 1)
            headers[key.strip()] = value.strip()

    content_length = int(headers.get("Content-Length", 0))
    if content_length == 0:
        return None

    body = sys.stdin.buffer.read(content_length)
    return json.loads(body)


def write_message(msg):
    """Write a JSON-RPC message with Content-Length header to stdout."""
    body = json.dumps(msg).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n"
    sys.stdout.buffer.write(header.encode("utf-8"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


TOOLS = [
    {
        "name": "search_sessions",
        "description": (
            "Search past session summaries for historical context. "
            "Use when the current session summary doesn't contain "
            "information you need, or when the user references past "
            "work not in your current state. "
            "Pass record_type='lesson' to surface only concentrated "
            "cross-cutting findings emitted by the final auditor."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return (default 5).",
                    "default": 5,
                },
                "record_type": {
                    "type": "string",
                    "description": (
                        "Optional. Filter by record_type. One of: "
                        "compaction, checkpoint, exploration, lesson. "
                        "Omit for all."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_sessions_by_id",
        "description": (
            "Retrieve a specific session's full summary by ID. "
            "Use this to get full context for a session found via "
            "context gems or the catalog."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session ID to retrieve.",
                },
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "list_session_catalog",
        "description": (
            "List sessions with their catalog metadata (topic, subtopic, "
            "tools, keywords). Returns a compact table for browsing. "
            "Use this to find sessions beyond the pre-loaded context gems."
        ),
        "inputSchema": {
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
    },
]


def handle_tool_call(conn, tool_name, arguments):
    """Dispatch a tool call and return the result text."""
    if tool_name == "search_sessions":
        query = arguments.get("query", "")
        limit = min(arguments.get("limit", 5), 20)
        record_type = arguments.get("record_type")
        try:
            results = search_sessions(conn, query, limit, record_type=record_type)
        except Exception:
            results = []

        if results:
            return "\n\n---\n\n".join(
                f"Session {r['id']} (depth {r['depth']}, {r['created_at']}):\n"
                f"{r['summary_xml']}"
                for r in results
            )
        return "No matching sessions found."

    elif tool_name == "search_sessions_by_id":
        session_id = arguments.get("session_id", "")
        try:
            session = get_session_by_id(conn, session_id)
        except Exception:
            session = None

        if session:
            return (
                f"Session {session['id']} (depth {session['depth']}, "
                f"{session['created_at']}):\n{session['summary_xml']}"
            )
        return f"No session found with ID: {session_id}"

    elif tool_name == "list_session_catalog":
        topic = arguments.get("topic_filter")
        tools = arguments.get("tools_filter")
        limit = min(arguments.get("limit", 25), 100)
        try:
            rows = list_session_catalog(
                conn, topic_filter=topic, tools_filter=tools, limit=limit
            )
        except Exception:
            rows = []

        if rows:
            lines = []
            for r in rows:
                lines.append(
                    f"{r['id'][:8]}... | {r.get('created_at', '')[:10]} | "
                    f"topic={r.get('topic', '')} subtopic={r.get('subtopic', '')} | "
                    f"tools={r.get('tools', '')} | keywords={r.get('keywords', '')}"
                )
            return "\n".join(lines)
        return "No sessions found matching filters."

    return f"Unknown tool: {tool_name}"


def main():
    db_path = os.environ.get("SESSIONS_DB", "")
    if db_path:
        db_path = Path(db_path)
    else:
        db_path = Path.home() / ".local" / "share" / "auto-compact" / "sessions.db"

    conn = init_db(db_path)

    while True:
        msg = read_message()
        if msg is None:
            break

        method = msg.get("method", "")
        msg_id = msg.get("id")

        if method == "initialize":
            write_message({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "sessions-search", "version": "2.0.0"},
                },
            })

        elif method == "notifications/initialized":
            pass  # Notification — no response

        elif method == "tools/list":
            write_message({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": TOOLS},
            })

        elif method == "tools/call":
            params = msg.get("params", {})
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})

            text = handle_tool_call(conn, tool_name, arguments)

            write_message({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                },
            })

        elif msg_id is not None:
            # Unknown method with an ID — return error
            write_message({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            })

    conn.close()


if __name__ == "__main__":
    main()
