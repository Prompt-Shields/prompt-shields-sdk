# Changelog

All notable changes to the Prompt Shields SDK and platform are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Cost-aware route hints (SDK)** — `RouteHint(quality=, max_cost=, model_group=, allow_cache=)` on `chat.completions.create(route=...)` for both sync and async clients. The SDK never routes; it emits `X-PS-Quality`, `X-PS-Max-Cost`, `X-PS-Route`, and `X-PS-Cache` headers that the gateway acts on (transparent-by-default, per-route override). An empty hint emits no headers.
- **`requested_model` / `served_model` on every event** — the requested model (possibly `"auto"`) is captured alongside the model that actually ran (from the provider response / gateway `X-PS-Served-Model`), so routing savings are provable in Atlas.
- **Gateway cost-aware router** (`gateway/src/middlewares/router/`) — pluggable `RouterStrategy` behind `resolveRoute` with a `HeuristicStrategy` (input-token/code-fence/JSON-schema difficulty → cheap vs. frontier group, `max_cost` budget-clamp across groups). `psRouterMiddleware` rewrites `model="auto"` (or any hinted request) to a concrete model; a concrete model with no hint is left untouched. Config-driven `RoutePolicy` (groups + pricing mirror the SDK) overridable per virtual-key. Fail-open. Telemetry now emits `requested_model`, `served_model`, `route_group`, `route_reason`, `route_est_cost`.
- **Router wired into the gateway pipeline** — `router/hono.ts` exposes `psRouter()`, mounted in `gateway/src/index.ts` (`app.use('*', psRouter())`) ahead of hooks/cache, gated on `PS_ROUTER_ENABLED=true`. Because Hono 4.x re-parses the body per `c.req.json()`, the adapter overwrites `c.req.bodyCache.json` so the rewritten model reaches the handler, and exposes the decision on `c.get('psRoute')`.
- **Telemetry wired into the gateway pipeline** — `ps-telemetry.ts` exposes `psTelemetry()`, a Hono middleware mounted after `psRouter` (gated on `PS_API_KEY`). It parses token usage + served model from the response, merges `X-PS-*` business headers, and folds in `c.get('psRoute')` so `requested_model`/`served_model`/`route_*` actually emit in production. Fire-and-forget, fail-open, POST-only; streaming responses emit without usage.

### Documentation

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
