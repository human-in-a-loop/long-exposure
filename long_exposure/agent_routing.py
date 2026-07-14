"""Centralized per-agent LLM routing: provider, model, and effort.

Single source of truth: the ``agent_models`` block in ``config.yaml``. Each of
the named agent types (``AGENT_TYPES``) maps to ``{provider, model, effort}``.
This module:

- resolves that block and merges it onto the score's per-agent definitions so
  the existing ``build_agent_config`` + command-construction funnel picks the
  values up with no change to the dispatch code (``apply_agent_models``);
- translates the canonical effort vocabulary (Claude's ``low..max``) into each
  provider's native reasoning control (``provider_reasoning_args`` for the CLI,
  ``gemini_thinking_level`` for Gemini's ``settings.json``);
- exposes the per-agent provider context switch used to pin heterogeneous
  providers (``agent_provider_context``) and the ``per_agent_pinned`` predicate
  that decides whether the run is heterogeneous.

Design invariants (see docs/configuration-reference.md and
docs/multi-account-pool.md):

- **Backward compatible.** When ``config.yaml`` has no ``agent_models`` block,
  every function here is a no-op and behavior is byte-identical to before.
- **Never hard-fail.** Unknown effort/provider values warn once and fall back to
  a safe default; a long autonomous run is never aborted over a config typo.
- **Homogeneous == today.** ``agent_provider_context`` only swaps the provider
  when the run resolves to more than one distinct provider, so single-provider
  runs (including the default all-Claude template) keep their exact code path,
  including multi-account pooling.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager

from long_exposure import provider as _provider

# The named agent types that participate in centralized routing. These match
# the top-level keys under ``agents:`` in exploration-score.yaml.
AGENT_TYPES = (
    "researcher",
    "worker",
    "auditor",
    "reporter",
    "final_auditor",
    "final_reporter",
    "manager",
    "curator",
)

# Canonical effort vocabulary is Claude's, ordered low -> high. The default
# template ships every agent at ``xhigh`` (Claude Code's default; best for
# agentic/coding work on Opus 4.8 / Fable 5 / Sonnet 5).
CANONICAL_EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "xhigh"

# Codex reasoning effort accepts minimal|low|medium|high|xhigh (xhigh is
# model-dependent; max/ultra are gpt-5.6-only and entitlement-gated, and are
# absent from the official config reference). Map canonical -> codex, capping
# ``max`` at ``xhigh`` so a heterogeneous template never sends an unsupported
# value to a gpt-5.5-class model.
_CODEX_EFFORT_MAP = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",
}

# Gemini 3 uses ``thinkingLevel`` (LOW|MEDIUM|HIGH; thinking cannot be
# disabled). Gemini 2.5 uses a numeric ``thinkingBudget`` instead — out of
# scope here (default model is gemini-3-*). Map canonical -> Gemini 3 level.
_GEMINI_THINKING_MAP = {
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
    "xhigh": "HIGH",
    "max": "HIGH",
}

# One-time warning de-dup so a misconfigured effort doesn't spam a multi-day run.
_warned_efforts: set[tuple[str | None, str]] = set()


def normalize_effort(effort: object, *, agent: str | None = None) -> str:
    """Return a valid canonical effort, warning once on an unknown value."""
    raw = "" if effort is None else str(effort).strip().lower()
    if raw in CANONICAL_EFFORTS:
        return raw
    key = (agent, raw)
    if raw and key not in _warned_efforts:
        _warned_efforts.add(key)
        where = f" for agent '{agent}'" if agent else ""
        print(
            f"[long-exposure] agent_models: unknown effort {effort!r}{where}; "
            f"using '{DEFAULT_EFFORT}'. Valid: {', '.join(CANONICAL_EFFORTS)}.",
            file=sys.stderr,
            flush=True,
        )
    return DEFAULT_EFFORT


def codex_effort(effort: object) -> str:
    """Canonical effort -> Codex ``model_reasoning_effort`` value."""
    return _CODEX_EFFORT_MAP.get(normalize_effort(effort), "high")


def gemini_thinking_level(effort: object) -> str | None:
    """Canonical effort -> Gemini 3 ``thinkingLevel`` (LOW|MEDIUM|HIGH)."""
    return _GEMINI_THINKING_MAP.get(normalize_effort(effort))


def provider_reasoning_args(provider: str | None, effort: object) -> list[str]:
    """Provider-specific CLI args that set reasoning effort.

    - claude: ``--effort <e>`` (native flag; unknown values warn + fall back
      inside the CLI, so this is safe).
    - codex:  ``-c model_reasoning_effort="<mapped>"`` (config override; the
      value is TOML-parsed, hence the embedded quotes).
    - gemini: ``[]`` — Gemini has no CLI effort flag; effort is applied via
      ``settings.json`` (see ``gemini_thinking_level``).
    - local:  ``[]`` — the OpenAI-compatible connector has no reasoning field;
      effort stays in the system prompt only.
    """
    prov = _provider.normalize_provider(provider or _provider.current_provider())
    eff = normalize_effort(effort)
    if prov == _provider.CLAUDE:
        return ["--effort", eff]
    if prov == _provider.CODEX:
        return ["-c", f'model_reasoning_effort="{_CODEX_EFFORT_MAP.get(eff, "high")}"']
    return []


# ---------------------------------------------------------------------------
# Centralized template resolution
# ---------------------------------------------------------------------------


def _block(config: dict | None) -> dict:
    block = (config or {}).get("agent_models")
    return block if isinstance(block, dict) else {}


def has_agent_models(config: dict | None) -> bool:
    return bool(_block(config))


def resolve_agent_routing(config: dict | None, agent_name: str) -> dict:
    """Return ``{provider, model, effort}`` for ``agent_name`` from the
    centralized block. Any field the operator left unset is returned as
    ``None`` so the caller falls back to legacy/global resolution."""
    entry = _block(config).get(agent_name)
    if not isinstance(entry, dict):
        entry = {}
    provider = entry.get("provider")
    model = entry.get("model")
    effort = entry.get("effort")
    return {
        "provider": _provider.normalize_provider(provider) if provider else None,
        "model": str(model) if model else None,
        "effort": normalize_effort(effort, agent=agent_name) if effort else None,
    }


def apply_agent_models(config: dict | None, score: dict | None) -> None:
    """Merge the centralized ``agent_models`` block onto each score agent_def.

    Mutates ``score['agents'][name]`` in place, setting ``provider`` / ``model``
    / ``effort`` for any field the block specifies. This is the single merge
    point: afterwards the existing ``build_agent_config`` whitelist (model,
    effort) plus the ``provider`` handling added to it carry the values through
    every dispatch path. No-op when the block is absent (full back-compat)."""
    if not has_agent_models(config):
        return
    agents = (score or {}).get("agents")
    if not isinstance(agents, dict):
        return
    for name, adef in agents.items():
        if not isinstance(adef, dict):
            continue
        routing = resolve_agent_routing(config, name)
        if routing["provider"]:
            adef["provider"] = routing["provider"]
        if routing["model"]:
            adef["model"] = routing["model"]
        if routing["effort"]:
            adef["effort"] = routing["effort"]


def agent_provider(agent_def: dict | None, config: dict | None) -> str:
    """Normalized provider for an agent: its per-agent ``provider`` if set,
    else the global ``llm_provider``."""
    if isinstance(agent_def, dict) and agent_def.get("provider"):
        return _provider.normalize_provider(agent_def["provider"])
    return _provider.normalize_provider((config or {}).get("llm_provider"))


def resolved_providers(config: dict | None, score: dict | None) -> set[str]:
    """The set of distinct providers across all score agents after the merge."""
    agents = (score or {}).get("agents") or {}
    base = _provider.normalize_provider((config or {}).get("llm_provider"))
    providers: set[str] = set()
    for adef in agents.values():
        if isinstance(adef, dict) and adef.get("provider"):
            providers.add(_provider.normalize_provider(adef["provider"]))
        else:
            providers.add(base)
    return providers or {base}


def per_agent_pinned(config: dict | None, score: dict | None) -> bool:
    """True when agents resolve to more than one distinct provider.

    In this ("heterogeneous") mode each agent's turn is pinned to its own
    provider and multi-account pooling is deferred (see docs/multi-account-pool.md).
    Homogeneous runs return False and keep their exact legacy behavior,
    including pooling."""
    return len(resolved_providers(config, score)) > 1


@contextmanager
def agent_provider_context(agent_def: dict | None, config: dict | None):
    """Pin the process provider to this agent's provider for the duration.

    A no-op unless the run is in per-agent-pinned mode (``config['_per_agent_pinned']``),
    so homogeneous runs keep byte-identical behavior. Sets the same
    ``LONG_EXPOSURE_LLM_PROVIDER`` env var the unified pool already swaps on;
    ``provider.configure_provider`` reads the env in preference to the config
    value, so a subsequent ``build_agent_config`` inside this context resolves
    to the pinned provider."""
    if not config or not config.get("_per_agent_pinned"):
        yield
        return
    target = agent_provider(agent_def, config)
    prior = os.environ.get("LONG_EXPOSURE_LLM_PROVIDER")
    os.environ["LONG_EXPOSURE_LLM_PROVIDER"] = target
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("LONG_EXPOSURE_LLM_PROVIDER", None)
        else:
            os.environ["LONG_EXPOSURE_LLM_PROVIDER"] = prior
