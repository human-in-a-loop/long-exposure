"""Operator-facing long-exposure command surface.

The historical `long_exposure.exploration:main` entrypoint remains valid for
start/resume/stop/clear. This module adds a small launcher/status layer while
delegating the actual conductor to exploration.py.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from long_exposure import exploration
from long_exposure import telemetry
from long_exposure.manager import (
    DEFAULT_SCORE_PATH as MANAGER_DEFAULT_SCORE_PATH,
    NOTIFICATIONS_FILE,
    run_manager_poll,
)
from long_exposure.orchestrator import load_config, resolve_instance_dir
from long_exposure.tools.setup_env import doctor_main


VALID_COMMANDS = (
    "launch",
    "start",
    "resume",
    "stop",
    "clear",
    "status",
    "tail",
    "guide",
    "manager",
    "cli-install",
    "telemetry",
)


class LongExposureArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if "invalid choice" in message and "command" in message:
            self.print_usage(sys.stderr)
            self.exit(
                2,
                "long-exposure: unknown command. Valid commands: "
                + ", ".join(VALID_COMMANDS)
                + '\nTry: long-exposure launch "<directive>"\n',
            )
        super().error(message)


def _output_dir(arg_output: str | None, instance_dir: Path | None) -> Path:
    return exploration._resolve_output_dir(arg_output, instance_dir)


def _state_path(arg_state: str | None, instance_dir: Path | None) -> Path:
    return exploration._resolve_state_path(arg_state, instance_dir)


def _print_status(args: argparse.Namespace) -> int:
    instance_dir = resolve_instance_dir(args.instance_dir)
    output_dir = _output_dir(args.output, instance_dir)
    status_path = output_dir / "exploration_status.md"
    state_path = _state_path(args.state, instance_dir)
    notify_path = state_path.parent / NOTIFICATIONS_FILE
    if status_path.exists():
        print(status_path.read_text().rstrip())
    else:
        print(f"[long-exposure] No status file found at {status_path}")
    if notify_path.exists():
        last = [ln for ln in notify_path.read_text().splitlines() if ln.strip()]
        if last:
            try:
                notice = json.loads(last[-1])
                print(
                    "\n# Latest Manager Notice\n"
                    f"- Verdict: {notice.get('verdict')}\n"
                    f"- Cycle: {notice.get('cycle')}\n"
                    f"- Event class: {notice.get('event_class')}\n"
                    f"- Summary: {notice.get('summary')}"
                )
            except json.JSONDecodeError:
                print(f"\n[long-exposure] Manager notices: {notify_path}")
    return 0


def _write_guide(args: argparse.Namespace) -> int:
    instance_dir = resolve_instance_dir(args.instance_dir)
    data_dir = _state_path(args.state, instance_dir).parent
    data_dir.mkdir(parents=True, exist_ok=True)
    text = " ".join(args.guidance).strip()
    if not text:
        print("long-exposure guide: guidance text is required", file=sys.stderr)
        return 2
    path = data_dir / "long-exposure.guide"
    existing = path.read_text().strip() if path.exists() else ""
    body = text if not existing else existing + "\n\n" + text
    path.write_text(body.strip() + "\n")
    print(f"[long-exposure] Guidance queued: {path}")
    return 0


def _tail_file(path: Path, *, follow: bool) -> int:
    if not path.exists():
        print(f"[long-exposure] File not found: {path}", file=sys.stderr)
        return 1
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            print(line, end="")
        while follow:
            line = fh.readline()
            if line:
                print(line, end="")
            else:
                time.sleep(1.0)
    return 0


def _tail(args: argparse.Namespace) -> int:
    instance_dir = resolve_instance_dir(args.instance_dir)
    output_dir = _output_dir(args.output, instance_dir)
    path = Path(args.file) if args.file else output_dir / "exploration_status.md"
    return _tail_file(path, follow=args.follow)


def _manager_poll(args: argparse.Namespace) -> int:
    instance_dir = resolve_instance_dir(args.instance_dir)
    state_path = _state_path(args.state, instance_dir)
    try:
        return run_manager_poll(
            score_path=Path(args.score or MANAGER_DEFAULT_SCORE_PATH),
            config_path=Path(args.config) if args.config else None,
            state_path=state_path,
            instance_dir=instance_dir,
            force_agent=args.force_agent,
            no_agent=args.no_agent,
            allow_pause_signal=args.allow_pause_signal,
        )
    except Exception as exc:
        print(f"long-exposure manager poll: failed gracefully: {exc}", file=sys.stderr)
        return 0


def _manager_loop(
    args: argparse.Namespace,
    *,
    stop_event: threading.Event,
) -> None:
    interval = max(1, int(args.manager_interval_seconds))
    while not stop_event.wait(interval):
        try:
            _manager_poll(args)
        except Exception as exc:
            print(f"[long-exposure] manager poll skipped: {exc}", flush=True)


def _launch(args: argparse.Namespace) -> int:
    if not args.skip_doctor:
        doctor_args = ["--json"]
        if args.config:
            doctor_args.extend(["--config", args.config])
        rc = doctor_main(doctor_args)
        if rc != 0 and not args.allow_doctor_failure:
            print(
                "[long-exposure] Preflight failed; rerun with "
                "--allow-doctor-failure to launch anyway.",
                file=sys.stderr,
            )
            return rc

    instance_dir = resolve_instance_dir(args.instance_dir)
    state_path = _state_path(args.state, instance_dir)
    output_dir = _output_dir(args.output, instance_dir)
    config = load_config(Path(args.config) if args.config else None)
    print("[long-exposure] launch")
    print(f"  provider: {config.get('llm_provider')}")
    print(f"  working_directory: {config.get('working_directory')}")
    print(f"  state: {state_path}")
    print(f"  output: {output_dir}")
    print(f"  manager_notifications: {state_path.parent / NOTIFICATIONS_FILE}")

    stop_event = threading.Event()
    manager_thread = None
    if args.manager:
        manager_thread = threading.Thread(
            target=_manager_loop,
            args=(args,),
            kwargs={"stop_event": stop_event},
            daemon=True,
        )
        manager_thread.start()

    try:
        exploration.run_exploration(
            score_path=args.score,
            config_path=args.config,
            output_dir=output_dir,
            state_path=state_path,
            task_override=" ".join(args.task) if args.task else None,
            instance_dir=instance_dir,
        )
    finally:
        stop_event.set()
        if manager_thread is not None:
            manager_thread.join(timeout=2.0)
    return 0


def _cli_install(args: argparse.Namespace) -> int:
    root = Path(args.directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    installed: list[Path] = []

    def write_new(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not args.force:
            print(f"[long-exposure] exists, skipped: {path}")
            return
        if path.exists() and args.force:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            backup = path.with_name(path.name + f".bak-{ts}")
            backup.write_text(path.read_text())
            print(f"[long-exposure] backup: {backup}")
        path.write_text(text)
        installed.append(path)

    def append_marked(path: Path, text: str, marker: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text() if path.exists() else ""
        if marker in existing:
            print(f"[long-exposure] marker already present, skipped: {path}")
            return
        if path.exists() and args.force:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            backup = path.with_name(path.name + f".bak-{ts}")
            backup.write_text(existing)
            print(f"[long-exposure] backup: {backup}")
        path.write_text((existing.rstrip() + "\n\n" + text.strip() + "\n").lstrip())
        installed.append(path)

    target = args.target
    if target in {"claude", "all"}:
        write_new(
            root / ".claude" / "commands" / "long-exposure.md",
            """# long-exposure

Route this command to the deterministic launcher:

```bash
long-exposure launch "$ARGUMENTS"
```

Use `long-exposure status`, `long-exposure guide "<text>"`, `long-exposure stop`,
and `long-exposure resume` for controls. Do not reimplement orchestration in
the assistant prompt; the Python launcher is the source of truth.
""",
        )
    if target in {"codex", "all"}:
        write_new(
            root / ".codex" / "skills" / "long-exposure" / "SKILL.md",
            """# long-exposure

Use this skill when the user asks to start, resume, stop, inspect, or guide a
long-exposure run.

Always route launches through the deterministic CLI:

```bash
long-exposure launch "<directive>"
```

Useful controls:

```bash
long-exposure status
long-exposure guide "<next-cycle guidance>"
long-exposure stop
long-exposure resume
```

Do not duplicate launcher logic in the model. The CLI owns preflight checks,
state paths, manager notifications, and provider routing.
""",
        )
    if target in {"gemini", "all"}:
        marker = "<!-- long-exposure-adapter -->"
        append_marked(
            root / "GEMINI.md",
            f"""{marker}
## long-exposure

When the user asks to launch or control long-exposure, route to the deterministic
CLI instead of implementing orchestration in the model:

```bash
long-exposure launch "<directive>"
long-exposure status
long-exposure guide "<next-cycle guidance>"
long-exposure stop
long-exposure resume
```

Manager notices are surfaced through `long-exposure status` from
`manager_notifications.jsonl`.
""",
            marker,
        )

    if installed:
        print("[long-exposure] Installed CLI adapter files:")
        for path in installed:
            print(f"  {path}")
    else:
        print("[long-exposure] No adapter files changed.")
    return 0


def _telemetry_summarize(args: argparse.Namespace) -> int:
    instance_dir = resolve_instance_dir(args.instance_dir)
    config = load_config(Path(args.config) if args.config else None)
    summary = telemetry.summarize(
        instance_dir,
        telemetry_dir=getattr(args, "telemetry_dir", None),
        config=config,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if "error" not in summary else 1


def build_parser() -> argparse.ArgumentParser:
    parser = LongExposureArgumentParser(
        prog="long-exposure",
        description="Autonomous long-exposure research conductor.",
    )
    parser.add_argument("--score", default=exploration.DEFAULT_SCORE_PATH)
    parser.add_argument("--config", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--state", default=None)
    parser.add_argument("--instance-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    p_launch = sub.add_parser("launch", help="Preflight and launch a run")
    p_launch.add_argument("task", nargs="*", help="Task description")
    p_launch.add_argument("--skip-doctor", action="store_true")
    p_launch.add_argument("--allow-doctor-failure", action="store_true")
    p_launch.add_argument("--manager", action="store_true", help="Run manager polling while attached")
    p_launch.add_argument("--manager-interval-seconds", type=int, default=1800)
    p_launch.add_argument("--force-agent", action="store_true")
    p_launch.add_argument("--no-agent", action="store_true")
    p_launch.add_argument("--allow-pause-signal", action="store_true")

    p_start = sub.add_parser("start", help="Start exploration")
    p_start.add_argument("task", nargs="*")

    p_resume = sub.add_parser("resume", help="Resume exploration")
    p_resume.add_argument("task", nargs="*")
    p_resume.add_argument("--from-archive", default=None, metavar="FILE")

    sub.add_parser("stop", help="Send stop signal")
    sub.add_parser("clear", help="Archive and clear state")
    sub.add_parser("status", help="Print status and latest manager notice")

    p_tail = sub.add_parser("tail", help="Print a status/log file")
    p_tail.add_argument("--file", default=None)
    p_tail.add_argument("-f", "--follow", action="store_true")

    p_guide = sub.add_parser("guide", help="Queue live guidance for next cycle")
    p_guide.add_argument("guidance", nargs="+")

    p_mgr = sub.add_parser("manager", help="Manager sidecar commands")
    mgr_sub = p_mgr.add_subparsers(dest="manager_command", required=True)
    p_poll = mgr_sub.add_parser(
        "poll",
        help="Run one manager poll",
        epilog=(
            "Examples:\n"
            "  long-exposure --instance-dir DIR manager poll --no-agent\n"
            "  long-exposure --config config.yaml --score score.yaml "
            "--instance-dir DIR manager poll"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_poll.add_argument("--force-agent", action="store_true")
    p_poll.add_argument("--no-agent", action="store_true")
    p_poll.add_argument("--allow-pause-signal", action="store_true")

    p_install = sub.add_parser("cli-install", help="Install CLI routing adapter files")
    p_install.add_argument("--target", choices=("claude", "codex", "gemini", "all"), default="all")
    p_install.add_argument("--directory", default=".", help="Project directory to install adapter files into")
    p_install.add_argument("--force", action="store_true", help="Overwrite adapter files after writing backups")

    p_telem = sub.add_parser("telemetry", help="Telemetry utilities")
    telem_sub = p_telem.add_subparsers(dest="telemetry_command", required=True)
    p_telem_sum = telem_sub.add_parser(
        "summarize",
        help="Summarize local telemetry events",
        epilog=(
            "Examples:\n"
            "  long-exposure --instance-dir DIR telemetry summarize\n"
            "  long-exposure --config config.yaml --instance-dir DIR telemetry summarize\n"
            "  long-exposure telemetry summarize --telemetry-dir DIR"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_telem_sum.add_argument(
        "--telemetry-dir",
        default=None,
        help="Read telemetry from this directory instead of config or instance defaults",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "launch":
        return _launch(args)
    if args.command == "start":
        exploration._cmd_start(args)
        return 0
    if args.command == "resume":
        exploration._cmd_resume(args)
        return 0
    if args.command == "stop":
        exploration._cmd_stop(args)
        return 0
    if args.command == "clear":
        exploration._cmd_clear(args)
        return 0
    if args.command == "status":
        return _print_status(args)
    if args.command == "tail":
        return _tail(args)
    if args.command == "guide":
        return _write_guide(args)
    if args.command == "manager" and args.manager_command == "poll":
        return _manager_poll(args)
    if args.command == "cli-install":
        return _cli_install(args)
    if args.command == "telemetry" and args.telemetry_command == "summarize":
        return _telemetry_summarize(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
