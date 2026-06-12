"""Regression tests for [OUTPUT:...] block parsing and transcript recovery.

Covers the failure where a periodic report captured the agent's trailing
cover-note instead of the report body:

  1. ``_OUTPUT_RE`` must match the named close tag ``[END OUTPUT: <name>]``
     (agents emit this far more often than the bare ``[END OUTPUT]``); matching
     only the bare form caused a Tier-1 miss and a Tier-2 whole-message dump.
  2. ``parse_outputs`` must keep the longest block for a name so an inline
     mention / short stub in trailing prose cannot clobber the real content.
  3. ``_session_transcript_text`` recovers the deliverable when it was emitted
     in a non-final assistant message of the turn (the CLI envelope's
     ``result`` is only the final message).
  4. Recovery is scoped to the CURRENT turn (text after the last genuine user
     prompt): sessions span cycles, so a prior cycle's [OUTPUT:] block must
     not be resurrected as this cycle's deliverable, and tool_result entries
     must not be mistaken for turn boundaries.
  5. The call-site recovery in ``_call_exploration_agent`` is per-name: only
     outputs whose primary parse failed are recovered; a successfully parsed
     fresh block is never overwritten with transcript content.
"""

import json
import os
import tempfile
from unittest.mock import patch

from long_exposure.conductor import (
    _OUTPUT_RE,
    parse_outputs,
    _session_transcript_text,
)


def test_named_close_tag_matches():
    text = "[OUTPUT: report]\nBODY TEXT\n[END OUTPUT: report]"
    assert _OUTPUT_RE.search(text)
    assert parse_outputs(text, ["report"])["report"] == "BODY TEXT"


def test_bare_close_tag_still_matches():
    text = "[OUTPUT: report]\nBODY\n[END OUTPUT]"
    assert parse_outputs(text, ["report"])["report"] == "BODY"


def test_prefer_longest_block_over_trailing_stub():
    text = (
        "[OUTPUT: report]\nFULL REAL REPORT BODY THAT IS LONG\n[END OUTPUT: report]\n"
        "Then I mention the [OUTPUT: report]\nstub\n[END OUTPUT]"
    )
    assert parse_outputs(text, ["report"])["report"] == "FULL REAL REPORT BODY THAT IS LONG"


def test_missing_output_among_several_gets_failure_marker():
    # Tier-3: with multiple expected outputs, a missing one is flagged.
    # (Single-output with no markers is Tier-2 and returns the whole message.)
    text = "[OUTPUT: brief]\nthe brief\n[END OUTPUT: brief]"
    parsed = parse_outputs(text, ["brief", "report"])
    assert parsed["brief"] == "the brief"
    assert parsed["report"].startswith("[PARSE_FAILED")


def test_transcript_recovery_from_non_final_turn(tmp_path, monkeypatch):
    """Deliverable in an earlier turn, trailing checkpoint in the final turn."""
    session_id = "sess-1234"
    projects = tmp_path / "projects" / "-some-cwd"
    projects.mkdir(parents=True)
    transcript = projects / f"{session_id}.jsonl"

    def assistant(text):
        return json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
        )

    transcript.write_text(
        "\n".join(
            [
                assistant("Let me gather material first."),
                assistant("[OUTPUT: report]\n# Real Report\nThe body.\n[END OUTPUT: report]"),
                assistant("<checkpoint>done</checkpoint>\nReport placed in the [OUTPUT: report] block."),
            ]
        )
    )

    # Point _claude_config_dir at our fake config root.
    monkeypatch.setattr(
        "long_exposure.exploration._claude_config_dir", lambda: tmp_path
    )

    full = _session_transcript_text(session_id)
    assert "# Real Report" in full
    recovered = parse_outputs(full, ["report"])
    assert recovered["report"] == "# Real Report\nThe body."


def test_transcript_recovery_missing_session_is_safe():
    assert _session_transcript_text(None) == ""
    assert _session_transcript_text("does-not-exist-anywhere") == ""


# --- Current-turn scoping ------------------------------------------------------


def _assistant(text):
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    )


def _user_prompt(text):
    return json.dumps({"type": "user", "message": {"content": text}})


def _user_prompt_list(text):
    return json.dumps(
        {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}
    )


def _sidechain_user(text):
    return json.dumps(
        {"type": "user", "isSidechain": True, "message": {"content": text}}
    )


def _sidechain_assistant(text):
    return json.dumps(
        {
            "type": "assistant",
            "isSidechain": True,
            "message": {"content": [{"type": "text", "text": text}]},
        }
    )


def _tool_result():
    return json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
                ]
            },
        }
    )


def _write_transcript(tmp_path, session_id, lines):
    projects = tmp_path / "projects" / "-some-cwd"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / f"{session_id}.jsonl").write_text("\n".join(lines))


def test_prior_cycle_block_is_not_resurrected(tmp_path, monkeypatch):
    """Assistant text before the last genuine user prompt is a PRIOR cycle's
    output and must be excluded — longest-block-wins would otherwise hand a
    stale deliverable (or a stale [[BRANCH_COMPLETE]]) to the current cycle."""
    session_id = "sess-scope"
    _write_transcript(tmp_path, session_id, [
        _user_prompt("cycle 17 prompt"),
        _assistant("[OUTPUT: report]\nSTALE CYCLE-17 REPORT, VERY LONG BODY\n[END OUTPUT: report]"),
        _user_prompt("cycle 18 prompt"),
        _assistant("Working on it — no deliverable yet."),
    ])
    monkeypatch.setattr(
        "long_exposure.exploration._claude_config_dir", lambda: tmp_path
    )
    full = _session_transcript_text(session_id)
    assert "STALE CYCLE-17" not in full
    assert "Working on it" in full


def test_user_prompt_with_text_chunk_list_resets_turn(tmp_path, monkeypatch):
    session_id = "sess-list-prompt"
    _write_transcript(tmp_path, session_id, [
        _assistant("old turn text"),
        _user_prompt_list("new prompt as content list"),
        _assistant("current turn text"),
    ])
    monkeypatch.setattr(
        "long_exposure.exploration._claude_config_dir", lambda: tmp_path
    )
    full = _session_transcript_text(session_id)
    assert "old turn text" not in full
    assert "current turn text" in full


def test_tool_result_entries_do_not_reset_turn(tmp_path, monkeypatch):
    """tool_result-only "user" entries are tool returns mid-turn, not prompts;
    they must not discard the deliverable emitted before a tool call."""
    session_id = "sess-toolresult"
    _write_transcript(tmp_path, session_id, [
        _user_prompt("the prompt"),
        _assistant("[OUTPUT: report]\n# Real Report\nbody\n[END OUTPUT: report]"),
        _tool_result(),
        _assistant("<checkpoint>done</checkpoint>"),
    ])
    monkeypatch.setattr(
        "long_exposure.exploration._claude_config_dir", lambda: tmp_path
    )
    full = _session_transcript_text(session_id)
    assert "# Real Report" in full
    recovered = parse_outputs(full, ["report"])
    assert recovered["report"] == "# Real Report\nbody"


def test_sidechain_entries_are_skipped_entirely(tmp_path, monkeypatch):
    """Task-subagent (sidechain) entries interleave with the main thread.
    A sidechain's seed prompt is a plain-string "user" entry and must NOT be
    treated as a turn boundary (it would wipe the main turn's blocks), and
    sidechain assistant text must NOT be harvested as the agent's own — it
    could clobber the real deliverable or inject a subagent's
    [[BRANCH_COMPLETE]]."""
    session_id = "sess-sidechain"
    _write_transcript(tmp_path, session_id, [
        _user_prompt("current cycle prompt"),
        _assistant("[OUTPUT: report]\n# Real Report\nbody\n[END OUTPUT: report]"),
        # Sidechain interleaves mid-turn: seed prompt + subagent output.
        _sidechain_user("subagent seed prompt"),
        _sidechain_assistant(
            "[OUTPUT: report]\nSUBAGENT REPORT THAT IS MUCH LONGER THAN THE "
            "REAL ONE AND MUST NOT WIN LONGEST-BLOCK SELECTION\n"
            "[END OUTPUT: report]\n[[BRANCH_COMPLETE]]"
        ),
        _assistant("<checkpoint>done</checkpoint>"),
    ])
    monkeypatch.setattr(
        "long_exposure.exploration._claude_config_dir", lambda: tmp_path
    )
    full = _session_transcript_text(session_id)
    # Sidechain text is absent entirely (neither harvested nor a boundary).
    assert "SUBAGENT REPORT" not in full
    assert "BRANCH_COMPLETE" not in full
    # The main turn's deliverable survived the false boundary.
    assert "# Real Report" in full
    recovered = parse_outputs(full, ["report"])
    assert recovered["report"] == "# Real Report\nbody"


def test_sidechain_prompt_does_not_reset_turn_before_late_deliverable(
    tmp_path, monkeypatch
):
    """Deliverable emitted AFTER a sidechain ran: the sidechain's plain-string
    user entry must not make the deliverable look like a fresh turn's only
    text while discarding earlier main-thread context."""
    session_id = "sess-sidechain-late"
    _write_transcript(tmp_path, session_id, [
        _user_prompt("current cycle prompt"),
        _assistant("Main thread: delegating research to a subagent."),
        _sidechain_user("subagent seed prompt"),
        _sidechain_assistant("subagent findings text"),
        _assistant("[OUTPUT: report]\n# Late Report\nbody\n[END OUTPUT: report]"),
    ])
    monkeypatch.setattr(
        "long_exposure.exploration._claude_config_dir", lambda: tmp_path
    )
    full = _session_transcript_text(session_id)
    assert "subagent findings" not in full
    assert "Main thread: delegating" in full
    assert parse_outputs(full, ["report"])["report"] == "# Late Report\nbody"


# --- Per-name recovery at the _call_exploration_agent call site ----------------


def _run_agent_with_transcript(tmp_path, monkeypatch, *, result_text, transcript_lines,
                               outputs):
    """Drive _call_exploration_agent (claude headless path, resume) with a
    patched CLI returning `result_text` and a fake transcript on disk."""
    from long_exposure.exploration import _call_exploration_agent
    from long_exposure.orchestrator import load_config

    session_id = "sess-recovery"
    _write_transcript(tmp_path, session_id, transcript_lines)
    monkeypatch.setattr(
        "long_exposure.exploration._claude_config_dir", lambda: tmp_path
    )

    def fake_invoke(cmd, stdin_text, **kwargs):
        return {
            "result": result_text,
            "usage": {"input_tokens": 10, "output_tokens": 10},
            "duration_ms": 1,
        }

    with tempfile.TemporaryDirectory() as td:
        config = load_config()
        config.update({"llm_provider": "claude", "working_directory": td})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LONG_EXPOSURE_LLM_PROVIDER", None)
            with patch("long_exposure.exploration._invoke_claude", fake_invoke):
                result = _call_exploration_agent(
                    agent_name="tester",
                    agent_def={
                        "role": "You are a tester.",
                        "inputs": ["directive"],
                        "outputs": list(outputs),
                    },
                    task="test task",
                    config=config,
                    results={"directive": "test task"},
                    score_inputs={"directive": "test task"},
                    agent_sessions={"tester": session_id},
                    agent_summaries={},
                )
    return result


def test_recovery_fills_only_failed_names_never_fresh_blocks(tmp_path, monkeypatch):
    # `brief` parses fresh from the envelope; `report` is missing there. The
    # transcript holds a LONGER stale brief plus the real report. Recovery
    # must fill `report` and leave the fresh `brief` untouched.
    result = _run_agent_with_transcript(
        tmp_path, monkeypatch,
        result_text="[OUTPUT: brief]\nFRESH BRIEF\n[END OUTPUT: brief]",
        transcript_lines=[
            _user_prompt("this cycle's prompt"),
            _assistant(
                "[OUTPUT: brief]\nSTALE BUT MUCH LONGER BRIEF FROM EARLIER IN TURN\n"
                "[END OUTPUT: brief]\n"
                "[OUTPUT: report]\nREAL REPORT BODY\n[END OUTPUT: report]"
            ),
        ],
        outputs=["brief", "report"],
    )
    assert result["outputs"]["brief"] == "FRESH BRIEF"
    assert result["outputs"]["report"] == "REAL REPORT BODY"


def test_recovery_ignores_prior_cycle_blocks(tmp_path, monkeypatch):
    # The only [OUTPUT: report] block in the transcript belongs to a PRIOR
    # turn. The current turn has no markers, so the Tier-2 whole-message
    # fallback must stand — the stale block must NOT be resurrected.
    result = _run_agent_with_transcript(
        tmp_path, monkeypatch,
        result_text="Checkpoint only — still researching.",
        transcript_lines=[
            _user_prompt("old cycle prompt"),
            _assistant("[OUTPUT: report]\nSTALE DELIVERABLE\n[END OUTPUT: report]"),
            _user_prompt("current cycle prompt"),
            _assistant("Checkpoint only — still researching."),
        ],
        outputs=["report"],
    )
    assert "STALE DELIVERABLE" not in result["outputs"]["report"]
    assert "Checkpoint only" in result["outputs"]["report"]


def test_recovery_still_rescues_cover_note_final_message(tmp_path, monkeypatch):
    # Original recovery scenario: deliverable emitted earlier in the SAME
    # turn, final message is a cover note. Tier-2 would capture the cover
    # note; transcript recovery must replace it with the real block.
    result = _run_agent_with_transcript(
        tmp_path, monkeypatch,
        result_text="Report placed in the output block above.",
        transcript_lines=[
            _user_prompt("current cycle prompt"),
            _assistant("[OUTPUT: report]\n# Real Report\nThe body.\n[END OUTPUT: report]"),
            _assistant("Report placed in the output block above."),
        ],
        outputs=["report"],
    )
    assert result["outputs"]["report"] == "# Real Report\nThe body."
