"""Model capability profiles: soft-guidance tiering keyed by the routed model.

See docs/advanced-model-modes-plan.md (Feature 2) and docs/soft-guidance.md.

The four-layer system prompt was calibrated for Opus 4.6/4.7. For a more
capable model a good share of layers 2 and 3 is ceremony rather than
mechanism: named anti-patterns, a nine-field checkpoint envelope, and per
stage an enumerated exit-gate list. This module thins *exactly* that, and
nothing else, when the model running a turn is one the operator has declared
advanced.

Two invariants:

- **Exhortation may thin, machinery may not.** Philosophy preset text, the
  operating protocol's prescriptive rules (off-limits paths, the derived
  harness fence, tool contracts, the ``[INPUT: x]`` / ``[OUTPUT: x]``
  envelope, compaction thresholds) and role blocks are never touched. They
  are what the harness *parses*; thinning them breaks the run.
- **``standard`` applies nothing.** It is the absence of thinning, not a set
  of values, so it can never override an operator who deliberately turned a
  knob off in config.yaml.

Resolution is per agent with no plumbing: ``assemble_system_prompt`` is
always handed a per-agent config from ``build_agent_config``, whose ``model``
key is already the model ``agent_models`` routed that role to. So a Fable
researcher and an Opus auditor in the same run resolve to different profiles
from the same code path, and the resolution stays a pure function of
(config), which is what keeps the prompt cache from splitting between two
roles on the same model.
"""

from __future__ import annotations

from long_exposure.flags import truthy

STANDARD = "standard"
ADVANCED = "advanced"
PROFILES = (STANDARD, ADVANCED)

# Verbosity levels for the framework layer's stages block.
VERBOSITY_FULL = "full"
VERBOSITY_LEAN = "lean"
VERBOSITIES = (VERBOSITY_FULL, VERBOSITY_LEAN)

# What each profile sets. `standard` is deliberately empty: applying it must
# be a no-op so a run that does not opt in gets a byte-identical prompt.
PROFILE_KNOBS: dict[str, dict[str, object]] = {
    STANDARD: {},
    ADVANCED: {
        "require_checkpoint_first": False,
        "checkpoint_format": "minimal",
        "anti_patterns_enabled": False,
        "framework_verbosity": VERBOSITY_LEAN,
    },
}

# Only these keys may be set by a profile or by `overrides`. Anything else is
# ignored with a warning — a profile must not be able to reach into
# working_directory, allowed_tools, compact_db or a model id.
ALLOWED_KNOBS = frozenset(
    {
        "require_checkpoint_first",
        "checkpoint_format",
        "anti_patterns_enabled",
        "framework_verbosity",
    }
)

DEFAULTS: dict[str, object] = {
    "enabled": False,
    "auto": True,
    "profile": None,
    "default": STANDARD,
    "families": {ADVANCED: []},
    "overrides": {},
}

# Warn once per distinct message; a long autonomous run must not print the
# same config complaint on every one of thousands of agent turns.
_WARNED: set[str] = set()


def _warn(message: str) -> None:
    if message in _WARNED:
        return
    _WARNED.add(message)
    print(f"[model-profiles] {message}", flush=True)


def _reset_warnings() -> None:
    """Test hook: clear the warn-once memo."""
    _WARNED.clear()


def settings(config: dict | None) -> dict:
    """The `model_profiles` block with defaults filled in."""
    raw = (config or {}).get("model_profiles")
    if not isinstance(raw, dict):
        raw = {}
    out = dict(DEFAULTS)
    out["families"] = dict(DEFAULTS["families"])
    out["overrides"] = dict(DEFAULTS["overrides"])
    for key, value in raw.items():
        if key == "families" and isinstance(value, dict):
            out["families"] = {k: list(v or []) for k, v in value.items()}
        elif key == "overrides" and isinstance(value, dict):
            out["overrides"] = dict(value)
        else:
            out[key] = value
    return out


def enabled(config: dict | None) -> bool:
    return truthy(
        settings(config).get("enabled"), name="model_profiles.enabled",
    )


def _match_family(model: str, families: dict) -> str | None:
    """First profile whose family list has a substring hit on `model`.

    Substring rather than exact match so `fable` catches `claude-fable-5-1`
    and any future point release without a config edit. Longest pattern wins
    within a profile so a narrow pattern can be listed alongside a broad one;
    profiles are checked in PROFILES order for determinism, since dict
    ordering must not decide a prompt's contents.
    """
    needle = (model or "").strip().lower()
    if not needle:
        return None
    for name in PROFILES:
        patterns = families.get(name) or []
        for pattern in sorted(
            (str(p).strip().lower() for p in patterns if str(p).strip()),
            key=len,
            reverse=True,
        ):
            if pattern in needle:
                return name
    return None


def resolve(config: dict | None) -> str:
    """The profile name for the model in `config`.

    Precedence: explicit `profile:` > `auto` family match > `default`.
    Unknown names warn and fall back to `default`, then to `standard`; a
    typo must never silently thin a prompt.
    """
    cfg = settings(config)
    if not enabled(config):
        return STANDARD

    fallback = str(cfg.get("default") or STANDARD).strip().lower()
    if fallback not in PROFILES:
        _warn(f"unknown default profile {fallback!r}; using {STANDARD!r}.")
        fallback = STANDARD

    explicit = cfg.get("profile")
    if explicit:
        name = str(explicit).strip().lower()
        if name in PROFILES:
            return name
        _warn(f"unknown profile {explicit!r}; using {fallback!r}.")
        return fallback

    if truthy(cfg.get("auto"), True, name="model_profiles.auto"):
        matched = _match_family(
            str((config or {}).get("model") or ""), cfg.get("families") or {}
        )
        if matched:
            return matched

    return fallback


def knobs(config: dict | None) -> dict:
    """The knob values this config's profile sets, with `overrides` on top."""
    cfg = settings(config)
    if not enabled(config):
        return {}

    resolved = dict(PROFILE_KNOBS.get(resolve(config), {}))
    for key, value in (cfg.get("overrides") or {}).items():
        if key not in ALLOWED_KNOBS:
            _warn(
                f"ignoring override {key!r}: not a guidance knob. "
                f"Allowed: {', '.join(sorted(ALLOWED_KNOBS))}."
            )
            continue
        resolved[key] = value

    verbosity = resolved.get("framework_verbosity")
    if verbosity is not None and str(verbosity).strip().lower() not in VERBOSITIES:
        _warn(
            f"unknown framework_verbosity {verbosity!r}; using "
            f"{VERBOSITY_FULL!r}."
        )
        resolved["framework_verbosity"] = VERBOSITY_FULL
    return resolved


def apply(config: dict | None) -> dict:
    """Return a copy of `config` with this profile's knobs applied.

    Never mutates the caller's dict: `assemble_system_prompt` is called with
    a per-agent config that other code reads afterwards, and a profile must
    shape one prompt rather than leak into the run's state.
    """
    base = dict(config or {})
    applied = knobs(config)
    if not applied:
        return base
    base.update(applied)
    base["_model_profile"] = resolve(config)
    return base


def describe(config: dict | None) -> str:
    """One-line summary for the startup gate's routing table and logs."""
    if not enabled(config):
        return "standard (profiles off)"
    name = resolve(config)
    applied = knobs(config)
    if not applied:
        return name
    thinned = [k for k in sorted(applied) if k != "framework_verbosity"]
    verbosity = applied.get("framework_verbosity", VERBOSITY_FULL)
    return f"{name} (framework: {verbosity}; sets: {', '.join(thinned) or 'nothing'})"
