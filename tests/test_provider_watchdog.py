import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from long_exposure.orchestrator import _run_cli_subprocess


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

    def test_provider_only_cpu_ticks_do_not_prevent_idle_watchdog(self):
        script = (
            "import time\n"
            "end = time.time() + 5\n"
            "while time.time() < end:\n"
            "    pass\n"
        )
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            _run_cli_subprocess(
                [sys.executable, "-c", script],
                stdin_text="",
                cwd=None,
                env={},
                timeout=30,
                idle_timeout=1,
                idle_poll=1,
            )
        self.assertLess(time.monotonic() - started, 4)

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

    def test_external_child_process_prevents_idle_watchdog_false_positive(self):
        script = "import subprocess; subprocess.run(['sleep', '2'], check=True)"
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


if __name__ == "__main__":
    unittest.main()
