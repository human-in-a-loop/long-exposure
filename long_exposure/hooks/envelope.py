"""Stop: make sure the turn actually emitted its [OUTPUT: x] block.

## Why this exists

`docs/gaps.md` records a High-severity incident: a periodic report captured
*"the reporter's trailing cover-note instead of the report body, producing a
~3 KB stub"* rather than the 29 KB report. The cause was structural, not a
one-off. The harness reads `envelope["result"]`, which is only the agent's
**final** assistant message; the reporter emitted the deliverable in block 8
of 10 and then trailed a checkpoint, so the deliverable was never in
`result`. And because `require_checkpoint_first: false` lets agents trail
content after a deliverable, the shape recurs.

That was fixed by re-parsing the full session transcript when `result` lacks
a complete block — a *recovery*, after the fact, and one that only works on
the Claude transcript shape (Codex and Gemini transcripts differ, so for
them the recovery is a no-op that returns "").

This hook is the prevention. On `Stop` it checks the final message for a
well-formed `[OUTPUT: name] ... [END OUTPUT: name]` and, if it is missing,
returns `{"decision": "block"}` — which on **both** Claude and Codex means
*continue the turn* rather than *stop it* — with a reason telling the agent
to emit the block properly. The agent gets another turn to do the one thing
the harness needs from it.

## Bounded by design

`max_nudges` (default 1) caps how many times a single turn can be pushed
back. Without a cap a stubborn agent and a strict hook loop forever, burning
a turn each iteration; with it, a turn that still has no block after one
nudge falls through to the existing transcript-recovery path, which is
exactly today's behaviour. The nudge count is kept in a per-session file
under the instance dir rather than in memory, because each hook invocation
is a fresh subprocess.

Only active inside a long-exposure agent turn, keyed on the
`LONG_EXPOSURE_HOOK_ACTIVE` env var the harness sets when it spawns one. An
operator can therefore install this in their home config without it taxing
the interactive sessions they take for unrelated work.

Not available on Gemini CLI, which has no `Stop` event. There the harness
behaves as it does now.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import long_exposure.hooks as _pkg
from long_exposure.hooks import _io

ENV_EXPECTED = "LONG_EXPOSURE_EXPECTED_OUTPUT"   # e.g. "research_brief"
ENV_STATE_DIR = "LONG_EXPOSURE_HOOK_STATE_DIR"
# Taken from the package's ENV_BY_KEY rather than re-spelt, so a rename
# cannot leave the harness setting one name and the hook reading another.
ENV_MAX_NUDGES = _pkg.ENV_BY_KEY[("envelope", "max_nudges")]
ENV_DISABLE = _pkg.ENV_BY_KEY[("envelope", "enabled")]

DEFAULT_MAX_NUDGES = 1

# Mirrors conductor._OUTPUT_RE: the close tag may be bare or named, which is
# the generalisation the 2026-05-28 incident forced.
def _block_re(name: str) -> re.Pattern[str]:
    n = re.escape(name)
    return re.compile(
        rf"\[OUTPUT:\s*{n}\s*\](.*?)\[END OUTPUT(?::\s*{n}\s*)?\]",
        re.DOTALL | re.IGNORECASE,
    )


_ANY_BLOCK_RE = re.compile(
    r"\[OUTPUT:\s*([A-Za-z_][A-Za-z0-9_]*)\s*\](.*?)\[END OUTPUT(?::[^\]]*)?\]",
    re.DOTALL | re.IGNORECASE,
)


def _max_nudges() -> int:
    try:
        return max(0, int(os.environ.get(ENV_MAX_NUDGES, DEFAULT_MAX_NUDGES)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_NUDGES


def _nudge_path(session_id: str) -> Path | None:
    base = os.environ.get(ENV_STATE_DIR, "").strip()
    if not base or not session_id:
        return None
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:96]
    return Path(base) / f"envelope_nudges_{safe}.json"


def _nudges_used(session_id: str) -> int:
    path = _nudge_path(session_id)
    if path is None:
        return 0
    try:
        return int((json.loads(path.read_text()) or {}).get("nudges", 0))
    except (OSError, ValueError, TypeError):
        return 0


def _record_nudge(session_id: str) -> None:
    path = _nudge_path(session_id)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"nudges": _nudges_used(session_id) + 1}))
    except OSError:
        pass


def has_block(text: str, expected: str = "") -> bool:
    """True if `text` carries a complete output block.

    With `expected` set, the named block must be present and non-empty —
    an empty block is the failure mode the incident produced, so a bare
    `[OUTPUT: x][END OUTPUT]` does not count.
    """
    if not text:
        return False
    if expected:
        m = _block_re(expected).search(text)
        return bool(m and m.group(1).strip())
    m = _ANY_BLOCK_RE.search(text)
    return bool(m and m.group(2).strip())


def check(payload: dict, *, expected: str = "") -> tuple[bool, str]:
    """Returns (nudge, reason). Pure; no I/O, no exceptions."""
    text = str(payload.get("last_assistant_message") or "")
    if not text.strip():
        # Nothing to judge — an empty final message is a provider-level
        # problem the harness's own failure handling already covers.
        return False, ""
    if has_block(text, expected):
        return False, ""
    want = expected or "your role's declared output"
    return True, (
        f"Your turn is ending without a complete [OUTPUT: {want}] block. "
        "The harness reads only your FINAL message, so a deliverable "
        "emitted earlier in the turn — or followed by a checkpoint or a "
        "cover note — is not captured. Re-emit it now as the last thing in "
        f"your response: [OUTPUT: {want}] ... [END OUTPUT: {want}]."
    )


def main(argv: list[str] | None = None) -> int:
    if os.environ.get(ENV_DISABLE, "").strip().lower() in ("1", "true", "yes", "on"):
        return _io.allow()
    # Outside a long-exposure agent turn there is no declared output to
    # require, so demanding one would be nonsense. Found live: an ungated
    # version nudged a bare `claude -p` turn into inventing a generic
    # [OUTPUT: result] label because the hook insisted on a block the turn
    # was never asked for.
    if not _io.harness_turn():
        return _io.allow()
    payload = _io.read_payload()
    expected = os.environ.get(ENV_EXPECTED, "").strip()
    nudge, reason = check(payload, expected=expected)
    if not nudge:
        return _io.allow()
    session_id = str(payload.get("session_id") or "")
    if _nudges_used(session_id) >= _max_nudges():
        # Cap reached: let the turn end and fall through to the existing
        # transcript-recovery path rather than looping.
        return _io.allow()
    _record_nudge(session_id)
    return _io.continue_turn(reason)


if __name__ == "__main__":
    sys.exit(main())
