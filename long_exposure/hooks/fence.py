"""PreToolUse: make the directory boundary a real fence, not prose.

## Why this exists

The operating-protocol template tells every agent which paths are off
limits — provider credentials, SSH and GPG keys, `~/.env`, shell and git
config, `~/.config/`, and the harness's own source tree. It then admits, in
the same breath, that the rule is advisory:

> File tools (Read, Write, Edit, Glob, Grep) are restricted to this path.
> Bash is NOT path-restricted by this setting — see operating protocol for
> soft directory boundaries enforced via instructions.

The harness root is the sharpest edge of that. It is on every agent turn's
`PYTHONPATH` — that is how `python3 -m long_exposure.tools...` resolves — so
it is both readable and writable from Bash, and the only thing stopping an
agent editing the harness mid-run is a paragraph asking it not to. These runs
go for hours, unattended, with tool permissions pre-granted.

This hook turns that paragraph into a denial.

## Threat model — read this before trusting it

**This stops mistakes, not adversaries.** It matches off-limits paths against
the literal text of the command, so any of the following would get through:
a path assembled from shell variables, `$HOME` expansion the hook does not
resolve, base64 or hex encoding, a path reached by `cd` and then a relative
reference, or a helper script written first and executed second.

That is the correct trade for the actual risk. The failure this prevents is
an agent that reasoned its way to `rm -rf` on the wrong tree, or decided the
harness had a bug worth patching mid-run. It is not a sandbox, and the docs
must not describe it as one. Real isolation is a container boundary, which
is a deployment decision rather than a hook.

## What it checks

1. **Off-limits paths**, expanded from the same list the prompt names.
   Matched as substrings of the command, reads included — deliberately
   conservative, because a read of `~/.ssh/id_ed25519` is as bad as a write.
2. **`git` history rewriting** (`commit`, `push`, `reset`, `rebase`,
   `checkout -B`, ...), when the operator has asked for harness-authored
   commits. In a federated run the harness owns history at cycle boundaries
   (see `docs/worktrees-hooks-swarms-plan.md` §4.2); an agent committing
   mid-cycle puts run provenance under model control.

Anything else is allowed: the hook returns no opinion, and the vendor's
normal flow applies.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from long_exposure.hooks import _io

# The paths the operating-protocol template names, in the same order. The
# template is the operator-facing statement of this list; a test asserts the
# two have not drifted apart.
HOME_RELATIVE_DENY = (
    ".claude",
    ".claude.json",
    ".codex",
    ".gemini",
    ".ssh",
    ".gnupg",
    ".env",
    ".bashrc",
    ".bash_profile",
    ".gitconfig",
    ".config",
)

# Env vars that override the derived values, so the installer does not have
# to bake absolute paths into a config file that outlives a checkout.
ENV_HARNESS_ROOT = "LONG_EXPOSURE_HARNESS_ROOT"
ENV_GIT_AUTHORITY = "LONG_EXPOSURE_GIT_HARNESS_ONLY"
ENV_DISABLE = "LONG_EXPOSURE_FENCE_OFF"
# "turn"   — enforce only inside a long-exposure agent turn (the default).
# "always" — enforce on every invocation of the CLI.
#
# `turn` is the default because `always` would deny the operator's OWN
# sessions any access to the harness source tree, which is where they do
# harness development. The threat this fence addresses is an unattended
# agent with pre-granted tool permissions, and that is exactly what `turn`
# covers.
ENV_SCOPE = "LONG_EXPOSURE_FENCE_SCOPE"

# Self-test marker. `hooks_install.verify()` sends a payload containing this
# and requires a denial, which is how the harness proves a configured fence
# is actually wired up before it starts a run (fail-closed).
SELFTEST_MARKER = "__LONG_EXPOSURE_FENCE_SELFTEST__"

# git subcommands that write history. `add`, `status`, `diff`, `log` and the
# other read/stage operations are deliberately absent: an agent staging its
# own work is fine, it is authoring commits that is not.
GIT_WRITE_SUBCOMMANDS = (
    "commit", "push", "reset", "rebase", "merge", "cherry-pick",
    "revert", "tag", "am", "apply", "filter-branch", "gc", "prune",
)
_GIT_RE = re.compile(r"\bgit\b", re.IGNORECASE)
_GIT_BRANCH_FORCE_RE = re.compile(r"\bcheckout\s+-B\b|\bswitch\s+-C\b", re.IGNORECASE)

# Tools whose input carries a shell command. Claude and Codex both name the
# shell tool "Bash"; Codex also exposes `apply_patch`, and Gemini calls its
# shell tool "run_shell_command".
COMMAND_KEYS = ("command", "cmd", "script", "shell_command")


def harness_root() -> str:
    """The harness source tree, as an absolute path.

    Derived from this file's own location so it follows a moved checkout,
    with an env override for the case where the hook config was generated
    against a different install than the one running.
    """
    override = os.environ.get(ENV_HARNESS_ROOT, "").strip()
    if override:
        return str(Path(override).expanduser())
    # long_exposure/hooks/fence.py -> long_exposure/hooks -> long_exposure -> root
    return str(Path(__file__).resolve().parent.parent.parent)


def denied_paths() -> list[str]:
    """Every off-limits path, absolute, home expanded."""
    home = Path.home()
    out = [str(home / name) for name in HOME_RELATIVE_DENY]
    out.append(harness_root())
    return out


def _command_text(payload: dict) -> str:
    ti = _io.tool_input(payload)
    parts: list[str] = []
    for key in COMMAND_KEYS:
        value = ti.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(str(v) for v in value)
    # Some tools carry the path rather than a command (Write/Edit under a
    # vendor that routes them through this matcher).
    for key in ("file_path", "path", "target_file"):
        value = ti.get(key)
        if isinstance(value, str):
            parts.append(value)
    return "\n".join(parts)


def _git_write_attempt(command: str) -> str | None:
    """The git subcommand this command would use to write history, if any."""
    if not _GIT_RE.search(command):
        return None
    lowered = command.lower()
    for sub in GIT_WRITE_SUBCOMMANDS:
        # Word-boundary so `git log --format=commit` is not a commit.
        if re.search(rf"\bgit\b[^|;&\n]*\b{re.escape(sub)}\b", lowered):
            return sub
    if _GIT_BRANCH_FORCE_RE.search(lowered):
        return "checkout -B"
    return None


def check(payload: dict) -> tuple[bool, str]:
    """Decide on one tool call. Returns (deny, reason).

    Pure and total: no I/O, no exceptions, so it can be unit-tested directly
    and so a surprising payload can never turn into a crash that the vendor
    reports as a hook error on a legitimate command.
    """
    command = _command_text(payload)
    if not command:
        return False, ""

    if SELFTEST_MARKER in command:
        return True, (
            "long-exposure fence self-test: the fence is installed and "
            "responding."
        )

    for path in denied_paths():
        if path and path in command:
            return True, (
                f"Blocked by the long-exposure path fence: {path} is "
                "off limits. This is the same boundary the operating "
                "protocol states; the hook enforces it. If you believe you "
                "need something under that path, record the limitation in "
                "your output and proceed without it."
            )

    if os.environ.get(ENV_GIT_AUTHORITY, "").strip().lower() in ("1", "true", "yes", "on"):
        sub = _git_write_attempt(command)
        if sub:
            return True, (
                f"Blocked by the long-exposure path fence: `git {sub}` "
                "writes history, and this run is configured so the harness "
                "authors commits at cycle boundaries. Write your work to "
                "the workspace and it will be committed for you. Staging "
                "and read-only git commands are allowed."
            )

    return False, ""


def scope() -> str:
    raw = os.environ.get(ENV_SCOPE, "").strip().lower()
    return raw if raw in ("turn", "always") else "turn"


def main(argv: list[str] | None = None) -> int:
    if os.environ.get(ENV_DISABLE, "").strip().lower() in ("1", "true", "yes", "on"):
        return _io.allow()
    payload = _io.read_payload()
    # The self-test must answer regardless of scope: `hooks_install.verify`
    # runs it from the harness process, not from an agent turn, and its
    # whole job is to prove the fence responds.
    selftest = SELFTEST_MARKER in _command_text(payload)
    if not selftest and scope() == "turn" and not _io.harness_turn():
        return _io.allow()
    deny, reason = check(payload)
    if deny:
        return _io.deny(_io.event_name(payload, "PreToolUse"), reason)
    return _io.allow()


if __name__ == "__main__":
    sys.exit(main())
