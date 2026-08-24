# Changelog

All notable changes to the Prompt Shields SDK and platform are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Missing initial migration.** Revision `002` declared `down_revision = '001'`, but revision `001` was absent, so `alembic upgrade head` failed with `KeyError: '001'` on any fresh database. Added `001_initial_schema.py`, creating `tenants`, `ai_assets`, `ai_usage_events`, `data_flows`, and `risk_mappings` with their indexes on stock PostgreSQL. `alembic check` now reports no drift against the models.
- **Collector container could not import the database layer.** `packages/collector/Dockerfile` set `PYTHONPATH` to `/app/packages/db`, but the code imports `db.models`, which needs `/app/packages` on the path. The container failed at startup with `ModuleNotFoundError: No module named 'db'`.
- **Test database lacked pgvector.** `scripts/init-test-db.sql` created `prompt_shields_test` without enabling the `vector` extension, so the collector suite failed at table creation. The extension is enabled per-database, so the script now connects to the test database and enables it.

### Documentation

- README rewritten for a security and governance audience: the problem statement, a verified five-command quickstart, an architecture diagram, definitions for each named concept, and an explicit limits section covering fail-open telemetry loss, PII heuristic error in both directions, the unwired gateway middleware, plaintext collector authentication, unenforced rate limiting, and predicate-based tenant isolation.
- Added root `SECURITY.md` — reporting process, scope, and the known limitations that will not be treated as vulnerabilities.
- Added root `CONTRIBUTING.md` — development setup, per-suite test invocation, migration workflow, and the fail-open and content-stays-local invariants.
- Corrected the clone URL, which pointed at a personal fork rather than `Bit-Pulse-AI/prompt-shields-sdk`.

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
