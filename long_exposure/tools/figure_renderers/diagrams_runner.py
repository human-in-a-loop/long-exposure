"""mingrammer/diagrams renderer for `figure arch` — cloud / system
architecture diagrams with curated iconography.

The agent writes a Python script that imports `from diagrams import …`
and uses `with Diagram(...)` blocks. We dispatch the script via
subprocess (rather than importing it into our own process) so:
  - The agent's script is a true standalone artifact (regenerable).
  - Import errors / cleanup are isolated from our process.
  - The script can be re-run by the operator outside long-exposure.

Requires the `graphviz` system binary (`dot`) on PATH. We surface a
clear error when it's missing rather than letting the diagrams library
explode with `ExecutableNotFound`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _locate_dot() -> str | None:
    """Search well-known locations for the graphviz `dot` binary.

    Falls back to PATH lookup. Returns None when not found.
    """
    candidates = [
        os.environ.get("GRAPHVIZ_DOT", ""),
        "/usr/bin/dot",
        "/usr/local/bin/dot",
        "/opt/graphviz/bin/dot",
        str(Path.home() / ".local" / "bin" / "dot"),
    ]
    for p in candidates:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("dot")


def _locate_python() -> str:
    """Return the Python interpreter to use for the user's diagrams script.

    Prefers the long-exposure venv (where `diagrams` was installed) over
    sys.executable. Falls back to `python3` on PATH.
    """
    # Prefer the venv that actually has diagrams installed
    here = Path(__file__).resolve()
    repo_root = here.parents[3]  # long-exposure/long_exposure/tools/figure_renderers/x.py
    venv_py = repo_root / ".venv" / "bin" / "python"
    if venv_py.is_file() and os.access(venv_py, os.X_OK):
        return str(venv_py)
    if sys.executable and os.path.isfile(sys.executable):
        return sys.executable
    p = shutil.which("python3")
    if p:
        return p
    return "python3"


def render(args) -> int:
    """Run the user's diagrams Python script.

    The script is responsible for calling `Diagram(filename=...)` with
    the desired output path. We surface dependency errors clearly and
    propagate the script's exit code on failure.
    """
    # Plan E: symmetric pre-check for the `diagrams` Python
    # library. Without this, a missing module manifests as a deep-stack
    # `ModuleNotFoundError` from inside the user-script subprocess. With
    # it, the operator gets the same loud, actionable shape as the
    # `_locate_dot()` failure below.
    try:
        import diagrams  # noqa: F401
    except ImportError:
        print(
            "[figure] `diagrams` Python library not installed. Install with:\n"
            "  uv sync --extra figures-arch    (long-exposure repo)\n"
            "  pip install diagrams            (any environment)\n"
            "Note: also requires graphviz `dot` system binary.",
            file=sys.stderr,
        )
        return 2

    if _locate_dot() is None:
        print(
            "[figure] Graphviz `dot` binary not found. Install with:\n"
            "  apt-get install graphviz       (Debian / Ubuntu)\n"
            "  brew install graphviz          (macOS)\n"
            "Or set GRAPHVIZ_DOT=/path/to/dot in your environment.\n"
            "(diagrams library invokes `dot` for layout; without it,\n"
            "rendering fails with `ExecutableNotFound`.)",
            file=sys.stderr,
        )
        return 2

    src = Path(args.src)
    if not src.is_file():
        print(f"[figure] Source not found: {src}", file=sys.stderr)
        return 2

    py = _locate_python()
    cmd = [py, str(src)]
    print(f"[figure] {py} {src}", flush=True)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(src.parent),
        )
    except subprocess.TimeoutExpired:
        print(f"[figure] diagrams script timed out after 300s", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"[figure] python invocation failed: {e}", file=sys.stderr)
        return 1

    # Surface stdout/stderr so the agent sees print() output and tracebacks.
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)
    if result.returncode != 0:
        return result.returncode

    # The user's script chose its own output path. We can't auto-detect it,
    # so we just confirm the script ran cleanly.
    print(f"[figure] script completed; check declared output path", flush=True)
    return 0
