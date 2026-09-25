"""Vendor-neutral lifecycle hooks for long-exposure.

Claude Code and Codex are wire-compatible for the eleven lifecycle events
they share: the same stdin JSON (`session_id`, `cwd`, `hook_event_name`,
plus event fields such as `tool_name` / `tool_input`), the same
`hookSpecificOutput` / `additionalContext` / `permissionDecision` output
convention, the same "exit 2 blocks, with the reason on stderr" fallback,
and the same `{matcher, hooks:[{type:"command", command, timeout}]}` config
shape — which Codex accepts as either `hooks.json` or a `[[hooks.X]]` TOML
table.

So these are not vendor adapters. Each module here is **one script that both
CLIs run unmodified**; only the config file that points at it differs, and
`long_exposure.hooks_install` writes those.

Gemini CLI has hooks too but renames the events (`BeforeTool` / `AfterTool`
/ `BeforeAgent`) and lacks `Stop`, `UserPromptSubmit`, `PermissionRequest`
and the compaction pair. Where a Gemini equivalent exists the installer maps
the name; where it does not, the hook is simply not installed and the
harness behaves as it does today.

## The rule these obey

A hook may **harden** a rule the prompt already states. It must never be the
only place a rule exists.

Gemini lacks four of the events, Claude can be invoked with `--bare` (which
skips hooks), and Codex requires operator review of non-managed hooks. Any
behaviour living only in a hook would silently vanish on the vendor or
invocation that lacks it. So every hook here duplicates guidance that is
already in the system prompt, and the run still works — less safely, or less
observably — without it.
"""
