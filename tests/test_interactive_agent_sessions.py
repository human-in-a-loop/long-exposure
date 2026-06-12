"""Interactive-transport session hygiene at the _call_exploration_agent level.

1. Interactive mode has no provider-native per-agent session, so the minted
   per-call UUID must NOT be persisted into agent_sessions — a later headless
   resume would burn one failed cycle per agent on ``--resume <bogus-uuid>``.
2. When the score requested ``mcp: true`` (dropped by interactive transport),
   a one-line notice is printed once per agent per run.
"""

import os
import tempfile
from unittest.mock import patch

import long_exposure.exploration as exploration
from long_exposure.exploration import _call_exploration_agent
from long_exposure.orchestrator import load_config


def _fake_run_turn(**kwargs):
    return {
        "result": "[OUTPUT: research_brief]\nthe brief\n[END OUTPUT: research_brief]",
        "usage": {"output_tokens": 10},
        "duration_ms": 1,
    }


def _call(agent_sessions, agent_def_extra=None):
    agent_def = {
        "role": "You are a researcher.",
        "inputs": ["directive"],
        "outputs": ["research_brief"],
    }
    agent_def.update(agent_def_extra or {})
    with tempfile.TemporaryDirectory() as td:
        config = load_config()
        config.update({
            "llm_provider": "claude",
            "claude_transport": "interactive",
            "working_directory": td,
            "compact_db": os.path.join(td, "sessions.db"),
        })
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LONG_EXPOSURE_LLM_PROVIDER", None)
            with patch(
                "long_exposure.interactive_transport.run_turn", _fake_run_turn
            ):
                return _call_exploration_agent(
                    agent_name="researcher",
                    agent_def=agent_def,
                    task="test task",
                    config=config,
                    results={"directive": "test task"},
                    score_inputs={"directive": "test task"},
                    agent_sessions=agent_sessions,
                    agent_summaries={},
                )


def test_interactive_does_not_persist_placeholder_session():
    sessions = {}
    result = _call(sessions)
    assert result["status"] == "ok"
    assert result["outputs"]["research_brief"] == "the brief"
    # No fabricated UUID pinned for a later headless --resume to choke on.
    assert sessions == {}


def test_interactive_leaves_prior_headless_session_untouched():
    # A genuine session from an earlier headless run must not be overwritten
    # by a per-call placeholder.
    sessions = {"researcher": "real-headless-session"}
    result = _call(sessions)
    assert result["status"] == "ok"
    assert sessions == {"researcher": "real-headless-session"}


def test_mcp_drop_notice_printed_once_per_agent(capsys):
    exploration._INTERACTIVE_MCP_NOTICE_SEEN.clear()
    try:
        _call({}, agent_def_extra={"mcp": True})
        _call({}, agent_def_extra={"mcp": True})
        out = capsys.readouterr().out
        assert out.count("session-search MCP unavailable in interactive transport") == 1
        assert "researcher" in out
    finally:
        exploration._INTERACTIVE_MCP_NOTICE_SEEN.clear()


def test_no_mcp_notice_when_agent_does_not_request_mcp(capsys):
    exploration._INTERACTIVE_MCP_NOTICE_SEEN.clear()
    _call({})
    out = capsys.readouterr().out
    assert "session-search MCP unavailable" not in out
