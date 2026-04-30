"""D2 renderer for `figure flow` — primary structural-diagram backend.

Produces PNG (default) or SVG from a D2 source file. Uses the ELK
layout engine by default per the recent evaluation (clean
top-down hierarchy on first render).

Binary lookup falls back through well-known paths so the agent's
non-interactive subprocess works even when `~/.local/bin` isn't on
PATH (cron, systemd, minimal containers).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Search order: env var, well-known absolute paths, then PATH lookup.
# Path order roughly mirrors install-location frequency from the
# upstream install script's docs.
_D2_SEARCH_PATHS = (
    os.environ.get("D2_BIN", ""),
    str(Path.home() / ".local" / "bin" / "d2"),
    "/usr/local/bin/d2",
    "/opt/d2/bin/d2",
)


def _locate_d2() -> str | None:
    for p in _D2_SEARCH_PATHS:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("d2")


def render(args) -> int:
    """Render a D2 source file. `args` carries: src, out, layout, format."""
    d2_bin = _locate_d2()
    if not d2_bin:
        print(
            "[figure] D2 binary not found. Install with:\n"
            "  curl -fsSL https://d2lang.com/install.sh | sh -s -- --prefix ~/.local\n"
            "Or set D2_BIN=/path/to/d2 in your environment.",
            file=sys.stderr,
        )
        return 2

    src = Path(args.src)
    if not src.is_file():
        print(f"[figure] Source not found: {src}", file=sys.stderr)
        return 2

    # Determine output path. Default: same basename as source, format
    # extension. The default format is PNG per Plan C lessons:
    # PNG embeds cleanly into pandoc + tectonic.
    fmt = (args.format or "png").lower()
    if fmt not in ("png", "svg"):
        print(f"[figure] Unknown format: {fmt!r}. Use png or svg.", file=sys.stderr)
        return 2

    if args.out:
        out = Path(args.out)
        # If an out path was given without an extension, append the format
        if not out.suffix:
            out = out.with_suffix(f".{fmt}")
    else:
        out = src.with_suffix(f".{fmt}")

    layout = (args.layout or "elk").lower()
    if layout not in ("elk", "dagre"):
        print(f"[figure] Unknown layout: {layout!r}. Use elk or dagre.", file=sys.stderr)
        return 2

    cmd = [d2_bin, f"--layout={layout}", str(src), str(out)]
    print(f"[figure] d2 {' '.join(cmd[1:])}", flush=True)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        print(f"[figure] d2 timed out after 120s", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"[figure] d2 invocation failed: {e}", file=sys.stderr)
        return 1

    if result.returncode != 0:
        # First-time PNG render downloads Chromium; surface stderr verbatim
        # so the operator sees the download progress / final error message.
        print(result.stderr or result.stdout, file=sys.stderr)
        return result.returncode

    # Success path — print the d2 success line if present, else our own.
    msg = (result.stdout or result.stderr or "").strip()
    if msg:
        print(msg, flush=True)
    if out.is_file():
        size_kb = out.stat().st_size // 1024
        print(f"[figure] wrote {out} ({size_kb} KB)", flush=True)
    return 0
