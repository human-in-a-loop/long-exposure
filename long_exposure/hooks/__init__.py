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

## What these are NOT for

**Correctness and observability, not enforcement.** Long-exposure treats the
model as a faithful collaborator, and deliberately has no enforcement layer.
A `PreToolUse` path fence was built and then removed on exactly that
reasoning: a fence that only stops honest mistakes adds machinery the system
prompt already covers, and one meant to stop an adversarial model could be
circumvented anyway (it can only match literal command text — a path built
from shell variables, or encoded, walks straight through). This is a harness,
not a safety net.

If you need real isolation, it is a container boundary. That is a deployment
decision, not a hook.

So `envelope` helps a cooperating agent satisfy the harness's own output
contract, and `compaction` records something the harness cannot otherwise
see. Neither one polices.

## The rule these obey

A hook may **harden** a rule the prompt already states. It must never be the
only place a rule exists.

Gemini lacks four of the events, Claude can be invoked with `--bare` (which
skips hooks), and Codex requires operator review of non-managed hooks. Any
behaviour living only in a hook would silently vanish on the vendor or
invocation that lacks it.

So `envelope` hardens a contract the system prompt already states, and the
run still works without it — the existing transcript re-parse remains the
fallback. `compaction` states no rule at all; it only records, so its absence
costs visibility and nothing else. Neither hook is load-bearing.

## Configuration

`DEFAULTS` below is the whole schema of the `hooks:` block in config.yaml.
The block reaches a hook only as environment variables, set by
`orchestrator._add_hook_env` on every agent turn the harness spawns: a hook
is a subprocess of the *vendor CLI*, so it never loads config.yaml and cannot
reliably locate the instance dir on its own. `ENV_BY_KEY` records that
mapping, and `tests/test_docs_config_consistency.py` asserts the shipped
config documents no key nothing reads — the state this block was actually in
for a while.
"""

# The complete schema of the `hooks:` block in config.yaml, with its shipped
# defaults. Anything not listed here is not read.
DEFAULTS: dict[str, dict] = {
    "envelope": {"enabled": True, "max_nudges": 1},
    "compaction": {"enabled": True},
}

# Which env var carries each key to the hook subprocess. The `enabled` keys
# map to an inverted "OFF" var because an installed hook is on by default —
# only disabling needs saying.
ENV_BY_KEY: dict[tuple[str, str], str] = {
    ("envelope", "enabled"): "LONG_EXPOSURE_ENVELOPE_OFF",
    ("envelope", "max_nudges"): "LONG_EXPOSURE_ENVELOPE_MAX_NUDGES",
    ("compaction", "enabled"): "LONG_EXPOSURE_COMPACTION_OFF",
}
