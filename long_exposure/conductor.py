#!/usr/bin/env python3
"""
Multi-Agent Conductor for Agent Conditioning.

Executes a "score" — a YAML file defining multiple agents, their roles,
inputs/outputs, and execution flow (sequential and parallel steps).

Each agent is a single-turn agent-conditioning agent: it gets a full
system prompt (philosophy + framework + protocol + role) via
assemble_system_prompt(), and runs once via call_claude().

Usage:
    python -m long_exposure.conductor score.yaml [--config config.yaml] \
        [--output run_log.json] [--print-result name]
"""

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml

from long_exposure.orchestrator import (
    FRAMEWORK_PRESETS,
    PHILOSOPHY_EFFORT_MAP,
    PHILOSOPHY_PRESETS,
    ClaudeCliError,
    assemble_system_prompt,
    build_allowed_tools_flags,
    call_claude,
    estimate_tokens,
    generate_gemini_project_settings,
    generate_mcp_config,
    load_config,
    resolve_instance_dir,
)
from long_exposure import provider as _provider

# ---------------------------------------------------------------------------
# Score loading & validation
# ---------------------------------------------------------------------------


def load_score(path: str | Path) -> dict:
    """Parse and validate a score YAML file.

    A score defines:
      - task: high-level description
      - agents: dict of agent_name -> {role, philosophy, framework, model, inputs, outputs}
      - flow: list of step strings or parallel blocks

    Validates:
      - All agents referenced in flow exist
      - All inputs resolve to outputs from prior steps or score-level inputs
      - Output names are unique across all agents
    """
    path = Path(path)
    with open(path) as f:
        score = yaml.safe_load(f)

    # Basic structure checks
    if not isinstance(score, dict):
        raise ValueError("Score must be a YAML mapping")

    for required in ("task", "agents", "flow"):
        if required not in score:
            raise ValueError(f"Score missing required key: {required}")

    agents = score["agents"]
    if not isinstance(agents, dict) or not agents:
        raise ValueError("Score 'agents' must be a non-empty mapping")

    flow = score["flow"]
    if not isinstance(flow, list) or not flow:
        raise ValueError("Score 'flow' must be a non-empty list")

    # Collect all output names and check uniqueness + valid characters
    all_outputs: dict[str, str] = {}  # output_name -> agent_name
    for agent_name, agent_def in agents.items():
        for output_name in agent_def.get("outputs", []):
            if "[" in output_name or "]" in output_name:
                raise ValueError(
                    f"Output name '{output_name}' in agent '{agent_name}' "
                    f"must not contain '[' or ']' characters"
                )
            if output_name in all_outputs:
                raise ValueError(
                    f"Duplicate output '{output_name}' in agents "
                    f"'{all_outputs[output_name]}' and '{agent_name}'"
                )
            all_outputs[output_name] = agent_name

    # ---------------------------------------------------------------------
    # STALE: this strict input-resolution check is conductor-only and is
    # NOT exercised by the exploration cycle loop, which uses the looser
    # `load_exploration_score` and feeds inputs at runtime via
    # `score_inputs` (directive, plan_of_record, promise_ledger_summary,
    # live_guidance, audit_report). Running
    # `conductor.load_score` against `exploration-score.yaml` would fail
    # because the score has no top-level `inputs:` declaration listing the
    # six runtime-supplied inputs. Either declare them at score-level when
    # adding a new conductor-driven entry point, or REMOVE this validator
    # entirely as part of a future cleanup. Leaving as-is: this code path
    # is dormant for the current exploration runtime.
    # ---------------------------------------------------------------------
    # Collect score-level inputs (available from the start)
    score_inputs = set((score.get("inputs") or {}).keys())

    # Validate flow references and input resolution
    available_outputs: set[str] = set(score_inputs)

    for step in flow:
        step_agents = _extract_step_agents(step)
        for agent_name in step_agents:
            if agent_name not in agents:
                raise ValueError(
                    f"Flow references unknown agent: '{agent_name}'"
                )
            # Check that this agent's inputs are available
            agent_def = agents[agent_name]
            for input_name in agent_def.get("inputs", []):
                if input_name not in available_outputs:
                    raise ValueError(
                        f"Agent '{agent_name}' requires input '{input_name}' "
                        f"which is not available at this point in the flow"
                    )
        # After this step, its outputs become available
        for agent_name in step_agents:
            for output_name in agents[agent_name].get("outputs", []):
                available_outputs.add(output_name)

    return score


def _extract_step_agents(step) -> list[str]:
    """Extract agent names from a flow step.

    A step is either:
      - A string (single agent name)
      - A dict with key "parallel" -> list of agent names
    """
    if isinstance(step, str):
        return [step]
    if isinstance(step, dict) and "parallel" in step:
        return list(step["parallel"])
    raise ValueError(f"Invalid flow step: {step!r}")


# ---------------------------------------------------------------------------
# Agent config building
# ---------------------------------------------------------------------------


def build_agent_config(base_config: dict, agent_def: dict) -> dict:
    """Merge agent-level overrides onto the base config.

    Agent definitions can override: philosophy, framework, model, model_tier,
    working_directory, allowed_tools, cli_timeout, effort.
    """
    config = dict(base_config)

    # Direct overrides
    for key in (
        "philosophy", "framework", "model", "model_tier",
        "working_directory", "allowed_tools", "cli_timeout", "effort",
        "provider_idle_timeout_seconds", "provider_idle_poll_seconds",
    ):
        if key in agent_def:
            config[key] = agent_def[key]

    _provider.configure_provider(config)
    if (
        _provider.is_codex()
        and "model" not in agent_def
        and config.get("codex_model")
    ):
        config["model"] = config["codex_model"]
    if _provider.is_codex():
        config["context_window"] = int(config.get("codex_context_window", 400_000))
    if (
        _provider.is_gemini()
        and "model" not in agent_def
        and config.get("gemini_model")
    ):
        config["model"] = config["gemini_model"]
    if _provider.is_gemini():
        config["context_window"] = int(config.get("gemini_context_window", 1_000_000))
    if (
        _provider.is_local()
        and "model" not in agent_def
        and config.get("local_model")
    ):
        config["model"] = config["local_model"]
    if _provider.is_local():
        config["context_window"] = int(config.get("local_context_window", 32768))

    # Custom philosophy/framework from agent def
    if "custom_philosophy" in agent_def:
        config["custom_philosophy"] = agent_def["custom_philosophy"]
        config["philosophy"] = "custom"
    if "custom_framework" in agent_def:
        config["custom_framework"] = agent_def["custom_framework"]
        config["framework"] = "custom"

    return config


# ---------------------------------------------------------------------------
# Role block construction
# ---------------------------------------------------------------------------


def build_role_block(role_text: str, inputs: list[str], outputs: list[str]) -> str:
    """Format the <agent-role> XML block with I/O protocol instructions.

    This is injected into the system prompt between protocol and session layers.
    """
    parts = [
        "<agent-role>",
        role_text,
        "",
    ]

    if inputs:
        parts.append("INPUT PROTOCOL:")
        parts.append("You will receive inputs in this format:")
        for name in inputs:
            parts.append(f"  [INPUT: {name}]...content...[END INPUT]")
        parts.append("Read and use all inputs to inform your response.")
        parts.append("")

    if outputs:
        parts.append("OUTPUT PROTOCOL:")
        parts.append("You MUST wrap each output in these exact markers:")
        for name in outputs:
            parts.append(f"  [OUTPUT: {name}]...your content...[END OUTPUT]")
        parts.append(
            "These markers are machine-parsed. Do not omit them. "
            "Do not nest them. Place all substantive content inside the markers."
        )
        parts.append("")

    parts.append("</agent-role>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Agent prompt building
# ---------------------------------------------------------------------------


def build_agent_prompt(
    score_task: str,
    step_agent_name: str,
    agent_def: dict,
    results: dict[str, str],
    score_inputs: dict[str, str],
) -> str:
    """Assemble the user prompt for a single agent turn.

    Includes:
      - The overall task context
      - Input sections from resolved results or score-level inputs
      - The agent's specific instructions (if any beyond the role)
    """
    parts = [f"TASK: {score_task}", ""]

    # Gather inputs
    agent_inputs = agent_def.get("inputs", [])
    for input_name in agent_inputs:
        if input_name in results:
            value = results[input_name]
        elif input_name in score_inputs:
            value = score_inputs[input_name]
        else:
            value = f"[UNAVAILABLE: {input_name}]"
            # Surface the silent fallback to the off-nominal events log.
            # Top-level schema validation in load_exploration_score should
            # have caught typos before this; an UNAVAILABLE here at runtime
            # almost always means a runtime-injected input wasn't populated
            # this cycle (e.g. failure-streak rotation truncated the flow).
            try:
                from long_exposure import health_events as _he
                _he.append_event(
                    "input_unavailable",
                    detail=f"input '{input_name}' not in results or score_inputs",
                    agent=step_agent_name,
                )
            except Exception:
                pass
        parts.append(f"[INPUT: {input_name}]")
        parts.append(value)
        parts.append(f"[END INPUT]")
        parts.append("")

    # Agent-specific instructions (optional field in agent def)
    instructions = agent_def.get("instructions")
    if instructions:
        parts.append(f"INSTRUCTIONS: {instructions}")
        parts.append("")

    # Expected outputs reminder
    outputs = agent_def.get("outputs", [])
    if outputs:
        output_list = ", ".join(outputs)
        parts.append(
            f"Produce the following outputs using [OUTPUT: name]...[END OUTPUT] markers: {output_list}"
        )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

# Tier 1: exact regex match
_OUTPUT_RE = re.compile(
    r"\[OUTPUT:\s*(\S+)\](.*?)\[END OUTPUT\]",
    re.DOTALL,
)


def parse_outputs(
    response_text: str,
    expected_outputs: list[str],
) -> dict[str, str]:
    """Parse [OUTPUT: name]...[END OUTPUT] blocks from agent response.

    Tier 1: regex match for each expected output.
    Tier 2: if single output expected and no markers found, use entire response.
    Tier 3: missing outputs get failure markers.
    """
    found: dict[str, str] = {}

    # Tier 1: regex extraction
    for match in _OUTPUT_RE.finditer(response_text):
        name = match.group(1).strip()
        content = match.group(2).strip()
        if name in expected_outputs:
            found[name] = content

    # Tier 2: single-output fallback
    if not found and len(expected_outputs) == 1:
        found[expected_outputs[0]] = response_text.strip()

    # Tier 3: failure markers for missing outputs
    for name in expected_outputs:
        if name not in found:
            found[name] = f"[PARSE_FAILED: {name}] No output block found in agent response"

    return found


# ---------------------------------------------------------------------------
# Single agent execution
# ---------------------------------------------------------------------------


def run_agent(
    agent_name: str,
    agent_def: dict,
    score_task: str,
    base_config: dict,
    results: dict[str, str],
    score_inputs: dict[str, str],
) -> dict:
    """Execute a single agent turn via call_claude().

    Returns a dict with:
      - agent: str
      - outputs: dict[str, str]
      - usage: dict (from CLI envelope)
      - duration_ms: int
      - status: "ok" | "error"
      - error: str | None
    """
    agent_config = build_agent_config(base_config, agent_def)

    # Build role block
    role_block = build_role_block(
        role_text=agent_def.get("role", f"You are the {agent_name} agent."),
        inputs=agent_def.get("inputs", []),
        outputs=agent_def.get("outputs", []),
    )

    # Assemble system prompt with conditioning + role
    system_prompt = assemble_system_prompt(agent_config, role=role_block)

    # Build user prompt
    user_prompt = build_agent_prompt(
        score_task=score_task,
        step_agent_name=agent_name,
        agent_def=agent_def,
        results=results,
        score_inputs=score_inputs,
    )

    # Build permission flags
    permission_flags = build_allowed_tools_flags(agent_config)

    # Generate MCP config if agent needs it. Per-instance path keeps concurrent
    # conductor runs from racing on a single mcp_config.json file.
    mcp_config = None
    if agent_def.get("mcp", False):
        db_path = agent_config.get("compact_db", "")
        if db_path and _provider.is_claude():
            mcp_config = generate_mcp_config(
                db_path,
                instance_dir=agent_config.get("instance_dir"),
            )
    if _provider.is_gemini():
        generate_gemini_project_settings(agent_config)

    try:
        envelope = call_claude(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=agent_config.get("model", "opus"),
            timeout=agent_config.get("cli_timeout", 0),
            disable_tools=agent_def.get("disable_tools", False),
            mcp_config=mcp_config,
            cwd=agent_config.get("working_directory") or None,
            permission_flags=permission_flags or None,
            effort=agent_def.get("effort") or PHILOSOPHY_EFFORT_MAP.get(
                agent_config.get("philosophy", "efficient"), "high"
            ),
        )

        response_text = envelope.get("result", "")
        expected_outputs = agent_def.get("outputs", [])
        outputs = parse_outputs(response_text, expected_outputs)

        return {
            "agent": agent_name,
            "outputs": outputs,
            "usage": envelope.get("usage", {}),
            "duration_ms": envelope.get("duration_ms", 0),
            "status": "ok",
            "error": None,
            "response_tokens": estimate_tokens(response_text),
        }

    except Exception as e:
        error_msg = str(e)
        expected_outputs = agent_def.get("outputs", [])
        failure_outputs = {
            name: f"[FAILED: {agent_name}] {error_msg}"
            for name in expected_outputs
        }
        return {
            "agent": agent_name,
            "outputs": failure_outputs,
            "usage": {},
            "duration_ms": 0,
            "status": "error",
            "error": error_msg,
            "response_tokens": 0,
        }


# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------


def _execute_parallel_block(
    parallel_agents: list[str],
    score: dict,
    base_config: dict,
    results: dict[str, str],
    run_log: dict,
) -> list[dict]:
    """Execute a parallel block of agents using ThreadPoolExecutor.

    Takes a frozen snapshot of results so parallel agents don't see
    each other's outputs.
    """
    frozen_results = dict(results)
    score_inputs = score.get("inputs") or {}
    agent_results = []

    with ThreadPoolExecutor(max_workers=len(parallel_agents)) as executor:
        futures = {}
        for agent_name in parallel_agents:
            agent_def = score["agents"][agent_name]
            future = executor.submit(
                run_agent,
                agent_name=agent_name,
                agent_def=agent_def,
                score_task=score["task"],
                base_config=base_config,
                results=frozen_results,
                score_inputs=score_inputs,
            )
            futures[future] = agent_name

        for future in as_completed(futures):
            agent_name = futures[future]
            try:
                result = future.result()
            except Exception as e:
                # Shouldn't happen since run_agent catches exceptions,
                # but handle just in case
                expected_outputs = score["agents"][agent_name].get("outputs", [])
                result = {
                    "agent": agent_name,
                    "outputs": {
                        name: f"[FAILED: {agent_name}] {e}"
                        for name in expected_outputs
                    },
                    "usage": {},
                    "duration_ms": 0,
                    "status": "error",
                    "error": str(e),
                    "response_tokens": 0,
                }
            agent_results.append(result)

    return agent_results


# ---------------------------------------------------------------------------
# Flow execution
# ---------------------------------------------------------------------------


def execute_flow(score: dict, base_config: dict) -> dict:
    """Walk the flow list, dispatch sequential/parallel steps, build run log."""
    run_log = create_run_log(score)
    results: dict[str, str] = {}
    score_inputs = score.get("inputs") or {}

    # Score-level inputs are immediately available
    results.update(score_inputs)

    for step in score["flow"]:
        if isinstance(step, str):
            # Sequential: single agent
            agent_name = step
            agent_def = score["agents"][agent_name]
            _log_step_start(run_log, agent_name, parallel=False)

            result = run_agent(
                agent_name=agent_name,
                agent_def=agent_def,
                score_task=score["task"],
                base_config=base_config,
                results=results,
                score_inputs=score_inputs,
            )

            # Merge outputs into results
            results.update(result["outputs"])
            _log_step_result(run_log, result)

            _print_step_status(result)

        elif isinstance(step, dict) and "parallel" in step:
            # Parallel block
            parallel_agents = step["parallel"]
            _log_step_start(run_log, parallel_agents, parallel=True)

            agent_results = _execute_parallel_block(
                parallel_agents=parallel_agents,
                score=score,
                base_config=base_config,
                results=results,
                run_log=run_log,
            )

            # Merge all outputs
            for result in agent_results:
                results.update(result["outputs"])
                _log_step_result(run_log, result)
                _print_step_status(result)

        else:
            raise ValueError(f"Invalid flow step: {step!r}")

    finalize_run_log(run_log, results)
    return run_log


# ---------------------------------------------------------------------------
# Run log management
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent


def create_run_log(score: dict) -> dict:
    """Create a new run log dict."""
    return {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "task": score.get("task", ""),
        "agents": list(score.get("agents", {}).keys()),
        "flow": score.get("flow", []),
        "steps": [],
        "final_outputs": {},
        "status": "running",
        "total_input_tokens": 0,
        "total_output_tokens": 0,
    }


def _log_step_start(run_log: dict, agents, parallel: bool) -> None:
    """Log the start of a step (informational print only)."""
    if parallel:
        names = ", ".join(agents)
        print(f"[conductor] parallel: {names}", flush=True)
    else:
        print(f"[conductor] running: {agents}", flush=True)


def _log_step_result(run_log: dict, result: dict) -> None:
    """Append a step result to the run log."""
    usage = result.get("usage", {})
    run_log["steps"].append({
        "agent": result["agent"],
        "status": result["status"],
        "error": result.get("error"),
        "outputs": list(result["outputs"].keys()),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "duration_ms": result.get("duration_ms", 0),
    })
    run_log["total_input_tokens"] += usage.get("input_tokens", 0)
    run_log["total_output_tokens"] += usage.get("output_tokens", 0)


def _print_step_status(result: dict) -> None:
    """Print step completion status to stderr."""
    status = result["status"]
    agent = result["agent"]
    duration_s = result.get("duration_ms", 0) / 1000
    usage = result.get("usage", {})
    in_tok = usage.get("input_tokens", 0)
    out_tok = usage.get("output_tokens", 0)

    if status == "ok":
        print(
            f"[conductor]   {agent}: ok "
            f"({duration_s:.1f}s, {in_tok}in/{out_tok}out)",
            flush=True,
        )
    else:
        error = result.get("error", "unknown")
        print(
            f"[conductor]   {agent}: FAILED — {error}",
            file=sys.stderr,
            flush=True,
        )


def finalize_run_log(run_log: dict, results: dict[str, str]) -> None:
    """Finalize the run log with completion info."""
    run_log["finished_at"] = datetime.now(timezone.utc).isoformat()
    run_log["final_outputs"] = results
    errors = [s for s in run_log["steps"] if s["status"] == "error"]
    run_log["status"] = "completed_with_errors" if errors else "completed"


def save_run_log(run_log: dict, output_path: str | Path | None = None) -> Path:
    """Save run log to JSON file. Returns the path written."""
    if output_path:
        path = Path(output_path)
    else:
        # Writable data dir helper handles the wheel-install / read-only case.
        from long_exposure.exploration import _user_writable_data_dir
        data_dir = _user_writable_data_dir()
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = data_dir / f"run_{ts}.json"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run_log, indent=2, default=str))
    return path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        prog="agent-conductor",
        description="Execute a multi-agent score via agent-conditioning",
    )
    parser.add_argument(
        "score",
        help="Path to score YAML file",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to base config.yaml (default: agent/config.yaml)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path to save run log JSON (default: data/run_<timestamp>.json)",
    )
    parser.add_argument(
        "--print-result",
        default=None,
        metavar="NAME",
        help="Print the value of a specific output to stdout after execution",
    )
    parser.add_argument(
        "--instance-dir",
        default=None,
        help=(
            "Per-session workspace directory. Scopes mcp_config.json (and, if "
            "--output is unset, run log) to this dir so concurrent conductor "
            "runs don't collide. Also available via AGENT_INSTANCE_DIR env var."
        ),
    )

    args = parser.parse_args()

    # Resolve instance dir (None preserves legacy single-session behavior).
    instance_dir = resolve_instance_dir(args.instance_dir)

    # Load base config
    base_config = load_config(args.config)
    # Propagate instance_dir so nested build_agent_config inherits it.
    base_config["instance_dir"] = str(instance_dir) if instance_dir is not None else None

    # Load and validate score
    try:
        score = load_score(args.score)
    except (ValueError, FileNotFoundError, yaml.YAMLError) as e:
        print(f"Error loading score: {e}", file=sys.stderr)
        sys.exit(1)

    print(
        f"[conductor] task: {score['task']}",
        flush=True,
    )
    print(
        f"[conductor] agents: {', '.join(score['agents'].keys())}",
        flush=True,
    )

    # Execute flow
    run_log = execute_flow(score, base_config)

    # Save run log. Fall back to <instance-dir>/run_<ts>.json when --output
    # is unset and an instance dir is configured.
    output_arg = args.output
    if output_arg is None and instance_dir is not None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_arg = str(instance_dir / f"run_{ts}.json")
    log_path = save_run_log(run_log, output_arg)
    print(f"[conductor] run log: {log_path}", flush=True)
    print(
        f"[conductor] status: {run_log['status']} "
        f"({run_log['total_input_tokens']}in/{run_log['total_output_tokens']}out)",
        flush=True,
    )

    # Print specific result if requested
    if args.print_result:
        final = run_log.get("final_outputs", {})
        if args.print_result in final:
            print(final[args.print_result])
        else:
            available = ", ".join(final.keys())
            print(
                f"Output '{args.print_result}' not found. "
                f"Available: {available}",
                file=sys.stderr,
            )
            sys.exit(1)

    # Exit with error code if there were failures
    if run_log["status"] == "completed_with_errors":
        sys.exit(2)


if __name__ == "__main__":
    main()
