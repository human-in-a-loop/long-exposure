import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from long_exposure.orchestrator import _activity_signature, _run_cli_subprocess


class ActivitySignatureTests(unittest.TestCase):
    def test_signature_buckets_cpu_ticks(self):
        self.assertEqual(_activity_signature((3, 42, True)), (3, True, 4))

    def test_tick_noise_below_granularity_is_not_progress(self):
        # A sleeping tree accrues a few scheduler-noise ticks; staying inside
        # one granularity bucket must not register as a signature change.
        self.assertEqual(
            _activity_signature((2, 5, False)),
            _activity_signature((2, 9, False)),
        )

    def test_real_cpu_accumulation_changes_signature(self):
        self.assertNotEqual(
            _activity_signature((2, 5, False)),
            _activity_signature((2, 105, False)),
        )

    def test_tree_shape_change_changes_signature(self):
        self.assertNotEqual(
            _activity_signature((1, 5, False)),
            _activity_signature((2, 5, False)),
        )


class ProviderWatchdogTests(unittest.TestCase):
    def test_idle_watchdog_kills_silent_provider_process(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            _run_cli_subprocess(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                stdin_text="",
                cwd=None,
                env={},
                timeout=30,
                idle_timeout=1,
                idle_poll=1,
            )
        self.assertLess(time.monotonic() - started, 4)

    def test_stdout_progress_prevents_idle_watchdog(self):
        script = (
            "import time\n"
            "for i in range(3):\n"
            "    print(i, flush=True)\n"
            "    time.sleep(0.6)\n"
        )
        result = _run_cli_subprocess(
            [sys.executable, "-c", script],
            stdin_text="",
            cwd=None,
            env={},
            timeout=30,
            idle_timeout=1,
            idle_poll=1,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("2", result.stdout)

    def test_provider_cpu_activity_prevents_idle_watchdog(self):
        # CPU accumulation IS the progress signal now: a tree doing real
        # work (even silently) keeps the watchdog satisfied.
        script = (
            "import time\n"
            "end = time.time() + 2.5\n"
            "while time.time() < end:\n"
            "    pass\n"
        )
        result = _run_cli_subprocess(
            [sys.executable, "-c", script],
            stdin_text="",
            cwd=None,
            env={},
            timeout=30,
            idle_timeout=1,
            idle_poll=1,
        )
        self.assertEqual(result.returncode, 0)

    def test_output_file_progress_prevents_idle_watchdog(self):
        with tempfile.TemporaryDirectory() as td:
            output_file = Path(td) / "out.txt"
            script = (
                "import pathlib, sys, time\n"
                "p = pathlib.Path(sys.argv[1])\n"
                "for i in range(3):\n"
                "    p.write_text(str(i))\n"
                "    time.sleep(0.6)\n"
            )
            result = _run_cli_subprocess(
                [sys.executable, "-c", script, str(output_file)],
                stdin_text="",
                cwd=None,
                env={},
                timeout=30,
                output_file=output_file,
                idle_timeout=1,
                idle_poll=1,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(output_file.read_text(), "2")

    def test_sleeping_external_child_does_not_prevent_idle_watchdog(self):
        # Regression: a long-lived but idle child (e.g. an MCP server
        # sleeping on a socket) used to refresh the watchdog on every poll
        # just by existing. A sleeping tree accumulates no CPU, so the idle
        # timeout must fire.
        script = "import subprocess; subprocess.run(['sleep', '15'], check=True)"
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            _run_cli_subprocess(
                [sys.executable, "-c", script],
                stdin_text="",
                cwd=None,
                env={},
                timeout=30,
                idle_timeout=2,
                idle_poll=1,
            )
        self.assertLess(time.monotonic() - started, 12)

    def test_working_external_child_prevents_idle_watchdog(self):
        # A genuinely working tool child accumulates CPU ticks and keeps the
        # watchdog satisfied even with no stdout/stderr progress.
        child = (
            "import time\n"
            "end = time.time() + 2.5\n"
            "while time.time() < end:\n"
            "    pass\n"
        )
        script = (
            "import subprocess, sys\n"
            f"subprocess.run([sys.executable, '-c', {child!r}], check=True)\n"
        )
        result = _run_cli_subprocess(
            [sys.executable, "-c", script],
            stdin_text="",
            cwd=None,
            env={},
            timeout=30,
            idle_timeout=1,
            idle_poll=1,
        )
        self.assertEqual(result.returncode, 0)

    def test_blocked_stdin_write_does_not_defeat_watchdog(self):
        # A >pipe-buffer prompt written to a child that never drains stdin
        # used to block the synchronous write before the watchdog loop ever
        # started. The stdin feed now runs on a daemon thread, so the idle
        # timeout still fires and the kill unblocks the writer.
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            _run_cli_subprocess(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin_text="x" * 1_000_000,
                cwd=None,
                env={},
                timeout=30,
                idle_timeout=1,
                idle_poll=1,
            )
        self.assertLess(time.monotonic() - started, 10)

    def test_timeout_carries_output_tails_for_forensics(self):
        script = (
            "import sys, time\n"
            "print('partial stdout before hang', flush=True)\n"
            "print('stderr breadcrumb', file=sys.stderr, flush=True)\n"
            "time.sleep(30)\n"
        )
        with self.assertRaises(subprocess.TimeoutExpired) as ctx:
            _run_cli_subprocess(
                [sys.executable, "-c", script],
                stdin_text="",
                cwd=None,
                env={},
                timeout=30,
                idle_timeout=2,
                idle_poll=1,
            )
        self.assertIn("partial stdout before hang", str(ctx.exception.output))
        self.assertIn("stderr breadcrumb", str(ctx.exception.stderr))


if __name__ == "__main__":
    unittest.main()
