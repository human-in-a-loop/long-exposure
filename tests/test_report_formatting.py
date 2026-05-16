import tempfile
import unittest
from pathlib import Path

from long_exposure.report_formatting import (
    REPORT_FONT_SIZE,
    build_pandoc_report_command,
    normalize_report_markdown,
    sanitize_markdown_for_pdf,
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
        self.assertIn(
            "--from markdown+tex_math_single_backslash+tex_math_dollars"
            "+raw_tex+autolink_bare_uris",
            joined,
        )
        self.assertIn(f"fontsize={REPORT_FONT_SIZE}", cmd)
        self.assertIn("mainfont=DejaVu Serif", cmd)
        self.assertIn("monofont=DejaVu Sans Mono", cmd)
        self.assertIn("monofontoptions=Scale=0.82", cmd)
        self.assertIn("--toc-depth=2", joined)
        self.assertNotIn("--number-sections", cmd)

    def test_pdf_sanitizer_rewrites_svg_to_png_when_available(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "figures").mkdir()
            (root / "figures" / "matrix.svg").write_text("<svg></svg>")
            (root / "figures" / "matrix.png").write_bytes(b"png")
            md_path = root / "reports" / "report.md"
            md_path.parent.mkdir()
            source = "![Matrix](figures/matrix.svg)\n"

            out = sanitize_markdown_for_pdf(source, md_path=md_path, resource_root=root)

        self.assertIn("![Matrix](figures/matrix.png)", out)
        self.assertNotIn(".svg)", out)

    def test_pdf_sanitizer_degrades_svg_without_png_to_artifact_link(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "figures").mkdir()
            (root / "figures" / "matrix.svg").write_text("<svg></svg>")
            md_path = root / "reports" / "report.md"
            md_path.parent.mkdir()
            source = "![Matrix](figures/matrix.svg)\n"

            out = sanitize_markdown_for_pdf(source, md_path=md_path, resource_root=root)

        self.assertIn("Figure artifact", out)
        self.assertIn("figures/matrix.svg", out)
        self.assertNotIn("![Matrix]", out)

    def test_pdf_sanitizer_preserves_svg_inside_fenced_code(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            md_path = root / "report.md"
            source = "```md\n![Matrix](figures/matrix.svg)\n```\n"

            out = sanitize_markdown_for_pdf(source, md_path=md_path, resource_root=root)

        self.assertEqual(source, out)


if __name__ == "__main__":
    unittest.main()
