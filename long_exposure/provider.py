"""Provider selection and provider-specific environment names."""

from __future__ import annotations

import os
from pathlib import Path

CLAUDE = "claude"
CODEX = "codex"
GEMINI = "gemini"
LOCAL = "local"
PROVIDERS = {CLAUDE, CODEX, GEMINI, LOCAL}


def normalize_provider(raw: str | None) -> str:
    provider = (raw or CLAUDE).strip().lower()
    if provider in ("anthropic", "claude-code"):
        return CLAUDE
    if provider in ("openai", "codex-cli"):
        return CODEX
    if provider in ("google", "google-gemini", "gemini-cli"):
        return GEMINI
    if provider in ("openai-compatible", "openai_compatible", "custom", "byo"):
        return LOCAL
    return provider if provider in PROVIDERS else CLAUDE


def configure_provider(config: dict | None = None) -> str:
    env_provider = os.environ.get("LONG_EXPOSURE_LLM_PROVIDER")
    cfg_provider = (config or {}).get("llm_provider")
    provider = normalize_provider(env_provider or cfg_provider)
    os.environ["LONG_EXPOSURE_LLM_PROVIDER"] = provider
    return provider


def current_provider() -> str:
    return normalize_provider(os.environ.get("LONG_EXPOSURE_LLM_PROVIDER"))


def is_codex() -> bool:
    return current_provider() == CODEX


def is_gemini() -> bool:
    return current_provider() == GEMINI


def is_claude() -> bool:
    return current_provider() == CLAUDE


def is_local() -> bool:
    return current_provider() == LOCAL


def account_pool_envs(provider: str | None = None) -> tuple[str, ...]:
    provider = normalize_provider(provider or current_provider())
    if provider == CODEX:
        return ("CODEX_ACCOUNT_POOL", "CODEX_HOMES")
    if provider == GEMINI:
        return ("GEMINI_ACCOUNT_POOL", "GEMINI_HOMES")
    if provider == LOCAL:
        return ("LOCAL_LLM_POOL",)
    return ("CLAUDE_ACCOUNT_POOL", "CLAUDE_ACCOUNTS")


def legacy_rotation_env(provider: str | None = None) -> str:
    provider = normalize_provider(provider or current_provider())
    if provider == LOCAL:
        return "LOCAL_LLM_POOL"
    if provider == GEMINI:
        return "GEMINI_HOMES"
    return "CODEX_HOMES" if provider == CODEX else "CLAUDE_ACCOUNTS"


def force_account_env(provider: str | None = None) -> str:
    provider = normalize_provider(provider or current_provider())
    if provider == LOCAL:
        return "LOCAL_LLM_FORCE_ACCOUNT"
    if provider == GEMINI:
        return "GEMINI_FORCE_ACCOUNT"
    return "CODEX_FORCE_ACCOUNT" if provider == CODEX else "CLAUDE_FORCE_ACCOUNT"


def child_config_env(provider: str | None = None) -> str:
    provider = normalize_provider(provider or current_provider())
    if provider == LOCAL:
        return "LOCAL_LLM_HOME"
    if provider == GEMINI:
        return "GEMINI_CLI_HOME"
    return "CODEX_HOME" if provider == CODEX else "CLAUDE_CONFIG_DIR"


def pool_state_path(provider: str | None = None) -> Path:
    provider = normalize_provider(provider or current_provider())
    if provider == LOCAL:
        name = ".local-llm-pool-state.json"
        return Path.home() / name
    if provider == GEMINI:
        return Path.home() / ".gemini-pool-state.json"
    name = ".codex-pool-state.json" if provider == CODEX else ".claude-pool-state.json"
    return Path.home() / name


def pool_lock_path(provider: str | None = None) -> Path:
    provider = normalize_provider(provider or current_provider())
    if provider == LOCAL:
        name = ".local-llm-pool-state.lock"
        return Path.home() / name
    if provider == GEMINI:
        return Path.home() / ".gemini-pool-state.lock"
    name = ".codex-pool-state.lock" if provider == CODEX else ".claude-pool-state.lock"
    return Path.home() / name


def accounts_state_path(provider: str | None = None) -> Path:
    provider = normalize_provider(provider or current_provider())
    if provider == LOCAL:
        name = ".local-llm-accounts-state.json"
        return Path.home() / name
    if provider == GEMINI:
        return Path.home() / ".gemini-accounts-state.json"
    name = ".codex-accounts-state.json" if provider == CODEX else ".claude-accounts-state.json"
    return Path.home() / name


def accounts_lock_path(provider: str | None = None) -> Path:
    provider = normalize_provider(provider or current_provider())
    if provider == LOCAL:
        name = ".local-llm-accounts-state.lock"
        return Path.home() / name
    if provider == GEMINI:
        return Path.home() / ".gemini-accounts-state.lock"
    name = ".codex-accounts-state.lock" if provider == CODEX else ".claude-accounts-state.lock"
    return Path.home() / name


def parse_pool_env(provider: str | None = None) -> list[str]:
    provider = normalize_provider(provider or current_provider())
    # Gemini CLI supports isolating user config via GEMINI_CLI_HOME, but
    # multi-account OAuth pooling needs operator-managed auth flows per home
    # and has not been live-validated. Keep pooling disabled rather than
    # pretending the Claude/Codex account-pool contract is safe.
    if provider == GEMINI:
        return []
    for env_name in account_pool_envs(provider):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            return [p.strip() for p in raw.split(",") if p.strip()]
    return []
