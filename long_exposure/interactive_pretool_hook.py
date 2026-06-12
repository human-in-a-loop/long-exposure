#!/usr/bin/env python3
"""PreToolUse hook for the interactive transport in ``scoped`` permission mode.

In ``scoped`` mode the driver session is launched without
``--dangerously-skip-permissions``; a stray tool call to something not on the
allowlist would otherwise pop an interactive approval prompt and wedge the
unattended session. This hook converts that into a model-visible deny+feedback,
so the driver self-corrects instead of hanging.

Allowlist comes from ``LONG_EXPOSURE_INTERACTIVE_ALLOW`` (comma-separated tool
names). Unset ⇒ allow everything (no-op; used in ``skip`` mode where this hook
is not installed anyway). Best-effort: any error allows the call.
"""
import json
import os
import sys


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return  # allow
    name = data.get("tool_name", "")
    allow_env = os.environ.get("LONG_EXPOSURE_INTERACTIVE_ALLOW", "")
    if not allow_env or not name:
        return  # allow
    allowed = {a.strip() for a in allow_env.split(",") if a.strip()}
    if name in allowed:
        return  # allow
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"'{name}' is not available in this session. Use only: "
                f"{', '.join(sorted(allowed))}."
            ),
        }
    }))


if __name__ == "__main__":
    main()
