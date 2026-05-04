# Changelog

All notable changes to the Prompt Shields SDK and platform are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Documentation

- README updated to reflect SDK v0.2 capabilities (Anthropic, async, PII detection, cost estimation, API key fingerprinting, `ps_metadata` wiring).

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
