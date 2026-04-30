"""matplotlib (and seaborn / pandas / numpy) renderer for `figure plot`.

The agent writes a Python script that produces a quantitative figure
via matplotlib or compatible. We dispatch via subprocess for the
same reasons as `diagrams_runner`: agent script remains a regenerable
standalone artifact; import errors stay isolated.

No external binary dependency — matplotlib is a pure Python package.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _locate_python() -> str:
    here = Path(__file__).resolve()
    repo_root = here.parents[3]
    venv_py = repo_root / ".venv" / "bin" / "python"
    if venv_py.is_file() and os.access(venv_py, os.X_OK):
        return str(venv_py)
    if sys.executable and os.path.isfile(sys.executable):
        return sys.executable
    p = shutil.which("python3")
    return p or "python3"


def render(args) -> int:
    src = Path(args.src).resolve()
    if not src.is_file():
        print(f"[figure] Source not found: {src}", file=sys.stderr)
        return 2

    py = _locate_python()
    cmd = [py, str(src)]
    print(f"[figure] {py} {src}", flush=True)

    # If --out is given, resolve to absolute BEFORE setting FIGURE_OUT.
    # The subprocess cwd is set to the script's directory (so the script
    # can read sibling data files via relative paths), which means a
    # relative FIGURE_OUT would resolve under the SCRIPT'S directory,
    # not the figure CLI's cwd. Resolving to absolute eliminates the
    # ambiguity — the script writes exactly where the agent intended,
    # regardless of how its cwd is set.
    out_abs: Path | None = None
    env = dict(os.environ)
    if args.out:
        out_abs = Path(args.out).resolve()
        env["FIGURE_OUT"] = str(out_abs)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(src.parent),
            env=env,
        )
    except subprocess.TimeoutExpired:
        print(f"[figure] plot script timed out after 300s", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"[figure] python invocation failed: {e}", file=sys.stderr)
        return 1

    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)
    if result.returncode != 0:
        return result.returncode

    if out_abs and out_abs.is_file():
        size_kb = out_abs.stat().st_size // 1024
        print(f"[figure] wrote {out_abs} ({size_kb} KB)", flush=True)
    elif out_abs:
        # The script ran clean but didn't write to FIGURE_OUT. Either it
        # ignored the env var (saved to a hardcoded path) or the path was
        # mistyped. Surface so the agent doesn't assume success.
        print(
            f"[figure] WARNING script exited 0 but {out_abs} was not "
            f"written. Did the script save to a different path?",
            file=sys.stderr,
        )
    print(f"[figure] script completed", flush=True)
    return 0
