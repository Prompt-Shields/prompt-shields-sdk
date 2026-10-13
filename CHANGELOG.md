# Changelog

All notable changes to the Prompt Shields SDK and platform are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **TypeScript Partner SDK (`@prompt-shields/partner`)** in `packages/partner-ts/`: a typed, dependency-free client for every `/partner/v1` endpoint. It supports OAuth client credentials with token caching or API-key auth, cursor pagination (async iterator, `toArray()`, `listPage()`), retries on 429 that honour `Retry-After`, and typed errors (`APIError`, `RateLimitError`, and others). `assets.usageSummary()` needs the collector endpoint from #99. The package is not yet published to npm (#105).
- **Gateway: Gemini 3 tool calling across turns** — thought signatures are returned on tool calls (`function.thought_signature`, non-strict mode) and sent back to Gemini/Vertex; per-part `media_resolution` for images. Ported from Portkey 2.0.0 (#108).
- **Gateway: Bedrock on Portkey 2.0's provider code** — Converse and Messages for Claude (`anthropic_beta` as header string or body array, cache reads and writes in `prompt_tokens`), inference-profile ARNs, batch, files and fine-tuning; IRSA, pod identity, ECS and IMDS credentials for `serviceRole` behind `AWS_AMBIENT_CREDENTIALS_ENABLED` (#108).
- **Gateway: Anthropic structured outputs and effort** — `response_format` JSON schema and `reasoning_effort` map to Anthropic `output_config` (#108).

- **Gateway: Responses and Messages API on any provider** — when a provider lacks native support, `/v1/responses` and `/v1/messages` requests (JSON and streaming) are translated through Chat Completions and back, decided per target. Ported from Portkey 2.0.0 (#59).
- **Gateway `/v1/health` and opt-in Prometheus `/metrics`** — `PS_METRICS_ENABLED=true`, optional `PS_METRICS_TOKEN`. Labels include route, status, provider, model and the X-PS business unit and environment, never prompt content. `deployment.yaml` now has liveness and readiness probes (#61).
- **Gateway provider fixes from Portkey 2.0.0** — `prompt_cache_retention`, OpenAI `service_tier` in responses, gpt-image-1 parameters, Azure image-edit transform, Z.ai image generation (#60).

- **Gateway `/v1/rerank`** — a unified, Cohere-style rerank endpoint for Cohere, Jina, Voyage, Bedrock (Agents runtime) and Pinecone. Ported from Portkey gateway 2.0.0 (#58).
- **Gateway providers: Databricks, Latitude, Pinecone** — Databricks takes its workspace from `x-portkey-databricks-workspace`. Ported from Portkey 2.0.0 (#57).
- **Gateway JSON payload-size limit** — set `MAX_JSON_PAYLOAD_SIZE_IN_MB` to reject oversized JSON bodies with 413. Ported from Portkey 2.0.0 (#62).

- **Cost-aware route hints (SDK)** — `RouteHint(quality=, max_cost=, model_group=, allow_cache=)` on `chat.completions.create(route=...)` for both sync and async clients. The SDK never routes; it emits `X-PS-Quality`, `X-PS-Max-Cost`, `X-PS-Route`, and `X-PS-Cache` headers that the gateway acts on (transparent-by-default, per-route override). An empty hint emits no headers.
- **`requested_model` / `served_model` on every event** — the requested model (possibly `"auto"`) is captured alongside the model that actually ran (from the provider response / gateway `X-PS-Served-Model`), so routing savings are provable in Atlas.
- **Gateway cost-aware router** (`gateway/src/middlewares/router/`) — pluggable `RouterStrategy` behind `resolveRoute` with a `HeuristicStrategy` (input-token/code-fence/JSON-schema difficulty → cheap vs. frontier group, `max_cost` budget-clamp across groups). `psRouterMiddleware` rewrites `model="auto"` (or any hinted request) to a concrete model; a concrete model with no hint is left untouched. Config-driven `RoutePolicy` (groups + pricing mirror the SDK) overridable per virtual-key. Fail-open. Telemetry now emits `requested_model`, `served_model`, `route_group`, `route_reason`, `route_est_cost`.
- **Router wired into the gateway pipeline** — `router/hono.ts` exposes `psRouter()`, mounted in `gateway/src/index.ts` (`app.use('*', psRouter())`) ahead of hooks/cache, gated on `PS_ROUTER_ENABLED=true`. Because Hono 4.x re-parses the body per `c.req.json()`, the adapter overwrites `c.req.bodyCache.json` so the rewritten model reaches the handler, and exposes the decision on `c.get('psRoute')`.
- **Telemetry wired into the gateway pipeline** — `ps-telemetry.ts` exposes `psTelemetry()`, a Hono middleware mounted after `psRouter` (gated on `PS_API_KEY`). It parses token usage + served model from the response, merges `X-PS-*` business headers, and folds in `c.get('psRoute')` so `requested_model`/`served_model`/`route_*` actually emit in production. Fire-and-forget, fail-open, POST-only; streaming responses emit without usage.

### Fixed

- **Anthropic `prompt_tokens` excluded cached tokens** — they are now included, matching OpenAI's usage convention, so cost and usage reports count cached input (#108).
- **OpenAI batch output fetched `/files/undefined/content`** when a batch had neither an output nor an error file; it now uses the error file, or returns 400 (#108).
- **Bedrock** no longer crashes on a tool call with empty arguments, and adds the text block Converse requires next to a document (#108).

- **Gateway telemetry dropped Messages/Responses token counts** — `input_tokens`/`output_tokens` usage is now counted, which also fixes native Anthropic `/v1/messages` events. Events now carry `api_surface`.

- **Gateway telemetry reported vendor `unknown`** — the gateway's telemetry middleware looked for the vendor in the gateway's own URL. It now uses `x-portkey-provider`, or the provider in `x-portkey-config`.

### Security

- **Gateway admin console and log stream require auth** — `/public` and `/log/stream` now need `PS_GATEWAY_ADMIN_TOKEN` (at least 16 characters) as a bearer token or session cookie, and the log stream is disabled when it is unset. Previously anyone who could reach the gateway port could watch live request logs. Provider options and headers are redacted in the stream. Ported from upstream Portkey `main` (#54).
- **`x-portkey-forward-headers` loop guard** — requests or configs that try to forward the forward-headers header itself are rejected with 400 (upstream `de0e6c5`, #55).
- **Upstream URL SSRF check** — provider URLs built from request fields (Azure resource name, Vertex region, AWS region, account ids) may no longer point at metadata, private, link-local or alternatively encoded IPs (#65).
- **Credential masking in gateway logs** — bearer tokens, API keys, AWS and Google keys, and passwords in URLs are masked in the shared logger, the Redis connection log and error logs (#65).
- **Config fan-out cap** — at most 100 root targets (1000 for conditional routing) and 50 nested targets per root target; override with `MAX_ROOT_CONFIG_TARGETS`, `MAX_CONDITIONAL_ROOT_CONFIG_TARGETS` and `MAX_NESTED_CONFIG_TARGETS` (#65).
- **Custom-host response size cap** — responses from custom hosts are limited to 50 MiB (`PS_MAX_CUSTOM_HOST_RESPONSE_BYTES`) (#65).
- **Provider credentials win on proxy routes** — forwarded or proxied client headers no longer overwrite the credential headers a provider config sets (#65).
- **Bedrock/SageMaker cannot borrow the gateway's AWS identity** — `awsAuthType=serviceRole`, and `assumedRole` without operator-configured keys, sign with the host's credentials only when `AWS_AMBIENT_CREDENTIALS_ENABLED=true`; otherwise `serviceRole` returns 403. `assumedRole` without a role ARN returns 400 instead of signing with the operator's assume-role keys. Upstream 2.0 allows all of these for any caller (#108).
- **Azure callers cannot borrow the gateway's Azure identity** — `x-portkey-azure-auth-mode: managed` (host managed identity via IMDS) and `workload` (host federated token) on Azure OpenAI and Azure AI Inference, and `managed` credentials on the Azure guardrails, work only when `AZURE_AMBIENT_CREDENTIALS_ENABLED=true`; otherwise the request returns 403 and the guardrail fails closed (verdict `false`). Upstream allows them for any caller.
- **AWS signed requests stay on AWS** — a crafted `x-portkey-aws-region` (`x@evil.example/`) or S3 bucket name could point Bedrock, SageMaker or STS calls, and their signed headers (access key id, session token), at another host. Regions are now format-checked, signing is refused for hosts outside `AWS_ENDPOINT_DOMAIN`, and STS `RoleArn`/`ExternalId` are URL-encoded (#108).

### Documentation

- `gateway/FORK_NOTICE.md` records the upstream fork base (`28ed85e`), the decision to port features from Portkey `2.0.0` rather than rebase onto it, the cherry-picked commits and how to sync. New `scripts/upstream-watch.py` reports untriaged Portkey OSS commits and enterprise changelog entries (#56, #66).
- README updated to reflect SDK v0.2 capabilities (Anthropic, async, PII detection, cost estimation, API key fingerprinting, `ps_metadata` wiring).
- Added SDK Guide section 6 "Cost-aware routing", `docs/sdks/python.mdx` routing reference, and the gateway routing-header contract + `ps-router.ts` spec in `gateway/src/middlewares/PS_README.md`.

## SDK [0.2.0] — 2026-04

### Added

- **Anthropic provider** via `ShieldsAnthropic` and `AsyncShieldsAnthropic`. Tool-use content blocks are parsed alongside OpenAI `tool_calls`.
- **Provider adapter layer** (`prompt_shields.providers`) — `ProviderAdapter` base class with `OpenAIAdapter` and `AnthropicAdapter` implementations. New vendors require ~20 lines.
- **Async clients** — `AsyncShieldsClient`, `AsyncShieldsOpenAI`, `AsyncShieldsAnthropic`. Native `await` flush instead of the threaded fast path used by sync clients.
- **PII detection** (`prompt_shields.pii`) — pattern-based detection for `email`, `phone`, `ssn`, `credit_card`, `ip_address`, `iban`, plus keyword-based `health_data` and `financial_data` categories. Categories only — prompt content never leaves the host unless `send_prompt_text=True` is explicitly opted in.
- **Cost estimation** (`prompt_shields.pricing`) — token-to-USD estimator with default pricing table covering OpenAI, Anthropic, and Google Gemini models. Custom `pricing_table=` override on the client.
- **API key fingerprint** — SHA-256 hash truncated to 16 hex chars, attached to every event as `api_key_fingerprint`. The raw API key is never sent in telemetry.
- **`ps_metadata` per-request wiring** — `data_sources`, `output_destination`, `risk_tags`, `session_id`, `user_id` now flow through to events. Previously accepted as a parameter but silently dropped.
- **`calling_service`** client constructor argument — populates the asset record's calling-service field for deduplication fallback.
- **Typed convenience subclasses** — `ShieldsOpenAI` and `ShieldsAnthropic` for IDE completion, alongside the generic `ShieldsClient(vendor="...")`.

### Changed

- Optional dependencies restructured. `pip install prompt-shields[openai]`, `[anthropic]`, or `[all]`. The base install no longer pulls `openai`.
- `__init__.py` exports the full public surface — clients, types (`PSMetadata`, `PSConfig`, `Vendor`, `DataClassification`, `DiscoverySource`), and utilities (`detect_pii_categories`, `estimate_cost`).

### Tests

- Test count increased from 8 → 49. New coverage: PII categories (12 tests), pricing (9 tests), provider adapters (8 tests), client metadata mapping, fingerprint stability, `ps_metadata` wiring, PII opt-out, prompt-text opt-in, Anthropic vendor end-to-end.

## SDK [0.1.0] — 2026-03

### Added

- Initial Python SDK with `ShieldsClient` wrapping OpenAI's chat completions
- Telemetry collector (FastAPI) with PostgreSQL backend
- AI Asset Registry REST API with cursor-based pagination
- Asset deduplication with confidence scoring (`low` / `medium` / `high` / `verified`)
- AI Gateway fork (TypeScript) based on Portkey AI Gateway
- pgvector semantic search over discovered AI assets
- Mintlify Partner API documentation
- Demo scripts and Ardoq Integration Builder recipe
