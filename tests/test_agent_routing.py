"""Tests for centralized per-agent LLM routing (provider/model/effort).

Covers the pure resolution/translation helpers plus the two integration
guarantees that matter most:
  - backward compatibility: no `agent_models` block => byte-identical behavior;
  - per-agent provider pinning: the process provider is swapped for a pinned
    agent and restored afterwards, and build_agent_config resolves the pinned
    provider's model.
"""

import os

import pytest

from long_exposure import agent_routing, provider as P
from long_exposure.conductor import build_agent_config


# --------------------------------------------------------------------------
# effort normalization + translation
# --------------------------------------------------------------------------

def test_normalize_effort_valid_passthrough():
    for e in agent_routing.CANONICAL_EFFORTS:
        assert agent_routing.normalize_effort(e) == e
    assert agent_routing.normalize_effort("XHIGH") == "xhigh"  # case-insensitive


def test_normalize_effort_unknown_falls_back_to_default():
    assert agent_routing.normalize_effort("bogus") == agent_routing.DEFAULT_EFFORT
    assert agent_routing.normalize_effort(None) == agent_routing.DEFAULT_EFFORT
    assert agent_routing.DEFAULT_EFFORT == "xhigh"


def test_codex_effort_map_caps_max_at_xhigh():
    assert agent_routing.codex_effort("low") == "low"
    assert agent_routing.codex_effort("high") == "high"
    assert agent_routing.codex_effort("xhigh") == "xhigh"
    # max/ultra are gpt-5.6-only + gated; cap so gpt-5.5 never gets an
    # unsupported value.
    assert agent_routing.codex_effort("max") == "xhigh"


def test_gemini_thinking_level_map():
    assert agent_routing.gemini_thinking_level("low") == "LOW"
    assert agent_routing.gemini_thinking_level("medium") == "MEDIUM"
    assert agent_routing.gemini_thinking_level("high") == "HIGH"
    assert agent_routing.gemini_thinking_level("xhigh") == "HIGH"
    assert agent_routing.gemini_thinking_level("max") == "HIGH"


def test_provider_reasoning_args_per_provider():
    assert agent_routing.provider_reasoning_args("claude", "xhigh") == ["--effort", "xhigh"]
    # codex value is TOML-parsed, hence the embedded quotes
    assert agent_routing.provider_reasoning_args("codex", "medium") == [
        "-c",
        'model_reasoning_effort="medium"',
    ]
    assert agent_routing.provider_reasoning_args("codex", "max") == [
        "-c",
        'model_reasoning_effort="xhigh"',
    ]
    # gemini/local carry effort out-of-band (settings.json / system prompt)
    assert agent_routing.provider_reasoning_args("gemini", "high") == []
    assert agent_routing.provider_reasoning_args("local", "high") == []


def test_provider_reasoning_args_normalizes_aliases_and_unknowns():
    # provider aliases normalize
    assert agent_routing.provider_reasoning_args("anthropic", "high") == ["--effort", "high"]
    assert agent_routing.provider_reasoning_args("openai", "high")[0] == "-c"
    # unknown effort -> claude default; never raises
    assert agent_routing.provider_reasoning_args("claude", "nope") == ["--effort", "xhigh"]


# --------------------------------------------------------------------------
# resolution + merge
# --------------------------------------------------------------------------

def _score():
    return {
        "agents": {
            "researcher": {"philosophy": "research", "effort": "high"},
            "worker": {"philosophy": "efficient", "effort": "high"},
            "auditor": {"philosophy": "audit", "effort": "high"},
        }
    }


def test_resolve_agent_routing_fields():
    cfg = {
        "llm_provider": "claude",
        "agent_models": {
            "worker": {"provider": "codex", "model": "gpt-5.5", "effort": "medium"},
            "auditor": {"provider": "gemini"},  # model/effort omitted
        },
    }
    w = agent_routing.resolve_agent_routing(cfg, "worker")
    assert w == {"provider": "codex", "model": "gpt-5.5", "effort": "medium"}
    a = agent_routing.resolve_agent_routing(cfg, "auditor")
    assert a == {"provider": "gemini", "model": None, "effort": None}
    # agent absent from block => all None (legacy fallback)
    assert agent_routing.resolve_agent_routing(cfg, "researcher") == {
        "provider": None, "model": None, "effort": None,
    }


def test_apply_agent_models_merges_onto_score():
    cfg = {
        "llm_provider": "claude",
        "agent_models": {
            "researcher": {"provider": "claude", "model": "fable", "effort": "xhigh"},
            "worker": {"provider": "codex", "model": "gpt-5.5", "effort": "medium"},
        },
    }
    score = _score()
    agent_routing.apply_agent_models(cfg, score)
    assert score["agents"]["researcher"] == {
        "philosophy": "research", "effort": "xhigh",
        "provider": "claude", "model": "fable",
    }
    assert score["agents"]["worker"]["provider"] == "codex"
    assert score["agents"]["worker"]["model"] == "gpt-5.5"
    assert score["agents"]["worker"]["effort"] == "medium"
    # auditor not in block: untouched
    assert "provider" not in score["agents"]["auditor"]
    assert score["agents"]["auditor"]["effort"] == "high"


def test_apply_agent_models_noop_without_block():
    cfg = {"llm_provider": "claude"}  # no agent_models
    score = _score()
    before = {a: dict(d) for a, d in score["agents"].items()}
    agent_routing.apply_agent_models(cfg, score)
    assert {a: dict(d) for a, d in score["agents"].items()} == before
    assert agent_routing.per_agent_pinned(cfg, score) is False


# --------------------------------------------------------------------------
# pinning detection
# --------------------------------------------------------------------------

def test_per_agent_pinned_homogeneous_false():
    cfg = {"llm_provider": "claude", "agent_models": {
        a: {"provider": "claude", "model": "opus", "effort": "xhigh"}
        for a in ("researcher", "worker", "auditor")
    }}
    score = _score()
    agent_routing.apply_agent_models(cfg, score)
    assert agent_routing.per_agent_pinned(cfg, score) is False
    assert agent_routing.resolved_providers(cfg, score) == {"claude"}


def test_per_agent_pinned_heterogeneous_true():
    cfg = {"llm_provider": "claude", "agent_models": {
        "researcher": {"provider": "claude"},
        "worker": {"provider": "codex"},
    }}
    score = _score()
    agent_routing.apply_agent_models(cfg, score)
    assert agent_routing.per_agent_pinned(cfg, score) is True
    assert agent_routing.resolved_providers(cfg, score) == {"claude", "codex"}


def test_resolved_providers_uses_global_when_agent_has_no_provider():
    # An agent without a per-agent provider inherits the global llm_provider.
    cfg = {"llm_provider": "codex", "agent_models": {"worker": {"provider": "gemini"}}}
    score = _score()
    agent_routing.apply_agent_models(cfg, score)
    # researcher/auditor -> codex (global), worker -> gemini => heterogeneous
    assert agent_routing.resolved_providers(cfg, score) == {"codex", "gemini"}
    assert agent_routing.per_agent_pinned(cfg, score) is True


# --------------------------------------------------------------------------
# provider context switch
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _restore_provider_env():
    prior = os.environ.get("LONG_EXPOSURE_LLM_PROVIDER")
    yield
    if prior is None:
        os.environ.pop("LONG_EXPOSURE_LLM_PROVIDER", None)
    else:
        os.environ["LONG_EXPOSURE_LLM_PROVIDER"] = prior


def test_agent_provider_context_noop_when_not_pinned():
    os.environ["LONG_EXPOSURE_LLM_PROVIDER"] = "claude"
    cfg = {"llm_provider": "claude"}  # no _per_agent_pinned
    with agent_routing.agent_provider_context({"provider": "codex"}, cfg):
        # not pinned => no swap
        assert P.current_provider() == "claude"
    assert P.current_provider() == "claude"


def test_agent_provider_context_swaps_and_restores_when_pinned():
    os.environ["LONG_EXPOSURE_LLM_PROVIDER"] = "claude"
    cfg = {"llm_provider": "claude", "_per_agent_pinned": True}
    with agent_routing.agent_provider_context({"provider": "codex"}, cfg):
        assert P.current_provider() == "codex"
    assert P.current_provider() == "claude"  # restored


def test_agent_provider_context_restores_on_exception():
    os.environ["LONG_EXPOSURE_LLM_PROVIDER"] = "claude"
    cfg = {"llm_provider": "claude", "_per_agent_pinned": True}
    with pytest.raises(RuntimeError):
        with agent_routing.agent_provider_context({"provider": "gemini"}, cfg):
            assert P.current_provider() == "gemini"
            raise RuntimeError("boom")
    assert P.current_provider() == "claude"


# --------------------------------------------------------------------------
# integration: build_agent_config honors the pinned provider + model fallback
# --------------------------------------------------------------------------

def test_build_agent_config_resolves_pinned_provider_and_model():
    cfg = {
        "llm_provider": "claude", "model": "opus",
        "codex_model": "gpt-5.5", "_per_agent_pinned": True,
    }
    # worker pinned to codex with an explicit model
    with agent_routing.agent_provider_context({"provider": "codex", "model": "gpt-5.5"}, cfg):
        ac = build_agent_config(cfg, {"provider": "codex", "model": "gpt-5.5"})
        assert P.current_provider() == "codex"
        assert ac["model"] == "gpt-5.5"


def test_build_agent_config_codex_without_model_uses_codex_default():
    cfg = {
        "llm_provider": "claude", "model": "opus",
        "codex_model": "gpt-5.5", "_per_agent_pinned": True,
    }
    # No per-agent model => provider default (codex_model) applies.
    with agent_routing.agent_provider_context({"provider": "codex"}, cfg):
        ac = build_agent_config(cfg, {"provider": "codex"})
        assert ac["model"] == "gpt-5.5"
