"""The startup gate: four questions asked once, before a run begins.

See docs/advanced-model-modes-plan.md (Feature 3).

  Q1  Which model should this run use?
  Q2  Which workspace directory?
  Q3  Resume a previous run, or start fresh?
  Q4  What spend limit for this run?  (only when `usage_allowance` is on)

## Where it runs, and where it must not

`launch` only. `start`, `resume` and `python -m long_exposure.exploration`
stay non-interactive, which is what keeps cron jobs, the fan-out clone spawn
and the benchmark adapter working unchanged. A clone that stopped to ask a
human which model to use would hang the barrier until the 10 h cap.

Answers are persisted to `<instance_dir>/gate_answers.json` and applied as an
in-memory config overlay, so `resume` never re-asks. A copy goes to the run's
`output/` for provenance — that is what lets a benchmark state the exact
model and caps a run actually used.

Every answer also has a flag, so a fully-flagged `launch` is headless. A
non-TTY stdin with an unanswered question exits and names the missing flag
rather than defaulting: a wrong model is an expensive mistake to discover
three hours in.

## Q1's subtlety

The shipped config pins all eight roles explicitly in `agent_models`, so
setting the global `model` alone would have no effect on any actual agent
turn. The gate therefore rewrites `model` *and* every `agent_models.*.model`
that still equals the pre-gate global default — leaving a deliberately
heterogeneous routing (a Codex worker, a Sonnet reporter) untouched. Because
that rule is subtle, the gate then prints the resulting routing table,
including each role's resolved capability profile, so one answer has one
visible consequence.

## Q3's run list

There is no instances root in the harness: instance dirs come only from
`--instance-dir` or `AGENT_INSTANCE_DIR`, and the legacy default is a single
state file in the data dir. So runs are enumerated from an append-only
registry (`~/.long-exposure/runs.jsonl`), with a scan of
`startup_gate.instances_root` as the fallback for runs that predate it.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from long_exposure import agent_routing
from long_exposure import model_profiles as _model_profiles
from long_exposure import spend_limit as _spend_limit

ANSWERS_FILENAME = "gate_answers.json"
DEFAULT_REGISTRY = Path.home() / ".long-exposure" / "runs.jsonl"
FRESH = "__fresh__"

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "model_choices": ["opus", "fable", "sonnet"],
    "instances_root": "./instances",
    "registry_path": str(DEFAULT_REGISTRY),
    # How many runs to offer in Q3. More than this and the prompt stops
    # being a menu and starts being a haystack.
    "max_runs_listed": 10,
}


class GateAbort(RuntimeError):
    """The gate could not be answered. Carries the exit code and the remedy."""

    EXIT_CODE = 4

    def __init__(self, message: str, *, missing_flag: str | None = None):
        self.missing_flag = missing_flag
        super().__init__(message)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def settings(config: dict | None) -> dict:
    raw = (config or {}).get("startup_gate")
    if not isinstance(raw, dict):
        raw = {}
    out = dict(DEFAULTS)
    out["model_choices"] = list(DEFAULTS["model_choices"])
    for key, value in raw.items():
        if key == "model_choices" and isinstance(value, (list, tuple)):
            out["model_choices"] = [str(v) for v in value if str(v).strip()]
        else:
            out[key] = value
    return out


def enabled(config: dict | None) -> bool:
    return bool(settings(config).get("enabled"))


# ---------------------------------------------------------------------------
# Run registry
# ---------------------------------------------------------------------------


def registry_path(config: dict | None) -> Path:
    return Path(str(settings(config).get("registry_path"))).expanduser()


def register_run(
    config: dict | None,
    *,
    run_id: str,
    instance_dir: Path | str | None,
    state_path: Path | str,
    task: str | None,
) -> None:
    """Append one line describing this run, so Q3 can offer it later.

    Append-only and one `write` per line so two concurrent runs (or a root
    and its clones) cannot interleave into a corrupt record. Best-effort:
    failing to register must never stop a run from starting.

    Clones are NOT registered — a fork is not a resumable run, and listing
    one would offer to resume half a fan-out branch.
    """
    try:
        from long_exposure.fanout import _is_clone

        if _is_clone():
            return
    except Exception:
        pass
    row = {
        "run_id": run_id,
        "instance_dir": str(instance_dir) if instance_dir else None,
        "state_path": str(state_path),
        "task": (task or "")[:300],
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        path = registry_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError as e:
        print(f"[gate] Run registry append skipped: {e}", flush=True)


def _read_registry(config: dict | None) -> list[dict]:
    path = registry_path(config)
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    rows: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a torn line must not hide the rest of the registry
        if isinstance(row, dict) and row.get("state_path"):
            rows.append(row)
    # Newest last in the file; newest first in the menu. Later entries for
    # the same state_path supersede earlier ones (a resumed run re-registers).
    deduped: dict[str, dict] = {}
    for row in rows:
        deduped[str(row["state_path"])] = row
    return list(reversed(list(deduped.values())))


def _scan_instances_root(config: dict | None) -> list[dict]:
    """Fallback enumeration for runs that predate the registry."""
    root = Path(str(settings(config).get("instances_root"))).expanduser()
    rows: list[dict] = []
    try:
        candidates = sorted(root.glob("*/exploration_state.json"))
    except OSError:
        return []
    for state in candidates:
        try:
            data = json.loads(state.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        rows.append(
            {
                "run_id": data.get("run_id") or state.parent.name,
                "instance_dir": str(state.parent),
                "state_path": str(state),
                "task": str(data.get("task") or "")[:300],
                "cycle": data.get("cycle"),
                "started_at": None,
                "_source": "instances_root",
            }
        )
    return list(reversed(rows))


def list_runs(config: dict | None) -> list[dict]:
    """Resumable runs, newest first, annotated with liveness and cycle.

    A run whose state file has gone is kept as a tombstone (`resumable:
    False`) rather than dropped, so an operator looking for it learns it is
    gone instead of wondering whether the gate simply missed it.
    """
    rows = _read_registry(config)
    if not rows:
        rows = _scan_instances_root(config)
    limit = int(settings(config).get("max_runs_listed") or 10)
    out: list[dict] = []
    for row in rows:
        state = Path(str(row.get("state_path")))
        entry = dict(row)
        entry["resumable"] = state.is_file()
        if entry["resumable"] and entry.get("cycle") is None:
            try:
                entry["cycle"] = (json.loads(state.read_text()) or {}).get("cycle")
            except (OSError, ValueError):
                entry["cycle"] = None
        out.append(entry)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Applying answers
# ---------------------------------------------------------------------------


def apply_model(config: dict, model: str) -> list[str]:
    """Set the global model and every agent still on the old global default.

    Returns the role names that were rewritten. A role explicitly pointed at
    a different model is left alone: that is a deliberate heterogeneous
    routing, not a stale default.
    """
    old_default = str(config.get("model") or "").strip()
    config["model"] = model
    rewritten: list[str] = []
    block = config.get("agent_models")
    if not isinstance(block, dict):
        return rewritten
    for role, routing in block.items():
        if not isinstance(routing, dict):
            continue
        current = str(routing.get("model") or "").strip()
        if not current or current == old_default:
            routing["model"] = model
            rewritten.append(role)
    return rewritten


def routing_table(config: dict) -> str:
    """The per-role provider/model/effort/profile table Q1's answer produced.

    Printed because `apply_model`'s rule is subtle enough that an operator
    should see its effect rather than infer it.
    """
    from long_exposure.conductor import build_agent_config

    lines = [
        "  role            provider  model                 effort  profile",
        "  --------------  --------  --------------------  ------  --------",
    ]
    for role in agent_routing.AGENT_TYPES:
        routing = agent_routing.resolve_agent_routing(config, role)
        provider = routing.get("provider") or config.get("llm_provider") or "?"
        model = routing.get("model") or config.get("model") or "?"
        effort = routing.get("effort") or "-"
        try:
            profile = _model_profiles.resolve(
                build_agent_config(config, {"model": model, "provider": provider})
            )
        except Exception:
            profile = "?"
        lines.append(
            f"  {role:<14}  {provider:<8}  {str(model):<20}  {effort:<6}  {profile}"
        )
    return "\n".join(lines)


def apply_answers(config: dict, answers: dict) -> dict:
    """Overlay persisted gate answers onto a config, in place.

    Returns a summary of what changed, for the banner and for provenance.
    """
    summary: dict[str, Any] = {}
    model = answers.get("model")
    if model:
        summary["model"] = model
        summary["roles_rewritten"] = apply_model(config, str(model))
    workspace = answers.get("workspace")
    if workspace:
        config["working_directory"] = str(workspace)
        summary["workspace"] = str(workspace)
    pct = answers.get("usage_run_pct")
    if pct is not None:
        block = config.get("usage_allowance")
        if not isinstance(block, dict):
            block = {}
            config["usage_allowance"] = block
        block["run_pct"] = pct
        summary["usage_run_pct"] = pct
        summary["spend_limit"] = _spend_limit.describe(config)
    return summary


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def answers_path(instance_dir: Path | str | None, state_path: Path | str) -> Path:
    """Where the answers live: beside the state file the run resumes from."""
    if instance_dir:
        return Path(instance_dir) / ANSWERS_FILENAME
    return Path(state_path).parent / ANSWERS_FILENAME


def load_answers(path: Path | str) -> dict | None:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_answers(path: Path | str, answers: dict) -> Path | None:
    record = dict(answers)
    record["answered_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        return p
    except OSError as e:
        print(f"[gate] Answers not persisted: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


def _is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _ask_choice(
    prompt: str,
    choices: list[tuple[str, str]],
    *,
    allow_other: bool = False,
    input_fn=input,
) -> str:
    """Numbered menu. Returns the chosen value, or free text via "other"."""
    while True:
        print(f"\n{prompt}")
        for i, (_value, label) in enumerate(choices, 1):
            print(f"  {i}) {label}")
        other_index = len(choices) + 1
        if allow_other:
            print(f"  {other_index}) other (type a value)")
        raw = input_fn("  choice: ").strip()
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(choices):
                return choices[n - 1][0]
            if allow_other and n == other_index:
                typed = input_fn("  value: ").strip()
                if typed:
                    return typed
                continue
            # An out-of-range NUMBER is a mistyped menu choice, not a value.
            # Falling through to the free-text branch below would turn a
            # fat-fingered "99" into a model id, and the run would fail
            # three hours later with an unrecognised model.
            print(f"  {n} is not on the menu.")
            continue
        # Accept the value typed directly, so a scripted stdin can answer
        # without counting menu positions.
        for value, _label in choices:
            if raw == value:
                return value
        if allow_other and raw:
            return raw
        print("  Not a valid choice.")


def _ask_pct(prompt: str, *, input_fn=input) -> float:
    while True:
        raw = input_fn(f"\n{prompt}\n  percent (1-100): ").strip().rstrip("%")
        try:
            pct = float(raw)
        except ValueError:
            print("  Enter a number between 1 and 100.")
            continue
        if 0 < pct <= 100:
            return pct
        print("  Enter a number between 1 and 100.")


def _run_label(entry: dict) -> str:
    cycle = entry.get("cycle")
    task = (entry.get("task") or "").strip().replace("\n", " ")
    if len(task) > 60:
        task = task[:57] + "..."
    bits = [str(entry.get("run_id") or "(unnamed)")]
    if cycle is not None:
        bits.append(f"cycle {cycle}")
    if task:
        bits.append(f'"{task}"')
    if not entry.get("resumable"):
        bits.append("[state file missing — cannot resume]")
    return " — ".join(bits)


def ask(
    config: dict,
    *,
    flags: dict | None = None,
    input_fn=input,
    tty: bool | None = None,
) -> dict:
    """Ask the gate's questions, honouring `flags` for anything pre-supplied.

    Raises GateAbort when a question has no flag and there is no TTY to ask.
    """
    flags = flags or {}
    cfg = settings(config)
    answers: dict[str, Any] = {}
    interactive = _is_tty() if tty is None else bool(tty)

    def need(flag_name: str, question: str):
        if not interactive:
            raise GateAbort(
                f"startup gate needs an answer for {question!r} but stdin is "
                f"not a TTY. Supply {flag_name}, or pass --no-gate.",
                missing_flag=flag_name,
            )

    # -- Q1: model ------------------------------------------------------
    if flags.get("model"):
        answers["model"] = str(flags["model"])
    else:
        need("--gate-model", "model")
        current = str(config.get("model") or "").strip()
        choices = [
            (m, f"{m}{'  (current)' if m == current else ''}")
            for m in cfg["model_choices"]
        ]
        if current and current not in cfg["model_choices"]:
            choices.insert(0, (current, f"{current}  (current)"))
        answers["model"] = _ask_choice(
            "Q1. Which model should this run use?",
            choices,
            allow_other=True,
            input_fn=input_fn,
        )

    # -- Q2: workspace --------------------------------------------------
    if flags.get("workspace"):
        answers["workspace"] = str(flags["workspace"])
    else:
        need("--gate-workspace", "workspace directory")
        current_ws = str(config.get("working_directory") or "").strip()
        choices = []
        if current_ws:
            choices.append((current_ws, f"{current_ws}  (from config)"))
        choices.append((str(Path.cwd()), f"{Path.cwd()}  (current directory)"))
        answers["workspace"] = _ask_choice(
            "Q2. Which workspace directory should the run work in?",
            choices,
            allow_other=True,
            input_fn=input_fn,
        )

    ws = Path(str(answers["workspace"])).expanduser()
    if not ws.is_dir():
        raise GateAbort(
            f"workspace directory does not exist: {ws}. Create it, or answer "
            "with an existing path."
        )
    if not os.access(ws, os.W_OK):
        raise GateAbort(f"workspace directory is not writable: {ws}")
    answers["workspace"] = str(ws.resolve())

    # -- Q3: resume or fresh --------------------------------------------
    if flags.get("resume"):
        # Normalize the human-friendly spellings to the sentinel here, not
        # only in the CLI: `ask` is callable directly, and a raw "fresh"
        # leaking through would be treated downstream as a state-file path.
        # An empty flag never reaches here (falsy above) — that means "not
        # supplied", which falls through to asking.
        raw_resume = str(flags["resume"]).strip()
        answers["resume"] = (
            FRESH if raw_resume.lower() in ("fresh", "new") else raw_resume
        )
    else:
        runs = list_runs(config)
        if not runs:
            answers["resume"] = FRESH
            print(
                "\nQ3. No previous runs found — starting fresh.",
                flush=True,
            )
        else:
            need("--gate-resume", "resume-or-fresh")
            choices = [(FRESH, "start a new run")]
            choices += [
                (str(r.get("state_path")), _run_label(r)) for r in runs
            ]
            answers["resume"] = _ask_choice(
                "Q3. Resume a previous run, or start fresh?",
                choices,
                input_fn=input_fn,
            )

    # Validate the resume answer for BOTH branches. Checking only the menu
    # branch let a --gate-resume flag pointing at a vanished state file
    # through, and the run would then start fresh AT THAT PATH — silently
    # losing the operator's intent to resume, which is the worst possible
    # outcome for this question. Check the path itself rather than looking
    # it up in the listing, so a run absent from the registry but present on
    # disk still resumes.
    if answers["resume"] != FRESH and not Path(answers["resume"]).is_file():
        raise GateAbort(
            f"cannot resume: no state file at {answers['resume']}. "
            "Pass --gate-resume fresh to start a new run instead."
        )

    # -- Q4: spend limit (only when the feature is on) -------------------
    allowance = _spend_limit.settings(config)
    if allowance.get("enabled"):
        if flags.get("usage_run_pct") is not None:
            answers["usage_run_pct"] = float(flags["usage_run_pct"])
        else:
            need("--gate-usage-pct", "usage limit")
            declared = allowance.get("weekly_allowance_usd") or 0
            answers["usage_run_pct"] = _ask_pct(
                "Q4. What share of your declared weekly allowance "
                f"(${float(declared):,.2f}) may this run use?\n"
                "    This is a delta on top of what you have already used, "
                "and it KILLS the run when reached.",
                input_fn=input_fn,
            )

    return answers


def print_summary(config: dict, answers: dict, summary: dict) -> None:
    """The banner: what one answer did, in full, before any spend happens."""
    print("\n[gate] Run configuration")
    print(f"  model:            {answers.get('model')}")
    roles = summary.get("roles_rewritten") or []
    if roles:
        print(f"  roles retargeted: {', '.join(roles)}")
    print(f"  workspace:        {answers.get('workspace')}")
    resume = answers.get("resume")
    print(
        "  run:              new run"
        if resume in (None, FRESH)
        else f"  run:              resuming {resume}"
    )
    print(f"  spend limit:      {_spend_limit.describe(config)}")
    print("\n[gate] Resolved per-agent routing")
    print(routing_table(config))
