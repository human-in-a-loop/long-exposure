"""Portable Wolfram script runner for installations where -script is unavailable.

Some Wolfram Engine installations can run the interactive `wolfram` kernel but
fail under `wolfram -script` or `wolframscript` because those entry points use a
different licensing path. This wrapper preserves long-exposure's script-oriented
agent guidance by feeding `Get["file.wls"]; Exit[]` into the interactive kernel.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _quote_wolfram_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _find_wolfram(explicit: str | None = None) -> str:
    candidates = [
        explicit,
        os.environ.get("WOLFRAM_BIN"),
        shutil.which("wolfram"),
        "/usr/local/bin/wolfram",
        "/usr/bin/wolfram",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit(
        "wolfram-batch: could not find a wolfram executable. "
        "Set WOLFRAM_BIN=/path/to/wolfram."
    )


def run_script(script_path: str, wolfram_bin: str | None = None) -> int:
    script = Path(script_path).expanduser().resolve()
    if not script.is_file():
        print(f"wolfram-batch: script not found: {script}", file=sys.stderr)
        return 2

    wolfram = _find_wolfram(wolfram_bin)
    quoted = _quote_wolfram_string(str(script))
    stdin_text = f'Get["{quoted}"]\nExit[]\n'
    proc = subprocess.run(
        [wolfram],
        input=stdin_text,
        text=True,
    )
    return int(proc.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wolfram-batch",
        description=(
            "Run a .wls file through the interactive wolfram kernel. "
            "Usage is intentionally compatible with `wolfram -script FILE`."
        ),
    )
    parser.add_argument(
        "--wolfram-bin",
        default=None,
        help="Path to wolfram executable. Defaults to WOLFRAM_BIN or PATH.",
    )
    parser.add_argument(
        "-script",
        dest="script",
        metavar="FILE",
        help="Run a Wolfram Language script file.",
    )
    args = parser.parse_args(argv)

    if not args.script:
        parser.print_help(sys.stderr)
        return 2
    return run_script(args.script, args.wolfram_bin)


if __name__ == "__main__":
    raise SystemExit(main())
