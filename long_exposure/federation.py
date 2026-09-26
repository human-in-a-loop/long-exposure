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
    # What each operator shares. The integrator projects these, from each
    # operator's own run branch, to `operators/<operator>/<path>` on the shared
    # branch; every other operator's git_sync mirrors them into its gitignored
    # `peers/<operator>/`. Paths are workspace-relative files or directories.
    # A project adds its deliverable directory here.
    "publish_paths": ["promise_ledger.jsonl", "MEMOIR.md", "plan_of_record.md"],
    # One remote and one shared branch for the whole feature family. They used
    # to live inside conflict_radar; with git_sync also needing them, two copies
    # would let the radar forecast against one branch while sync integrated
    # another.
    "remote": "origin",
    "shared_branch": "main",
    "conflict_radar": {
        "enabled": False,
        "fetch": True,
        "timeout_seconds": 30,
        "max_paths": 20,
    },
    "git_sync": {
        "enabled": False,
        "push": True,
        "integrate": True,
        "timeout_seconds": 60,
    },
}

_NESTED = ("conflict_radar", "git_sync")

# An operator name ends up in a branch name, a ledger field and a prompt
# block, so it has to be safe in all three. Same spelling rules as a slice.
_UNSAFE = re.compile(r"[^a-z0-9._-]+")
_DASHES = re.compile(r"-{2,}")

ENV_OPERATOR = "LONG_EXPOSURE_OPERATOR"

MAX_NAME = 40

FALLBACK_OPERATOR = "local"


def settings(config: dict | None = None) -> dict:
    """The `federation:` block, merged over DEFAULTS. Never returns None.

    Nested blocks merge key-by-key, so a partial `conflict_radar:` or
    `git_sync:` keeps the defaults it does not mention. Never mutates DEFAULTS.
    """
    block = (config or {}).get("federation")
    if not isinstance(block, dict):
        block = {}
    merged = {k: v for k, v in DEFAULTS.items() if k not in _NESTED}
    merged.update({k: v for k, v in block.items() if k not in _NESTED})
    for name in _NESTED:
        section = dict(DEFAULTS[name])
        incoming = block.get(name)
        if isinstance(incoming, dict):
            section.update(incoming)
        merged[name] = section
    return merged


def remote_and_branch(config: dict | None = None) -> tuple[str, str]:
    """The shared remote and branch, blank-safe.

    Strip BEFORE defaulting: `"   "` is truthy, so `x or "main"` keeps it and
    `.strip()` then yields an empty ref name.
    """
    s = settings(config)
    remote = str(s.get("remote") or "").strip() or "origin"
    branch = str(s.get("shared_branch") or "").strip() or "main"
    return remote, branch


# Where each operator's published work lives on the shared branch, and where a
# workspace mirrors its PEERS' published work. Ownership is by path: nothing
# under operators/<x>/ is ever written by anyone but the integrator projecting
# operator x, so two operators can never conflict there.
OPERATORS_DIR = "operators"
PEERS_DIR = "peers"


def publish_paths(config: dict | None = None) -> list[str]:
    """The workspace-relative paths an operator shares, cleaned.

    Entries that would escape the workspace or reach into the projection or
    peer trees themselves are dropped: publishing `peers/` would re-share other
    operators' work under this operator's name.
    """
    from long_exposure.paths import canonical_rel_path

    out = []
    for raw in settings(config).get("publish_paths") or []:
        rel = canonical_rel_path(str(raw or ""))
        parts = rel.split("/")
        if (not rel or ".." in parts or parts[0] in (OPERATORS_DIR, PEERS_DIR, ".git")):
            continue
        if rel not in out:
            out.append(rel)
    return out


def slugify(text: str, *, max_len: int = MAX_NAME) -> str:
    """Lowercase, collapse anything unsafe to `-`, trim. Never raises.

    Returns "" for input that has no usable characters, so callers can tell
    "nothing given" from "something unusable" and fall back deliberately.
    """
    lowered = str(text or "").strip().lower()
    cleaned = _UNSAFE.sub("-", lowered)
    cleaned = _DASHES.sub("-", cleaned).strip("-._")
    return cleaned[:max_len].strip("-._")


def _from_config(config: dict | None = None) -> str:
    """Resolve an operator name WITHOUT consulting the environment.

    Split out so `bind` can re-resolve. `operator_name` prefers the env var, so
    a re-bind that went through it would just read back the value it set last
    time and no config change would ever take effect.
    """
    name = slugify((settings(config) or {}).get("operator", ""))
    if name:
        return name
    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    # A FQDN's first label is the machine; the domain is noise in a branch name.
    return slugify(host.split(".")[0]) or FALLBACK_OPERATOR


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
    from_env = slugify(os.environ.get(ENV_OPERATOR, ""))
    return from_env or _from_config(config)


def bind(config: dict | None = None) -> str:
    """Resolve the operator once per run and put it in the process environment.

    Without this, `federation.operator` in config is silently ignored by every
    append the harness makes itself. `append_ledger_event` takes no config — it
    is called from the cycle loop, the manager, the bootstrap event and an
    agent-run tool, and threading config to all four would be a worse change —
    so it resolves through `operator_name(None)`, which sees only the
    environment and the hostname.

    The effect, found by a two-operator end-to-end rig rather than by reading:
    with `operator: alice` in config on a host named `vm`, the bootstrap event
    was stamped `vm` while agent-written events were stamped `alice`. One
    machine wrote under two operator names and every shared milestone looked
    contested between an operator and themselves — the exact failure
    `orchestrator._add_federation_env` was added to prevent, arriving by
    another door.

    A name INHERITED from a parent process wins and is left alone. That is what
    makes a fan-out clone keep its root's identity: the clone is a separate
    process that receives the variable through its spawn environment, and its
    own config load must not relabel its events.

    A name this process set itself on an earlier `bind` does NOT win, and the
    distinction matters. `bind` mutates the process environment, so without it
    an embedder that calls `run_exploration` twice — a REPL, the benchmark
    adapter, a test session — would silently give the second run the first
    run's operator. Found by the full test suite, where one run_exploration
    test leaked its operator into every later test in the session.
    """
    inherited = slugify(os.environ.get(ENV_OPERATOR, ""))
    if inherited and not _BOUND_HERE:
        return inherited
    name = _from_config(config)
    _set_bound(name)
    return name


# True once `bind` has written the env var in THIS process. Distinguishes "a
# parent told us who we are" from "we decided earlier and may decide again".
_BOUND_HERE = False


def _set_bound(name: str) -> None:
    global _BOUND_HERE
    os.environ[ENV_OPERATOR] = name
    _BOUND_HERE = True


def _reset_binding() -> None:
    """Test hook: forget that this process bound an operator."""
    global _BOUND_HERE
    _BOUND_HERE = False
    os.environ.pop(ENV_OPERATOR, None)


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
