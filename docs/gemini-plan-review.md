# Gemini Plan Review

Review result: the plan is implementable with a small, robust patch because
the repo already has provider-aware Codex/local infrastructure.

## Gaps Addressed Up Front

| Gap | Resolution |
|---|---|
| 2M-token ambiguity | Use 1M. Official Gemini CLI and model docs support 1M; 2M is not treated as available. |
| Free-tier subagent ambiguity | Do not depend on native Gemini subagents. Preserve Python fan-out and add conservative guidance. |
| JSON shape mismatch | Add a Gemini envelope normalizer instead of special-casing downstream code. |
| Auth setup in headless mode | Set `GOOGLE_GENAI_USE_GCA=true` by default for Gemini unless another Gemini auth env is already present. |
| Account pooling | Disable Gemini pooling for now; do not expose unvalidated multi-account OAuth rotation as supported. |
| MCP support | Use Gemini project settings (`.gemini/settings.json`) for the sessions MCP server and tool allowlist. |
| Tool permissions | Map normal turns to `--yolo`, compaction to `--approval-mode plan`, and rely on sandboxing/soft guidance for fine-grained policy. |

## Implementation Order

1. Extend provider constants and env/state path helpers.
2. Add Gemini defaults in config loading and config YAML.
3. Add Gemini command helpers, JSON normalization, and invocation branch.
4. Extend sessionful agent calls and compaction calls.
5. Add Gemini prompt guidance for worker/auditor.
6. Add unit tests.
7. Run live Gemini CLI smoke and focused test suite.

## Risk Notes

The main major risk is Gemini CLI session-resume semantics in headless mode.
The command help exposes `--session-id` and `--resume`, so the implementation
should use them directly. If live smoke shows resume is unreliable, the robust
fallback is to treat Gemini like a stateless HTTP connector: keep provider calls
stateless and inject bounded recent context plus compaction summaries from
long-exposure. That fallback preserves correctness at the cost of token use.

The second major risk is native Gemini subagents on Google-account auth. The
plan intentionally avoids making them load-bearing.
