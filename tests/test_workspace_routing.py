import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from long_exposure import paths
from long_exposure.auditing import _run_final_auditor
from long_exposure.exploration import _run_reporter
from long_exposure.reporting import _run_final_reporter
from long_exposure.tools import org_check


def _ok_result(output_name: str, text: str = "# Stage\n\ncontent") -> dict:
    return {
        "status": "ok",
        "outputs": {output_name: text},
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "duration_ms": 1,
    }


class WorkspaceRoutingTests(unittest.TestCase):
    def test_workspace_root_invariants_stay_at_root(self):
        with tempfile.TemporaryDirectory() as td:
            config = {"working_directory": td}
            root = Path(td).resolve()

            self.assertEqual(paths.final_report_path(config).parent, root)
            self.assertEqual(paths.final_report_pdf_path(config).parent, root)
            self.assertEqual(paths.final_audit_report_path(config).parent, root)
            self.assertEqual(paths.final_audit_pdf_path(config).parent, root)
            self.assertEqual(paths.final_audit_summary_path(config).parent, root)

    def test_ensure_layout_creates_all_managed_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = {"working_directory": str(root)}

            paths.ensure_layout(config)

            self.assertTrue((root / "reports").is_dir())
            self.assertTrue((root / "reports" / "cycles").is_dir())
            self.assertTrue((root / "reports" / "final").is_dir())
            self.assertTrue((root / "audits").is_dir())
            self.assertTrue((root / "audits" / "final").is_dir())
            self.assertTrue((root / "audits" / "final" / "stages").is_dir())

    def test_cycle_reporter_writes_reports_under_cycles_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = {"working_directory": str(root)}
            agent_def = {"outputs": ["report"]}

            with (
                patch("long_exposure.exploration._call_agent_with_rotation",
                      return_value=_ok_result("report", "# Report\n\nbody")),
                patch("long_exposure.exploration._store_agent_output",
                      return_value="session"),
                patch("long_exposure.exploration._render_report_pdf"),
            ):
                _run_reporter(
                    agent_def, "task", config, {"run_id": "run-test"}, {},
                    {}, {}, None, 1, None, 1, 1, [], 1000, 900,
                )

            self.assertTrue((root / "reports" / "cycles" / "report_cycles_1-1.md").exists())
            self.assertFalse((root / "reports" / "report_cycles_1-1.md").exists())
            self.assertFalse((root / "report_cycles_1-1.md").exists())

    def test_final_reporter_uses_scratch_dir_and_commit_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = {"working_directory": str(root)}
            paths.ensure_layout(config)
            (root / "reports" / "cycles" / "report_cycles_1-1.md").write_text("# Cycle\n\nbody")
            agent_def = {"outputs": ["final_report_stage"]}

            def fake_call(**kwargs):
                expected = Path(kwargs["results"]["expected_file"])
                expected.parent.mkdir(parents=True, exist_ok=True)
                expected.write_text("# Final Report\n\nbody\n")
                return _ok_result("final_report_stage", "wrote file")

            with (
                patch("long_exposure.reporting._call_agent_with_rotation", fake_call),
                patch("long_exposure.reporting._store_agent_output",
                      return_value="session"),
                patch("long_exposure.reporting._render_final_pdf",
                      return_value=False),
            ):
                _run_final_reporter(
                    agent_def, "task", config, {"run_id": "run-test"}, {},
                    None, 1, None, 1000, 900,
                )

            self.assertTrue((root / "reports" / "final" / "outline.md").exists())
            self.assertTrue((root / "reports" / "final" / "draft.md").exists())
            self.assertTrue((root / "final_report.md").exists())
            self.assertTrue((root / "final_report.committed").exists())
            self.assertFalse((root / "final_report_outline.md").exists())
            self.assertFalse((root / "final_report_draft.md").exists())

    def test_final_reporter_detects_committed_delta_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = {"working_directory": str(root)}
            paths.ensure_layout(config)
            (root / "reports" / "cycles" / "report_cycles_1-1.md").write_text("# Cycle\n\nbody")
            (root / "final_report.md").write_text("# Prior Final\n\nbaseline\n")
            (root / "final_report.committed").write_text(json.dumps({
                "committed_at": "2000-01-01T00:00:00+00:00",
                "run_id": "run-test",
            }))
            agent_def = {"outputs": ["final_report_stage"]}

            def fake_call(**kwargs):
                expected = Path(kwargs["results"]["expected_file"])
                expected.parent.mkdir(parents=True, exist_ok=True)
                expected.write_text("# Final Report\n\nbody\n")
                return _ok_result("final_report_stage", "wrote file")

            with (
                patch("long_exposure.reporting._call_agent_with_rotation", fake_call),
                patch("long_exposure.reporting._store_agent_output",
                      return_value="session"),
                patch("long_exposure.reporting._render_final_pdf",
                      return_value=False),
            ):
                _run_final_reporter(
                    agent_def, "task", config, {"run_id": "run-test"}, {},
                    None, 1, None, 1000, 900,
                )

            mode = json.loads((root / "reports" / "final" / "run_mode.json").read_text())
            self.assertEqual(mode["mode"], "delta")
            self.assertEqual(mode["detection_source"], "marker")

    def test_final_reporter_delta_does_not_commit_unchanged_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = {"working_directory": str(root)}
            paths.ensure_layout(config)
            (root / "reports" / "cycles" / "report_cycles_1-1.md").write_text("# Cycle\n\nbody")
            (root / "final_report.md").write_text("# Prior Final\n\nbaseline\n")
            original_marker = {
                "committed_at": "2000-01-01T00:00:00+00:00",
                "run_id": "run-test",
            }
            (root / "final_report.committed").write_text(json.dumps(original_marker))
            agent_def = {"outputs": ["final_report_stage"]}

            def fake_call(**kwargs):
                expected = Path(kwargs["results"]["expected_file"])
                if expected.name != "final_report.md":
                    expected.parent.mkdir(parents=True, exist_ok=True)
                    expected.write_text("# Scratch\n\nbody\n")
                return _ok_result("final_report_stage", "status only")

            with (
                patch("long_exposure.reporting._call_agent_with_rotation", fake_call),
                patch("long_exposure.reporting._store_agent_output",
                      return_value="session"),
                patch("long_exposure.reporting._render_final_pdf",
                      return_value=False),
            ):
                _run_final_reporter(
                    agent_def, "task", config, {"run_id": "run-test"}, {},
                    None, 1, None, 1000, 900,
                )

            self.assertEqual((root / "final_report.md").read_text(), "# Prior Final\n\nbaseline\n")
            self.assertEqual(json.loads((root / "final_report.committed").read_text()), original_marker)

    def test_final_auditor_uses_audit_scratch_dir_and_commit_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = {"working_directory": str(root)}
            paths.ensure_layout(config)
            (root / "reports" / "cycles" / "report_cycles_1-1.md").write_text("# Cycle\n\nbody")
            agent_def = {"outputs": ["final_audit_stage"]}

            def fake_call(**kwargs):
                expected = Path(kwargs["results"]["expected_file"])
                expected.parent.mkdir(parents=True, exist_ok=True)
                expected.write_text("# Audit Stage\n\nbody\n")
                if expected.name == "final_audit_report.md":
                    (root / "final_audit_summary.json").write_text(json.dumps({
                        "run_id": "run-test",
                        "findings": {"CRITICAL": 0, "MODERATE": 0, "MINOR": 0},
                    }))
                return _ok_result("final_audit_stage", "wrote file")

            with (
                patch("long_exposure.auditing._call_agent_with_rotation", fake_call),
                patch("long_exposure.auditing._store_agent_output",
                      return_value="session"),
                patch("long_exposure.reporting.render_pdf", return_value=False),
            ):
                _run_final_auditor(
                    agent_def, "task", config, {"run_id": "run-test"}, {},
                    None, 1, None, 1000, 900,
                )

            self.assertTrue((root / "audits" / "final" / "explore.md").exists())
            self.assertTrue(list((root / "audits" / "final" / "stages").glob("*.md")))
            self.assertTrue((root / "final_audit_report.md").exists())
            self.assertTrue((root / "final_audit_report.committed").exists())
            self.assertFalse((root / "final_audit_explore.md").exists())

    def test_final_auditor_detects_committed_delta_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = {"working_directory": str(root)}
            paths.ensure_layout(config)
            (root / "reports" / "cycles" / "report_cycles_1-1.md").write_text("# Cycle\n\nbody")
            (root / "final_audit_report.md").write_text("# Prior Audit\n\nbaseline\n")
            (root / "final_audit_report.committed").write_text(json.dumps({
                "committed_at": "2000-01-01T00:00:00+00:00",
                "run_id": "run-test",
            }))
            agent_def = {"outputs": ["final_audit_stage"]}
            seen_directives = []

            def fake_call(**kwargs):
                seen_directives.append(kwargs["results"].get("directive", ""))
                expected = Path(kwargs["results"]["expected_file"])
                expected.parent.mkdir(parents=True, exist_ok=True)
                expected.write_text("# Audit Stage\n\nbody\n")
                if expected.name == "final_audit_report.md":
                    (root / "final_audit_summary.json").write_text("{}")
                return _ok_result("final_audit_stage", "wrote file")

            with (
                patch("long_exposure.auditing._call_agent_with_rotation", fake_call),
                patch("long_exposure.auditing._store_agent_output",
                      return_value="session"),
                patch("long_exposure.reporting.render_pdf", return_value=False),
            ):
                _run_final_auditor(
                    agent_def, "task", config, {"run_id": "run-test"}, {},
                    None, 1, None, 1000, 900,
                )

            mode = json.loads((root / "audits" / "final" / "run_mode.json").read_text())
            self.assertEqual(mode["mode"], "delta")
            self.assertEqual(mode["detection_source"], "marker")
            self.assertTrue(any("DELTA-AUDIT MODE" in item for item in seen_directives))

    def test_final_auditor_delta_does_not_commit_unchanged_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = {"working_directory": str(root)}
            paths.ensure_layout(config)
            (root / "reports" / "cycles" / "report_cycles_1-1.md").write_text("# Cycle\n\nbody")
            (root / "final_audit_report.md").write_text("# Prior Audit\n\nbaseline\n")
            original_marker = {
                "committed_at": "2000-01-01T00:00:00+00:00",
                "run_id": "run-test",
            }
            (root / "final_audit_report.committed").write_text(json.dumps(original_marker))
            agent_def = {"outputs": ["final_audit_stage"]}

            def fake_call(**kwargs):
                expected = Path(kwargs["results"]["expected_file"])
                if expected.name != "final_audit_report.md":
                    expected.parent.mkdir(parents=True, exist_ok=True)
                    expected.write_text("# Audit Stage\n\nbody\n")
                return _ok_result("final_audit_stage", "status only")

            with (
                patch("long_exposure.auditing._call_agent_with_rotation", fake_call),
                patch("long_exposure.auditing._store_agent_output",
                      return_value="session"),
                patch("long_exposure.reporting.render_pdf", return_value=False),
            ):
                _run_final_auditor(
                    agent_def, "task", config, {"run_id": "run-test"}, {},
                    None, 1, None, 1000, 900,
                )

            self.assertEqual((root / "final_audit_report.md").read_text(), "# Prior Audit\n\nbaseline\n")
            self.assertEqual(
                json.loads((root / "final_audit_report.committed").read_text()),
                original_marker,
            )

    def test_org_check_accepts_new_dirs_and_notes_legacy_stage_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths.ensure_layout({"working_directory": str(root)})
            (root / "STRUCTURE.md").write_text("# Structure\n")
            (root / "final_report_outline.md").write_text("legacy")

            findings = org_check.run(root)

            self.assertFalse(findings.errors)
            self.assertTrue(
                any("legacy root stage artifact" in note for note in findings.notes)
            )


if __name__ == "__main__":
    unittest.main()
