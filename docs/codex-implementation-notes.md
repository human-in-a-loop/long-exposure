# Codex Implementation Notes

## Implemented Stages

| Stage | Result | Double Check |
|---|---|---|
| Provider selection | Added `llm_provider` config and `LONG_EXPOSURE_LLM_PROVIDER` override. Claude remains default. | `py_compile` passed. |
| Codex invocation | `codex exec --json` and `codex exec resume --json` normalize into the existing envelope shape. | Direct `call_claude` Codex smoke returned `ok` with usage. |
| Codex permissions | Normal Codex agent turns run with `--yolo`, `-C working_directory`, `agents.max_threads`, `agents.max_depth`, and top-level `--search` when `WebSearch` is present. | Live adapter smoke with `--yolo` and `--search` returned `ok`. |
| Sessionful Codex agents | Fresh Codex calls wrap the layered system prompt into the first prompt; returned `thread_id` is stored in `agent_sessions`. | `_call_exploration_agent` Codex smoke succeeded and stored a session id. |
| Codex compaction | Codex resumes the stored thread with the existing compaction prompt and stores the summary in `sessions.db`. | Compaction smoke reset the session and populated `agent_summaries`. |
| Pool env generalization | Pool/env helpers now select Claude or Codex names at runtime. | Compile check passed; Claude env names remain default. |
| Fan-out pinning | Clone pinning uses provider-specific force-account env vars. | Compile check passed; clone process topology unchanged. |
| Subagent guidance | Claude keeps native agent-teams block/env; Codex gets a `<subagents>` block and runtime caps. | Prompt assembly remains provider-selected. |

## Provider Names

Claude remains the default provider:

```yaml
llm_provider: claude
model: opus
```

Codex can be enabled with either config or env:

```yaml
llm_provider: codex
codex_model: gpt-5.5
codex_context_window: 400000
codex_yolo: true
codex_subagents:
  max_threads: 3
  max_depth: 1
```

```bash
LONG_EXPOSURE_LLM_PROVIDER=codex long-exposure start "..."
```

## Codex Pool Env Vars

| Purpose | Env Var |
|---|---|
| Account pool | `CODEX_ACCOUNT_POOL` or `CODEX_HOMES` |
| Force pin | `CODEX_FORCE_ACCOUNT` |
| Child config/auth home | `CODEX_HOME` |
| Pool state | `~/.codex-pool-state.json` |

Claude names remain unchanged:

| Purpose | Env Var |
|---|---|
| Account pool | `CLAUDE_ACCOUNT_POOL` or `CLAUDE_ACCOUNTS` |
| Force pin | `CLAUDE_FORCE_ACCOUNT` |
| Child config/auth home | `CLAUDE_CONFIG_DIR` |
| Pool state | `~/.claude-pool-state.json` |

## Verification So Far

Commands run:

```bash
.venv/bin/python -m py_compile long_exposure/provider.py long_exposure/orchestrator.py long_exposure/exploration.py long_exposure/conductor.py long_exposure/pool.py long_exposure/fanout.py

LONG_EXPOSURE_LLM_PROVIDER=codex .venv/bin/python - <<'PY'
from long_exposure.orchestrator import call_claude
r = call_claude('Reply with exactly: ok', '[system] obey exact output',
                model='gpt-5.5', timeout=120,
                disable_tools=True, cwd='/path/to/long-exposure')
print(r.get('result'))
print(bool(r.get('usage')))
PY
```

Observed result: `ok`, usage present.

Permission/subagent checks:

```bash
LONG_EXPOSURE_LLM_PROVIDER=codex .venv/bin/python - <<'PY'
from long_exposure.orchestrator import load_config, assemble_system_prompt
from long_exposure.conductor import build_agent_config
config = load_config()
config['agent_teams'] = True
config['effort'] = 'high'
agent_config = build_agent_config(config, {'agent_teams': True})
prompt = assemble_system_prompt(agent_config)
print(config['codex_yolo'])
print(config['codex_subagents'])
print('<subagents>' in prompt)
print('--yolo' in prompt)
PY
```

Observed result: `codex_yolo=True`, `max_threads=3`, `max_depth=1`,
and the Codex prompt contains the `<subagents>` block.

Additional targeted checks:

```bash
LONG_EXPOSURE_LLM_PROVIDER=codex CODEX_ACCOUNT_POOL=/tmp/codex-a,/tmp/codex-b \
  .venv/bin/python - <<'PY'
from long_exposure import pool
from long_exposure.orchestrator import _parse_accounts
print(pool.parse_pool_config())
print(pool.is_active())
print(_parse_accounts())
PY

LONG_EXPOSURE_LLM_PROVIDER=claude CLAUDE_ACCOUNT_POOL=/tmp/claude-a,/tmp/claude-b \
  .venv/bin/python - <<'PY'
from long_exposure import pool
from long_exposure.orchestrator import _parse_accounts
print(pool.parse_pool_config())
print(pool.is_active())
print(_parse_accounts())
PY
```

Observed result: both providers parsed their own pool envs and reported an
active two-account pool.

Claude compatibility live smoke was attempted through `call_claude`, but the
local Claude CLI returned an authentication error (`401 Invalid authentication
credentials`). This is an operator auth state issue rather than a provider
adapter failure; compile and env parsing still cover the default-path code.
