import json
import os
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from long_exposure import cli, telemetry


class TelemetryTests(unittest.TestCase):
    def tearDown(self):
        telemetry.configure({"telemetry": {"enabled": False}}, None, None)

    def test_disabled_mode_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            telemetry.configure({"telemetry": {"enabled": False}}, Path(td), "run-x")
            telemetry.emit("run_start", phase="run", status="ok")
            self.assertFalse((Path(td) / "telemetry" / "events.jsonl").exists())

    def test_enabled_mode_writes_event_and_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            telemetry.configure({"telemetry": {"enabled": True}}, root, "run-x")
            telemetry.emit(
                "agent_call_end",
                phase="agent",
                cycle=1,
                agent="researcher",
                provider="local",
                model="test",
                status="ok",
                data={"usage": {"input_tokens": 2, "output_tokens": 3}},
            )
            events = (root / "telemetry" / "events.jsonl").read_text().splitlines()
            manifest = json.loads((root / "telemetry" / "telemetry_manifest.json").read_text())

        self.assertEqual(len(events), 1)
        record = json.loads(events[0])
        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(record["run_id"], "run-x")
        self.assertEqual(record["agent"], "researcher")
        self.assertEqual(manifest["privacy"]["include_prompt_text"], False)

    def test_event_size_limit_truncates_data(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            telemetry.configure({
                "telemetry": {
                    "enabled": True,
                    "max_event_bytes": 500,
                    "max_text_field_chars": 10_000,
                }
            }, root, "run-x")
            telemetry.emit("large", data={"blob": "x" * 10_000})
            record = json.loads((root / "telemetry" / "events.jsonl").read_text())
        self.assertTrue(record["data"]["truncated"])
        self.assertIn("original_event_hash", record["data"])

    def test_redact_paths_when_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            telemetry.configure({
                "telemetry": {"enabled": True, "redact_paths": True}
            }, root, "run-x")
            telemetry.emit("path_event", data={"path": "/home/user/.claude-acct"})
            record = json.loads((root / "telemetry" / "events.jsonl").read_text())
        self.assertIn("[path:", record["data"]["path"])
        self.assertNotIn("/home/user", record["data"]["path"])

    def test_account_usage_keys_are_hashed(self):
        redacted = telemetry.redact_account_usage({
            "/home/user/.claude-acct": {"tokens_input": 7}
        })
        key = next(iter(redacted))
        self.assertTrue(key.startswith("account:"))
        self.assertNotIn("/home/user", key)
        self.assertEqual(redacted[key]["tokens_input"], 7)

    def test_sensitive_text_fields_are_omitted_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            telemetry.configure({"telemetry": {"enabled": True}}, root, "run-x")
            telemetry.emit("privacy", data={
                "prompt": "secret prompt",
                "prompt_text": "secret prompt text",
                "response": "secret response",
                "raw_response": "secret raw response",
                "stdout": "secret stdout",
                "env": {"TOKEN": "secret"},
                "nested": {"messages": ["private"]},
                "safe": "kept",
            })
            record = json.loads((root / "telemetry" / "events.jsonl").read_text())

        self.assertEqual(record["data"]["prompt"], "[omitted:prompt_text_disabled]")
        self.assertEqual(record["data"]["prompt_text"], "[omitted:prompt_text_disabled]")
        self.assertEqual(record["data"]["response"], "[omitted:response_text_disabled]")
        self.assertEqual(record["data"]["raw_response"], "[omitted:response_text_disabled]")
        self.assertEqual(record["data"]["stdout"], "[omitted:tool_stdout_disabled]")
        self.assertEqual(record["data"]["env"], "[omitted:env_redaction_enabled]")
        self.assertEqual(record["data"]["nested"]["messages"], "[omitted:prompt_text_disabled]")
        self.assertEqual(record["data"]["safe"], "kept")
        self.assertNotIn("secret", json.dumps(record))

    def test_sensitive_text_fields_can_be_explicitly_opted_in(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            telemetry.configure({
                "telemetry": {
                    "enabled": True,
                    "include_prompt_text": True,
                    "include_response_text": True,
                    "include_tool_stdout": True,
                    "redact_env": False,
                }
            }, root, "run-x")
            telemetry.emit("privacy", data={
                "prompt": "prompt text",
                "response": "response text",
                "stdout": "tool output",
                "env": {"TOKEN": "value"},
            })
            record = json.loads((root / "telemetry" / "events.jsonl").read_text())

        self.assertEqual(record["data"]["prompt"], "prompt text")
        self.assertEqual(record["data"]["response"], "response text")
        self.assertEqual(record["data"]["stdout"], "tool output")
        self.assertEqual(record["data"]["env"]["TOKEN"], "value")

    def test_bad_output_path_fails_gracefully(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "not-a-dir"
            bad.write_text("file")
            telemetry.configure({
                "telemetry": {"enabled": True, "output_dir": str(bad)}
            }, Path(td), "run-x")
            telemetry.emit("run_start", status="ok")
            self.assertTrue(bad.exists())

    def test_summarize_counts_events_and_usage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            telemetry.configure({"telemetry": {"enabled": True}}, root, "run-x")
            telemetry.emit_agent_result(
                "worker",
                {
                    "status": "ok",
                    "duration_ms": 1,
                    "usage": {"input_tokens": 4, "output_tokens": 5},
                    "outputs": {"work_output": "ignored"},
                },
                cycle=2,
                provider="local",
                model="test",
                context_window=20,
            )
            summary = telemetry.summarize(root)
            saved = json.loads((root / "telemetry" / "rollups" / "summary.json").read_text())
            lessons = (root / "telemetry" / "lessons" / "lessons_summary.md").read_text()
            events_text = (root / "telemetry" / "events.jsonl").read_text()
            summary_md = (root / "telemetry" / "rollups" / "summary.md").read_text()

        self.assertEqual(summary["events"], 1)
        self.assertEqual(summary["usage"]["input_tokens"], 4)
        self.assertEqual(summary["context"]["max_tokens"], 9)
        self.assertEqual(
            summary["snapshot"]["events_sha256"],
            hashlib.sha256(events_text.encode("utf-8")).hexdigest(),
        )
        self.assertIn("events.jsonl", summary["snapshot"]["events_path"])
        self.assertIn("Event snapshot SHA-256", summary_md)
        self.assertEqual(saved["by_agent"]["worker"], 1)
        self.assertIn("Agent Review Prompt", lessons)

    def test_summarize_honors_config_output_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out_dir = root / "custom-telemetry"
            telemetry.configure(
                {"telemetry": {"enabled": True, "output_dir": str(out_dir)}},
                root,
                "run-x",
            )
            telemetry.emit("run_start", phase="run", status="ok")
            summary = telemetry.summarize(root, config={"telemetry": {"output_dir": str(out_dir)}})
            saved = json.loads((out_dir / "rollups" / "summary.json").read_text())

        self.assertEqual(summary["events"], 1)
        self.assertEqual(saved["events"], 1)

    def test_summarize_counts_usage_aliases(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            telemetry.configure({"telemetry": {"enabled": True}}, root, "run-x")
            telemetry.emit_agent_result(
                "worker",
                {
                    "status": "ok",
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 2,
                        "cached_input_tokens": 3,
                        "reasoning_output_tokens": 4,
                    },
                    "outputs": {},
                },
                context_window=10,
            )
            summary = telemetry.summarize(root)

        self.assertEqual(summary["usage"]["cache_read_input_tokens"], 3)
        self.assertEqual(summary["usage"]["reasoning_output_tokens"], 4)
        self.assertEqual(summary["context"]["max_tokens"], 3)

    def test_cli_telemetry_summarize(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            telemetry.configure({"telemetry": {"enabled": True}}, root, "run-x")
            telemetry.emit("run_start", phase="run", status="ok")
            with patch("builtins.print") as mock_print:
                rc = cli.main(["--instance-dir", str(root), "telemetry", "summarize"])
        self.assertEqual(rc, 0)
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        self.assertIn('"events": 1', printed)

    def test_cli_telemetry_summarize_honors_config_output_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out_dir = root / "custom-telemetry"
            config_path = root / "config.yaml"
            config_path.write_text(f"telemetry:\n  output_dir: {out_dir}\n")
            telemetry.configure(
                {"telemetry": {"enabled": True, "output_dir": str(out_dir)}},
                root,
                "run-x",
            )
            telemetry.emit("run_start", phase="run", status="ok")
            with patch("builtins.print") as mock_print:
                rc = cli.main([
                    "--config",
                    str(config_path),
                    "--instance-dir",
                    str(root),
                    "telemetry",
                    "summarize",
                ])

        self.assertEqual(rc, 0)
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        self.assertIn('"events": 1', printed)

    def test_cli_telemetry_summarize_dir_override(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out_dir = root / "custom-telemetry"
            telemetry.configure(
                {"telemetry": {"enabled": True, "output_dir": str(out_dir)}},
                root,
                "run-x",
            )
            telemetry.emit("run_start", phase="run", status="ok")
            with patch("builtins.print") as mock_print:
                rc = cli.main(["telemetry", "summarize", "--telemetry-dir", str(out_dir)])

        self.assertEqual(rc, 0)
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        self.assertIn('"events": 1', printed)

    def test_env_override_enables(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"LONG_EXPOSURE_TELEMETRY": "1"}, clear=False):
                telemetry.configure({"telemetry": {"enabled": False}}, Path(td), "run-x")
                telemetry.emit("run_start", status="ok")
            self.assertTrue((Path(td) / "telemetry" / "events.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
