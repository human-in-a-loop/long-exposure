"""One total spend limit (Feature 4 of docs/advanced-model-modes-plan.md).

The behaviours worth pinning are the ones that make this a KILL rather than
the graceful `loop.max_cost_usd` stop: no sub-budgets, the end-of-run
pipeline skipped, clone spend visible before collapse, a marker written, a
non-zero exit code, and clones that never enforce the cap themselves.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from long_exposure import spend_limit as sl
from long_exposure.orchestrator import load_config


def _cfg(*, enabled=True, allowance=400.0, pct=20.0):
    return {
        "usage_allowance": {
            "enabled": enabled,
            "weekly_allowance_usd": allowance,
            "run_pct": pct,
        }
    }


class CapArithmeticTests(unittest.TestCase):
    def setUp(self):
        sl.reset()

    def test_percentage_of_the_declared_allowance(self):
        self.assertEqual(sl.cap_usd(_cfg(allowance=400, pct=20)), 80.0)
        self.assertEqual(sl.cap_usd(_cfg(allowance=250, pct=100)), 250.0)
        self.assertAlmostEqual(sl.cap_usd(_cfg(allowance=33.33, pct=7.5)), 2.49975)

    def test_the_three_documented_ways_to_say_unlimited(self):
        self.assertIsNone(sl.cap_usd(_cfg(enabled=False)))
        self.assertIsNone(sl.cap_usd(_cfg(pct=0)))
        self.assertIsNone(sl.cap_usd(_cfg(allowance=0)))

    def test_absent_block_is_unlimited(self):
        self.assertIsNone(sl.cap_usd({}))
        self.assertIsNone(sl.cap_usd(None))
        self.assertFalse(sl.enabled({}))

    def test_pct_above_100_clamps_to_the_allowance(self):
        self.assertEqual(sl.cap_usd(_cfg(allowance=400, pct=250)), 400.0)

    def test_garbage_values_do_not_raise_and_read_as_unlimited(self):
        for bad in ("abc", None, [], {}, float("nan"), float("inf"), -5):
            self.assertIsNone(
                sl.cap_usd({"usage_allowance": {
                    "enabled": True, "weekly_allowance_usd": bad, "run_pct": 20,
                }}),
                bad,
            )

    def test_shipped_config_ships_the_feature_off(self):
        self.assertIsNone(sl.cap_usd(load_config()))


class TripwireTests(unittest.TestCase):
    def setUp(self):
        sl.reset()

    def test_does_not_trip_below_the_cap(self):
        self.assertIsNone(sl.check(79.99, _cfg(), source="t"))
        self.assertIsNone(sl.tripped())

    def test_trips_at_the_cap_not_only_above_it(self):
        self.assertIsNotNone(sl.check(80.0, _cfg(), source="t"))
        self.assertIsNotNone(sl.tripped())

    def test_never_trips_when_unlimited(self):
        self.assertIsNone(sl.check(1_000_000.0, _cfg(enabled=False), source="t"))
        self.assertIsNone(sl.tripped())

    def test_trip_is_sticky_and_keeps_the_first_record(self):
        first = sl.check(80.0, _cfg(), source="agent:worker")
        second = sl.check(500.0, _cfg(), source="cycle_boundary")
        self.assertEqual(first["source"], "agent:worker")
        self.assertEqual(second["source"], "agent:worker")
        self.assertEqual(second["observed_usd"], 80.0)

    def test_clone_spend_counts_toward_the_same_single_total(self):
        """No sub-budgets: root 50 + clones 30 trips an 80 cap."""
        self.assertIsNotNone(
            sl.check(50.0, _cfg(), source="fanout_barrier", extra_usd=30.0)
        )
        trip = sl.tripped()
        self.assertEqual(trip["root_usd"], 50.0)
        self.assertEqual(trip["clone_usd"], 30.0)
        self.assertEqual(trip["observed_usd"], 80.0)

    def test_root_alone_under_cap_with_clones_over_it_still_trips(self):
        self.assertIsNone(sl.check(10.0, _cfg(), source="a"))
        self.assertIsNotNone(sl.check(10.0, _cfg(), source="b", extra_usd=70.0))

    def test_reset_clears_the_wire(self):
        sl.check(80.0, _cfg(), source="t")
        sl.reset()
        self.assertIsNone(sl.tripped())


class CloneSpendPollTests(unittest.TestCase):
    """The root must see clone spend BEFORE barrier collapse."""

    def setUp(self):
        sl.reset()

    @staticmethod
    def _clone(root: Path, k: int, cost) -> Path:
        cd = root / f"clone-{k}"
        (cd / "output").mkdir(parents=True)
        (cd / "output" / "usage_summary.json").write_text(
            json.dumps({"totals": {"cost_usd": cost}})
        )
        return cd

    def test_sums_every_live_clone_summary(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            dirs = [self._clone(root, k, c) for k, c in enumerate((3.5, 7.25, 1.0))]
            self.assertAlmostEqual(sl.clone_spend_usd(dirs), 11.75)

    def test_missing_summary_counts_as_zero(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            present = self._clone(root, 0, 5.0)
            absent = root / "clone-1"
            absent.mkdir()
            self.assertEqual(sl.clone_spend_usd([present, absent]), 5.0)

    def test_partial_write_counts_as_zero_and_does_not_raise(self):
        """A summary caught mid-flush must not crash the root's poll."""
        with TemporaryDirectory() as td:
            root = Path(td)
            good = self._clone(root, 0, 4.0)
            bad = root / "clone-1"
            (bad / "output").mkdir(parents=True)
            (bad / "output" / "usage_summary.json").write_text('{"totals": {"cost_u')
            self.assertEqual(sl.clone_spend_usd([good, bad]), 4.0)

    def test_unexpected_shape_counts_as_zero(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            odd = root / "clone-0"
            (odd / "output").mkdir(parents=True)
            (odd / "output" / "usage_summary.json").write_text("[1, 2, 3]")
            self.assertEqual(sl.clone_spend_usd([odd]), 0.0)

    def test_empty_and_none_clone_lists(self):
        self.assertEqual(sl.clone_spend_usd([]), 0.0)
        self.assertEqual(sl.clone_spend_usd(None), 0.0)


class MarkerTests(unittest.TestCase):
    def setUp(self):
        sl.reset()

    def test_marker_records_the_cap_the_total_and_the_cycle(self):
        with TemporaryDirectory() as td:
            sl.check(80.0, _cfg(), source="agent:worker")
            path = sl.write_marker(td, cycle=7)
            data = json.loads(Path(path).read_text())
            self.assertEqual(data["cap_usd"], 80.0)
            self.assertEqual(data["observed_usd"], 80.0)
            self.assertEqual(data["cycle"], 7)
            self.assertEqual(data["source"], "agent:worker")
            self.assertIs(data["killed"], True)
            self.assertEqual(data["weekly_allowance_usd"], 400.0)
            self.assertEqual(data["run_pct"], 20.0)
            self.assertIn("tripped_at", data)

    def test_no_marker_when_nothing_tripped(self):
        with TemporaryDirectory() as td:
            self.assertIsNone(sl.write_marker(td, cycle=1))
            self.assertFalse(sl.marker_path(td).exists())

    def test_marker_write_failure_is_survivable(self):
        """Losing the marker is a reporting loss; failing to stop is a spending one."""
        sl.check(80.0, _cfg(), source="t")
        with mock.patch.object(
            Path, "write_text", side_effect=OSError("read-only")
        ):
            self.assertIsNone(sl.write_marker("/nonexistent-xyz", cycle=1))


class ReportingTests(unittest.TestCase):
    def setUp(self):
        sl.reset()

    def test_summary_is_empty_when_the_feature_is_off(self):
        self.assertEqual(sl.summary_lines(_cfg(enabled=False), 12.0), "")
        self.assertEqual(sl.summary_lines({}, 12.0), "")

    def test_summary_always_carries_the_proxy_disclaimer(self):
        out = sl.summary_lines(_cfg(), 61.42)
        self.assertIn("operator-declared", out)
        self.assertIn("not a meter reading", out)
        self.assertIn("$80.00 cap", out)
        self.assertIn("$61.42", out)
        self.assertIn("76.8%", out)

    def test_summary_says_killed_after_a_trip(self):
        sl.check(80.0, _cfg(), source="t")
        out = sl.summary_lines(_cfg(), 80.0)
        self.assertIn("KILLED", out)
        # Not "**Status:**": the status file already has one of those, and
        # two differently-worded Status lines in one artifact is confusing
        # for the operator reading it to find out why the run stopped.
        self.assertNotIn("**Status:**", out)
        self.assertIn(sl.MARKER_FILENAME, out)
        self.assertIn("end-of-run pipeline was skipped", out)

    def test_describe_names_the_proxy(self):
        self.assertIn("proxy", sl.describe(_cfg()))
        self.assertEqual(sl.describe(_cfg(enabled=False)), "unlimited (no spend limit)")


class WiringTests(unittest.TestCase):
    """The seams in exploration.py / fanout.py, checked by source inspection.

    An end-to-end cycle-loop run needs a live provider, so these assert the
    hook points exist and are guarded the way the design requires. The
    behavioural coverage is in the tests above plus the kill-path test below.
    """

    @classmethod
    def setUpClass(cls):
        cls.exp = Path("long_exposure/exploration.py").read_text()
        cls.fan = Path("long_exposure/fanout.py").read_text()

    def test_record_hook_is_root_only(self):
        """A clone sees its own spend, never the run total (module docstring)."""
        idx = self.exp.index('source=f"agent:{agent_name}"')
        window = self.exp[idx - 900:idx]
        self.assertIn("if not _is_clone():", window)

    def test_record_hook_reads_the_run_config_not_the_agent_config(self):
        """One cap, one source. A per-agent override would be a sub-budget."""
        idx = self.exp.index('source=f"agent:{agent_name}"')
        window = self.exp[idx - 300:idx]
        self.assertIn("_current_run_config", window)

    def test_three_kill_sites_exist(self):
        for source in ('"cycle_boundary"', '"fanout_barrier"'):
            self.assertIn(
                source, self.exp + self.fan, f"missing check source {source}",
            )
        # and the between-turns guard that stops the NEXT agent starting
        self.assertIn("if _spend_limit.tripped():", self.exp)

    def test_end_of_run_is_suppressed_on_a_kill(self):
        idx = self.exp.index("if spend_limit_killed:")
        window = self.exp[idx:idx + 400]
        self.assertIn("should_run_final = False", window)

    def test_kill_raises_after_state_is_saved(self):
        """A kill must not cost the run its resumability."""
        save = self.exp.rindex("save_state(state_path, cycle, results")
        raise_at = self.exp.index("raise SpendLimitKill(_trip, _marker)")
        self.assertLess(save, raise_at)
        close = self.exp.rindex("conn.close()")
        self.assertLess(close, raise_at)

    def test_barrier_signals_and_terminates_on_a_trip(self):
        """Behaviour is proven in FanOutKillTests; this guards the structure.

        Scoped to the trip branch by slicing from the barrier's check to the
        `continue` that collapses it, so the assertions cannot drift onto the
        unrelated 10h-cap or preemption code below.
        """
        start = self.fan.index('source="fanout_barrier"')
        end = self.fan.index("Stage 9: graceful barrier preemption check", start)
        branch = self.fan[start:end]
        self.assertIn("long-exposure.stop", branch)
        self.assertIn("SIGTERM", branch)
        self.assertIn("SIGKILL", branch)
        self.assertIn("SPEND_KILL_GRACE_SECONDS", branch)

    def test_exit_code_is_a_distinct_nonzero(self):
        from long_exposure.exploration import SpendLimitKill

        self.assertEqual(SpendLimitKill.EXIT_CODE, 3)
        self.assertNotEqual(SpendLimitKill.EXIT_CODE, 0)

    def test_cli_maps_the_kill_for_launch_start_and_resume(self):
        cli = Path("long_exposure/cli.py").read_text()
        self.assertIn("except exploration.SpendLimitKill as kill:", cli)
        self.assertIn("_run_or_spend_kill(exploration._cmd_start", cli)
        self.assertIn("_run_or_spend_kill(exploration._cmd_resume", cli)
        self.assertIn("except SpendLimitKill as kill:", self.exp)


class KillExceptionTests(unittest.TestCase):
    def test_carries_the_trip_and_the_marker(self):
        from long_exposure.exploration import SpendLimitKill

        kill = SpendLimitKill({"cap_usd": 80.0, "observed_usd": 81.5}, "/tmp/m.json")
        self.assertEqual(kill.trip["cap_usd"], 80.0)
        self.assertEqual(kill.marker, "/tmp/m.json")
        self.assertIn("$81.50", str(kill))
        self.assertIn("$80.00", str(kill))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# End-to-end: drive the real cycle loop and assert the kill actually happens
# ---------------------------------------------------------------------------

import tempfile  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

from long_exposure import telemetry  # noqa: E402
from long_exposure.exploration import SpendLimitKill, run_exploration  # noqa: E402
from tests.test_run_switches import (  # noqa: E402
    _fake_agent_factory,
    _write_files,
)


def _spend_config(config_path: Path, *, allowance: float, pct: float) -> None:
    """Append a usage_allowance block to a generated test config."""
    config_path.write_text(
        config_path.read_text()
        + "usage_allowance:\n"
        + "  enabled: true\n"
        + f"  weekly_allowance_usd: {allowance}\n"
        + f"  run_pct: {pct}\n"
    )


class KillPathIntegrationTests(unittest.TestCase):
    """The real loop, a stubbed provider, and a cap the first turn blows."""

    def setUp(self):
        sl.reset()

    def tearDown(self):
        sl.reset()
        telemetry.configure({"telemetry": {"enabled": False}}, None, None)

    def _run(self, *, allowance, pct, cost_usd, loop_extra="", final_agents=False):
        seen = []
        final_auditor = MagicMock(return_value=None)
        final_reporter = MagicMock(return_value=None)
        curator = MagicMock(return_value=None)
        td = tempfile.mkdtemp()
        score, config, inst = _write_files(
            Path(td),
            loop_extra="  max_cycles: 5\n" + loop_extra,
            final_agents=final_agents,
        )
        _spend_config(config, allowance=allowance, pct=pct)
        raised = None
        with patch(
            "long_exposure.exploration._call_exploration_agent",
            _fake_agent_factory(seen, cost_usd=cost_usd),
        ), patch(
            "long_exposure.auditing._run_final_auditor", final_auditor
        ), patch(
            "long_exposure.exploration._run_final_reporter", final_reporter
        ), patch(
            "long_exposure.exploration._run_curator", curator
        ):
            try:
                run_exploration(
                    score_path=str(score),
                    config_path=str(config),
                    output_dir=inst / "output",
                    state_path=inst / "exploration_state.json",
                    task_override=None,
                    instance_dir=inst,
                )
            except SpendLimitKill as kill:
                raised = kill
        return {
            "seen": seen,
            "inst": inst,
            "kill": raised,
            "final_auditor": final_auditor,
            "final_reporter": final_reporter,
            "curator": curator,
        }

    def test_a_blown_cap_kills_the_run_and_raises(self):
        # $10 cap; the first agent turn costs $25.
        out = self._run(allowance=100.0, pct=10.0, cost_usd=25.0)
        self.assertIsNotNone(out["kill"], "SpendLimitKill was not raised")
        self.assertEqual(out["kill"].trip["cap_usd"], 10.0)
        self.assertGreaterEqual(out["kill"].trip["observed_usd"], 25.0)

    def test_the_kill_stops_the_next_agent_turn_starting(self):
        """One turn blows the cap, so the tail must not run."""
        out = self._run(allowance=100.0, pct=10.0, cost_usd=25.0)
        agents_run = [s["agent"] for s in out["seen"]]
        self.assertEqual(agents_run, ["researcher"], agents_run)

    def test_the_marker_is_written_with_the_cap_and_the_total(self):
        out = self._run(allowance=100.0, pct=10.0, cost_usd=25.0)
        marker = out["inst"] / "output" / sl.MARKER_FILENAME
        self.assertTrue(marker.exists(), "marker file missing")
        data = json.loads(marker.read_text())
        self.assertEqual(data["cap_usd"], 10.0)
        self.assertIs(data["killed"], True)
        self.assertTrue(data["source"].startswith("agent:"))

    def test_state_is_still_saved_so_the_run_stays_resumable(self):
        out = self._run(allowance=100.0, pct=10.0, cost_usd=25.0)
        state = out["inst"] / "exploration_state.json"
        self.assertTrue(state.exists(), "state file missing after a kill")
        self.assertIn("cycle", json.loads(state.read_text()))

    def test_the_status_file_says_killed(self):
        out = self._run(allowance=100.0, pct=10.0, cost_usd=25.0)
        status = (out["inst"] / "output" / "exploration_status.md").read_text()
        self.assertIn("killed_spend_limit", status)

    def test_the_end_of_run_pipeline_is_skipped(self):
        """The difference that makes this a kill, not loop.max_cost_usd."""
        out = self._run(
            allowance=100.0, pct=10.0, cost_usd=25.0, final_agents=True,
        )
        out["final_auditor"].assert_not_called()
        out["final_reporter"].assert_not_called()
        out["curator"].assert_not_called()

    def test_a_run_under_the_cap_finishes_normally(self):
        """The negative control: no kill, no marker, no exception."""
        out = self._run(
            allowance=10_000.0, pct=100.0, cost_usd=0.01,
            loop_extra="", final_agents=True,
        )
        self.assertIsNone(out["kill"])
        self.assertFalse((out["inst"] / "output" / sl.MARKER_FILENAME).exists())
        self.assertIn("researcher", [s["agent"] for s in out["seen"]])

    def test_the_feature_off_never_kills_however_much_is_spent(self):
        seen = []
        td = tempfile.mkdtemp()
        score, config, inst = _write_files(Path(td), loop_extra="  max_cycles: 2\n")
        # No usage_allowance block at all — the shipped default.
        with patch(
            "long_exposure.exploration._call_exploration_agent",
            _fake_agent_factory(seen, cost_usd=10_000.0),
        ):
            run_exploration(
                score_path=str(score),
                config_path=str(config),
                output_dir=inst / "output",
                state_path=inst / "exploration_state.json",
                task_override=None,
                instance_dir=inst,
            )
        self.assertIsNone(sl.tripped())
        self.assertFalse((inst / "output" / sl.MARKER_FILENAME).exists())

    def test_the_usage_summary_carries_the_allowance_block(self):
        out = self._run(allowance=100.0, pct=10.0, cost_usd=25.0)
        status = (out["inst"] / "output" / "exploration_status.md").read_text()
        self.assertIn("Run spend limit", status)
        self.assertIn("operator-declared", status)
        self.assertIn("not a meter reading", status)


class TripwireConcurrencyTests(unittest.TestCase):
    """Spend is recorded from the cycle loop AND the manager poller thread.

    manager.py calls _call_agent_with_rotation -> _record_usage -> check, and
    _manager_loop runs in a daemon thread under `launch --manager`, so two
    threads really can reach the tripwire at once.
    """

    def setUp(self):
        sl.reset()

    def tearDown(self):
        sl.reset()

    def test_exactly_one_trip_record_under_contention(self):
        import threading

        cfg = _cfg()
        records = []

        def tripper(tid):
            # _cfg() is a $80 cap; every thread is over it, so they all race
            # to be the one that trips.
            for _ in range(500):
                r = sl.check(100.0 + tid, cfg, source=f"t{tid}")
                if r:
                    records.append(r)

        threads = [threading.Thread(target=tripper, args=(t,)) for t in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertTrue(records)
        distinct = {
            (r["source"], r["observed_usd"], r["tripped_at"]) for r in records
        }
        self.assertEqual(len(distinct), 1, f"multiple trip records: {distinct}")
        self.assertEqual(sl.tripped()["observed_usd"], records[0]["observed_usd"])

    def test_reset_and_check_interleaved_never_raise(self):
        import threading

        cfg = _cfg()
        errors = []

        def churn():
            for i in range(2000):
                try:
                    if i % 7 == 0:
                        sl.reset()
                    sl.check(80.0, cfg, source="churn")
                    sl.tripped()
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

        threads = [threading.Thread(target=churn) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


class StaleMarkerTests(unittest.TestCase):
    """A clean resume must not still look killed."""

    def setUp(self):
        sl.reset()

    def test_clear_marker_removes_it(self):
        with TemporaryDirectory() as td:
            sl.check(80.0, _cfg(), source="t")
            sl.write_marker(td, cycle=1)
            self.assertTrue(sl.marker_path(td).exists())
            self.assertTrue(sl.clear_marker(td))
            self.assertFalse(sl.marker_path(td).exists())

    def test_clear_marker_is_a_no_op_when_absent(self):
        with TemporaryDirectory() as td:
            self.assertFalse(sl.clear_marker(td))

    def test_clear_marker_survives_an_unlink_failure(self):
        with TemporaryDirectory() as td:
            sl.check(80.0, _cfg(), source="t")
            sl.write_marker(td, cycle=1)
            with mock.patch.object(Path, "unlink", side_effect=OSError("ro")):
                self.assertFalse(sl.clear_marker(td))

    def test_a_clean_run_clears_a_marker_from_a_killed_one(self):
        """End to end: kill at a low cap, raise it, resume, marker gone."""
        import tempfile
        from unittest.mock import patch

        from long_exposure.exploration import SpendLimitKill, run_exploration
        from tests.test_run_switches import _write_files

        td = Path(tempfile.mkdtemp())
        (td / "run").mkdir()
        score, config, inst = _write_files(td / "run", loop_extra="  max_cycles: 6\n")
        base = config.read_text()

        def write_cfg(pct):
            config.write_text(
                base
                + "usage_allowance:\n  enabled: true\n"
                + "  weekly_allowance_usd: 100\n"
                + f"  run_pct: {pct}\n"
            )

        def fake(agent_name, agent_def, **kwargs):
            return {
                "agent": agent_name,
                "outputs": {agent_def["outputs"][0]: agent_name + " " + "x" * 2100},
                "usage": {"input_tokens": 100, "output_tokens": 600},
                "duration_ms": 5, "status": "ok", "error": None,
                "cost_usd": 1.0, "num_turns": 1, "tool_calls": 1,
            }

        def go():
            with patch(
                "long_exposure.exploration._call_exploration_agent", fake
            ):
                run_exploration(
                    score_path=str(score), config_path=str(config),
                    output_dir=inst / "output",
                    state_path=inst / "exploration_state.json",
                    task_override=None, instance_dir=inst,
                )

        write_cfg(2)
        sl.reset()
        with self.assertRaises(SpendLimitKill):
            go()
        self.assertTrue((inst / "output" / sl.MARKER_FILENAME).exists())

        write_cfg(90)  # room for the remaining cycles
        sl.reset()
        go()
        self.assertFalse(
            (inst / "output" / sl.MARKER_FILENAME).exists(),
            "a clean resume still looks killed",
        )


class FanOutKillTests(unittest.TestCase):
    """The barrier must TERMINATE clones on a trip, not just signal them.

    A stop file is honoured at a clone's next cycle boundary, so a clone in
    the middle of a long agent turn keeps spending until it finishes — up to
    FANOUT_CAP_SECONDS (10h). For a limit whose purpose is to stop spending,
    that is the opposite of a kill.

    Uses real subprocesses so poll()/killpg behave as in production.
    """

    def setUp(self):
        sl.reset()

    def tearDown(self):
        sl.reset()
        from long_exposure import telemetry

        telemetry.configure({"telemetry": {"enabled": False}}, None, None)
        for _k, _cd, p in getattr(self, "_spawned", []):
            if p.poll() is None:
                try:
                    import os
                    import signal

                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    pass

    def test_a_trip_terminates_running_clones_promptly(self):
        import json
        import os
        import subprocess
        import tempfile
        import threading
        import time
        from unittest.mock import patch

        import long_exposure.exploration as exploration
        from long_exposure import fanout

        sb = Path(tempfile.mkdtemp())
        root = sb / "root"
        root.mkdir()
        ws = sb / "ws"
        ws.mkdir()
        config = {
            "usage_allowance": {
                "enabled": True, "weekly_allowance_usd": 100, "run_pct": 10,
            },
            "working_directory": str(ws),
            "llm_provider": "local", "model": "test",
        }

        self._spawned = []

        def fake_spawn(cdir, fork_id, k, score_path, config_path, **kw):
            p = subprocess.Popen(["sleep", "60"], start_new_session=True)
            self._spawned.append((k, Path(cdir), p))
            return p

        stop = threading.Event()

        def clone_writer():
            """Report clone spend the way a live clone's status write does."""
            time.sleep(0.2)
            for step in range(1, 40):
                if stop.is_set():
                    return
                for _k, cdir, _p in list(self._spawned):
                    out = Path(cdir) / "output"
                    out.mkdir(parents=True, exist_ok=True)
                    (out / "usage_summary.json").write_text(
                        json.dumps({"totals": {"cost_usd": step * 2.0}})
                    )
                time.sleep(0.1)

        # Root ledger stays well under the cap, so ONLY clone spend can trip
        # it — the property that distinguishes this from the root-side check.
        exploration._usage.load({})
        exploration._usage.record(
            "researcher",
            {"usage": {"input_tokens": 10, "output_tokens": 10},
             "cost_usd": 0.5, "status": "ok", "num_turns": 1, "tool_calls": 1},
            provider="local", model="test", config=config,
        )
        self.assertLess(exploration._usage.total_cost_usd(), 10.0)

        threading.Thread(target=clone_writer, daemon=True).start()
        started = time.monotonic()
        try:
            with patch.object(fanout, "_spawn_clone", fake_spawn), \
                 patch.object(fanout, "SPEND_KILL_GRACE_SECONDS", 1):
                fanout._run_fanout_conductor(
                    branches=[
                        {"objective": "a", "output_artifact": "a.md"},
                        {"objective": "b", "output_artifact": "b.md"},
                    ],
                    score_path="long_exposure/exploration-score.yaml",
                    config_path=None, root_instance_dir=root, data_dir=root,
                    task="t", parent_results={}, parent_agent_sessions={},
                    parent_agent_summaries={}, working_directory=str(ws),
                    parent_run_id="run-t", reporter_def=None,
                    config=config,
                    loop_cfg={"barrier_preempt_timeout_seconds": 0,
                              "min_clone_cycles_before_preempt": 0},
                )
        finally:
            stop.set()
        elapsed = time.monotonic() - started

        trip = sl.tripped()
        self.assertIsNotNone(trip, "clone spend did not trip the limit")
        self.assertEqual(trip["source"], "fanout_barrier")
        self.assertGreater(trip["clone_usd"], 0.0)
        self.assertLess(trip["root_usd"], 10.0)

        # The clones were `sleep 60`. If the barrier had only signalled them,
        # it would have waited the full 60s.
        self.assertLess(
            elapsed, 30.0,
            f"barrier took {elapsed:.1f}s — it waited for the clones instead "
            "of terminating them",
        )
        for _k, _cd, p in self._spawned:
            self.assertIsNotNone(
                p.poll(), "a clone process outlived the barrier",
            )

    def test_the_grace_period_is_short_by_design(self):
        """The 10h-cap path allows 120s; spending that long here is absurd."""
        from long_exposure import fanout

        self.assertLessEqual(fanout.SPEND_KILL_GRACE_SECONDS, 30)
        self.assertGreater(fanout.SPEND_KILL_GRACE_SECONDS, 0)
