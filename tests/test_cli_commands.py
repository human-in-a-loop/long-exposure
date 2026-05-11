import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from long_exposure import cli


class CliCommandTests(unittest.TestCase):
    def test_unknown_subcommand_is_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            cli.main(["does-not-exist"])
        self.assertEqual(cm.exception.code, 2)

    def test_start_delegates_to_run_exploration_with_instance_paths(self):
        with tempfile.TemporaryDirectory() as td:
            inst = Path(td) / "inst"
            captured = {}

            def fake_run(**kwargs):
                captured.update(kwargs)

            with patch("long_exposure.exploration.run_exploration", fake_run):
                rc = cli.main([
                    "--instance-dir", str(inst),
                    "start", "audit", "this",
                ])

        self.assertEqual(rc, 0)
        self.assertEqual(captured["task_override"], "audit this")
        self.assertEqual(captured["state_path"], inst / "exploration_state.json")
        self.assertEqual(captured["output_dir"], inst / "output")

    def test_launch_passes_config_to_doctor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = root / "config.yaml"
            cfg.write_text("llm_provider: codex\n")
            seen = {}

            def fake_doctor(argv):
                seen["argv"] = argv
                return 0

            with (
                patch("long_exposure.cli.doctor_main", side_effect=fake_doctor),
                patch("long_exposure.cli.load_config",
                      return_value={"llm_provider": "codex", "working_directory": str(root)}),
                patch("long_exposure.cli.exploration.run_exploration"),
            ):
                rc = cli.main([
                    "--config", str(cfg),
                    "--instance-dir", str(root / "instance"),
                    "launch",
                    "task",
                ])

        self.assertEqual(rc, 0)
        self.assertEqual(seen["argv"], ["--json", "--config", str(cfg)])

    def test_resume_from_archive_restores_state_before_running(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inst = root / "inst"
            inst.mkdir()
            archive = root / "archive.json"
            archive.write_text(json.dumps({"cycle": 7, "results": {}}))
            captured = {}

            def fake_run(**kwargs):
                captured.update(kwargs)

            with patch("long_exposure.exploration.run_exploration", fake_run):
                rc = cli.main([
                    "--instance-dir", str(inst),
                    "resume", "--from-archive", str(archive),
                ])

            active = inst / "exploration_state.json"
            self.assertEqual(rc, 0)
            self.assertTrue(active.exists())
            self.assertEqual(json.loads(active.read_text())["cycle"], 7)
            self.assertIsNone(captured["task_override"])

    def test_stop_and_guide_write_instance_scoped_files(self):
        with tempfile.TemporaryDirectory() as td:
            inst = Path(td) / "inst"
            self.assertEqual(cli.main(["--instance-dir", str(inst), "stop"]), 0)
            self.assertTrue((inst / "long-exposure.stop").exists())

            self.assertEqual(
                cli.main(["--instance-dir", str(inst), "guide", "focus", "now"]),
                0,
            )
            self.assertEqual(
                (inst / "long-exposure.guide").read_text().strip(),
                "focus now",
            )

    def test_status_prints_latest_manager_notice(self):
        with tempfile.TemporaryDirectory() as td:
            inst = Path(td) / "inst"
            out = inst / "output"
            out.mkdir(parents=True)
            (out / "exploration_status.md").write_text("# Exploration Status\n")
            (inst / "manager_notifications.jsonl").write_text(json.dumps({
                "cycle": 2,
                "verdict": "act",
                "event_class": "mechanism-overdue",
                "summary": "tighten mechanism",
            }) + "\n")

            with patch("builtins.print") as mock_print:
                rc = cli.main(["--instance-dir", str(inst), "status"])

        self.assertEqual(rc, 0)
        printed = "\n".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("Latest Manager Notice", printed)
        self.assertIn("tighten mechanism", printed)

    def test_cli_install_writes_adapter_files(self):
        with tempfile.TemporaryDirectory() as td:
            rc = cli.main(["cli-install", "--target", "all", "--directory", td])
            root = Path(td)
            self.assertEqual(rc, 0)
            self.assertTrue((root / ".claude" / "commands" / "long-exposure.md").exists())
            self.assertTrue((root / ".codex" / "skills" / "long-exposure" / "SKILL.md").exists())
            self.assertTrue((root / "GEMINI.md").exists())


if __name__ == "__main__":
    unittest.main()
