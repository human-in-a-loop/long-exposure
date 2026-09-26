# Not a test module (no test_ prefix): run as a subprocess by
# tests/test_git_sync_crash.py, so it can be SIGKILLed like a real crash.
"""Run real run_exploration cycles in THIS process, with a stub provider.

  driver.py <root> <mode>
    mode=crash   cycle 1 completes; cycle 2's worker writes a partial edit,
                 touches <root>/worker_mid_turn, then sleeps (gets SIGKILLed)
    mode=resume  completes normally; records what the researcher was handed
"""
import json, sys, time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from long_exposure.exploration import run_exploration

root, mode = Path(sys.argv[1]), sys.argv[2]
ws, inst = root / "ws", root / "inst"
calls = {"n": 0}
seen = []

def agent(agent_name, agent_def, **kw):
    calls["n"] += 1
    results = kw.get("results") or {}
    seen.append({"agent": agent_name, "live_guidance": results.get("live_guidance", "")})
    (root / f"seen_{mode}.json").write_text(json.dumps(seen))
    cycle_no = len([s for s in seen if s["agent"] == "researcher"])
    if agent_name == "worker":
        (ws / "data").mkdir(exist_ok=True)
        if mode == "crash" and cycle_no == 2:
            (ws / "data" / "result.py").write_text("HALF-WRITTEN BY A DYING TURN\n")
            (ws / "data" / "scratch.tmp").write_text("partial\n")
            (root / "worker_mid_turn").touch()
            time.sleep(600)                      # killed here
        (ws / "data" / "result.py").write_text(f"complete work, {mode} cycle {cycle_no}\n")
    out = agent_def["outputs"][0]
    return {"agent": agent_name, "outputs": {out: f"{agent_name} out " + "x" * 2100},
            "usage": {"input_tokens": 100, "output_tokens": 2100}, "duration_ms": 5,
            "status": "ok", "error": None, "cost_usd": 0.0, "num_turns": 1, "tool_calls": 1}

with patch("long_exposure.exploration._call_exploration_agent", agent):
    run_exploration(score_path=str(root / "score.yaml"),
                    config_path=str(root / "config.yaml"),
                    output_dir=inst / "output",
                    state_path=inst / "exploration_state.json",
                    task_override=None, instance_dir=inst)
