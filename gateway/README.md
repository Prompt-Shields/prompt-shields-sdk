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
PS_GATEWAY_ADMIN_TOKEN=<16+ random chars>             # unlocks /public console and /log/stream (bodies PII-redacted); unset = disabled
PS_MAX_CUSTOM_HOST_RESPONSE_BYTES=52428800            # optional; cap on custom-host response size
TRUSTED_CUSTOM_HOSTS=llm.internal.example.com         # recommended in production; hosts exempt from the SSRF checks (`none` = no host)
PS_METRICS_ENABLED=true                               # optional; Prometheus metrics at GET /metrics
PS_METRICS_TOKEN=<random>                             # optional; require Bearer token on /metrics
PS_SHUTDOWN_TIMEOUT_MS=25000                          # optional; on SIGTERM, wait this long for in-flight requests (streams included) before exiting
PS_SECURITY_EVENTS_LOG=true                           # optional; security events as JSON lines on stdout (or `stderr`); see docs/guides/security-events.mdx
PS_SECURITY_EVENTS_TENANT=bank-a                      # optional; ps.tenant on every security event
PS_SECURITY_EVENTS_OTLP=true                          # optional; security events as OTLP log records (/v1/logs); see docs/guides/observability.mdx
PS_OIDC_ENABLED=true                                  # optional; require IdP access tokens on /v1/* with group policy (PS_OIDC_ISSUER, PS_OIDC_AUDIENCE, PS_OIDC_POLICY, ...); see docs/guides/gateway-oidc.mdx
MAX_JSON_PAYLOAD_SIZE_IN_MB=10                        # optional; 413 for larger JSON bodies
MAX_FILE_PAYLOAD_SIZE_IN_MB=25                        # optional; 413 for larger multipart uploads (audio, files), 411 without Content-Length
AWS_AMBIENT_CREDENTIALS_ENABLED=true                  # optional; let Bedrock/SageMaker callers use this host's AWS identity (serviceRole)
AZURE_AMBIENT_CREDENTIALS_ENABLED=true                # optional; let Azure callers use this host's managed or workload identity
GCP_AMBIENT_CREDENTIALS_ENABLED=true                  # optional; let Vertex callers use this host's Google identity (vertex auth type workload)
OAUTH_TOKEN_URL_ALLOWED_HOSTS=login.example.com       # optional; restrict caller OAuth client-credentials token URLs to these hosts
PS_TELEMETRY_CREDENTIAL_FINGERPRINT=true              # optional; add a stable api_key_fingerprint (key or client identity hash, never a token) to telemetry
OPENAI_AMBIENT_CREDENTIALS_ENABLED=true               # optional; let openai callers use this host's workload identity federation (openai auth type workload)
ANTHROPIC_AMBIENT_CREDENTIALS_ENABLED=true            # optional; same for anthropic (anthropic auth type workload)
```

`TRUSTED_CUSTOM_HOSTS`: when unset, provider custom hosts may use `localhost`, `127.0.0.1`, `::1` and `host.docker.internal` (local development). Set it in production, to the internal hosts callers may reach or to `none`; the Helm chart sets `none`. Guardrail plugin egress (the `webhook`, `log` and `jwt` URLs, vendor base-URL overrides such as Aporia `apiEndpoint`, Azure `customHost`, Pangea or Javelin `domain`, CrowdStrike `baseUrl`, F5 `calypsoUrl`, and hosts built from Azure resource names or AWS regions) comes from caller-controlled guardrail config, so it always refuses metadata, loopback, private and reserved addresses, internal TLDs (`.internal`, `.local`, ...), single-label names and `*.svc` names (in-cluster services) unless the host is listed here, never trusts localhost by default, does not follow redirects (a 3xx is a check error), times out, and caps response bodies at 5 MiB. A refused request is reported as the check's error, so `failOnError` and `deny` behave as for any network error. The check is hostname-only (no DNS resolution); restrict network egress as well.

OAuth client credentials (`x-portkey-oauth-*` headers, `openai` provider) take precedence over `x-portkey-api-key` / `Authorization` when supplied. The token URL check is hostname-only and does not resolve DNS, so it cannot stop DNS rebinding; set `OAUTH_TOKEN_URL_ALLOWED_HOSTS` whenever callers are not fully trusted. Without the allowlist, IP literals, loopback, single-label and `*.svc` names and internal TLDs are refused.

AWS providers (`bedrock`, `sagemaker`, `bedrock-mantle`, `claude-platform-aws`) take the same OAuth headers with `x-portkey-aws-auth-type: oauth` and `x-portkey-aws-role-arn`: the gateway gets a client-credentials access token from the caller's IdP and exchanges it with STS `AssumeRoleWithWebIdentity` (`https://sts.<region>.<AWS_ENDPOINT_DOMAIN>/`, unsigned) for short-lived role credentials, cached until 5 minutes before they expire. Register the IdP as an IAM OIDC identity provider, let the role trust it, and set `x-portkey-oauth-audience` to the audience the trust policy expects; the access token must be a JWT. No gateway identity is involved, so no operator flag is needed. In a config: `{"provider": "bedrock", "aws_auth_type": "oauth", "aws_role_arn": "arn:aws:iam::123456789012:role/llm", "aws_region": "us-east-1", "oauth_token_url": "...", "oauth_client_id": "...", "oauth_client_secret": "...", "oauth_audience": "sts.amazonaws.com"}`.

Workload identity federation for `openai` and `anthropic` (`x-portkey-openai-auth-type: workload` / `x-portkey-anthropic-auth-type: workload`, or `openai_auth_type` / `anthropic_auth_type` in a config) uses the gateway host's identity token, such as a projected Kubernetes service-account token. The gateway exchanges it at the vendor's token endpoint and sends the minted, short-lived token upstream. It is off unless `OPENAI_AMBIENT_CREDENTIALS_ENABLED` / `ANTHROPIC_AMBIENT_CREDENTIALS_ENABLED` is `true` (403 otherwise), and callers cannot choose any federation parameter, only opt in to it.

**Who can spend this identity:** once a flag is on, *every* caller that can reach the gateway can send `workload` and have its requests billed to the operator's OpenAI / Anthropic service account. There is no per-caller grant beyond the flag; this is the same model as the AWS, Azure and GCP ambient flags. Turn it on only when every caller is trusted to use that account. Otherwise put the gateway behind `PS_OIDC_ENABLED` and limit each IdP group's `providers` (see `docs/guides/gateway-oidc.mdx`; the limit is per provider, not per auth type, so a group allowed `openai` may also use `workload`), and give the vendor service account only the projects / workspaces and spend limits those callers should have. Caller `OpenAI-Organization` / `OpenAI-Project` headers are still forwarded, so the service account's project access is what bounds them.

- OpenAI: an RFC 8693 token exchange at `https://auth.openai.com/oauth/token`, with `OPENAI_IDENTITY_TOKEN_FILE`, `OPENAI_IDENTITY_PROVIDER_ID`, `OPENAI_SERVICE_ACCOUNT_ID` and optionally `OPENAI_SUBJECT_TOKEN_TYPE` (`jwt`, the default, or `id`).
- Anthropic: an RFC 7523 jwt-bearer exchange at `https://api.anthropic.com/v1/oauth/token`, with `ANTHROPIC_IDENTITY_TOKEN_FILE`, `ANTHROPIC_FEDERATION_RULE_ID`, `ANTHROPIC_ORGANIZATION_ID`, `ANTHROPIC_SERVICE_ACCOUNT_ID` and optionally `ANTHROPIC_WORKSPACE_ID`. These are the variable names the Anthropic SDK uses.

The token file is read again for every exchange, so rotated projected tokens are picked up. Tokens are cached until 5 minutes before they expire (or halfway through a shorter lifetime). Custom hosts are refused, because the minted token belongs to the operator's vendor service account. `OPENAI_WIF_TOKEN_URL` / `ANTHROPIC_WIF_TOKEN_URL` can override the token endpoint. The endpoint, default or override, goes through the same check as caller OAuth token URLs. The vendor's own host passes `OAUTH_TOKEN_URL_ALLOWED_HOSTS`, and any other host must be listed there. Node runtime only. With the fingerprint on, the value is a hash of the vendor service account.

`PS_TELEMETRY_CREDENTIAL_FINGERPRINT=true` adds `api_key_fingerprint` to each telemetry event, for the credential of the target that answered: `sha256(key)[:16]` for a static key (what the SDK sends, so key links registered with the collector match gateway traffic too), and for keyless auth a hash of the client identity (OAuth token URL + client id, AWS role ARN or access key id, Entra tenant + client id, Vertex service account e-mail), so it stays the same while tokens rotate. Short-lived bearers (Azure AD tokens, a JWT passed as the key) and the gateway host's own identity get none. Off by default.

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

Prompt Shields owns an **exact-match** response cache in this fork, and an opt-in **semantic** mode with per-tenant partitions, a sensitivity and data policy gate, and a local or self-hosted embedder ([docs/semantic-cache.md](docs/semantic-cache.md)). Portkey's hosted simple/semantic caching is not used. It short-circuits identical requests to the same endpoint and reports hit/miss telemetry through `ps-telemetry` (`cache_status`, `est_tokens_saved`) on the call's own event, once per request.

```bash
PS_CACHE_DEFAULT=off        # on | semantic | off — behavior when the request header is omitted (unset: the config's `cache` setting decides; none means off)
PS_CACHE_MAX_ENTRIES=1000   # cap on the in-memory LRU cache
```

Per-request control via the `X-PS-Cache` header:

```
X-PS-Cache: on        # read from and write to the cache
X-PS-Cache: semantic  # semantic cache (needs PS_SEMANTIC_CACHE_EMBEDDER; see docs/semantic-cache.md)
X-PS-Cache: off       # bypass entirely
X-PS-Cache: refresh   # skip the read, repopulate the entry with the fresh response
```

Notes: exact-match only (SHA-256 of the request body, the upstream URL and a digest of the caller's credentials and identity headers, so callers with different keys never share entries) (semantic mode is separate: see [docs/semantic-cache.md](docs/semantic-cache.md)); the cache is in-memory and per-instance (not shared across replicas); only non-streaming responses are cached; and cache failures fail open (the request still proceeds to the provider).

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
