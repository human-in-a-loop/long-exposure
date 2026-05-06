# Google Cloud Open-Source LLM Hosting Cost Trade-Offs

Date: 2026-05-06.

## Executive Takeaway

Long-exposure should prefer Gemini CLI with Gemini Flash on the Google-account
free-tier path over self-hosting open-weight models on Google Cloud.

The previous Qwen-specific path is deprecated and removed from the native
long-exposure integration plan. Running even a small open model on a Google
Cloud VM adds compute, disk, operations, and performance trade-offs that do
not make sense when Gemini CLI provides a free-tier coding-agent path with
large context, built-in tools, MCP support, file operations, shell commands,
web fetching, and session resume.

Long-exposure keeps a generic `llm_provider: local` OpenAI-compatible HTTP
connector as an unsupported extension point for operators who bring their own
model server. It is not a built-in Qwen path and it is not the recommended
Google Cloud deployment.

## Current Gemini Baseline

Official Google docs checked on 2026-05-06:

- Gemini CLI README: personal Google-account auth advertises a free tier of
  60 requests/minute and 1,000 requests/day, Gemini 3 models, a 1M-token
  context window, built-in tools, MCP support, and checkpoint/resume features:
  https://github.com/google-gemini/gemini-cli/blob/main/README.md
- Gemini API pricing: `gemini-3-flash-preview` lists free input and output
  tokens on the free tier, while Pro-class preview pricing lists no free tier
  for standard token use:
  https://ai.google.dev/gemini-api/docs/pricing

That makes Gemini Flash the cheapest practical Google-hosted backend for
high-volume long-exposure experimentation. The free tier has quota limits and
Google's terms/data-use posture must be acceptable for the task, but it avoids
the fixed infrastructure cost of keeping model weights online.

## Why Self-Hosted Open Models Lose On GCP

Monthly estimates use:

```text
730 hours/month
```

Google Cloud cost varies by region, capacity mode, reservations, Spot/Flex
availability, disk, network, and committed-use terms. The numbers below are
planning estimates, not quotes.

| Option | Always-on monthly | 100 active hours/month | 20 active hours/month | Cost verdict |
|---|---:|---:|---:|---|
| Gemini CLI + Flash free tier | $0 model hosting | $0 model hosting | $0 model hosting | Cheapest path if quota/data-use terms fit. |
| Claude/Codex Max-style plan | Fixed subscription | Fixed subscription | Fixed subscription | Strong paid option when provider limits fit. |
| Small CPU VM for a small open model | about $200/month class | about $27 compute | about $5 compute | Cheap only if shut down aggressively; slow for long prompts. |
| Small L4 GPU VM | about $500/month class | about $70 compute | about $14 compute | More responsive, but still extra infrastructure. |
| Medium multi-GPU open-model VM | about $4k-$11k/month class | hundreds to low thousands | hundreds | Only justified for serious self-hosting or privacy/control. |
| Full frontier open model on 8xH100 class | about $65k/month class | about $9k compute | about $1.8k compute | Not rational for routine long-exposure use. |

The direct compute cost is only part of the issue. Self-hosting also adds:

- model download and storage management;
- runtime selection and tuning;
- monitoring and restarts;
- throughput contention during fan-out;
- tool bridge and sandbox design if the model should execute actions;
- weaker or missing provider-native session and subagent features.

Gemini CLI already gives long-exposure a headless agent runtime with tool
access, MCP configuration, session resume, and a 1M context assumption. A
generic OpenAI-compatible chat endpoint does not.

## Local Connector Status

`llm_provider: local` remains as a generic extension point:

```yaml
llm_provider: local
local_model: custom-local-model
local_base_url: http://127.0.0.1:18080/v1
local_context_window: 32768
local_max_tokens: 2048
```

This connector expects an operator-supplied OpenAI-compatible
`/v1/chat/completions` server. Long-exposure can maintain local JSONL
transcripts, bounded recent-log injection, and compaction into `sessions.db`,
but the connector is stateless from the model server's perspective.

Known limitations:

- no native tool bridge;
- no native subagents;
- no managed account pooling;
- no bundled model, runtime, or Google Cloud setup;
- no guarantee that a given model can follow long-exposure's long layered
  prompts well enough for autonomous research.

## Recommendation

Use Gemini CLI with `gemini-3-flash-preview` for the low-cost Google-hosted
path. Use Claude or Codex paid plans when their paid runtime features and
quality are worth the subscription. Treat self-hosted open models on Google
Cloud as a custom extension project, not as a native long-exposure backend.
