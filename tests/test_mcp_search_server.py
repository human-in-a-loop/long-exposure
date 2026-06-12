"""Tests for the MCP stdio server: malformed input resilience and conformance."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from auto_compact.db import init_db
from long_exposure import mcp_search_server

REPO_ROOT = Path(__file__).resolve().parents[1]


def _rpc(method, msg_id=None, params=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        msg["id"] = msg_id
    if params is not None:
        msg["params"] = params
    return (json.dumps(msg) + "\n").encode("utf-8")


class ServerSubprocessTests(unittest.TestCase):
    """Drive the real server over stdio as Claude Code would."""

    def _run_server(self, stdin_bytes):
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ)
            env["SESSIONS_DB"] = str(Path(td) / "sessions.db")
            proc = subprocess.Popen(
                [sys.executable, "-m", "long_exposure.mcp_search_server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(REPO_ROOT),
                env=env,
            )
            out, err = proc.communicate(input=stdin_bytes, timeout=60)
        responses = [json.loads(line) for line in out.splitlines() if line.strip()]
        return proc.returncode, responses, err

    def test_malformed_lines_do_not_kill_server(self):
        stdin = (
            b"this is not json\n"          # -> -32700, keep serving
            b"\n"                           # blank line -> skipped, no response
            b"5\n"                          # valid JSON, not a dict -> -32600
            + _rpc("initialize", msg_id=1)  # server must still be alive
        )
        rc, responses, err = self._run_server(stdin)

        self.assertEqual(rc, 0, f"server crashed: {err!r}")
        self.assertEqual(len(responses), 3)

        parse_err = responses[0]
        self.assertIsNone(parse_err["id"])
        self.assertEqual(parse_err["error"]["code"], -32700)

        invalid_req = responses[1]
        self.assertIsNone(invalid_req["id"])
        self.assertEqual(invalid_req["error"]["code"], -32600)

        init_resp = responses[2]
        self.assertEqual(init_resp["id"], 1)
        self.assertEqual(init_resp["result"]["protocolVersion"], "2024-11-05")

    def test_ping_returns_empty_result(self):
        stdin = _rpc("initialize", msg_id=1) + _rpc("ping", msg_id=2)
        rc, responses, err = self._run_server(stdin)

        self.assertEqual(rc, 0, f"server crashed: {err!r}")
        ping_resp = responses[1]
        self.assertEqual(ping_resp["id"], 2)
        self.assertEqual(ping_resp["result"], {})

    def test_unknown_tool_call_is_marked_error(self):
        stdin = (
            _rpc("initialize", msg_id=1)
            + _rpc("tools/call", msg_id=2,
                   params={"name": "no_such_tool", "arguments": {}})
        )
        rc, responses, err = self._run_server(stdin)

        self.assertEqual(rc, 0, f"server crashed: {err!r}")
        call_resp = responses[1]
        self.assertEqual(call_resp["id"], 2)
        result = call_resp["result"]
        self.assertTrue(result.get("isError"))
        self.assertIn("Unknown tool: no_such_tool", result["content"][0]["text"])

    def test_hostile_search_query_returns_normal_result(self):
        stdin = (
            _rpc("initialize", msg_id=1)
            + _rpc("tools/call", msg_id=2,
                   params={"name": "search_sessions",
                           "arguments": {"query": "don't (crash"}})
        )
        rc, responses, err = self._run_server(stdin)

        self.assertEqual(rc, 0, f"server crashed: {err!r}")
        call_resp = responses[1]
        result = call_resp["result"]
        self.assertNotIn("isError", result)
        self.assertEqual(result["content"][0]["text"], "No matching sessions found.")


class ReadMessageTests(unittest.TestCase):
    def _read_from(self, data):
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(data))
        with patch.object(mcp_search_server.sys, "stdin", fake_stdin):
            return mcp_search_server.read_message()

    def test_eof_returns_none(self):
        self.assertIsNone(self._read_from(b""))

    def test_blank_lines_skipped_before_message(self):
        msg = self._read_from(b"\n  \n{\"jsonrpc\":\"2.0\",\"id\":1}\n")
        self.assertEqual(msg, {"jsonrpc": "2.0", "id": 1})

    def test_garbage_returns_parse_error_sentinel(self):
        msg = self._read_from(b"{nope\n")
        self.assertIs(msg, mcp_search_server.PARSE_ERROR)


class HandleToolCallTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.conn = init_db(Path(self._td.name) / "sessions.db")
        self.addCleanup(self.conn.close)

    def test_unknown_tool_is_error(self):
        text, is_error = mcp_search_server.handle_tool_call(self.conn, "bogus", {})
        self.assertTrue(is_error)
        self.assertIn("Unknown tool: bogus", text)

    def test_search_failure_surfaces_as_error_not_empty(self):
        def boom(*args, **kwargs):
            raise RuntimeError("backend exploded")

        with patch.object(mcp_search_server, "search_sessions", boom):
            text, is_error = mcp_search_server.handle_tool_call(
                self.conn, "search_sessions", {"query": "anything"}
            )
        self.assertTrue(is_error)
        self.assertIn("backend exploded", text)

    def test_no_matches_is_not_an_error(self):
        text, is_error = mcp_search_server.handle_tool_call(
            self.conn, "search_sessions", {"query": "nothing_stored_yet"}
        )
        self.assertFalse(is_error)
        self.assertEqual(text, "No matching sessions found.")


if __name__ == "__main__":
    unittest.main()
