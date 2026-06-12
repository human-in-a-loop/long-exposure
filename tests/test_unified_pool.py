import os
import unittest
from unittest.mock import patch

from long_exposure import pool, unified_pool
from long_exposure import exploration


class UnifiedPoolTests(unittest.TestCase):
    def test_inactive_with_one_provider_or_disabled(self):
        with patch.dict(os.environ, {"CLAUDE_ACCOUNT_POOL": "/a"}, clear=True):
            self.assertFalse(unified_pool.is_unified_active())
        with patch.dict(
            os.environ,
            {
                "CLAUDE_ACCOUNT_POOL": "/a",
                "CODEX_ACCOUNT_POOL": "/b",
                "LONG_EXPOSURE_UNIFIED_POOL": "disabled",
            },
            clear=True,
        ):
            self.assertFalse(unified_pool.is_unified_active())

    def test_active_with_two_provider_pools(self):
        with patch.dict(os.environ, {"CLAUDE_ACCOUNT_POOL": "/a", "CODEX_ACCOUNT_POOL": "/b"}, clear=True):
            self.assertTrue(unified_pool.is_unified_active())
            self.assertEqual(unified_pool.configured_providers(), ["claude", "codex"])

    def test_preference_and_release_route_to_origin_provider(self):
        calls = []

        def available():
            return {"claude": 0, "codex": 1}[os.environ["LONG_EXPOSURE_LLM_PROVIDER"]]

        def acquire(role, fork_id=None, clone_k=None, pid=None):
            calls.append(("acquire", os.environ["LONG_EXPOSURE_LLM_PROVIDER"], role, fork_id, clone_k))
            return "/codex"

        def release_branch(fork_id, clone_k):
            calls.append(("release_branch", os.environ["LONG_EXPOSURE_LLM_PROVIDER"], fork_id, clone_k))
            return True

        with patch.dict(os.environ, {"CLAUDE_ACCOUNT_POOL": "/a", "CODEX_ACCOUNT_POOL": "/b"}, clear=True):
            with (
                patch("long_exposure.unified_pool.pool.available_slots", side_effect=available),
                patch("long_exposure.unified_pool.pool.acquire_slot", side_effect=acquire),
                patch("long_exposure.unified_pool.pool.release_slot_by_branch", side_effect=release_branch),
            ):
                holder = unified_pool.acquire_slot(
                    role="clone",
                    provider_preference=["claude", "codex"],
                    fork_id="fork",
                    clone_k=2,
                )
                unified_pool.release_slot_by_holder(holder)

        self.assertEqual(holder.provider, "codex")
        self.assertEqual(calls[0], ("acquire", "codex", "clone", "fork", 2))
        self.assertEqual(calls[1], ("release_branch", "codex", "fork", 2))

    def test_root_rotation_marks_old_holder_cooling_and_acquires_new_slot(self):
        calls = []
        old = unified_pool.UnifiedHolder(
            holder_id="old",
            provider="claude",
            account_dir="/claude-old",
            pid=123,
        )
        new = unified_pool.UnifiedHolder(
            holder_id="new",
            provider="codex",
            account_dir="/codex-new",
            pid=123,
        )

        def mark_rate_limited(account_dir):
            calls.append(("cooling", os.environ["LONG_EXPOSURE_LLM_PROVIDER"], account_dir))

        def release(holder):
            calls.append(("release", holder.provider, holder.account_dir))

        def acquire(**kwargs):
            calls.append(("acquire", tuple(kwargs.get("provider_preference") or ())))
            return new

        with patch.dict(os.environ, {"LONG_EXPOSURE_LLM_PROVIDER": "claude"}, clear=True):
            with (
                patch("long_exposure.exploration.pool.mark_rate_limited", side_effect=mark_rate_limited),
                patch("long_exposure.exploration.unified_pool.release_slot_by_holder", side_effect=release),
                patch("long_exposure.exploration.unified_pool.acquire_slot", side_effect=acquire),
            ):
                exploration._unified_root_holder = old
                try:
                    holder = exploration._rotate_unified_root_after_rate_limit(["codex", "claude"])
                    env_provider = os.environ.get("LONG_EXPOSURE_LLM_PROVIDER")
                    env_codex_force = os.environ.get("CODEX_FORCE_ACCOUNT")
                    has_claude_force = "CLAUDE_FORCE_ACCOUNT" in os.environ
                finally:
                    exploration._unified_root_holder = None

        self.assertEqual(holder, new)
        self.assertEqual(calls[0], ("cooling", "claude", "/claude-old"))
        self.assertEqual(calls[1], ("release", "claude", "/claude-old"))
        self.assertEqual(calls[2], ("acquire", ("codex", "claude")))
        self.assertEqual(env_provider, "codex")
        self.assertEqual(env_codex_force, "/codex-new")
        self.assertFalse(has_claude_force)

    def test_call_agent_with_rotation_allows_unified_root_force_pin(self):
        calls = []
        holder = unified_pool.UnifiedHolder(
            holder_id="new",
            provider="codex",
            account_dir="/codex-new",
            pid=123,
        )

        def fake_agent(**kwargs):
            calls.append("agent")
            if len(calls) == 1:
                return {"status": "rate_limit"}
            return {"status": "ok", "outputs": {}}

        with patch.dict(
            os.environ,
            {
                "LONG_EXPOSURE_LLM_PROVIDER": "claude",
                "CLAUDE_FORCE_ACCOUNT": "/claude-old",
            },
            clear=True,
        ):
            with (
                patch("long_exposure.exploration._is_clone", return_value=False),
                patch("long_exposure.exploration.pool.is_active", return_value=False),
                patch("long_exposure.exploration.unified_pool.is_unified_active", return_value=True),
                patch("long_exposure.exploration.unified_pool.callable_account_count", return_value=2),
                patch("long_exposure.exploration._rotate_unified_root_after_rate_limit", return_value=holder) as rotate,
                patch("long_exposure.exploration._call_exploration_agent", side_effect=fake_agent),
            ):
                sessions = {"researcher": "old-session"}
                result = exploration._call_agent_with_rotation(
                    "researcher",
                    {"outputs": []},
                    sessions,
                )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, ["agent", "agent"])
        rotate.assert_called_once()
        self.assertEqual(sessions, {})

    def test_heartbeat_and_thaw_all_counts_thawed_dirs(self):
        # Regression: thaw_eligible returns list[str]; int(list) raised
        # TypeError (swallowed by the bare except) so unified maintenance
        # always reported thawed=0 on real thaws.
        with patch.dict(os.environ, {"CLAUDE_ACCOUNT_POOL": "/a,/b", "CODEX_ACCOUNT_POOL": "/c,/d"}, clear=True):
            with (
                patch("long_exposure.unified_pool.pool.heartbeat_sweep", return_value=1),
                patch(
                    "long_exposure.unified_pool.pool.thaw_eligible",
                    return_value=["/acct-x", "/acct-y"],
                ),
            ):
                swept, thawed = unified_pool.heartbeat_and_thaw_all()
        self.assertEqual(swept, 2)   # 1 per provider
        self.assertEqual(thawed, 4)  # 2 dirs per provider

    def test_unified_fanout_cap_sums_remaining_branch_slots(self):
        available = {"claude": 3, "codex": 5}

        def slots():
            return available[os.environ["LONG_EXPOSURE_LLM_PROVIDER"]]

        with patch.dict(os.environ, {"CLAUDE_ACCOUNT_POOL": "/a", "CODEX_ACCOUNT_POOL": "/b"}, clear=True):
            with patch("long_exposure.unified_pool.pool.available_slots", side_effect=slots):
                self.assertEqual(unified_pool.fanout_cap(), 8)


if __name__ == "__main__":
    unittest.main()
