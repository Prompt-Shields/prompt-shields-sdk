<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset=".github/assets/logo-dark.svg">
  <img src=".github/assets/logo-light.svg" alt="Prompt Shields" width="72" height="72">
</picture>

# Prompt Shields SDK & AI Gateway

### Find every AI system your organisation runs.<br>Two lines of code — or none.

[Quickstart](#quickstart) · [How it works](#how-it-works) · [Docs](docs/introduction.mdx) · [SDK guide](SDK_GUIDE.md) · [Gateway](gateway/README.md) · [Changelog](CHANGELOG.md)

[![Licence](https://img.shields.io/badge/licence-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg?logo=python&logoColor=white)](packages/sdk/pyproject.toml)
[![SDK](https://img.shields.io/badge/sdk-v0.2.0-6C5CE7.svg)](CHANGELOG.md)
[![Self-hosted](https://img.shields.io/badge/self--hosted-no%20account%20needed-2ea44f.svg)](#quickstart)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-ff69b4.svg)](CONTRIBUTING.md)

</div>

<br>

Prompt Shields discovers and catalogues every AI system running inside an organisation — sanctioned or not — and publishes that inventory to your Enterprise Architecture tooling.

- [x] **Drop-in SDK** — swap `OpenAI` for `ShieldsOpenAI` and every call reports itself, with owner and business unit attached
- [x] **Zero-code gateway** — point `OPENAI_BASE_URL` at the proxy and discover traffic from apps you cannot modify
- [x] **One register** — events from every source are deduplicated into AI assets, each with a confidence score
- [x] **Private by default** — PII *categories* and a key *fingerprint* leave your process; prompt text and raw keys do not
- [x] **Fail-open** — a collector outage never blocks a model call
- [x] **Built to be exported** — a Partner API with OAuth 2.0, delta sync and bulk export, plus an Ardoq recipe
- [x] **Runs on your machine** — Docker and Python. No Prompt Shields account, no LLM API key

<br>

## Two ways in

<table>
<tr>
<td width="50%" valign="top">

**Python SDK** — rich business context

```python
from prompt_shields import ShieldsOpenAI

client = ShieldsOpenAI(
    api_key="sk-...",
    ps_api_key="ps-...",
    ps_collector_url="http://localhost:8000",
    business_unit="HR",
    use_case="interview-screening",
    owner="jane.doe@acme.com",
    data_classification="confidential",
)

client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "..."}],
)
```

</td>
<td width="50%" valign="top">

**AI Gateway** — no code change

```bash
export OPENAI_BASE_URL=http://localhost:8787/v1
```

```python
from openai import OpenAI

client = OpenAI()  # unmodified

client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "..."}],
    extra_headers={
        "X-PS-Business-Unit": "HR",
        "X-PS-Use-Case": "interview-screening",
        "X-PS-Owner": "jane.doe@acme.com",
    },
)
```

</td>
</tr>
</table>

`AsyncShieldsOpenAI`, `ShieldsAnthropic` and `AsyncShieldsAnthropic` present the same surface as `ShieldsOpenAI`. The `X-PS-*` headers are optional; without them the gateway still records vendor, model, tokens and latency.

<br>

## The problem

Most organisations cannot answer a basic governance question: which business processes depend on which models, holding which data, owned by whom. Procurement records miss the API key a team expensed last quarter, CMDB entries miss the LLM call buried in a microservice, and neither sees the employee pasting customer records into a browser chatbot. Without a live inventory, EU AI Act classification, ISO 42001 scoping, and third-party risk review are all performed against a guess — and the controls you design protect a system you have never actually enumerated.

<br>

## Quickstart

Requires Docker and Python 3.11 or newer. Runs entirely on your machine; no Prompt Shields account and no LLM API key are needed.

```bash
git clone https://github.com/Prompt-Shields/prompt-shields-sdk.git && cd prompt-shields-sdk
docker compose up -d
pip install -e "packages/sdk[all]" -e "packages/collector[dev]"
until docker compose exec -T db pg_isready -q; do sleep 1; done && (cd packages/db && PYTHONPATH=../../packages alembic upgrade head)
PYTHONPATH=packages:packages/collector python3 demo/seed_data.py && python3 demo/demo_sdk_flow.py
```

The demo ingests an event, then reads the resulting asset back out of the registry. The registry is then browsable at `http://localhost:8000/api/v1/registry/assets` with the header `Authorization: Bearer ps-demo-key-acme`.

<details>
<summary><b>Port 5432 already in use?</b></summary>
<br>

If you already run PostgreSQL on port 5432, the container will not receive the connection and you will see `role "ps_user" does not exist`. Publish the database on another port instead, and point the tooling at it:

```bash
export PS_DB_PORT=5433
export PS_DATABASE_URL="postgresql+asyncpg://ps_user:ps_local_dev@localhost:5433/prompt_shields"
```

</details>

<details>
<summary><b>Re-running the seed script</b></summary>
<br>

Seeding is one-shot. It creates a demo tenant each time it runs, so re-run it only against a fresh database.

</details>

<details>
<summary><b>Running the gateway</b></summary>
<br>

The gateway is a Node.js service in [`gateway/`](gateway/). Telemetry is switched on by setting `PS_API_KEY`:

```bash
cd gateway && npm install
PS_COLLECTOR_URL=http://localhost:8000 PS_API_KEY=ps-demo-key-acme npm run dev:node
```

It listens on port 8787. See the [gateway README](gateway/README.md) for Docker, Cloudflare Workers and the other deployment targets inherited from Portkey.

</details>

<br>

## What you get

### Discovery

| | |
|---|---|
| **AI Asset Registry** | A deduplicated register of AI use cases — one vendor, one model, one business unit, one purpose — in PostgreSQL, behind a REST API with cursor pagination |
| **Confidence scoring** | Each asset is scored `low`, `medium`, `high` or `verified` by how many independent sources reported it |
| **Semantic search** | Optional pgvector search across discovered assets |
| **Provider coverage** | The SDK wraps OpenAI and Anthropic. The gateway inherits 70+ provider adapters from Portkey behind an OpenAI-compatible API |

### Signal on every call

| Captured | Notes |
|---|---|
| Vendor, model, tokens, latency, cost | Built-in pricing table for OpenAI, Anthropic and Google models |
| Ownership metadata | Business unit, use case, owner, environment, data classification |
| PII categories | `email`, `phone`, `ssn`, `credit_card`, `ip_address`, `iban`, `health_data`, `financial_data` — categories only, never the matched value |
| Tool and function calls | OpenAI `tool_calls` and Anthropic `tool_use` blocks |
| API key fingerprint | Truncated SHA-256, never the raw key |
| Requested versus served model | Makes cost-routing savings provable |

Prompt text is never transmitted unless `send_prompt_text=True` is explicitly set.

### Gateway extras, both off by default

| | |
|---|---|
| **Cost-aware routing** | Send `model="auto"` or a `RouteHint(quality=..., max_cost=...)` and the gateway picks a concrete model. Enabled with `PS_ROUTER_ENABLED=true`; a concrete model with no hint is left untouched |
| **Exact-match cache** | Identical requests are answered from an in-memory LRU cache, controlled per request with `X-PS-Cache: on \| off \| refresh` |

### Integrations

| | |
|---|---|
| **Partner API** | OAuth 2.0 client credentials, delta sync, bulk export, rate limited |
| **Ardoq** | A ready-made Integration Builder recipe in [`demo/ardoq_recipe.json`](demo/ardoq_recipe.json) |
| **Anything else** | Plain REST — see the [integration guides](docs/integrations/overview.mdx) |

<br>

## How it works

Prompt Shields separates *collection* from *inventory*. Several independent collectors observe AI usage and emit events; one collector service turns that stream of events into a deduplicated register of assets.

```mermaid
flowchart TD
    A["Your code<br/><i>ShieldsOpenAI / ShieldsAnthropic</i>"] --> SDK["Python SDK"]
    B["Your code, unmodified<br/><i>OPENAI_BASE_URL=gateway</i>"] --> GW["AI Gateway<br/><i>TypeScript, Portkey fork</i>"]
    SDK --> P(["Model provider"])
    GW --> P
    SDK -. "structured events" .-> C
    GW -. "structured events" .-> C
    C["Telemetry Collector<br/><i>FastAPI · fail-open</i>"] --> R[("AI Asset Registry<br/><i>PostgreSQL + pgvector</i>")]
    R --> API["Registry REST API<br/><i>tenant bearer token</i>"]
    R --> PA["Partner API<br/><i>OAuth 2.0 · delta sync · bulk export</i>"]
    API --> Q["Your queries"]
    PA --> EA["Ardoq, ServiceNow,<br/>custom REST consumers"]
```

**An AI asset is a single AI use case as an auditor would describe it** — one vendor, one model, one business unit, one purpose, for example "HR screening interview transcripts with GPT-4o". **A discovery source is a mechanism that observes AI usage and reports it.** This repository contains two of them, the SDK and the gateway; the browser extensions and macOS desktop app that capture shadow AI live in separate repositories and feed the same collector.

**The gateway is an HTTP proxy on the path between an application and a model provider, so calls can be observed without the application being modified.** Its telemetry middleware is registered in the request pipeline at [`gateway/src/index.ts:115`](gateway/src/index.ts).

**Fail-open means the telemetry path is allowed to fail silently.** Events buffer locally in a bounded in-memory queue and the model call proceeds regardless of whether the collector is reachable.

**Confidence is a count of distinct discovery sources**, computed in [`packages/collector/collector/dedup.py`](packages/collector/collector/dedup.py): two or more sources yields `verified`; a single SDK or gateway sighting yields `high`; a browser extension, macOS app, or platform signal yields `medium`; anything else `low`.

<br>

## What this does not do

Most READMEs stop at the feature list. This one is for people who have to sign off on what they deploy, so the limits are stated as plainly as the features.

This is a discovery and inventory tool. It is not a control, and the statements below are the boundary of what it can be relied upon for.

**It does not block anything.** No prompt, response or tool call is ever filtered, redacted, rewritten, or refused. Every call proceeds exactly as it would without Prompt Shields; the only change is that a record is written. The one exception is the model name, and only if you opt in: with `PS_ROUTER_ENABLED=true` the gateway may swap the requested model for a cheaper one. Despite the product name, there is no prompt-injection, jailbreak, or unsafe-output defence anywhere in this repository.

**PII detection is a signal, not a data loss prevention control.** It is regular-expression and keyword matching over prompt text, run locally, and it is wrong in both directions. `10.2.14.3` in a release note is reported as `ip_address`; a sixteen-digit order number is reported as both `credit_card` and `phone`; `Jane Doe, 14 Rue Lafayette, Paris` is reported as nothing at all. Names, postal addresses, free-text identifiers, and anything in a language or format not covered by the patterns are missed. Treat the output as a hint about where to look, never as evidence that a payload was clean.

**Discovery is opt-in per code path, and silent where it is not applied.** The SDK requires a code change at the call site; the gateway requires traffic to be routed through it. Any code path that does neither is invisible to the register, and the register cannot tell you that it is missing. There is no network scanning, no eBPF, and no agent that finds AI usage you have not instrumented. An empty result means nothing was reported, not that nothing is running.

**`verified` does not mean a human verified it.** Confidence is a count of distinct discovery sources. Two automated sources reporting the same asset yields `verified`; no human review is involved at any point, and a persistently wrong owner or classification will be scored `verified` just the same.

**The register is not an audit log.** Telemetry is fail-open and lossy by design. Events buffer in memory, up to 1,000 per client, and the oldest are dropped on overflow; the buffer is not persisted, so anything unsent is lost when the process exits. Nothing reconciles what was sent against what was received. Do not present the register as a complete record of AI usage in a regulatory or evidentiary context.

**The collector is not hardened for production.** It authenticates by looking up the bearer token as plaintext in the tenant record, which the source itself flags as a Phase 1 shortcut that must never be deployed to production or pilot environments. The `rate_limit_per_minute` setting in `config.py` is defined but not enforced anywhere — only the Partner API has a working rate limiter. There is no TLS termination, no role-based access control, no encryption at rest, no data-retention or deletion policy, and no audit logging of registry reads. Tenant isolation is a `tenant_id` predicate applied per query, not database row-level security, so it holds only as far as the application code is correct.

**Semantic search calls OpenAI.** When `OPENAI_API_KEY` is set on the collector, asset metadata — vendor, model, use case, business unit — is sent to OpenAI's embeddings API (`text-embedding-3-small`) to build the search index. Leave the variable unset and embeddings are skipped, disabling `/search` but keeping asset metadata inside your network.

**Cost figures are estimates.** Computed from a local pricing table against observed token counts. They will not reconcile to a provider invoice, and the table must be kept current by hand.

**Shadow AI capture is not in this repository.** The browser extensions and macOS app that detect employees using ChatGPT, Gemini, and Copilot are separate products. Cloning this repository gives you developer-side and infrastructure-side discovery only.

<br>

## Free vs Prompt Shields Cloud

Everything in this repository is Apache 2.0, self-hosted, and runs without a Prompt Shields account. The intended boundary is that **anything an individual engineer needs is free; anything an organisation or an auditor needs is paid**, and no capability moves from the free side to the paid side.

> This table is provisional. It was reconstructed from what is present in this repository against what the documentation points at hosted infrastructure, and has not been confirmed against the canonical commercial boundary statement. Replace it before publication.

| | This repository, self-hosted | Prompt Shields Cloud |
|---|---|---|
| Python SDK, telemetry collector, registry API | Yes | Yes |
| AI Gateway fork | Yes, self-built | Managed |
| Partner API — OAuth 2.0, delta sync, bulk export | Yes | Managed, with support |
| Ardoq recipe and custom REST integration | Yes | Yes |
| Hosted collector and registry | You run PostgreSQL and the service | Managed |
| Browser extensions and macOS app for shadow AI | Not included | Included |
| Governance dashboard, OWASP LLM Top 10 and MITRE ATLAS mapping | Not included | Included |
| Managed detection models, retrained continuously | Not included | Included |
| Managed multi-tenancy, sandbox environment, status page | Not included | Included |
| SSO and SAML, SCIM, RBAC, policy versioning and approval | Not included | Included |
| Tamper-evident audit logs and regulatory evidence exports | Not included | Included |
| Operational responsibility — upgrades, backups, hardening | Yours | Prompt Shields |
| Support and service levels | Community, best effort | Contractual |

The self-hosted path carries the hardening gaps listed in the section above; closing them is your responsibility.

<br>

## Documentation

| | |
|---|---|
| [Introduction and key concepts](docs/introduction.mdx) | Assets, discovery sources, confidence, data flows |
| [SDK guide](SDK_GUIDE.md) | Architecture, configuration, debugging, FastAPI patterns |
| [Python SDK reference](docs/sdks/python.mdx) | Clients, metadata, routing hints |
| [Partner API reference](docs/api-reference/overview.mdx) | Authentication, pagination, rate limits, errors |
| [Integration guides](docs/integrations/overview.mdx) | Ardoq, LeanIX, ServiceNow |
| [Gateway](gateway/README.md) · [fork notice](gateway/FORK_NOTICE.md) | What was kept, stripped and added in the vendored Portkey gateway, and its MIT licence |
| [Changelog](CHANGELOG.md) | Release history |

## Contributing

Bug reports, provider adapters and documentation fixes are all welcome. [CONTRIBUTING.md](CONTRIBUTING.md) covers development setup, tests, and pull request expectations; the [code of conduct](CODE_OF_CONDUCT.md) applies to every interaction.

Found a vulnerability? Report it privately to security@promptshields.com, never via a public issue — see the [security policy](SECURITY.md).

## Licence

Apache 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

The `gateway/` directory is a fork of the [Portkey AI Gateway](https://github.com/Portkey-AI/gateway), licensed under the MIT Licence. That licence continues to govern the upstream code and is retained at [`gateway/LICENSE`](gateway/LICENSE). MIT is compatible with Apache 2.0, so the combined work may be distributed under these terms provided both notices are preserved.
