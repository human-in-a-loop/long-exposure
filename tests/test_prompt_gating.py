"""Headless agent prompts must not carry interactive-only guidance.

The operating protocol used to inject the REPL's slash-command text, the
context-gems section (a REPL-only feature), "ask the user" instructions,
and Wolfram guidance into every conductor turn, plus a list of off-limits
paths from one operator's machine.
"""

import sys
import unittest

from long_exposure.orchestrator import assemble_system_prompt, load_config


def _cfg(**over):
    config = load_config()
    config["working_directory"] = "/ws"
    config.update(over)
    return config


class HeadlessPromptTests(unittest.TestCase):
    def setUp(self):
        self.headless = assemble_system_prompt(
            _cfg(), role="<agent-role>role text</agent-role>"
        )

    def test_no_slash_command_or_session_completion_text(self):
        for probe in ("/complete", "/clear", "SESSION COMPLETION", "user-typed commands"):
            self.assertNotIn(probe, self.headless, probe)

    def test_no_ask_the_user_instructions(self):
        self.assertNotIn("ask the user", self.headless.lower())
        self.assertIn("no interactive user to ask", self.headless)

    def test_no_context_gems_block_when_no_gems_are_injected(self):
        self.assertNotIn("CONTEXT GEMS", self.headless)
        self.assertNotIn("pre-ranked context gems", self.headless)
        self.assertNotIn("<catalog> section", self.headless)

    def test_no_machine_specific_off_limits_paths(self):
        for probe in ("agent-conditioning", "Mathematica", "~/bin/", "auto-compact/"):
            self.assertNotIn(probe, self.headless, probe)
        # The security-sensitive, machine-independent entries stay.
        for probe in ("~/.ssh/", "~/.env", "~/.claude/", "DIRECTORY BOUNDARIES"):
            self.assertIn(probe, self.headless, probe)

    def test_role_and_protocol_still_present(self):
        self.assertIn("<agent-role>role text</agent-role>", self.headless)
        self.assertIn("BASH WAIT LOOPS", self.headless)
        self.assertIn("REPORTER TRANSLATION TABLE", self.headless)

    def test_no_unsubstituted_protocol_placeholders(self):
        for probe in ("{session_completion_block}", "{context_gems_block}",
                      "{wolfram_block}", "{missing_info_sentence}",
                      "{test_runner_block}", "{working_directory}"):
            self.assertNotIn(probe, self.headless, probe)


class WolframGatingTests(unittest.TestCase):
    def test_wolfram_block_present_only_when_a_kernel_is_configured(self):
        with_kernel = assemble_system_prompt(_cfg(wolfram_path="wolfram-batch"), role="r")
        self.assertIn("WOLFRAM EXECUTION", with_kernel)
        self.assertIn("wolfram-batch -script", with_kernel)

        for empty in ("", "   ", None):
            without = assemble_system_prompt(_cfg(wolfram_path=empty), role="r")
            self.assertNotIn("WOLFRAM EXECUTION", without)
            self.assertNotIn("-script", without)
            self.assertLess(len(without), len(with_kernel))


class InteractiveReplPromptTests(unittest.TestCase):
    def test_repl_keeps_its_guidance(self):
        repl = assemble_system_prompt(_cfg(), session_summary=None, interactive_repl=True)
        self.assertIn("/complete", repl)
        self.assertIn("/clear", repl)
        self.assertIn("ask the user rather than reading the files", repl)
        self.assertNotIn("no interactive user to ask", repl)

    def test_gems_block_appears_only_with_gems(self):
        with_gems = assemble_system_prompt(
            _cfg(), session_summary=None,
            gems_xml="<context_gems><gem/></context_gems>", interactive_repl=True,
        )
        self.assertIn("CONTEXT GEMS", with_gems)
        self.assertIn("<context_gems>", with_gems)
        without = assemble_system_prompt(_cfg(), session_summary=None, interactive_repl=True)
        self.assertNotIn("CONTEXT GEMS", without)


class LazyPromptToolkitTests(unittest.TestCase):
    def test_importing_the_harness_does_not_load_prompt_toolkit(self):
        # Guard the lazy import: a module-scope prompt_toolkit import cost
        # every CLI invocation, cron manager poll and clone spawn ~55 ms
        # warm (~0.6 s cold) and 115 modules for a REPL they never open.
        import subprocess
        code = (
            "import sys; import long_exposure.exploration, long_exposure.cli; "
            "print('prompt_toolkit' in sys.modules)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True,
        )
        self.assertEqual(out.stdout.strip(), "False")


class FigureCheckFloorTests(unittest.TestCase):
    """A flat 1 KB floor failed legitimately small vector output."""

    def _check(self, name, payload):
        import tempfile
        from argparse import Namespace
        from pathlib import Path
        from long_exposure.tools.figure import _cmd_check
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / name
            target.write_bytes(payload)
            return _cmd_check(Namespace(target=str(target)))

    def test_small_svg_passes(self):
        svg = (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="120">'
            b'<rect width="200" height="120" fill="#fff"/>'
            b'<path d="M10 110 L60 40 L110 80 L190 10" stroke="#333" fill="none"/>'
            b'</svg>'
        )
        self.assertLess(len(svg), 200)  # the old flat 1 KB floor rejected this
        self.assertEqual(self._check("fig.svg", svg), 0)

    def test_blank_svg_still_flagged(self):
        self.assertEqual(self._check("blank.svg", b"<svg/>"), 1)

    def test_small_png_still_flagged(self):
        self.assertEqual(self._check("fig.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 100), 1)

    def test_large_png_passes(self):
        self.assertEqual(self._check("fig.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 4000), 0)

    def test_unknown_suffix_uses_the_default_floor(self):
        self.assertEqual(self._check("fig.dat", b"x" * 50), 1)
        self.assertEqual(self._check("fig.dat", b"x" * 500), 0)

    def test_missing_file_still_exits_2(self):
        from argparse import Namespace
        from long_exposure.tools.figure import _cmd_check
        self.assertEqual(_cmd_check(Namespace(target="/nonexistent/x.png")), 2)

if __name__ == "__main__":
    unittest.main()
