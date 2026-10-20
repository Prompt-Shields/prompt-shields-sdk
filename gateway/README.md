<div align="center">

# Prompt Shields Gateway

#### An LLM gateway for AI discovery, forked from the [Portkey AI Gateway](https://github.com/Portkey-AI/gateway) (MIT)

</div>

The Prompt Shields Gateway is an OpenAI-compatible LLM proxy. Its main job is **AI discovery**: the `ps-telemetry` middleware records which vendors and models are called, by which business unit, with what usage, and sends it to the Prompt Shields collector. It keeps Portkey's provider abstraction and routing (retries, fallbacks, load balancing), runs Portkey's guardrail checks on the requests it can inspect and refuses the ones it cannot, and can write a security-event stream for a SIEM.

**Zero code changes** — point your existing OpenAI client at the gateway and start populating your AI asset registry.

The gateway does not authenticate its clients: the key a caller sends is the provider's key. Run it behind an API gateway such as Kong for client authentication and rate limits.

Docs:

- [Gateway API](../docs/gateway/overview.mdx): headers, error codes and Kong import; the OpenAPI spec is [`docs/gateway-openapi.yaml`](../docs/gateway-openapi.yaml)
- [Security events](../docs/guides/security-events.mdx) (schema [`docs/security-events.schema.json`](../docs/security-events.schema.json))
- [Kong AI Gateway integration](../docs/integrations/kong.mdx): send Kong traffic to the collector
- [Helm chart](../charts/prompt-shields/README.md) for the gateway and collector on Kubernetes
- [`FORK_NOTICE.md`](./FORK_NOTICE.md): upstream base and every change made to upstream files

---

## What this fork keeps, changes and leaves out

| Feature | In this fork | Notes |
|---------|--------------|-------|
| **Provider abstraction** | Kept | 90 provider ids in `src/providers/index.ts`, including the ones this fork added (see [Supported Providers](#supported-providers-inherited-from-portkey)) |
| **API surface** | Kept, extended | OpenAI-compatible routes, Anthropic `/v1/messages` and the Responses API. `/v1/messages` for OpenAI models that only have a Responses API (codex, `-pro`, deep research) is served via the Responses API |
| **Streaming (SSE)** | Kept | Telemetry is sent when the stream ends, with the usage from its last events |
| **Multi-modal** (vision, audio, images, OCR) | Kept | Telemetry records the modality and non-token usage (audio seconds, OCR pages, speech characters) |
| **Realtime** (WebSocket) | Kept | Upstream URLs are checked before connecting. Realtime sessions produce no `ps-telemetry` events |
| **Retries, fallbacks, load balancing, conditional routing** | Kept (upstream) | Configured with `strategy` / `retry` in `x-portkey-config`. Telemetry records the upstream attempts of each request. A provider redirect is never followed or retried (502 `upstream_redirect`) |
| **Guardrails and mutators** | Kept, enforced in more places | The hooks engine and checks in `plugins/` (68 checks under 20 plugin ids; partner checks send the text they inspect to that vendor's API). They run on unified routes as upstream, on Responses API requests (the upstream version this fork is based on skipped them), and on proxied model calls (`/v1/proxy/*` and the `/v1/*` catch-all), which upstream skips. A request they cannot inspect is refused with a 400 `unsupported_guardrail` before any upstream call. Output checks do not inspect streamed responses. New guardrails from upstream are not merged |
| **Caching** | Replaced | Portkey's simple and semantic caches are not used. PS has its own in-memory, exact-match cache, scoped to the caller's credentials and off unless turned on (see [Exact-match cache](#exact-match-cache)) |
| **Cost-aware routing** | PS addition, opt-in | `PS_ROUTER_ENABLED=true` and `X-PS-Quality` / `X-PS-Max-Cost` / `X-PS-Route` hints. See [`src/middlewares/PS_README.md`](./src/middlewares/PS_README.md) |
| **Security events and metrics** | PS addition, opt-in | `PS_SECURITY_EVENTS_LOG` writes JSON lines (guardrail verdicts, PII and injection detections, auth failures, 429s, rejected payloads) without prompt content. `PS_METRICS_ENABLED` exposes Prometheus metrics on `/metrics` |
| **OpenTelemetry GenAI spans** | PS addition, opt-in | `PS_OTEL_ENDPOINT`; see [`PS_README.md`](./src/middlewares/PS_README.md#psotelts-opentelemetry-genai-spans-70) |
| **MCP Gateway** | Not included | Upstream's MCP Gateway is not ported. MCP tool use is discovered by the SDK and the collector; see [`docs/mcp-tool-call-discovery.md`](./docs/mcp-tool-call-discovery.md) |
| **Virtual keys / key management** | Not included | No key store. Callers send provider keys, or the gateway uses the host's cloud identity when an `*_AMBIENT_CREDENTIALS_ENABLED` flag allows it |
| **Client authentication and RBAC** | Not included | Put the gateway behind an API gateway (see above). Only the admin console (`PS_GATEWAY_ADMIN_TOKEN`) and `/metrics` (`PS_METRICS_TOKEN`) take a token |
| **Prompt template management** | Not included | No prompt store |
| **Deployment** | Kept, extended | Docker image `ghcr.io/prompt-shields/prompt-shields-sdk/gateway`, Node.js (drains in-flight requests on SIGTERM), Cloudflare Workers, the [Helm chart](../charts/prompt-shields/README.md); upstream's [deployment guides](./docs/installation-deployments.md) |

## What This Fork Adds

### `src/middlewares/ps-telemetry.ts`

The middleware that makes this a discovery gateway (on when `PS_API_KEY` is set). On every POST request:

1. **Before forward:** reads `X-PS-*` headers for business context; the gateway never sends `X-PS-*` headers to the provider
2. **After response:** extracts vendor, model, token usage (including cached and reasoning tokens), latency, cache status, routing decision and upstream attempts, and POSTs them to the Prompt Shields collector. Prompt and response content is not sent
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
PS_SECURITY_EVENTS_OTLP=true                          # optional; security events as OTLP log records (/v1/logs); see docs/guides/observability.mdx
MAX_JSON_PAYLOAD_SIZE_IN_MB=10                        # optional; 413 for larger JSON bodies
MAX_FILE_PAYLOAD_SIZE_IN_MB=25                        # optional; 413 for larger multipart uploads (audio, files), 411 without Content-Length
AWS_AMBIENT_CREDENTIALS_ENABLED=true                  # optional; let Bedrock/SageMaker callers use this host's AWS identity (serviceRole)
AZURE_AMBIENT_CREDENTIALS_ENABLED=true                # optional; let Azure callers use this host's managed or workload identity
GCP_AMBIENT_CREDENTIALS_ENABLED=true                  # optional; let Vertex callers use this host's Google identity (vertex auth type workload)
OAUTH_TOKEN_URL_ALLOWED_HOSTS=login.example.com       # optional; restrict caller OAuth client-credentials token URLs to these hosts
```

`TRUSTED_CUSTOM_HOSTS`: when unset, provider custom hosts may use `localhost`, `127.0.0.1`, `::1` and `host.docker.internal` (local development). Set it in production, to the internal hosts callers may reach or to `none`; the Helm chart sets `none`. Guardrail plugin egress (the `webhook`, `log` and `jwt` URLs, vendor base-URL overrides such as Aporia `apiEndpoint`, Azure `customHost`, Pangea or Javelin `domain`, CrowdStrike `baseUrl`, F5 `calypsoUrl`, and hosts built from Azure resource names or AWS regions) comes from caller-controlled guardrail config, so it always refuses metadata, loopback, private and reserved addresses, internal TLDs (`.internal`, `.local`, ...), single-label names and `*.svc` names (in-cluster services) unless the host is listed here, never trusts localhost by default, does not follow redirects (a 3xx is a check error), times out, and caps response bodies at 5 MiB. A refused request is reported as the check's error, so `failOnError` and `deny` behave as for any network error. The check is hostname-only (no DNS resolution); restrict network egress as well.

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
PS_CACHE_DEFAULT=off        # on | off — behavior when the request header is omitted (unset: the config's `cache` setting decides; none means off)
PS_CACHE_MAX_ENTRIES=1000   # cap on the in-memory LRU cache
```

Per-request control via the `X-PS-Cache` header:

```
X-PS-Cache: on        # read from and write to the cache
X-PS-Cache: off       # bypass entirely
X-PS-Cache: refresh   # skip the read, repopulate the entry with the fresh response
```

Notes: exact-match only (SHA-256 of the request body, the upstream URL and a digest of the caller's credentials and identity headers, so callers with different keys never share entries) — semantic caching is out of scope and planned as a separate sub-project; the cache is in-memory and per-instance (not shared across replicas); only non-streaming responses are cached; and cache failures fail open (the request still proceeds to the provider).

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

The provider configs in `src/providers/` (90 provider ids) are intact and report telemetry through the PS middleware. Common ones:

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

This fork tracks Portkey at a specific commit, with selected changes ported from upstream `main` and `2.0.0`. Provider additions and fixes are taken as needed; new routing strategies, caching layers and guardrail checks from upstream are not merged. The routing and guardrails already in the fork stay and are maintained here.

See [`FORK_NOTICE.md`](./FORK_NOTICE.md) for the upstream base commit and modification log.

## License

MIT, inherited from upstream Portkey. See [`LICENSE`](./LICENSE).

The `ps-telemetry.ts` middleware and PS-specific configuration are wholly original code, also released under MIT.
