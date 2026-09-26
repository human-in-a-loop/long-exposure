"""The single place long-exposure invokes git.

Two modules need git: `conflict_radar`, which only reads, and `git_sync`, which
commits and pushes. Both call `run` here, so there is one function with one
timeout and one never-raises contract — and one place a test can inspect to
see every git invocation the harness makes.

`tests/test_git_federation_doc.py::GitPolicyTests` pins what each caller may
do: the radar only read-only subcommands, the sync layer no destructive ones
(no reset, clean, force-push, rebase, or checkout of paths). Adding a git call
anywhere else fails the build.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run(args: list[str], cwd: Path, timeout: int = 20, *,
        input: str | None = None, env: dict | None = None,
        binary: bool = False) -> tuple[int, str | bytes, str]:
    """Run git. Never raises; a failure to launch looks like a non-zero exit.

    Exit 124 is a timeout and 127 a launch failure, matching the shell
    conventions so a caller can tell them from git's own codes.

    `input` feeds stdin (plumbing such as `update-index --index-info`), `env`
    is merged over the process environment (for a temporary GIT_INDEX_FILE),
    and `binary=True` returns stdout as bytes — needed for `cat-file blob`,
    where a published file may not be valid UTF-8.
    """
    import os

    full_env = None
    if env:
        full_env = {**os.environ, **env}
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True,
            text=not binary, timeout=timeout, env=full_env,
            input=(input.encode() if binary and input is not None else input),
        )
        out = proc.stdout if proc.stdout is not None else (b"" if binary else "")
        err = proc.stderr or (b"" if binary else "")
        if binary and isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        return 124, (b"" if binary else ""), f"timed out after {timeout}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, (b"" if binary else ""), repr(exc)


def rejects_as_option(value: str) -> bool:
    """True if git would read this value as an option rather than a name.

    git parses a leading `-` as an option wherever it appears, including the
    refspec slot: `git fetch origin "--upload-pack=touch /tmp/x"` executes the
    command (verified live). Any config value that reaches git argv as a name —
    a remote, a branch, an operator segment of a branch — goes through this.
    """
    return str(value or "").startswith("-")
