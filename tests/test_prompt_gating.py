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

    def test_off_limits_list_names_the_harness_root_it_derives(self):
        """The fence must name a real path — "the harness installation you
        are running within" fences nothing, and the repo root IS on every
        agent turn's PYTHONPATH. Derived, so no literal path is asserted."""
        from long_exposure.orchestrator import SCRIPT_DIR
        harness_root = str(SCRIPT_DIR.parent)
        self.assertIn(harness_root, self.headless)
        self.assertIn("never edit it", self.headless)

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


class TestRunnerBlockTests(unittest.TestCase):
    """The test suite must be announced independently of Wolfram: nesting
    the block inside the Wolfram section meant a deployment with
    `wolfram_path: ""` never told any agent its test suite existed."""

    RUNNER = "tests/run_all.wls"

    def test_announced_without_a_wolfram_kernel(self):
        prompt = assemble_system_prompt(
            _cfg(test_runner=self.RUNNER, wolfram_path=""), role="r"
        )
        self.assertIn("TEST SUITE", prompt)
        self.assertIn(self.RUNNER, prompt)
        # ...but no invented `wolfram -script` command for a kernel that
        # is not installed.
        self.assertNotIn("-script", prompt)

    def test_wolfram_command_used_when_a_kernel_is_configured(self):
        prompt = assemble_system_prompt(
            _cfg(test_runner=self.RUNNER, wolfram_path="wolfram-batch"), role="r"
        )
        self.assertIn("TEST SUITE", prompt)
        self.assertIn(f"wolfram-batch -script {self.RUNNER}", prompt)

    def test_absent_when_no_runner_is_configured(self):
        for wolfram in ("", "wolfram-batch"):
            prompt = assemble_system_prompt(
                _cfg(test_runner="", wolfram_path=wolfram), role="r"
            )
            self.assertNotIn("TEST SUITE", prompt)

    def test_bash_heading_keeps_its_blank_line_in_every_combination(self):
        for runner in ("", self.RUNNER):
            for wolfram in ("", "wolfram-batch"):
                prompt = assemble_system_prompt(
                    _cfg(test_runner=runner, wolfram_path=wolfram), role="r"
                )
                idx = prompt.index("== BASH WAIT LOOPS ==")
                self.assertEqual(
                    prompt[idx - 2:idx], "\n\n",
                    f"runner={runner!r} wolfram={wolfram!r}: "
                    f"{prompt[max(0, idx - 60):idx]!r}",
                )


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

    def test_white_rectangle_canvas_is_flagged_despite_clearing_the_floor(self):
        """130 bytes clears any floor low enough to accept real single-panel
        SVG, and a white rect is the likely product of a failed Export — so
        size alone cannot decide it. The content probe must."""
        svg = (
            b'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            b'width="640" height="480">'
            b'<rect width="640" height="480" fill="white"/></svg>'
        )
        self.assertGreater(len(svg), 120)  # clears the .svg floor
        self.assertEqual(self._check("blank_canvas.svg", svg), 1)

    def test_rect_only_bar_chart_passes(self):
        """Real geometry with no path/text at all: several rects, not one."""
        bars = (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
            b'<rect x="10" y="40" width="20" height="50" fill="steelblue"/>'
            b'<rect x="40" y="20" width="20" height="70" fill="steelblue"/>'
            b'<rect x="70" y="60" width="20" height="30" fill="steelblue"/></svg>'
        )
        self.assertEqual(self._check("bars.svg", bars), 0)

    def test_text_only_svg_passes(self):
        label = (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="220" height="40">'
            b'<text x="10" y="25" font-family="DejaVu Sans" font-size="12">'
            b'measured 4.2 +/- 0.3</text></svg>'
        )
        self.assertGreater(len(label), 120)  # past the floor: probe decides
        self.assertEqual(self._check("label.svg", label), 0)

    def test_large_svg_is_not_probed(self):
        """Past the probe ceiling a file has content whatever its element
        mix; probing it would also mean reading an arbitrarily large file."""
        big = (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480">'
            + b"<!-- " + b"x" * 3000 + b" -->"
            + b'<rect width="640" height="480" fill="white"/></svg>'
        )
        self.assertEqual(self._check("big.svg", big), 0)

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
