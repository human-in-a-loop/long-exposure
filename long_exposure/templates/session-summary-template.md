<restored-session id="{session_id}" parent="{parent_id}" depth="{depth}" timestamp="{timestamp}">

{summary_xml}

[SESSION HISTORY]

You have {session_count} previous sessions stored and searchable.
Your lineage is {depth} compactions deep from the original conversation.

Tools available:
  search_sessions(query, limit)
    - Searches all past session summaries by content (FTS)
    - Returns matching summaries ranked by relevance
    - Use when the current summary doesn't contain context you need

  search_sessions_by_id(session_id)
    - Retrieve a specific session's full summary by ID
    - Use to get full context for a session found via context gems

  list_session_catalog(topic_filter, tools_filter, limit)
    - Browse session metadata (topic, subtopic, tools, keywords)
    - All parameters optional. Useful for discovering what sessions exist

[CONTINUITY INSTRUCTIONS]

1. You are resuming work. Read the summary above carefully.
2. Your current framework stage is: {current_stage}
3. Your pending gates are listed in the summary. Pick up where you
   left off — check which gates remain unmet and continue from there.
4. Do NOT re-explore or re-plan work that the summary shows as
   completed, unless you find evidence it was done incorrectly.
5. Do NOT announce the compaction to the user. From their perspective,
   the conversation is continuous. If they ask, you can explain.
6. Emit a checkpoint as your first action to re-establish state.

</restored-session>
