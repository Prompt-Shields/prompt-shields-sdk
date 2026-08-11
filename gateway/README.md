<div align="center">

# Prompt Shields Gateway

#### A discovery-focused fork of the [Portkey AI Gateway](https://github.com/Portkey-AI/gateway) (MIT)

</div>

The Prompt Shields Gateway is a thin LLM proxy that captures **AI discovery telemetry** for every request flowing through it. It inherits Portkey's provider abstraction (250+ models across 45+ providers) and OpenAI-compatible API surface, then strips features that don't serve discovery and adds the `ps-telemetry` middleware that feeds the Prompt Shields collector.

**Zero code changes** — point your existing OpenAI client at the gateway and start populating your AI asset registry.

---

## What This Fork Keeps from Portkey

These features are kept because they directly support discovery, observability, and provider coverage:

| Feature | Why it stays | Portkey docs |
|---------|--------------|--------------|
| **Provider abstraction** (250+ models, 45+ providers) | Lets one proxy capture telemetry from any LLM customers use | [Providers](https://portkey.wiki/gh-59) |
| **OpenAI-compatible API surface** | Drop-in replacement for `openai`/`anthropic` SDKs — no SDK install required | [Quickstart](https://portkey.ai/docs) |
| **Request/response interception pipeline** | The middleware hook where `ps-telemetry` runs | — |
| **Streaming (SSE) support** | Tokens/cost still captured at stream completion | [Streaming](https://portkey.ai/docs) |
| **Multi-modal capabilities** (vision, audio, image) | Multi-modal calls are still AI assets that need to be discovered | [Multi-modal](https://portkey.wiki/gh-41) |
| **Realtime APIs** (WebSocket) | OpenAI Realtime API observability | [Realtime](https://portkey.wiki/gh-42) |
| **MCP Gateway observability hooks** | Tool calls via MCP are AI surface area worth tracking | [MCP Gateway](https://portkey.ai/docs/product/mcp-gateway) |
| **Usage analytics primitives** | Token/cost/latency capture is core to discovery | [Analytics](https://portkey.wiki/gh-49) |
| **Deployment options** — Docker, Node.js, Cloudflare Workers, Replit, EC2 | Customers deploy where their apps already live | [Deployment](./docs/installation-deployments.md) |

## What This Fork Strips

These features are deliberately removed or disabled. They belong to other product layers (routing tools like Portkey itself, security tools like Defender/Purview, or PS's own copilot):

| Feature | Why it's stripped | Where to get it |
|---------|-------------------|-----------------|
| **Fallbacks & retries** | Reliability is the upstream gateway's job, not discovery's | Use [Portkey](https://portkey.ai) itself |
| **Load balancing & weighted routing** | Same — not in discovery's lane | Portkey |
| **Smart caching** (Portkey simple + semantic) | Portkey's built-in caching is disabled; PS ships its own exact-match cache instead (see [What This Fork Adds](#what-this-fork-adds)) | Portkey |
| **Guardrails engine** (40+ guardrails) | PS ships its own prompt improvement copilot via browser extensions | [Prompt Shields](https://promptshields.io) |
| **Virtual keys / secure key management** | Enterprise customers manage LLM keys in their own vaults | Use existing vault |
| **Role-based access control on the gateway** | RBAC lives in the Atlas AI dashboard (atlas.ai repo) | atlas.ai |
| **Prompt template management** | Out of scope; PS browser extensions handle prompt improvement | — |
| **Provider optimization / cost routing** | Discovery is read-only — we observe spend, we don't route around it | Portkey |

If you need any of the stripped features, run an upstream Portkey instance **in front of** the PS gateway: Portkey routes and caches, then forwards to the PS gateway for discovery telemetry.

## What This Fork Adds

### `src/middlewares/ps-telemetry.ts`

The single, self-contained middleware that makes this a discovery gateway. On every request:

1. **Before forward:** reads `X-PS-*` headers for business context, strips them from the upstream call
2. **After response:** extracts vendor, model, tokens, latency, tool calls, fingerprints the LLM provider API key, and POSTs to the Prompt Shields collector
3. **Fail-open:** telemetry errors are swallowed — the LLM response always reaches the client

Configuration:

```bash
PS_COLLECTOR_URL=https://collector.promptshields.io   # PS collector
PS_API_KEY=ps-...                                     # tenant API key
```

Per-request annotations:

```
X-PS-Business-Unit: HR
X-PS-Use-Case: interview-screening
X-PS-Owner: jane.doe@acme.com
X-PS-Data-Classification: confidential
X-PS-Environment: production
```

### Exact-match cache

Prompt Shields owns an **exact-match** response cache in this fork (Portkey's simple/semantic caching is disabled). It short-circuits identical requests to the same endpoint and reports hit/miss telemetry through `ps-telemetry` (`cache_status`, `est_tokens_saved`).

```bash
PS_CACHE_DEFAULT=off        # on | off — behavior when the request header is omitted (default off)
PS_CACHE_MAX_ENTRIES=1000   # cap on the in-memory LRU cache
```

Per-request control via the `X-PS-Cache` header:

```
X-PS-Cache: on        # read from and write to the cache
X-PS-Cache: off       # bypass entirely
X-PS-Cache: refresh   # skip the read, repopulate the entry with the fresh response
```

Notes: exact-match only (SHA-256 of request body + endpoint) — semantic caching is out of scope and planned as a separate sub-project; the cache is in-memory and per-instance (not shared across replicas); only non-streaming responses are cached; and cache failures fail open (the request still proceeds to the provider).

---

## Quickstart

```bash
# Run the gateway (Docker)
docker run -p 8787:8787 \
  -e PS_COLLECTOR_URL=https://collector.promptshields.io \
  -e PS_API_KEY=ps-... \
  promptshields/gateway

# Point any OpenAI client at the gateway
export OPENAI_BASE_URL=http://localhost:8787/v1
```

That's the whole integration. Existing code unchanged:

```python
from openai import OpenAI
client = OpenAI()   # picks up OPENAI_BASE_URL

resp = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "..."}],
    extra_headers={
        "X-PS-Business-Unit": "HR",
        "X-PS-Use-Case": "interview-screening",
    },
)
# Asset now visible in the Atlas AI dashboard + Partner API registry.
```

Want richer per-request annotations and PII detection on the client side? Use the [Python SDK](https://github.com/jun-bit-pulse-ai/prompt-shields-sdk/tree/main/packages/sdk) instead — same data model, more knobs.

---

## Supported Providers (inherited from Portkey)

All 78 providers in `src/providers/` are intact and forward telemetry through the PS middleware. Common ones:

OpenAI · Anthropic · Google (Gemini, Vertex) · Azure OpenAI · AWS Bedrock · Cohere · Mistral · Meta Llama · Groq · Together AI · Fireworks · DeepSeek · Cerebras · Perplexity · OpenRouter · LM Studio · Ollama · vLLM · …

[Full list →](./src/providers)

For per-provider auth conventions, see Portkey's [provider docs](https://portkey.wiki/gh-59). The PS fork doesn't override any of them.

---

## When to use the Gateway vs the SDK

| Use the Gateway when… | Use the SDK when… |
|-----------------------|-------------------|
| You can't modify application code | You want rich annotations per request |
| You're proxying many services centrally | You want client-side PII detection |
| You need a network-level audit point | You need cost estimation locally |
| You're on a non-Python/TS stack | You're already in Python or TypeScript |

Both write to the same Prompt Shields collector — events are merged by the deduplication logic in the registry, so you can mix and match.

---

## Versioning & upstream merges

This fork tracks Portkey at a specific commit. Provider additions from upstream are cherry-picked as needed — routing/guardrails additions are skipped (PS ships its own exact-match cache — see [What This Fork Adds](#what-this-fork-adds)).

See [`FORK_NOTICE.md`](./FORK_NOTICE.md) for the upstream base commit and modification log.

## License

MIT, inherited from upstream Portkey. See [`LICENSE`](./LICENSE).

The `ps-telemetry.ts` middleware and PS-specific configuration are wholly original code, also released under MIT.
