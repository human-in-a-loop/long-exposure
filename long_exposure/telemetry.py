"""Opt-in local telemetry for long-exposure.

Telemetry is pure observability. Public functions never raise and callers must
not branch on their return values. The default is disabled.
"""

from __future__ import annotations

import json
import os
import hashlib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_MAX_EVENT_BYTES = 65_536
DEFAULT_MAX_TEXT_FIELD_CHARS = 2_000

_enabled = False
_run_id: str | None = None
_base_dir: Path | None = None
_events_path: Path | None = None
_config: dict[str, Any] = {}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_enabled(config: dict | None = None) -> bool:
    """Return whether telemetry should be active for this config/env."""
    try:
        if _env_truthy(os.environ.get("LONG_EXPOSURE_TELEMETRY")):
            return True
        if config:
            telem = config.get("telemetry") or {}
            if isinstance(telem, dict):
                return bool(telem.get("enabled", False))
    except Exception:
        return False
    return _enabled


def _telemetry_cfg(config: dict | None = None) -> dict[str, Any]:
    cfg = dict((config or {}).get("telemetry") or {})
    if os.environ.get("LONG_EXPOSURE_TELEMETRY_LEVEL"):
        cfg["level"] = os.environ["LONG_EXPOSURE_TELEMETRY_LEVEL"]
    cfg.setdefault("level", "standard")
    cfg.setdefault("include_prompt_text", False)
    cfg.setdefault("include_response_text", False)
    cfg.setdefault("include_tool_stdout", False)
    cfg.setdefault("max_text_field_chars", DEFAULT_MAX_TEXT_FIELD_CHARS)
    cfg.setdefault("max_event_bytes", DEFAULT_MAX_EVENT_BYTES)
    cfg.setdefault("redact_env", True)
    cfg.setdefault("redact_paths", False)
    return cfg


def configure(
    config: dict | None = None,
    instance_dir: Path | str | None = None,
    run_id: str | None = None,
) -> None:
    """Configure telemetry for a run. Safe to call repeatedly."""
    global _enabled, _run_id, _base_dir, _events_path, _config
    try:
        active = is_enabled(config)
        _enabled = bool(active)
        _run_id = run_id
        _config = _telemetry_cfg(config)
        if not _enabled:
            _base_dir = None
            _events_path = None
            return
        out_dir = _config.get("output_dir")
        if out_dir:
            base = Path(str(out_dir)).expanduser()
        elif instance_dir:
            base = Path(instance_dir) / "telemetry"
        else:
            base = Path("long_exposure/data/telemetry")
        _base_dir = base
        _events_path = base / "events.jsonl"
        base.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "created_or_updated_at": _utc_iso(),
            "run_id": run_id,
            "enabled": True,
            "level": _config.get("level"),
            "events_path": str(_events_path),
            "privacy": {
                "include_prompt_text": bool(_config.get("include_prompt_text")),
                "include_response_text": bool(_config.get("include_response_text")),
                "include_tool_stdout": bool(_config.get("include_tool_stdout")),
                "redact_paths": bool(_config.get("redact_paths")),
                "redact_env": bool(_config.get("redact_env")),
            },
        }
        (base / "telemetry_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True)
        )
    except Exception:
        _enabled = False
        _base_dir = None
        _events_path = None


def hash_value(value: Any) -> str:
    try:
        raw = json.dumps(value, sort_keys=True, default=str)
    except Exception:
        raw = str(value)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _looks_like_path(text: str) -> bool:
    return (
        "/" in text
        or "\\" in text
        or text.startswith("~")
        or text.startswith(".")
    )


def _redact_string(value: str) -> str:
    if bool(_config.get("redact_paths", False)) and _looks_like_path(value):
        try:
            name = Path(value).name
        except Exception:
            name = ""
        suffix = f":{name}" if name else ""
        return f"[path:{hash_value(value)}{suffix}]"
    return value


_PROMPT_KEYS = {
    "prompt",
    "prompt_text",
    "prompts",
    "system_prompt",
    "system_prompt_text",
    "user_prompt",
    "user_prompt_text",
    "role_prompt",
    "role_prompt_text",
    "directive",
    "task",
    "messages",
    "raw_messages",
}
_RESPONSE_KEYS = {
    "response",
    "raw_response",
    "responses",
    "completion",
    "completions",
    "output",
    "outputs",
    "output_text",
    "response_text",
    "transcript",
    "raw_transcript",
    "content",
}
_TOOL_STDOUT_KEYS = {
    "stdout",
    "stderr",
    "tool_stdout",
    "tool_stderr",
    "command_output",
    "command_stdout",
    "command_stderr",
}
_ENV_KEYS = {"env", "environment", "environ", "os_environ"}


def _sanitize_by_key(value: Any) -> Any:
    """Drop opt-in-sensitive fields before truncation/redaction.

    This central guard keeps telemetry privacy robust even if a future caller
    accidentally passes prompt text, response text, tool output, or env maps.
    """
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_str = str(key)
            normalized = key_str.strip().lower()
            if normalized in _PROMPT_KEYS and not bool(_config.get("include_prompt_text")):
                out[key_str] = "[omitted:prompt_text_disabled]"
                continue
            if normalized in _RESPONSE_KEYS and not bool(_config.get("include_response_text")):
                out[key_str] = "[omitted:response_text_disabled]"
                continue
            if normalized in _TOOL_STDOUT_KEYS and not bool(_config.get("include_tool_stdout")):
                out[key_str] = "[omitted:tool_stdout_disabled]"
                continue
            if normalized in _ENV_KEYS and bool(_config.get("redact_env", True)):
                out[key_str] = "[omitted:env_redaction_enabled]"
                continue
            out[key_str] = _sanitize_by_key(item)
        return out
    if isinstance(value, list):
        return [_sanitize_by_key(v) for v in value]
    if isinstance(value, tuple):
        return [_sanitize_by_key(v) for v in value]
    return value


def _truncate(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return _redact_string(value)[:limit]
    if isinstance(value, list):
        return [_truncate(v, limit) for v in value[:200]]
    if isinstance(value, tuple):
        return [_truncate(v, limit) for v in list(value)[:200]]
    if isinstance(value, dict):
        return {str(k)[:200]: _truncate(v, limit) for k, v in list(value.items())[:200]}
    return value


def redact_account_usage(accounts: dict[str, Any] | None) -> dict[str, Any]:
    """Hash account/path keys before telemetry capture."""
    safe: dict[str, Any] = {}
    try:
        for key, value in (accounts or {}).items():
            safe[f"account:{hash_value(key)}"] = value
    except Exception:
        return {}
    return safe


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _usage_value(usage: dict[str, Any], *keys: str) -> int:
    for key in keys:
        if key in usage:
            return _safe_int(usage.get(key))
    return 0


def _usage_counter(usage: dict[str, Any]) -> Counter:
    return Counter({
        "input_tokens": _usage_value(usage, "input_tokens"),
        "output_tokens": _usage_value(usage, "output_tokens"),
        "cache_read_input_tokens": _usage_value(
            usage, "cache_read_input_tokens", "cached_input_tokens"
        ),
        "cache_creation_input_tokens": _usage_value(usage, "cache_creation_input_tokens"),
        "reasoning_output_tokens": _usage_value(usage, "reasoning_output_tokens"),
    })


def _context_tokens_from_usage(usage: dict[str, Any]) -> int:
    counts = _usage_counter(usage)
    cache_read = _safe_int(usage.get("cache_read_input_tokens"))
    return (
        counts["input_tokens"]
        + counts["output_tokens"]
        + cache_read
        + counts["cache_creation_input_tokens"]
    )


def emit(
    event_type: str,
    *,
    phase: str | None = None,
    cycle: int | None = None,
    agent: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    status: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    """Append one telemetry event if enabled. Never raises."""
    try:
        if not _enabled or _events_path is None:
            return
        text_limit = int(_config.get("max_text_field_chars", DEFAULT_MAX_TEXT_FIELD_CHARS))
        max_bytes = int(_config.get("max_event_bytes", DEFAULT_MAX_EVENT_BYTES))
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "ts": _utc_iso(),
            "run_id": _run_id,
            "event_type": str(event_type),
            "data": _truncate(_sanitize_by_key(data or {}), text_limit),
        }
        if phase is not None:
            record["phase"] = str(phase)
        if cycle is not None:
            record["cycle"] = int(cycle)
        if agent is not None:
            record["agent"] = str(agent)
        if provider is not None:
            record["provider"] = str(provider)
        if model is not None:
            record["model"] = str(model)
        if status is not None:
            record["status"] = str(status)

        line = json.dumps(record, sort_keys=True, default=str)
        raw = line.encode("utf-8", errors="replace")
        if len(raw) > max_bytes:
            record["data"] = {
                "truncated": True,
                "original_event_hash": hash_value(record.get("data")),
            }
            line = json.dumps(record, sort_keys=True, default=str)
        _events_path.parent.mkdir(parents=True, exist_ok=True)
        with _events_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        return


def emit_agent_result(
    agent_name: str,
    result: dict[str, Any],
    *,
    cycle: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    context_window: int | None = None,
) -> None:
    try:
        usage = result.get("usage") or {}
        outputs = result.get("outputs") or {}
        data = {
            "duration_ms": result.get("duration_ms", 0),
            "usage": usage,
            "output_keys": sorted(outputs.keys()),
            "error_class": (
                type(result.get("error")).__name__
                if result.get("error") is not None else None
            ),
            "error_preview": str(result.get("error") or "")[:300],
        }
        if context_window:
            context_tokens = _context_tokens_from_usage(usage)
            window = max(int(context_window), 1)
            data["context_window"] = window
            data["context_tokens"] = context_tokens
            data["context_ratio"] = context_tokens / window
        emit(
            "agent_call_end",
            phase="agent",
            cycle=cycle,
            agent=agent_name,
            provider=provider,
            model=model,
            status=result.get("status"),
            data=data,
        )
    except Exception:
        return


def _summary_base_dir(
    instance_dir: Path | str | None = None,
    *,
    telemetry_dir: Path | str | None = None,
    config: dict[str, Any] | None = None,
) -> Path:
    if telemetry_dir:
        return Path(telemetry_dir).expanduser()
    cfg = _telemetry_cfg(config)
    if cfg.get("output_dir"):
        return Path(str(cfg["output_dir"])).expanduser()
    if instance_dir:
        return Path(instance_dir) / "telemetry"
    if _base_dir is not None:
        return _base_dir
    return Path("long_exposure/data/telemetry")


def summarize(
    instance_dir: Path | str | None = None,
    *,
    telemetry_dir: Path | str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read local telemetry events and write a deterministic rollup."""
    try:
        base = _summary_base_dir(instance_dir, telemetry_dir=telemetry_dir, config=config)
        events_path = base / "events.jsonl"
        events = []
        raw_text = ""
        if events_path.exists():
            raw_text = events_path.read_text()
            for raw in raw_text.splitlines():
                if not raw.strip():
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict):
                    events.append(ev)
        if not events and not events_path.exists():
            return {
                "schema_version": SCHEMA_VERSION,
                "generated_at": _utc_iso(),
                "events": 0,
                "cycles": {"min": None, "max": None, "count": 0},
                "by_type": {},
                "by_status": {},
                "by_agent": {},
                "by_provider": {},
                "usage": {},
                "context": {},
                "snapshot": {
                    "events_path": str(events_path),
                    "events_sha256": hashlib.sha256(b"").hexdigest(),
                    "first_event_ts": None,
                    "last_event_ts": None,
                },
            }
        by_type = Counter(str(ev.get("event_type")) for ev in events)
        by_status = Counter(str(ev.get("status")) for ev in events if ev.get("status") is not None)
        by_agent = Counter(str(ev.get("agent")) for ev in events if ev.get("agent") is not None)
        by_provider = Counter(str(ev.get("provider")) for ev in events if ev.get("provider") is not None)
        cycles = [ev.get("cycle") for ev in events if isinstance(ev.get("cycle"), int)]
        usage = Counter()
        context_max = {"ratio": 0.0, "tokens": 0, "agent": None, "cycle": None}
        for ev in events:
            data = ev.get("data") or {}
            u = data.get("usage") if isinstance(data, dict) else None
            if isinstance(u, dict):
                usage.update(_usage_counter(u))
            if isinstance(data, dict):
                ratio = data.get("context_ratio")
                if isinstance(ratio, (int, float)) and ratio >= context_max["ratio"]:
                    context_max = {
                        "ratio": float(ratio),
                        "tokens": _safe_int(data.get("context_tokens")),
                        "agent": ev.get("agent"),
                        "cycle": ev.get("cycle"),
                    }
        event_ts = [
            str(ev.get("ts"))
            for ev in events
            if ev.get("ts") is not None
        ]
        summary = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _utc_iso(),
            "events": len(events),
            "cycles": {
                "min": min(cycles) if cycles else None,
                "max": max(cycles) if cycles else None,
                "count": len(set(cycles)),
            },
            "by_type": dict(sorted(by_type.items())),
            "by_status": dict(sorted(by_status.items())),
            "by_agent": dict(sorted(by_agent.items())),
            "by_provider": dict(sorted(by_provider.items())),
            "usage": {k: v for k, v in sorted(usage.items()) if v},
            "context": {
                "max_ratio": context_max["ratio"],
                "max_tokens": context_max["tokens"],
                "max_agent": context_max["agent"],
                "max_cycle": context_max["cycle"],
            },
            "snapshot": {
                "events_path": str(events_path),
                "events_sha256": hashlib.sha256(
                    raw_text.encode("utf-8", errors="replace")
                ).hexdigest(),
                "first_event_ts": min(event_ts) if event_ts else None,
                "last_event_ts": max(event_ts) if event_ts else None,
            },
        }
        rollups = base / "rollups"
        rollups.mkdir(parents=True, exist_ok=True)
        (rollups / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
        md = [
            "# Telemetry Summary",
            "",
            f"- Events: {summary['events']}",
            f"- Cycle range: {summary['cycles']['min']} - {summary['cycles']['max']}",
            f"- Event snapshot SHA-256: {summary['snapshot']['events_sha256']}",
            "",
            "## Event Types",
        ]
        for key, value in summary["by_type"].items():
            md.append(f"- {key}: {value}")
        md += ["", "## Statuses"]
        for key, value in summary["by_status"].items():
            md.append(f"- {key}: {value}")
        (rollups / "summary.md").write_text("\n".join(md) + "\n")
        lessons = base / "lessons"
        lessons.mkdir(parents=True, exist_ok=True)
        lesson_lines = [
            "# Telemetry Lessons Shell",
            "",
            "This file is deterministic scaffolding for later human or agent review.",
            "",
            "## Signals",
            f"- Events observed: {summary['events']}",
            f"- Cycle range: {summary['cycles']['min']} - {summary['cycles']['max']}",
        ]
        error_count = sum(
            count for status, count in summary["by_status"].items()
            if status in {"error", "rate_limit", "degraded"}
        )
        if error_count:
            lesson_lines.append(
                f"- Reliability: {error_count} error/rate-limit/degraded events need review."
            )
        if summary["by_type"].get("fanout_collapsed"):
            lesson_lines.append(
                "- Fan-out: review branch outcomes and deliverable statuses for usefulness."
            )
        if summary["by_type"].get("manager_poll_end"):
            lesson_lines.append(
                "- Manager: review verdict distribution and whether guidance was followed."
            )
        if summary["context"]["max_ratio"]:
            lesson_lines.append(
                "- Context: review agents with high context ratios before they approach compaction."
            )
        if summary["by_type"].get("report_pdf_render_end"):
            lesson_lines.append(
                "- Reports: review PDF render statuses for formatting/toolchain reliability."
            )
        if not lesson_lines[-1].startswith("-"):
            lesson_lines.append("- No specific review signals were detected in this rollup.")
        lesson_lines += [
            "",
            "## Agent Review Prompt",
            "Use `rollups/summary.json` and `events.jsonl` as evidence. Cite event types,",
            "cycles, and artifact paths. Label causal claims as inferences unless directly",
            "supported by telemetry fields.",
        ]
        (lessons / "lessons_summary.md").write_text("\n".join(lesson_lines) + "\n")
        return summary
    except Exception as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _utc_iso(),
            "events": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
