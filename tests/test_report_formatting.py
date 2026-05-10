import tempfile
import unittest
from pathlib import Path

from long_exposure.report_formatting import (
    REPORT_FONT_SIZE,
    build_pandoc_report_command,
    normalize_report_markdown,
)


class ReportFormattingTests(unittest.TestCase):
    def test_normalize_adds_standard_frontmatter_and_heading_spacing(self):
        source = "# Run Summary\n\n1. First item\n## Findings\nBody\n"
        out = normalize_report_markdown(source, fallback_title="Fallback")

        self.assertTrue(out.startswith("---\n"))
        self.assertIn('title: "Run Summary"\n', out)
        self.assertIn("toc: true\n", out)
        self.assertIn("toc-depth: 2\n", out)
        self.assertIn("numbersections: false\n", out)
        self.assertIn(f'fontsize: "{REPORT_FONT_SIZE}"\n', out)
        self.assertIn("1. First item\n\n## Findings\n", out)

    def test_normalize_preserves_headings_inside_fences(self):
        source = "# T\n\n```text\nnot blank\n## not a heading\n```\n## Real\n"
        out = normalize_report_markdown(source, fallback_title="Fallback")

        self.assertIn("not blank\n## not a heading\n", out)
        self.assertIn("```\n\n## Real\n", out)

    def test_normalize_updates_existing_frontmatter(self):
        source = "---\ntitle: Old\ntoc: false\ncustom: keep\n---\n# Body\n"
        out = normalize_report_markdown(source, fallback_title="Fallback")

        self.assertIn('title: "Old"\n', out)
        self.assertIn("toc: true\n", out)
        self.assertIn('custom: "keep"\n', out)

    def test_pandoc_command_uses_standard_pdf_settings(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cmd = build_pandoc_report_command(
                root / "report.md",
                root / "report.pdf",
                root / "header.tex",
                resource_root=root,
            )

        joined = " ".join(cmd)
        self.assertIn("--pdf-engine=tectonic", cmd)
        self.assertIn(f"fontsize={REPORT_FONT_SIZE}", cmd)
        self.assertIn("mainfont=DejaVu Serif", cmd)
        self.assertIn("monofont=DejaVu Sans Mono", cmd)
        self.assertIn("monofontoptions=Scale=0.82", cmd)
        self.assertIn("--toc-depth=2", joined)
        self.assertNotIn("--number-sections", cmd)


if __name__ == "__main__":
    unittest.main()
