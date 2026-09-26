"""Operator identity and slice naming for multi-operator runs.

## Why this exists

`docs/git-federation.md` §7.1 recorded a measured defect. Two operators
running against one repository share `promise_ledger.jsonl`, and the ledger
had no way to tell their events apart:

- `run_id` is `workspace_bootstrap.derive_run_id()` — a bare UTC timestamp to
  the second, with no host component and no randomness. Two operators who
  start in the same second get the *same* `run_id`.
- `milestone_id` is a shared namespace, and `summarize_ledger` keeps the
  latest event per `milestone_id`. So when two operators reached the same
  milestone with confident, settled results, the earlier one was **dropped
  from the summary** — including a contradicting result. Measured: two
  `validated`/`high` events on `spectral/bound`, and "ALICE bound is 3.2"
  was simply absent, with nothing flagged.

That is the worst available outcome. The two operators disagree, and the
mechanism meant to carry the disagreement silently keeps one side.

## The fix, and why it is a field rather than a prefix

§7.1 originally proposed namespacing `milestone_id` itself
(`<operator>/<milestone>`). That is the wrong shape, for two concrete
reasons found while implementing it:

1. `promise_check.RESERVED_NAMESPACES` matches milestone PREFIXES (`_run/`,
   `_plan/`, `_archive/`, ...). `alice/_run/start` defeats every one of them.
2. Agents write `milestone_id` themselves, via the `ledger_append` tool. They
   have no idea what operator they are running under, and telling them would
   put identity under prompt adherence.

So `operator` is a separate field, stamped by the harness inside
`append_ledger_event` — the single chokepoint every writer already goes
through — and readers group by `(operator, milestone_id)`.

## Backward compatibility

A missing `operator` reads as the LOCAL operator, not as a distinct empty
one. Without that rule, resuming a pre-upgrade ledger would split each
milestone into a legacy group and a new group and report two operators where
there is one. Attributing old events to the machine reading them is both
correct and behaviour-neutral: a single-operator ledger groups exactly as it
did before, and `summarize_ledger` renders no operator column at all unless
two or more are genuinely present.
"""

from __future__ import annotations

import os
import re
import socket

DEFAULTS: dict = {
    "operator": "",
    "conflict_radar": {
        "enabled": False,
        "remote": "origin",
        "shared_branch": "main",
        "fetch": True,
        "timeout_seconds": 30,
        "max_paths": 20,
    },
}

# An operator name ends up in a branch name, a ledger field and a prompt
# block, so it has to be safe in all three. Same spelling rules as a slice.
_UNSAFE = re.compile(r"[^a-z0-9._-]+")
_DASHES = re.compile(r"-{2,}")

ENV_OPERATOR = "LONG_EXPOSURE_OPERATOR"

MAX_NAME = 40

FALLBACK_OPERATOR = "local"


def settings(config: dict | None = None) -> dict:
    """The `federation:` block, merged over DEFAULTS. Never returns None."""
    block = (config or {}).get("federation")
    if not isinstance(block, dict):
        block = {}
    merged = {k: v for k, v in DEFAULTS.items() if k != "conflict_radar"}
    merged.update({k: v for k, v in block.items() if k != "conflict_radar"})
    radar = dict(DEFAULTS["conflict_radar"])
    incoming = block.get("conflict_radar")
    if isinstance(incoming, dict):
        radar.update(incoming)
    merged["conflict_radar"] = radar
    return merged


def slugify(text: str, *, max_len: int = MAX_NAME) -> str:
    """Lowercase, collapse anything unsafe to `-`, trim. Never raises.

    Returns "" for input that has no usable characters, so callers can tell
    "nothing given" from "something unusable" and fall back deliberately.
    """
    lowered = str(text or "").strip().lower()
    cleaned = _UNSAFE.sub("-", lowered)
    cleaned = _DASHES.sub("-", cleaned).strip("-._")
    return cleaned[:max_len].strip("-._")


def operator_name(config: dict | None = None) -> str:
    """This machine's operator name.

    Order: the `LONG_EXPOSURE_OPERATOR` env var, then `federation.operator`
    in config, then the slugified hostname, then `local`.

    The hostname default is deliberate. The whole point of the field is to
    distinguish two machines, and a default that requires configuration would
    leave the common case — two operators who each just installed the harness
    — with no identity at all. Deriving it means federation works before
    anyone edits a config file.
    """
    for candidate in (
        os.environ.get(ENV_OPERATOR, ""),
        (settings(config) or {}).get("operator", ""),
    ):
        name = slugify(candidate)
        if name:
            return name
    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    # A FQDN's first label is the machine; the domain is noise in a branch name.
    return slugify(host.split(".")[0]) or FALLBACK_OPERATOR


def event_operator(event: dict, local: str) -> str:
    """The operator an event belongs to, for grouping and display.

    A missing or blank `operator` reads as `local` — see the module docstring
    on backward compatibility. `local` is passed in rather than resolved here
    so readers resolve it once per call instead of once per event.
    """
    if not isinstance(event, dict):
        return local
    return slugify(event.get("operator") or "") or local


def stamp(event: dict, config: dict | None = None) -> dict:
    """Add `operator` to an event if it does not carry one. Mutates and returns.

    Deliberately does not overwrite an existing value: a clone's shadow ledger
    is concatenated into the workspace ledger by the fan-out conductor, and a
    re-stamp there would relabel another process's events.
    """
    if not isinstance(event, dict):
        return event
    if not slugify(event.get("operator") or ""):
        event["operator"] = operator_name(config)
    return event


# ---------------------------------------------------------------------------
# Slice naming
# ---------------------------------------------------------------------------

def canonical_slice(text: str) -> str:
    """Canonical name for a claimable slice of work.

    `git-federation.md` §6.2 measured the limit of git-as-lock: two operators
    claiming `spectral-theory.json` collide and git tells them, while
    `spectral-theory.json` versus `spectral_theory.json` is accepted with two
    live claims on one topic and no collision at all. The lock is on the
    PATH, so the names have to agree before git can help.

    This closes the near-miss half of that: separators, case and pluralisation
    noise all fold to one spelling. It does NOT close the synonym half —
    "spectral-theory" and "eigenvalue-bounds" stay distinct, and no string
    function will fix that. §6.2 says so; this does not pretend otherwise.

    Used today to group conflict-radar paths into readable regions. The claims
    registry that will also need it is not built.
    """
    slug = slugify(str(text or "").replace("_", "-"))
    if not slug:
        return ""
    parts = [p for p in slug.split("-") if p]
    return "-".join(_singular(p) for p in parts)


# Endings that look plural but are not. Without these, a first attempt at
# this folded `bias` to `bia` — precisely the over-stemming the docstring
# below warns about, caught by its own test.
_FALSE_PLURAL_ENDINGS = ("ss", "is", "us", "as", "os")

# Irregular plurals common enough in research vocabulary to be worth naming.
# A rule cannot get these: `analyses` -> `analysis` and `bases` -> `base`
# share an ending and differ in the answer, so a `-ses` rule guesses wrong
# half the time. An explicit map is shorter than the rule it replaces and it
# is auditable.
_IRREGULAR = {
    "analyses": "analysis",
    "hypotheses": "hypothesis",
    "theses": "thesis",
    "bases": "basis",
    "matrices": "matrix",
    "indices": "index",
    "vertices": "vertex",
    "criteria": "criterion",
    "phenomena": "phenomenon",
}


def _singular(word: str) -> str:
    """Crude depluralisation: enough to fold `reports` and `report`.

    Deliberately not a stemmer. A slice name is a handful of words chosen by
    a person or a model, and the failure mode of over-stemming — two distinct
    slices folding together, so one operator's claim silently matches
    another's and neither is told — is worse than the failure mode of
    under-stemming, which is the near-miss surviving exactly as it does today.

    So the rule is conservative: an explicit map for the irregulars, then
    `-ies` and a plain trailing `-s`, and the Latin and Greek singulars that
    end in an `s` (`bias`, `analysis`, `corpus`, `chaos`, `loss`) are left
    exactly as they are.
    """
    if word in _IRREGULAR:
        return _IRREGULAR[word]
    if len(word) <= 3:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(_FALSE_PLURAL_ENDINGS):
        return word
    if word.endswith("s"):
        return word[:-1]
    return word


def slice_for_path(rel_path: str) -> str:
    """The slice a workspace-relative path belongs to.

    Directory-shaped: `data/spectral/bound.py` → `data-spectral`. A file at
    the workspace root is its own slice. Used to turn a list of conflicting
    paths into a short list of regions.
    """
    from long_exposure.paths import canonical_rel_path

    clean = canonical_rel_path(str(rel_path or ""))
    if not clean:
        return ""
    parts = [p for p in clean.split("/") if p and p != "."]
    if not parts:
        return ""
    if len(parts) == 1:
        return canonical_slice(parts[0].rsplit(".", 1)[0])
    return canonical_slice("-".join(parts[:-1]))
