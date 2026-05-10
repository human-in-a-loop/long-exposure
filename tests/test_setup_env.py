import unittest
from unittest.mock import patch

from long_exposure.tools import setup_env


class SetupEnvTests(unittest.TestCase):
    def test_missing_required_reports_only_required_tools(self):
        report = {
            "tools": [
                {"name": "pandoc", "ok": True},
                {"name": "tectonic", "ok": False},
            ]
        }
        self.assertEqual(setup_env.missing_required(report), ["tectonic"])

    def test_apt_install_command_uses_required_missing_tools(self):
        def fake_which(name):
            return "/usr/bin/apt-get" if name == "apt-get" else None

        with patch("platform.system", return_value="Linux"):
            with patch("long_exposure.tools.setup_env._which", fake_which):
                with patch("long_exposure.tools.setup_env._sudo_prefix", return_value=["sudo"]):
                    cmds, note = setup_env._pkg_manager_install_cmds(["pandoc", "tectonic"])

        self.assertIsNone(note)
        self.assertEqual(cmds[0], ["sudo", "apt-get", "update"])
        self.assertEqual(cmds[1], ["sudo", "apt-get", "install", "-y", "pandoc"])
        self.assertEqual(cmds[2], ["sudo", "apt-get", "install", "-y", "tectonic"])

    def test_unsupported_platform_returns_manual_note(self):
        with patch("platform.system", return_value="Plan9"):
            cmds, note = setup_env._pkg_manager_install_cmds(["pandoc"])
        self.assertEqual(cmds, [])
        self.assertIn("Install manually", note)

    def test_doctor_forces_check_mode(self):
        captured = {}

        def fake_setup(argv):
            captured["argv"] = argv
            return 0

        with patch("long_exposure.tools.setup_env.setup_main", fake_setup):
            rc = setup_env.doctor_main(["--json"])

        self.assertEqual(rc, 0)
        self.assertEqual(captured["argv"], ["--check", "--json"])

    def test_doctor_preserves_console_script_args(self):
        captured = {}

        def fake_setup(argv):
            captured["argv"] = argv
            return 0

        with patch("sys.argv", ["long-exposure-doctor", "--json"]):
            with patch("long_exposure.tools.setup_env.setup_main", fake_setup):
                rc = setup_env.doctor_main()

        self.assertEqual(rc, 0)
        self.assertEqual(captured["argv"], ["--check", "--json"])


if __name__ == "__main__":
    unittest.main()
