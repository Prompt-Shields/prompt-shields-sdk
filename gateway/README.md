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
PS_GATEWAY_ADMIN_TOKEN=<16+ random chars>             # unlocks /public console and /log/stream; unset = disabled
PS_MAX_CUSTOM_HOST_RESPONSE_BYTES=52428800            # optional; cap on custom-host response size
TRUSTED_CUSTOM_HOSTS=llm.internal.example.com         # recommended in production; hosts exempt from the SSRF checks (`none` = no host)
PS_METRICS_ENABLED=true                               # optional; Prometheus metrics at GET /metrics
PS_METRICS_TOKEN=<random>                             # optional; require Bearer token on /metrics
PS_SHUTDOWN_TIMEOUT_MS=25000                          # optional; on SIGTERM, wait this long for in-flight requests (streams included) before exiting
PS_SECURITY_EVENTS_LOG=true                           # optional; security events as JSON lines on stdout (or `stderr`); see docs/guides/security-events.mdx
PS_SECURITY_EVENTS_TENANT=bank-a                      # optional; ps.tenant on every security event
MAX_JSON_PAYLOAD_SIZE_IN_MB=10                        # optional; 413 for larger JSON bodies
MAX_FILE_PAYLOAD_SIZE_IN_MB=25                        # optional; 413 for larger multipart uploads (audio, files), 411 without Content-Length
AWS_AMBIENT_CREDENTIALS_ENABLED=true                  # optional; let Bedrock/SageMaker callers use this host's AWS identity (serviceRole)
AZURE_AMBIENT_CREDENTIALS_ENABLED=true                # optional; let Azure callers use this host's managed or workload identity
GCP_AMBIENT_CREDENTIALS_ENABLED=true                  # optional; let Vertex callers use this host's Google identity (vertex auth type workload)
OAUTH_TOKEN_URL_ALLOWED_HOSTS=login.example.com       # optional; restrict caller OAuth client-credentials token URLs to these hosts
```

`TRUSTED_CUSTOM_HOSTS`: when unset, provider custom hosts may use `localhost`, `127.0.0.1`, `::1` and `host.docker.internal` (local development). Set it in production, to the internal hosts callers may reach or to `none`; the Helm chart sets `none`. Guardrail plugin egress (the `webhook`, `log` and `jwt` URLs, vendor base-URL overrides such as Aporia `apiEndpoint`, Azure `customHost`, Pangea or Javelin `domain`, CrowdStrike `baseUrl`, F5 `calypsoUrl`, and hosts built from Azure resource names or AWS regions) comes from caller-controlled guardrail config, so it always refuses metadata, loopback, private and reserved addresses and internal TLDs (`.internal`, `.local`, ...) unless the host is listed here, never trusts localhost by default, does not follow redirects (a 3xx is a check error), times out, and caps response bodies at 5 MiB. A refused request is reported as the check's error, so `failOnError` and `deny` behave as for any network error. The check is hostname-only (no DNS resolution); restrict network egress as well.

OAuth client credentials (`x-portkey-oauth-*` headers, `openai` provider) take precedence over `x-portkey-api-key` / `Authorization` when supplied. The token URL check is hostname-only and does not resolve DNS, so it cannot stop DNS rebinding; set `OAUTH_TOKEN_URL_ALLOWED_HOSTS` whenever callers are not fully trusted.

Liveness: `GET /v1/health` (no auth, no upstream call). Metrics are labelled with route, status, provider, model and the `X-PS-Business-Unit` / `X-PS-Environment` values; prompt and completion content never appear in metrics. Guardrail verdicts, PII detections (category only), 429s and dropped telemetry are counted too (`ps_gateway_guardrail_verdicts_total`, `ps_gateway_pii_detections_total`, `ps_gateway_rate_limited_total`, `ps_gateway_telemetry_dropped_total`).

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
  ghcr.io/prompt-shields/prompt-shields-sdk/gateway

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

Want richer per-request annotations and PII detection on the client side? Use the [Python SDK](https://github.com/Prompt-Shields/prompt-shields-sdk/tree/main/packages/sdk) instead — same data model, more knobs.

---

## Supported Providers (inherited from Portkey)

All 78 providers in `src/providers/` are intact and forward telemetry through the PS middleware. Common ones:

OpenAI · Anthropic · Google (Gemini, Vertex) · Azure OpenAI · AWS Bedrock · Cohere · Mistral · Meta Llama · Groq · Together AI · Fireworks · DeepSeek · Cerebras · Perplexity · OpenRouter · LM Studio · Ollama · vLLM · …

[Full list →](./src/providers)

For per-provider auth conventions, see Portkey's [provider docs](https://portkey.wiki/gh-59). The PS fork doesn't override any of them.

Providers added by this fork for #74 use fixed vendor hosts. Most take an API key as `Authorization: Bearer`. `meta` (Llama API, `https://api.llama.com/compat/v1`) works this way and supports chat (with streaming) and `GET /v1/models`. `claude-platform-aws` (Claude Platform on AWS, which Anthropic operates and AWS bills; this is not Bedrock) needs a region and a workspace:

```bash
# SigV4 with AWS keys (aws_auth_type assumedRole / serviceRole also work, as for Bedrock)
curl localhost:8787/v1/chat/completions \
  -H 'x-portkey-config: {"provider":"claude-platform-aws","aws_access_key_id":"AKIA...","aws_secret_access_key":"...","aws_region":"us-west-2","anthropic_aws_workspace_id":"wrkspc_..."}' \
  -H 'content-type: application/json' \
  -d '{"model":"claude-sonnet-5-5","max_tokens":256,"messages":[{"role":"user","content":"Hello"}]}'
```

To use an API key generated in the AWS console (*Claude Platform on AWS → API keys*), pass `"api_key"` instead of the AWS keys; it is sent as `x-api-key`. With headers instead of a config, use `x-portkey-provider: claude-platform-aws`, the `x-portkey-aws-*` headers and `anthropic-workspace-id`. Claude Console keys and Bedrock keys do not work on this endpoint, and the AWS account needs outbound web identity federation enabled (`aws iam enable-outbound-web-identity-federation`).

`bedrock-mantle` (Amazon Bedrock Mantle, Bedrock's OpenAI-compatible endpoint at `https://bedrock-mantle.<region>.api.aws`) serves `/v1/chat/completions`, `/v1/responses` and, for Claude models, `/v1/messages`. Pass `aws_region` and either AWS credentials (SigV4, service `bedrock-mantle`; `aws_auth_type` `assumedRole` / `serviceRole` work as for Bedrock) or a Bedrock API key as `api_key`. OpenAI's GPT models (not gpt-oss) are called on `/openai/v1`, other models on `/v1`; set `bedrock_mantle_api_path` to `/v1` or `/openai/v1` to choose:

```bash
curl localhost:8787/v1/chat/completions \
  -H 'x-portkey-config: {"provider":"bedrock-mantle","api_key":"<bedrock api key>","aws_region":"us-east-1"}' \
  -H 'content-type: application/json' \
  -d '{"model":"openai.gpt-oss-120b","messages":[{"role":"user","content":"Hello"}]}'
```

`fal-ai` (fal.ai) takes the fal key (sent as `Authorization: Key <key>`) and a fal model id as `model`. `/v1/images/generations` maps `n`, `size` (`WxH` or a fal preset such as `landscape_16_9`) and `response_format`; video and audio models are called through the proxy with the model id as the path:

```bash
curl localhost:8787/v1/images/generations -H 'x-portkey-provider: fal-ai' -H 'authorization: Bearer <fal key>' \
  -H 'content-type: application/json' -d '{"model":"fal-ai/flux/schnell","prompt":"a lighthouse","size":"1024x768"}'
curl localhost:8787/v1/fal-ai/kling-video/v2/master/text-to-video -H 'x-portkey-provider: fal-ai' -H 'authorization: Bearer <fal key>' \
  -H 'content-type: application/json' -d '{"prompt":"a lighthouse at dusk"}'
```

Only fal's synchronous API (`fal.run`) is used; long video jobs can hit fal's sync timeout, and the queue API is not wired.

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
