# Prompt Shields — Developer SDK & AI Gateway

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15%2B%20%2B%20pgvector-336791)
![License](https://img.shields.io/badge/license-MIT-green)

Discover, classify, and govern every AI system running across your enterprise — whether sanctioned or shadow.

This repository contains the **developer-facing components** of Prompt Shields: a Python SDK, an AI gateway proxy, a telemetry collector, and connectors to Enterprise Architecture tools such as Ardoq.

Browser extensions (Chrome, Safari, Edge) and the macOS desktop app that capture shadow AI usage live in separate repositories. Everything feeds into the same collector and registry.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                 ENTERPRISE AI LANDSCAPE                  │
├────────────────────┬────────────────────────────────────┤
│  CLIENT-SIDE       │       CODE/INFRA-SIDE              │
│  (Existing)        │       (This Repo)                  │
│                    │                                    │
│  Chrome Extension  │  ┌──────────────────────┐         │
│  Safari Extension  │  │  PS Developer SDK    │         │
│  Edge Extension    │  │  (Python)            │         │
│  macOS App         │  └──────┬───────────────┘         │
│                    │         │                         │
│                    │  ┌──────▼───────────────┐         │
│                    │  │  PS AI Gateway       │         │
│                    │  │  (Forked Portkey)    │         │
│                    │  └──────┬───────────────┘         │
├────────────────────┴─────────┼─────────────────────────┤
│              PROMPT SHIELDS CORE                        │
│                              │                         │
│           ┌──────────────────▼──────────────┐          │
│           │     Telemetry Collector          │          │
│           │     (FastAPI)                    │          │
│           └──────────────────┬──────────────┘          │
│                              │                         │
│           ┌──────────────────▼──────────────┐          │
│           │     PostgreSQL + pgvector        │          │
│           │     AI Asset Registry            │          │
│           └──────────────────┬──────────────┘          │
│                              │                         │
│           ┌──────────────────▼──────────────┐          │
│           │     Registry REST API            │          │
│           └─────────────────────────────────┘          │
├─────────────────────────────────────────────────────────┤
│              CONNECTOR LAYER                            │
│  ┌─────────┐  ┌─────────────┐  ┌──────────┐           │
│  │ Ardoq   │  │ ServiceNow  │  │ Custom   │           │
│  │ (v1)    │  │ (Future)    │  │ REST     │           │
│  └─────────┘  └─────────────┘  └──────────┘           │
└─────────────────────────────────────────────────────────┘
```

---

## Repository Layout

```
prompt-shields-sdk/
├── packages/
│   ├── sdk/                    # Python SDK (ShieldsClient)
│   │   └── prompt_shields/
│   │       ├── client.py       # Drop-in OpenAI wrapper
│   │       ├── telemetry.py    # Async event shipping
│   │       └── types.py        # Shared type definitions
│   ├── collector/              # Telemetry Collector (FastAPI)
│   │   └── collector/
│   │       ├── app.py          # Application entrypoint
│   │       ├── ingest.py       # Event ingestion endpoint
│   │       ├── dedup.py        # Asset deduplication + confidence scoring
│   │       ├── registry.py     # Registry REST API
│   │       ├── embeddings.py   # pgvector semantic search
│   │       └── auth.py         # Multi-tenant auth
│   └── db/                     # Database layer
│       ├── models.py           # SQLAlchemy async models
│       └── alembic/            # Schema migrations
├── gateway/                    # AI Gateway (forked Portkey, TypeScript)
│   └── src/middlewares/
│       └── ps-telemetry.ts     # Prompt Shields telemetry middleware
├── demo/
│   ├── seed_data.py            # Seed the registry with sample assets
│   ├── demo_sdk_flow.py        # End-to-end demo script
│   └── ardoq_recipe.json       # Ardoq Integration Builder recipe
├── tests/                      # Integration tests (requires PostgreSQL)
├── scripts/
│   └── init-test-db.sql        # Test database initialisation
└── docker-compose.yml          # PostgreSQL + Collector
```

---

## Components

### Python SDK

Drop-in replacements for the OpenAI and Anthropic clients. Every LLM call is wrapped with structured telemetry — **fail-open**, so a collector outage never blocks a model call. Sync and async surfaces, PII detection, and cost estimation are all built in.

```python
from prompt_shields import ShieldsOpenAI

client = ShieldsOpenAI(
    api_key="sk-...",                          # OpenAI API key (hashed before send)
    ps_api_key="ps-...",                       # Prompt Shields tenant key
    ps_collector_url="http://localhost:8000",
    business_unit="HR",
    use_case="interview-screening",
    owner="jane.doe@acme.com",
    data_classification="confidential",
    environment="production",
    calling_service="hiring-service",
)

response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Summarize this candidate..."}],
    ps_metadata={
        "data_sources": ["candidates_db"],
        "output_destination": "hiring_dashboard",
        "risk_tags": ["pii", "gdpr"],
        "session_id": "review-2025-04-12-001",
    },
)
```

**Anthropic, identical surface:**

```python
from prompt_shields import ShieldsAnthropic

client = ShieldsAnthropic(api_key="sk-ant-...", ps_api_key="ps-...")
response = client.chat.completions.create(
    model="claude-sonnet-4-20250514",
    messages=[{"role": "user", "content": "..."}],
    max_tokens=1024,
)
```

**Async variants** for FastAPI / asyncio agents:

```python
from prompt_shields import AsyncShieldsOpenAI

client = AsyncShieldsOpenAI(api_key="sk-...", ps_api_key="ps-...")
response = await client.chat.completions.create(model="gpt-4o", messages=[...])
```

**What ships in the SDK:**

| Capability | Notes |
|------------|-------|
| OpenAI + Anthropic providers | Pluggable `providers.py` adapter layer; new vendors add ~20 lines |
| Sync + async clients | `ShieldsClient` (threaded flush) and `AsyncShieldsClient` (native await) |
| Tool/function call capture | OpenAI `tool_calls`, Anthropic `tool_use` blocks parsed automatically |
| PII detection | Pattern-based: `email`, `phone`, `ssn`, `credit_card`, `ip_address`, `iban`, `health_data`, `financial_data` — categories only, never content |
| Cost estimation | Built-in pricing table for OpenAI/Anthropic/Google models; `pricing_table=` override |
| API key fingerprint | SHA-256 truncated identity, never the raw key |
| `ps_metadata` per-request | `data_sources`, `output_destination`, `risk_tags`, `session_id`, `user_id` flow into events |
| Privacy by default | `prompt_text` never sent unless `send_prompt_text=True` is explicitly opted in |
| Fail-open buffering | 1000-event local buffer with exponential-backoff retry; oldest events drop on overflow |

### AI Gateway (zero code change)

Route existing applications through the gateway proxy instead of calling OpenAI directly. No application changes required — telemetry is injected at the HTTP layer.

Built on a focused fork of [Portkey AI Gateway](https://github.com/Portkey-AI/gateway) (MIT) with a custom `ps-telemetry.ts` middleware. Routing, caching, and guardrails features have been stripped; the fork is discovery-focused.

```bash
docker run -p 8080:8080 \
  -e PS_COLLECTOR_URL=http://collector:8000 \
  -e PS_API_KEY=ps-... \
  promptshields/gateway

# Point your app at the gateway — nothing else changes
export OPENAI_BASE_URL=http://localhost:8080/v1
```

### Telemetry Collector

FastAPI service that receives events from the SDK and gateway, deduplicates AI assets with confidence scoring (`low / medium / high / verified`), and exposes the Registry API.

**Ingest**

```
POST /ingest/events
```

**Registry API**

```
GET  /api/v1/registry/assets                    # List assets (filterable)
GET  /api/v1/registry/assets/{id}               # Asset detail
GET  /api/v1/registry/assets/{id}/data-flows    # Data lineage
GET  /api/v1/registry/assets/{id}/risks         # Risk mappings
GET  /api/v1/registry/vendors                   # Discovered vendors
GET  /api/v1/registry/models                    # Discovered models
GET  /api/v1/registry/search?q=...              # Semantic search (pgvector)
```

### Ardoq Connector

`demo/ardoq_recipe.json` is an [Ardoq Integration Builder](https://help.ardoq.com/en/articles/44154-integration-builder) recipe that reads from the Registry API and writes structured AI asset data into Ardoq AI Lens — including vendors, models, use cases, data flows, and risk mappings.

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/jun-bit-pulse-ai/prompt-shields-sdk.git
cd prompt-shields-sdk

# 2. Start PostgreSQL with pgvector
docker compose up -d db

# 3. Install Python packages
pip install -e packages/collector/[dev]
pip install -e packages/sdk/[dev]

# 4. Run migrations
cd packages/db && alembic upgrade head && cd ../..

# 5. Seed demo data
PYTHONPATH=packages:packages/collector python3 demo/seed_data.py

# 6. Start the collector
PYTHONPATH=packages:packages/collector uvicorn collector.app:app --port 8000

# 7. Run the end-to-end demo
python3 demo/demo_sdk_flow.py
```

---

## Running Tests

```bash
# Unit tests — no database required
PYTHONPATH=packages:packages/collector \
  python3 -m pytest packages/collector/tests/test_dedup.py \
                     packages/collector/tests/test_semantic_search.py -v

# SDK tests
PYTHONPATH=packages/sdk python3 -m pytest packages/sdk/tests/ -v

# Integration tests — requires PostgreSQL
PYTHONPATH=packages:packages/collector python3 -m pytest tests/ -v
```

---

## Key Features

- **Multi-source discovery** — SDK instrumentation, gateway proxy, browser extensions, and macOS app all feed a single registry.
- **Asset deduplication** — fingerprints AI assets across sources and assigns confidence scores (`low / medium / high / verified`).
- **Semantic search** — pgvector HNSW index over asset metadata for natural-language registry queries.
- **Fail-open telemetry** — collector failures never propagate to LLM calls.
- **Multi-tenant isolation** — tenant-scoped API keys throughout.
- **200+ LLM providers** — gateway inherits full Portkey provider support.
- **Ardoq AI Lens ready** — Integration Builder recipe included; ServiceNow and custom REST connectors planned.

---

## Tech Stack

| Layer | Technology |
|---|---|
| SDK | Python 3.11+, openai, httpx |
| Collector | FastAPI, SQLAlchemy (async), Pydantic v2, Alembic |
| Database | PostgreSQL 15, pgvector (HNSW) |
| Gateway | TypeScript, Node.js (forked Portkey) |
| Infrastructure | Docker Compose |
| Testing | pytest, pytest-asyncio, httpx, respx |

---

## License

The AI Gateway is forked from [Portkey AI Gateway](https://github.com/Portkey-AI/gateway) under the **MIT License** — see [`gateway/LICENSE`](gateway/LICENSE).

Prompt Shields SDK, Collector, and all extensions to the gateway are proprietary. See [`gateway/FORK_NOTICE.md`](gateway/FORK_NOTICE.md) for details on modifications.
