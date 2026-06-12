import os
import tempfile
import unittest
from unittest.mock import patch

from long_exposure import pool
from long_exposure import orchestrator


class ClonePoolFallbackTests(unittest.TestCase):
    """Regression: pinned clones must keep pool semantics after the conductor
    pops the primary pool env vars (CLAUDE_ACCOUNT_POOL etc.).

    _spawn_clone stashes the original pool config under
    LONG_EXPOSURE_CLONE_POOL_CONFIG; pool.parse_pool_config falls back to it
    so clone-side is_active / slot re-tag / rate-limit marking still resolve
    the account list and state file. The orchestrator's rotation parser reads
    only the primary env names, so it must NOT see the fallback.
    """

    def test_parse_pool_config_falls_back_to_clone_env(self):
        env = {pool.CLONE_POOL_CONFIG_ENV: "/acct-a,/acct-b"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(pool.parse_pool_config(), ["/acct-a", "/acct-b"])
            self.assertTrue(pool.is_active())

    def test_primary_env_takes_precedence_over_fallback(self):
        env = {
            "CLAUDE_ACCOUNT_POOL": "/acct-a,/acct-b",
            pool.CLONE_POOL_CONFIG_ENV: "/stale-x,/stale-y",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(pool.parse_pool_config(), ["/acct-a", "/acct-b"])

    def test_single_account_fallback_stays_inactive(self):
        with patch.dict(
            os.environ, {pool.CLONE_POOL_CONFIG_ENV: "/acct-a"}, clear=True
        ):
            self.assertFalse(pool.is_active())

    def test_orchestrator_parse_accounts_ignores_fallback(self):
        # The rotation parser must keep seeing a single (default) account in
        # clones — that's the whole point of popping the primary env vars.
        with patch.dict(
            os.environ, {pool.CLONE_POOL_CONFIG_ENV: "/acct-a,/acct-b"}, clear=True
        ):
            self.assertEqual(orchestrator._parse_accounts(), [None])

    def test_clone_env_resolves_same_state_file_for_slot_lifecycle(self):
        """Parent acquires under primary envs; the simulated clone env (primary
        popped, fallback set) must see the same pool state and be able to
        re-tag, mark cooling, and release the slot — exploration.py's clone
        bootstrap + §6.2 rate-limit path."""
        with tempfile.TemporaryDirectory() as td:
            parent_env = {
                "HOME": td,
                "CLAUDE_ACCOUNT_POOL": "/acct-a,/acct-b",
            }
            with patch.dict(os.environ, parent_env, clear=True):
                pool.init_pool()
                pinned = pool.acquire_slot(role="clone", fork_id="fk", clone_k=0)
                self.assertIn(pinned, ("/acct-a", "/acct-b"))

            clone_env = {
                "HOME": td,
                "CLAUDE_FORCE_ACCOUNT": pinned,
                pool.CLONE_POOL_CONFIG_ENV: "/acct-a,/acct-b",
            }
            with patch.dict(os.environ, clone_env, clear=True):
                self.assertTrue(pool.is_active())
                # Clone bootstrap re-tag (exploration.py clone bootstrap block).
                self.assertTrue(pool.update_slot_pid("fk", 0, os.getpid()))
                # §6.2 clone rate-limit path.
                pool.mark_rate_limited(pinned)
                self.assertEqual(pool.account_state(pinned), pool.COOLING)
                self.assertTrue(pool.release_slot_by_branch("fk", 0))
                # Idempotent: second release is a no-op.
                self.assertFalse(pool.release_slot_by_branch("fk", 0))


class FanoutCapTests(unittest.TestCase):
    def test_fanout_cap_does_not_double_reserve_root(self):
        # The root's ledger slot is already excluded from available_slots();
        # docs: 2 accounts x 3 slots, root holding 1 -> available 5 -> cap 5.
        with patch("long_exposure.pool.available_slots", return_value=5):
            self.assertEqual(pool.fanout_cap(), 5)

    def test_fanout_cap_floors_at_zero(self):
        with patch("long_exposure.pool.available_slots", return_value=0):
            self.assertEqual(pool.fanout_cap(), 0)

    def test_fanout_cap_end_to_end_two_accounts_root_holding_one_slot(self):
        with tempfile.TemporaryDirectory() as td:
            env = {"HOME": td, "CLAUDE_ACCOUNT_POOL": "/acct-a,/acct-b"}
            with patch.dict(os.environ, env, clear=True):
                pool.init_pool()
                pool.acquire_slot(role="root", pid=os.getpid())
                # 2 accounts x 3 slots = 6 raw, minus the root holder = 5.
                self.assertEqual(pool.available_slots(), 5)
                self.assertEqual(pool.fanout_cap(), 5)


if __name__ == "__main__":
    unittest.main()
