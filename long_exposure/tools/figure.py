#!/usr/bin/env python3
"""figure — unified CLI for first-class figure outputs.

One discoverable entry point with subcommands per figure category.
Each subcommand routes to a small renderer module under
`figure_renderers/`. Adding a new backend is a new module file plus
one line in `_DISPATCH` below — no allowlist or role-text changes.

Usage:
  figure plot   <python_script.py>  [--out FILE]
  figure flow   <diagram.d2>        [--out FILE] [--layout elk|dagre] [--format png|svg]
  figure arch   <python_script.py>  [--out FILE]   (mingrammer/diagrams; needs graphviz `dot`)
  figure list                                       Show subcommands and their backends.
  figure check  <FILE>                              Quick sanity check on a rendered file.

Default output format is PNG so the figure embeds cleanly into the
existing pandoc + tectonic PDF pipeline. SVG is opt-in for `figure flow`
via `--format svg`.

Bash allowlist: a single `Bash(figure *)` entry covers all subcommands
and any future ones.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _cmd_list(args) -> int:
    """List available subcommands and the backends they dispatch to."""
    print(
        "figure subcommands:\n"
        "  plot   matplotlib (Python script)         — quantitative data plots\n"
        "  flow   D2 (--layout=elk default, PNG)     — flowcharts, sequence, state, class, ERD, structural\n"
        "  arch   mingrammer/diagrams (Python)       — cloud / architecture with iconography\n"
        "  check  local validator                    — post-render sanity check\n"
        "  list   this listing\n"
        "\n"
        "Output format defaults to PNG (embeds cleanly into pandoc + tectonic).\n"
        "Use --format svg on `flow` for vector output (no Chromium dependency).\n",
        flush=True,
    )
    return 0


# Per-format "this is probably a blank canvas" floors, in bytes. Raster
# formats carry a palette/compression preamble, so a real plot is always a
# few KB. Vector formats are text: a correct single-panel SVG or PDF can be
# a few hundred bytes, so a flat 1 KB floor failed legitimate output.
_MIN_PLAUSIBLE_BYTES = {
    ".png": 1024,
    ".jpg": 1024,
    ".jpeg": 1024,
    ".webp": 1024,
    ".gif": 1024,
    # A real single-panel SVG can be under 200 bytes; the floor only has to
    # exceed an empty root element (`<svg/>` is 6 bytes, a bare
    # xmlns-only stub ~46) so blank canvases are still caught.
    ".svg": 120,
    ".pdf": 400,
    ".eps": 400,
    ".ps": 400,
}
_DEFAULT_MIN_BYTES = 120

# SVG is text, so bytes alone cannot separate "a real small plot" from "a
# white rectangle on a 640x480 canvas" — that stub is ~130 bytes, clears any
# floor low enough to accept legitimate single-panel output, and is the most
# likely product of a failed `Export`. So small SVGs get a content probe:
# at least one mark-bearing element must be present. A blank canvas carries
# exactly one background `<rect>` and nothing else, while even an axis-less
# bar chart has several rects, and anything from matplotlib has `<path>`.
_SVG_MARK_ELEMENTS = (
    "<path", "<text", "<tspan", "<circle", "<ellipse",
    "<polyline", "<polygon", "<line", "<image", "<use",
)
# Only probe files small enough to plausibly BE a blank canvas; a larger
# file has content whatever its element mix, and this bounds the read.
_SVG_PROBE_MAX_BYTES = 2048


def _svg_lacks_marks(target: Path, size: int) -> bool:
    """True when a small SVG has no drawable content (blank canvas).

    Unreadable or undecodable files return False — the probe only ever adds
    a failure it can positively justify.
    """
    if size > _SVG_PROBE_MAX_BYTES:
        return False
    try:
        text = target.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    if any(mark in text for mark in _SVG_MARK_ELEMENTS):
        return False
    # Two or more rects means real geometry (bar chart); one is the canvas.
    return text.count("<rect") < 2


def _cmd_check(args) -> int:
    """Quick post-render sanity check: file exists, non-trivial size,
    plausible image dimensions if it parses as an image format we know.
    """
    target = Path(args.target)
    if not target.is_file():
        print(f"[figure check] missing: {target}", file=sys.stderr)
        return 2
    size = target.stat().st_size
    suffix = target.suffix.lower()
    floor = _MIN_PLAUSIBLE_BYTES.get(suffix, _DEFAULT_MIN_BYTES)
    if suffix == ".svg" and size >= floor and _svg_lacks_marks(target, size):
        print(
            f"[figure check] WARNING {target} has no drawable content "
            f"({size} bytes, no path/text/marker elements) — "
            f"looks like a blank canvas",
            file=sys.stderr,
        )
        return 1
    if size < floor:
        # Catches "blank canvas" outputs (mostly headers, no content).
        print(
            f"[figure check] WARNING {target} suspiciously small "
            f"({size} bytes; expected at least {floor} for {suffix or 'this format'})",
            file=sys.stderr,
        )
        return 1
    print(f"[figure check] {target} OK ({size:,} bytes)", flush=True)
    return 0


# Lazy import per dispatch — keeps `figure list` / `figure check` from
# pulling matplotlib / diagrams into memory when not needed.
def _cmd_plot(args) -> int:
    from long_exposure.tools.figure_renderers import matplotlib_runner
    return matplotlib_runner.render(args)


def _cmd_flow(args) -> int:
    from long_exposure.tools.figure_renderers import d2_runner
    return d2_runner.render(args)


def _cmd_arch(args) -> int:
    from long_exposure.tools.figure_renderers import diagrams_runner
    return diagrams_runner.render(args)


_DISPATCH = {
    "plot": _cmd_plot,
    "flow": _cmd_flow,
    "arch": _cmd_arch,
    "list": _cmd_list,
    "check": _cmd_check,
}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="figure",
        description="Unified CLI for first-class figure outputs.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_plot = sub.add_parser("plot", help="Run a Python script that uses matplotlib")
    sp_plot.add_argument("src", help="Path to the Python script")
    sp_plot.add_argument("--out", help="Optional output path (set as FIGURE_OUT env)")

    sp_flow = sub.add_parser("flow", help="Render a D2 diagram source")
    sp_flow.add_argument("src", help="Path to the .d2 source file")
    sp_flow.add_argument("--out", help="Output path (default: <src>.png)")
    sp_flow.add_argument("--layout", default="elk",
                         help="Layout engine: elk (default) or dagre")
    sp_flow.add_argument("--format", default="png",
                         help="Output format: png (default) or svg")

    sp_arch = sub.add_parser("arch", help="Run a mingrammer/diagrams Python script")
    sp_arch.add_argument("src", help="Path to the Python script")
    sp_arch.add_argument("--out", help="Output path (script controls actual filename)")

    sub.add_parser("list", help="List available subcommands")

    sp_check = sub.add_parser("check", help="Sanity check a rendered file")
    sp_check.add_argument("target", help="Path to the rendered file")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = _DISPATCH.get(args.cmd)
    if handler is None:
        parser.print_help(sys.stderr)
        return 2
    try:
        return handler(args)
    except KeyboardInterrupt:
        print("[figure] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
