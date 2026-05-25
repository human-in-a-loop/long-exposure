"""Provider-agnostic selector over existing per-provider pools.

The per-provider pool state machines remain authoritative. This module only
chooses which provider's pool to call and routes release/update operations
back to the provider that acquired the slot.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from long_exposure import pool, provider as _provider


SUPPORTED_PROVIDERS_FOR_POOL = (_provider.CLAUDE, _provider.CODEX)


@dataclass(frozen=True)
class UnifiedHolder:
    holder_id: str
    provider: str
    account_dir: str
    fork_id: str | None = None
    clone_k: int | None = None
    pid: int | None = None


@contextmanager
def swap_active_provider(target: str):
    prior = os.environ.get("LONG_EXPOSURE_LLM_PROVIDER")
    os.environ["LONG_EXPOSURE_LLM_PROVIDER"] = _provider.normalize_provider(target)
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("LONG_EXPOSURE_LLM_PROVIDER", None)
        else:
            os.environ["LONG_EXPOSURE_LLM_PROVIDER"] = prior


def configured_providers() -> list[str]:
    providers: list[str] = []
    for prv in SUPPORTED_PROVIDERS_FOR_POOL:
        try:
            if _provider.parse_pool_env(prv):
                providers.append(prv)
        except Exception:
            continue
    return providers


def is_unified_active() -> bool:
    if os.environ.get("LONG_EXPOSURE_UNIFIED_POOL", "").strip().lower() == "disabled":
        return False
    return len(configured_providers()) >= 2


def pool_engaged() -> bool:
    return pool.is_active() or is_unified_active()


def init_all_pools() -> None:
    for prv in configured_providers():
        with swap_active_provider(prv):
            try:
                pool.init_pool()
                pool.thaw_eligible()
            except Exception:
                pass


def available_slots_by_provider() -> dict[str, int]:
    out: dict[str, int] = {}
    for prv in configured_providers():
        with swap_active_provider(prv):
            try:
                out[prv] = pool.available_slots()
            except Exception:
                out[prv] = 0
    return out


def _pick_provider(preference: list[str] | None = None) -> str | None:
    caps = available_slots_by_provider()
    for prv in preference or []:
        norm = _provider.normalize_provider(prv)
        if caps.get(norm, 0) > 0:
            return norm
    ranked = sorted(caps.items(), key=lambda item: item[1], reverse=True)
    if ranked and ranked[0][1] > 0:
        return ranked[0][0]
    return None


def acquire_slot(
    role: str = "agent",
    provider_preference: list[str] | None = None,
    fork_id: str | None = None,
    clone_k: int | None = None,
    pid: int | None = None,
) -> UnifiedHolder:
    target = _pick_provider(provider_preference)
    if target is None:
        raise pool.PoolExhausted(
            f"Unified pool: no provider has free capacity {available_slots_by_provider()}"
        )
    actual_pid = pid or os.getpid()
    with swap_active_provider(target):
        account_dir = pool.acquire_slot(
            role=role,
            fork_id=fork_id,
            clone_k=clone_k,
            pid=actual_pid,
        )
    return UnifiedHolder(
        holder_id=str(uuid.uuid4()),
        provider=target,
        account_dir=account_dir,
        fork_id=fork_id,
        clone_k=clone_k,
        pid=actual_pid if fork_id is None else None,
    )


def release_slot_by_holder(holder: UnifiedHolder) -> None:
    with swap_active_provider(holder.provider):
        if holder.fork_id is not None and holder.clone_k is not None:
            pool.release_slot_by_branch(holder.fork_id, holder.clone_k)
        else:
            pool.release_slot(holder.pid or os.getpid())


def update_slot_pid_for_holder(holder: UnifiedHolder, new_pid: int) -> bool:
    if holder.fork_id is None or holder.clone_k is None:
        return False
    with swap_active_provider(holder.provider):
        return pool.update_slot_pid(holder.fork_id, holder.clone_k, new_pid)


def promote_fresh_unified(cooling_provider: str | None = None) -> tuple[str, str] | None:
    providers = configured_providers()
    ordered = [p for p in providers if p != cooling_provider]
    if cooling_provider:
        ordered.append(cooling_provider)
    for prv in ordered:
        with swap_active_provider(prv):
            try:
                fresh = pool.promote_fresh()
            except Exception:
                fresh = None
        if fresh:
            return prv, fresh
    return None


def fanout_cap() -> int:
    # Root unified runs reserve their root slot at startup before cycle-level
    # fan-out guidance is rendered. At that point provider-local
    # available_slots() already excludes the root holder, so the unified cap is
    # the remaining free branch capacity summed across providers. This also
    # lets heterogeneous caps compose directly, e.g. Claude root+3 plus Codex 5
    # exposes 8 branch slots after the root holder is acquired.
    total = 0
    for prv in configured_providers():
        with swap_active_provider(prv):
            try:
                total += max(0, pool.available_slots())
            except Exception:
                pass
    return total


def callable_account_count() -> int:
    total = 0
    for prv in configured_providers():
        with swap_active_provider(prv):
            try:
                total += sum(
                    1 for account in pool.pool_state().get("accounts", [])
                    if account.get("state") in ("cold", "primary", "overflow")
                )
            except Exception:
                pass
    return total


def heartbeat_and_thaw_all() -> tuple[int, int]:
    swept = 0
    thawed = 0
    for prv in configured_providers():
        with swap_active_provider(prv):
            try:
                swept += int(pool.heartbeat_sweep() or 0)
            except Exception:
                pass
            try:
                thawed += int(pool.thaw_eligible() or 0)
            except Exception:
                pass
    return swept, thawed


def format_unified_summary() -> str:
    caps = available_slots_by_provider()
    return "Unified pool slots: " + ", ".join(
        f"{provider}={count}" for provider, count in sorted(caps.items())
    )
