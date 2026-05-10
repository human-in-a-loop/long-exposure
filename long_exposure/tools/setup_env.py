"""Environment setup and diagnostics for long-exposure.

This module keeps first-run setup deterministic: inspect the environment, run
``uv sync`` when requested, install required system binaries through known
package managers, and print exact manual commands when automatic install is not
safe for the current platform.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


REQUIRED_BINARIES = ("pandoc", "tectonic")
OPTIONAL_BINARIES = ("dot", "d2")
PYTHON_IMPORTS = ("yaml", "prompt_toolkit", "matplotlib")
OPTIONAL_PYTHON_IMPORTS = ("diagrams",)


@dataclass(frozen=True)
class Probe:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class CommandResult:
    cmd: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _which(name: str) -> str | None:
    return shutil.which(name)


def _run(cmd: list[str], *, cwd: Path | None = None) -> CommandResult:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    return CommandResult(
        cmd=cmd,
        returncode=proc.returncode,
        stdout=proc.stdout.strip(),
        stderr=proc.stderr.strip(),
    )


def _version(binary: str) -> str:
    path = _which(binary)
    if not path:
        return "missing"
    for args in ((binary, "--version"), (binary, "-v")):
        try:
            result = _run(list(args))
        except OSError:
            continue
        text = result.stdout or result.stderr
        if result.returncode == 0 and text:
            return text.splitlines()[0][:160]
    return path


def _import_ok(name: str) -> tuple[bool, str]:
    try:
        __import__(name)
    except Exception as exc:
        return False, str(exc)
    return True, "import ok"


def probe_environment() -> dict:
    system = platform.system().lower()
    binary_probes = [
        Probe(name, _which(name) is not None, _version(name))
        for name in REQUIRED_BINARIES
    ]
    optional_probes = [
        Probe(name, _which(name) is not None, _version(name))
        for name in OPTIONAL_BINARIES
    ]
    python_probes = []
    for name in PYTHON_IMPORTS:
        ok, detail = _import_ok(name)
        python_probes.append(Probe(name, ok, detail))
    optional_python_probes = []
    for name in OPTIONAL_PYTHON_IMPORTS:
        ok, detail = _import_ok(name)
        optional_python_probes.append(Probe(name, ok, detail))

    return {
        "platform": {
            "system": system,
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "repo_root": str(_repo_root()),
        },
        "tools": [p.__dict__ for p in binary_probes],
        "optional_tools": [p.__dict__ for p in optional_probes],
        "python_imports": [p.__dict__ for p in python_probes],
        "optional_python_imports": [p.__dict__ for p in optional_python_probes],
        "uv": {
            "ok": _which("uv") is not None,
            "detail": _version("uv") if _which("uv") else "missing",
        },
    }


def missing_required(report: dict) -> list[str]:
    return [
        p["name"] for p in report["tools"]
        if not p["ok"]
    ]


def _sudo_prefix() -> list[str]:
    if os.name != "posix":
        return []
    try:
        if os.geteuid() == 0:
            return []
    except AttributeError:
        return []
    return ["sudo"]


def _pkg_manager_install_cmds(missing: list[str]) -> tuple[list[list[str]], str | None]:
    """Return install commands for the current platform, plus a note if any."""
    if not missing:
        return [], None

    system = platform.system().lower()
    sudo = _sudo_prefix()

    if system == "darwin" and _which("brew"):
        return [["brew", "install", name] for name in missing], None

    if system == "linux":
        if _which("apt-get"):
            return [
                [*sudo, "apt-get", "update"],
                *[[*sudo, "apt-get", "install", "-y", name] for name in missing],
            ], None
        if _which("dnf"):
            return [[*sudo, "dnf", "install", "-y", name] for name in missing], None
        if _which("yum"):
            return [[*sudo, "yum", "install", "-y", name] for name in missing], None
        if _which("pacman"):
            return [
                [*sudo, "pacman", "-S", "--needed", "--noconfirm", name]
                for name in missing
            ], None
        if _which("zypper"):
            return [
                [*sudo, "zypper", "--non-interactive", "install", name]
                for name in missing
            ], None

    if system == "windows" and _which("winget"):
        package_ids = {
            "pandoc": "JohnMacFarlane.Pandoc",
            "tectonic": "Tectonic.Tectonic",
        }
        return [
            ["winget", "install", "--id", package_ids[name], "-e"]
            for name in missing
            if name in package_ids
        ], None

    return [], (
        "No supported package manager was detected. Install manually: "
        "pandoc from https://pandoc.org/installing.html and tectonic from "
        "https://tectonic-typesetting.github.io/."
    )


def _confirm(prompt: str, *, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        return False
    answer = input(prompt + " [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _print_report(report: dict, *, json_output: bool = False) -> None:
    if json_output:
        print(json.dumps(report, indent=2))
        return

    print("long-exposure environment report")
    print(f"  repo: {report['platform']['repo_root']}")
    print(f"  python: {report['platform']['python']}")
    print(f"  uv: {'OK' if report['uv']['ok'] else 'missing'} - {report['uv']['detail']}")
    print("")
    print("Required system tools:")
    for probe in report["tools"]:
        mark = "OK" if probe["ok"] else "missing"
        print(f"  {probe['name']}: {mark} - {probe['detail']}")
    print("")
    print("Python imports:")
    for probe in report["python_imports"]:
        mark = "OK" if probe["ok"] else "missing"
        print(f"  {probe['name']}: {mark} - {probe['detail']}")
    print("")
    print("Optional figure tools:")
    for probe in report["optional_tools"]:
        mark = "OK" if probe["ok"] else "missing"
        print(f"  {probe['name']}: {mark} - {probe['detail']}")
    print("")
    print("Optional Python imports:")
    for probe in report["optional_python_imports"]:
        mark = "OK" if probe["ok"] else "missing"
        print(f"  {probe['name']}: {mark} - {probe['detail']}")


def _run_uv_sync(*, extras: list[str], yes: bool) -> bool:
    if not _which("uv"):
        print("uv: missing. Install uv first: https://docs.astral.sh/uv/")
        return False
    if not (_repo_root() / "pyproject.toml").exists():
        print("uv sync skipped: pyproject.toml not found near installed package.")
        return True
    cmd = ["uv", "sync"]
    for extra in extras:
        cmd.extend(["--extra", extra])
    print("+ " + shlex.join(cmd))
    result = _run(cmd, cwd=_repo_root())
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode == 0


def install_system_tools(missing: list[str], *, yes: bool) -> bool:
    cmds, note = _pkg_manager_install_cmds(missing)
    if not cmds:
        if note:
            print(note)
        return False
    print("System tools missing: " + ", ".join(missing))
    for cmd in cmds:
        print("  planned: " + shlex.join(cmd))
    if not _confirm("Install missing system tools now?", yes=yes):
        print("Skipped system install.")
        return False
    ok = True
    for cmd in cmds:
        print("+ " + shlex.join(cmd))
        result = _run(cmd)
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if result.returncode != 0:
            print(f"Command failed with exit code {result.returncode}: {shlex.join(cmd)}")
            ok = False
    return ok


def setup_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="long-exposure-setup",
        description="Install and verify long-exposure runtime dependencies.",
    )
    parser.add_argument("--check", action="store_true", help="Only inspect; do not install.")
    parser.add_argument("--json", action="store_true", help="Emit the final report as JSON.")
    parser.add_argument("--yes", "-y", action="store_true", help="Run non-interactively.")
    parser.add_argument(
        "--skip-uv-sync",
        action="store_true",
        help="Do not run uv sync before checking Python dependencies.",
    )
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        help="Pass an optional dependency extra to uv sync, e.g. figures-arch.",
    )
    args = parser.parse_args(argv)

    if not args.check and not args.skip_uv_sync:
        uv_ok = _run_uv_sync(extras=args.extra, yes=args.yes)
        if not uv_ok:
            print("uv sync did not complete; continuing with environment probe.")

    report = probe_environment()
    missing = missing_required(report)
    if missing and not args.check:
        install_system_tools(missing, yes=args.yes)
        report = probe_environment()
        missing = missing_required(report)

    _print_report(report, json_output=args.json)
    if missing:
        print(
            "\nMissing required report-rendering tools: "
            + ", ".join(missing),
            file=sys.stderr,
        )
        return 1
    return 0


def doctor_main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--check" not in argv:
        argv.insert(0, "--check")
    return setup_main(argv)


if __name__ == "__main__":
    raise SystemExit(setup_main())
