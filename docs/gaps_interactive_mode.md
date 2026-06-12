# Interactive Transport — Design, Gaps & Weak Points

**Status:** implemented, opt-in, default-off. Companion to `docs/gaps.md`
(same table format below). Covers the `claude_transport: interactive` mode
added to route Claude agent turns through a persistent interactive Claude Code
session instead of `claude -p`.

---

## Why this exists

From **2026-06-15**, Anthropic's docs state that *"Claude Agent SDK and
`claude -p` usage no longer counts toward your Claude plan's usage limits"* —
it draws from a separate monthly Agent SDK credit pool (Pro $20 / Max 5x $100 /
Max 20x $200), then standard API rates as overage. Meanwhile *"using Claude
Code in the terminal or your IDE continues to use your subscription usage
limits exactly as before."* (Sources: `code.claude.com/docs/en/authentication`
note; `support.claude.com` article 15036540.)

long-exposure runs every agent turn as `claude -p`, so after 2026-06-15 those
turns consume the metered SDK pool. Interactive mode routes them through a real
interactive session so they draw on the included subscription instead.

> **Compliance is unconfirmed.** Driving an unattended interactive session to
> perform `-p`-equivalent work may fall under Anthropic's prohibitions on
> circumventing usage limits. This feature is opt-in and **must not be used
> until the operator has Anthropic's explicit go-ahead.** If not permitted,
> keep `claude_transport: headless` (the default) and accept SDK-credit
> billing. Nothing about the headless path is changed by this feature.

## How it works

```
 long-exposure cycle loop (UNCHANGED — Python push model)
   └─ _call_exploration_agent assembles system_prompt + user_prompt
        └─ interactive_transport.run_turn(...)  ── returns the SAME envelope
              {result, usage, duration_ms, session_id} as _invoke_claude
   ┌──────────────────────────────────────────────────────────────────┐
   │ persistent interactive `claude` session (tmux) = the DRIVER       │
   │  seed prompt + Stop hook → loop:                                  │
   │   fetch_next_task (MCP bridge, long-poll)                         │
   │     → spawn ONE subagent (Task tool) = fresh context              │
   │         subagent reads brief from prompt_file, does the work,     │
   │         writes <turn>.out + <turn>.out.done                       │
   │     → fetch_next_task again                                       │
   └──────────────────────────────────────────────────────────────────┘
```

- **Files** (`long_exposure/`): `interactive_transport.py` (lifecycle +
  `run_turn`), `interactive_bridge.py` (MCP stdio `fetch_next_task`),
  `interactive_stop_hook.py` (keeps the driver looping),
  `interactive_pretool_hook.py` (scoped-mode anti-hang deny).
- **Integration:** one branch in `_call_exploration_agent` (force fresh-context
  + call `run_turn`), guarded by `interactive_transport.is_enabled(config)`.
  Default-off ⇒ zero behavior change; the `-p`/codex/gemini/local paths are
  untouched (verified: full existing suite still passes, 161/161 of the
  previously-passing tests).
- **Why subagents:** each turn needs a fresh context with full conditioning —
  the `claude -p` semantics. A subagent gives that inside one interactive
  session. The driver context stays tiny (only turn-ids/paths), so the session
  is recycled every `interactive_recycle_turns` (stateless driver = free
  relaunch).
- **Why file-based completion (not an MCP `submit_result`):** the worker writes
  its output via the always-available `Write` tool + a `.done` marker, so the
  contract does not depend on subagents being able to call MCP tools, and large
  deliverables are not JSON-escaped through a tool argument.
- **Output fidelity:** `run_turn`'s returned `result` is fed to long-exposure's
  existing `parse_outputs` + file-gate rescue, so a malformed `[OUTPUT]` block
  degrades **identically to headless** mode.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `claude_transport` | `headless` | `headless` (`claude -p`) or `interactive` |
| `interactive_driver_model` | `sonnet` | model for the lightweight driver loop |
| `interactive_permission_mode` | `skip` | `skip` (`--dangerously-skip-permissions`, like codex/gemini `--yolo`) or `scoped` (allowlist + PreToolUse deny + trust-dialog handling) |
| `interactive_recycle_turns` | `40` | relaunch driver every N turns to bound context |
| `interactive_fetch_window_seconds` | `30` | bridge long-poll window |
| `interactive_turn_timeout_seconds` | `1800` | max wait per turn |

Env override: `LONG_EXPOSURE_CLAUDE_TRANSPORT=interactive`. Requires `tmux` on
PATH and a logged-in Claude account (`claude login`).

## Verification performed

- Deterministic bridge protocol test (subprocess JSON-RPC) — pass.
- `run_turn` unit tests with mocked session + simulated worker: envelope shape,
  brief contents, timeout, session-death fail-fast, recycle — 9/9 pass.
- Full existing suite unchanged (1 unrelated pre-existing failure,
  `test_reanchor_is_injected_before_compaction_threshold`, fails identically on
  baseline).
- **Live end-to-end smoke** (scoped mode, real interactive session): a turn
  returned `"The capital of France is Paris … [OUTPUT: answer] …"` in ~30s; the
  driver session was torn down with **no orphaned tmux/claude processes**.

---

## Deferred items & weak points (gaps table)

| Gap | Impact | Justification | Status |
|---|---:|---|---|
| **Compliance with Anthropic usage policy is unconfirmed.** | High | Routing unattended interactive work specifically to avoid the metered SDK pool may be limit-circumvention. Operator will confirm with Anthropic; until then headless remains the only sanctioned path. Feature is opt-in and default-off. | **Blocking for use** |
| **Multi-account pooling is deferred.** | High | `pool.py`/`unified_pool.py` rotation is driven by per-call `-p` rate-limit detection and per-account `CLAUDE_FORCE_ACCOUNT` pinning. Interactive mode runs on the single logged-in account; pool init is skipped and a warning is printed if a pool is configured. Re-introducing pooling needs N interactive sessions (one per account) and an interactive rate-limit signal. | Deferred (guarded) |
| **Parallel cycle fan-out is deferred.** | High | Each clone would need its own interactive session + bridge. The fan-out trigger is skipped in interactive mode, so a researcher's `<parallel_cycle_fanout>` block is ignored and the cycle stays sequential. Whole-cycle parallelism via parallel subagents within one session is the planned phase 2. | Deferred (guarded) |
| **No provider-native session continuity / per-turn compaction.** | Medium | Interactive turns are always fresh-context (no `--resume`), so per-agent provider conversation history and the 90%-context compaction path do not apply. `usage={}` ⇒ `_total_context_tokens` returns 0 ⇒ compaction/reanchor naturally no-op. Continuity is fully file-backed (POR, ledger, inputs, gems, restored summaries), which long-exposure already treats as the durable source. Acceptable; matches the documented "durable continuity is file-backed" invariant. | By design |
| **`tmux` is a hard dependency.** | Medium | The persistent PTY is provided by tmux; if absent, `run_turn` raises a clear `ClaudeCliError` at launch. A stdlib-`pty` fallback would remove the dependency. `long-exposure-doctor` does not yet check for tmux when interactive mode is selected. | Deferred |
| **Driver/worker run with broad permissions.** | Medium | `skip` mode uses `--dangerously-skip-permissions` (consistent with the existing codex/gemini `--yolo` posture and the "run in a sandbox" requirement). `scoped` mode constrains to an allowlist + PreToolUse deny but still grants the worker broad tools to do real work. Per-agent tool scoping (mapping each agent_def's `allowed_tools` onto the subagent) is not yet wired. | Deferred |
| **Per-agent model selection is advisory for the worker subagent.** | Medium | The agent's `model` is recorded in the task, and the seed prompt now instructs the driver to pass it as the Task tool's model parameter when supported. Whether the worker actually runs at the requested model depends on the Task tool honoring that parameter; `run_turn` prints a one-time advisory the first time an agent's model differs from `interactive_driver_model`. | Mitigated (advisory) |
| **Driver-loop fidelity depends on the model following the seed.** | Medium | The driver must call the MCP tool and spawn a subagent per task. A weak driver model improvised a shell call in testing; mitigated by (a) requiring a capable `interactive_driver_model` (default `sonnet`), (b) a firm seed prompt, and (c) the scoped-mode PreToolUse deny that converts a stray tool call into model-visible feedback instead of a hang. A wedged driver is bounded by the Stop-hook safety cap and the per-turn timeout; a task whose dispatch keeps failing is re-offered at most twice, then the bridge fails it through the completion channel (sentinel response + `status: failed`) so `run_turn` raises promptly instead of waiting out the full turn timeout. | Mitigated |
| **Worker may omit the `.done` marker or response file.** | Medium | Completion is signalled by the worker writing `<turn>.out` + `<turn>.out.done`. If it forgets, `run_turn` times out → `ClaudeCliError` → existing skip/cooldown/retry handling (same as a headless CLI failure). Deliverables written to the workspace are still recoverable by file-gate rescue. | Accepted (degrades safely) |
| **Startup-dialog handling is screen-scrape based.** | Low | `scoped` mode detects "trust this folder" in the captured pane and sends `1`+Enter. `skip` mode bypasses the trust dialog via `--dangerously-skip-permissions`, but on a fresh account/machine shows a one-time "Bypass Permissions mode … Yes, I accept" dialog instead — `_await_ready` now detects it the same way and sends `2`+Enter. Pre-trusting via `~/.claude.json` did **not** suppress the trust dialog in CLI 2.1.173, so the scrape path is required. Both matches are brittle to TUI wording changes; bounded by the readiness timeout. | Known limitation (handled) |
| **Token usage / cost telemetry is estimated for interactive turns.** | Low | The bridge does not see per-turn token counts. `run_turn` synthesizes `usage={"output_tokens": ≈len(result)/4}` (chars/4 estimate) so the relative low-output exhaustion detector and telemetry see a real output signal instead of a constant 0 (which falsely tripped "topic exhausted" after 2 cycles). Input/context fields stay absent, so compaction/reanchor still no-op by design. Exact counts remain unavailable without the SDK path. | Mitigated (estimate) |
| **No full-cycle live integration test in the suite.** | Low | The seam (`run_turn`) and the bridge are covered by unit + live smoke tests; a complete `long-exposure start --interactive` run over real researcher→worker→auditor cycles is recommended as a manual pre-adoption check (heavy, account-dependent — kept out of the automated suite like the other live smoke tests in `docs/gaps.md`). | Deferred |
| **Recycle relaunch loses an in-flight long-poll fetch.** | Low | When the driver session is recycled at the turn threshold, a pending task simply waits for the new driver's first fetch (≤ fetch window). No task is lost (it stays `pending`); only a few seconds of latency. | Accepted |
| **No rate-limit channel: a rate-limited account is indistinguishable from a hang.** | Medium | Headless `claude -p` failures are classified by `_is_rate_limit` and raise `ClaudeRateLimitError` → the loop's cooldown/rotation handling. `run_turn` can only raise `ClaudeCliError` (timeout / dead driver / abandoned task), so when the logged-in account hits its subscription limit mid-run, each turn blocks for the full `interactive_turn_timeout_seconds` (default 1800 s), kills and relaunches the driver, and feeds the generic failure-streak path — the operator sees "turn timed out", never "rate limited". Detecting the limit needs a signal from inside the session (pane scrape or a worker-visible error file); fold into the same phase-2 work as multi-account pooling, which needs an interactive rate-limit signal anyway. Until then, expect slow 30-minute failure loops at limit boundaries (bounded by the failure streak / operator stop). | Deferred (phase 2) |
