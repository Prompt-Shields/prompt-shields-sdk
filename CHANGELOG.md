# Changelog

All notable changes to the Prompt Shields SDK and platform are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Collector: local embeddings for semantic search** — `PS_EMBEDDINGS_BACKEND=openai|local|none` (default `auto` keeps today's behaviour). `local` uses fastembed in-process via the optional `local-embeddings` extra, so asset metadata never leaves the box; `none` makes `/search` fall back to keyword matching instead of meaningless mock vectors. `PS_EMBEDDINGS_OPENAI_BASE_URL` targets any OpenAI-compatible endpoint. `python -m collector.reembed` rebuilds the index after a switch (#86).
- **Collector: provider org usage pull (platform signal)** — pulls daily usage from the OpenAI organization usage API and the Anthropic Admin usage report, per project/workspace, API key and model. Keys nobody instrumented appear as `medium`-confidence assets with `discovery_source=["platform_signal"]`; re-runs overwrite the same buckets. Admin keys come from `PS_PROVIDER_ADMIN_KEYS`; trigger with `POST /api/v1/admin/provider-usage/pull` (operator only: `Authorization: Bearer $PS_ADMIN_TOKEN` and a `tenant_id` in the body; 404 when the token is unset) or `python -m collector.provider_usage_admin` (#93).
- **Collector: data retention and tenant erasure** — per-tenant `retention_days` (default `PS_RETENTION_DAYS_DEFAULT=365`), a batched, idempotent purge of old `ai_usage_events` (`python -m collector.retention purge` or `POST /api/v1/operator/retention/purge`), and `DELETE /api/v1/operator/tenants/{id}` for erasure. Operator routes need `PS_ADMIN_TOKEN`. Purges and erasures are recorded in the new `retention_audit_log` table (migration 005). Read paths can use `retention_cutoff(tenant)`; the Partner API usage-summary now clamps its window to it (#85).
- **Gateway `/v1/ocr`, ElevenLabs and Deepgram** — `POST /v1/ocr` (Mistral OCR shape) for Mistral, Vertex-hosted Mistral and Azure AI Foundry; `elevenlabs` and `deepgram` providers for `/v1/audio/speech` and `/v1/audio/transcriptions`, with transcripts mapped to the OpenAI shape. Telemetry now emits `modality` (text, audio, image, document) and non-token usage (audio seconds, OCR pages, speech characters) (#75).
- **Collector: OTel GenAI ingest** — `POST /ingest/otel/v1/traces` accepts OTLP/HTTP JSON traces with the same tenant key as `/ingest/events` and turns `gen_ai.*` spans into usage events with discovery source `otel`. Other spans are ignored. Prompt and completion content attributes and span events are always dropped. Bodies are capped at 4 MiB and 2000 spans. Protobuf is not supported yet (#92).
- **Partner API `GET /partner/v1/assets/{asset_id}/usage-summary`** — the documented endpoint now exists. It returns requests, tokens in and out, cost, unique sessions and average latency for the last `7d`, `30d` or `90d` from `ai_usage_events`. Assets in another tenant return 404 (#99).
- **Collector: per-tenant ingest rate limit** — `POST /ingest/events` now enforces `PS_RATE_LIMIT_PER_MINUTE` (default 100 requests/minute, `0` disables) per tenant, reusing the Partner API's fixed-window limiter. Exceeding it returns `429` with `Retry-After` and `X-RateLimit-*` headers. In-process, so the count is per worker (#82).
- **Gateway: unified, provider-backed `GET /v1/models`** — goes through the same validation as other `/v1` routes, proxies to the configured provider, and returns Anthropic's list shape (with `limit` / `after_id` / `before_id`) when the caller sends `anthropic-version`, so Claude Code can discover models through the gateway; other callers get OpenAI's shape (#79).
- **`route-policy.v1` JSON Schema and golden decision vectors** — `schemas/route-policy/` holds the shared routing contract (JSON Schema 2020-12): groups, quality mapping, sensitivity levels, PII escalation, data policy, per-use-case quality bar and fallback. It also has three example policies and decision vectors in `vectors/*.json`. The gateway router runs every active vector through `resolveRoute`. The SDK checks the schema, the examples and `RouteHint` encoding. Sensitivity, data-policy and quality-bar vectors are marked `pending` and skipped with their reason until those P1 features land (#11).

- **Gateway: token usage and truncation for streamed responses** — gateway telemetry now reads usage from SSE streams as they pass to the client (OpenAI chat `usage` chunks, Anthropic `message_start`/`message_delta`, Responses `response.completed`) and sends the event when the stream ends, so streamed calls are no longer costed at zero. A stream that ends without its terminal event is marked `stream_truncated`, a client disconnect `stream_cancelled`; `/metrics` gains `ps_gateway_streams_total{outcome}` and counts streamed tokens (#72).

- **TypeScript Partner SDK (`@prompt-shields/partner`)** in `packages/partner-ts/`: a typed, dependency-free client for every `/partner/v1` endpoint. It supports OAuth client credentials with token caching or API-key auth, cursor pagination (async iterator, `toArray()`, `listPage()`), retries on 429 that honour `Retry-After`, and typed errors (`APIError`, `RateLimitError`, and others). `assets.usageSummary()` needs the collector endpoint from #99. The package is not yet published to npm (#105).
- **SDK: telemetry for streamed calls** — `stream=True` on the sync and async clients now emits one event when the stream ends, with usage from OpenAI's final usage chunk (when the client points at api.openai.com the SDK requests `stream_options.include_usage` and hides that chunk unless you asked for it; other OpenAI-compatible hosts are left untouched, since some reject the parameter) or Anthropic's `message_start`/`message_delta`, and a ~4-chars-per-token estimate (`tokens_estimated`) otherwise. Events carry `stream_completed` (false on early close or error) and `stream_error`. Chunks pass through unchanged; telemetry failures never break the stream (#91).
- **`ps-scan` repository scanner (SDK)** — `ps-scan [PATH]` statically finds AI usage: LLM SDK imports and clients (OpenAI, Anthropic, Google GenAI, Bedrock, Mistral, Cohere, LangChain, LlamaIndex, LiteLLM), model names, provider base URLs and API-key env var names (never values) in Python, TypeScript/JavaScript, Java and Go. Prints a summary, a JSON report, collector `/ingest/events` bodies with `source="code_scan"`, or SARIF. Standard library only, no network; skips `.env*`, `.git`, `node_modules`, virtualenvs, binaries and large files (#95).

- **SDK: Nordic national-ID detection** — new `national_id` PII category for Swedish personnummer/samordningsnummer (Luhn), Norwegian fødselsnummer/D-/H-numbers (two mod-11 digits), Danish CPR (date, plus mod-11 when unhyphenated) and Finnish HETU (century marker and check character). Dates and checksums are validated to keep phone numbers, order IDs and dates out (#10).
- **Gateway: Gemini 3 tool calling across turns** — thought signatures are returned on tool calls (`function.thought_signature`, non-strict mode) and sent back to Gemini/Vertex; per-part `media_resolution` for images. Ported from Portkey 2.0.0 (#108).
- **Gateway: Bedrock on Portkey 2.0's provider code** — Converse and Messages for Claude (`anthropic_beta` as header string or body array, cache reads and writes in `prompt_tokens`), inference-profile ARNs, batch, files and fine-tuning; IRSA, pod identity, ECS and IMDS credentials for `serviceRole` behind `AWS_AMBIENT_CREDENTIALS_ENABLED` (#108).
- **Gateway: Anthropic structured outputs and effort** — `response_format` JSON schema and `reasoning_effort` map to Anthropic `output_config` (#108).
- **Gateway: Messages and Responses adapter parity** — adapted responses report cached tokens (`cache_read_input_tokens`/`cache_creation_input_tokens` for Messages, `input_tokens_details.cached_tokens` and reasoning tokens for Responses, JSON and streaming); provider-native stop reasons (e.g. Gemini `STOP`, `MAX_TOKENS`, `SAFETY`) map correctly and tool calls report `tool_use`; Messages streams number blocks in order with one stop each and stream reasoning as `thinking` blocks; Responses streams give each function call its own item and end truncated output with `response.incomplete`; tool results sent with text keep the text; Messages errors use Anthropic's error shape; telemetry and metrics count Anthropic-style cache reads and writes in `tokens_in`, so input matches Chat Completions and Responses (#73).
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

- **Gateway: batch output streams instead of buffering** — OpenAI, Azure OpenAI and Vertex batch output files are passed through as `application/octet-stream`. Vertex embedding batches read `000000000000.jsonl`, row ids lose a stray `-`, provider errors return error responses, and batch uploads keep multi-byte characters split across chunks. The shared line splitter now emits bytes, which also stops Fireworks dataset uploads from dropping every line. Batch output follows provider-returned locations only to GCS and Azure Blob Storage, and Vertex output resolves `publishers/<provider>/models/...` names to that provider's transform. Ported from Portkey 2.0.0 (#108).
- **Anthropic `prompt_tokens` excluded cached tokens** — they are now included, matching OpenAI's usage convention, so cost and usage reports count cached input (#108).
- **OpenAI batch output fetched `/files/undefined/content`** when a batch had neither an output nor an error file; it now uses the error file, or returns 400 (#108).
- **Bedrock** no longer crashes on a tool call with empty arguments, and adds the text block Converse requires next to a document (#108).

- **Gateway telemetry dropped Messages/Responses token counts** — `input_tokens`/`output_tokens` usage is now counted, which also fixes native Anthropic `/v1/messages` events. Events now carry `api_surface`.

- **Gateway proxy returned 500 for GET/DELETE without a `content-type`** — the catch-all `/v1/*` proxy left the request params undefined, so `tryPost` crashed reading `stream`. Most SDKs send GETs (e.g. `/v1/files`) this way. Body-less requests now proxy with no body, the same method and the upstream response.

- **Gateway telemetry reported vendor `unknown`** — the gateway's telemetry middleware looked for the vendor in the gateway's own URL. It now uses `x-portkey-provider`, or the provider in `x-portkey-config`.

- **Gateway legacy test suites didn't compile** — jest now compiles `src/utils/env.ts` (top-level await) through a test-only transformer, and the legacy handler-service suites run again with mock paths and assertions updated to the current service APIs (#69).

### Security

- **Collector tenant ingest keys are hashed** — tenants now store a SHA-256 lookup prefix and a bcrypt hash instead of the plaintext key in `settings`. Migration `004` hashes existing keys and removes the plaintext; downgrading cannot restore them. An unknown key and a wrong key get the same 401 after the same bcrypt work (#81).
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
