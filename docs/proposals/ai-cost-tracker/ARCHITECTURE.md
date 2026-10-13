# AI Cost Tracker: Architecture Plan (Draft v0.1)

Status: **Draft for review** (v0.2: product shape, hosting and frontend decided, see §2a).
Owner: Prompt Shields. Last updated: 2026-10-13.

## 1. Problem

Azure Cost Management (Cost Analysis) answers "what did this subscription cost?" but not
the questions an AI team actually asks:

- What did **each model deployment** cost yesterday, and how many tokens did it burn?
- What is our **effective cost per 1M tokens** per model, and is it drifting?
- Which **team / app / environment** is driving spend?
- Are our **PTU (provisioned) deployments** actually utilised, or are we paying for idle capacity?
- Did something **spike** overnight, and who owns it?

Getting this in the portal means jumping between Cost Analysis, Azure Monitor metrics,
the Foundry / Azure OpenAI blades, and the pricing page, with billing data 24–72h behind
usage. Doing it across several subscriptions, tenants, or clouds is worse.

**Goal:** one place that joins *billed cost* with *token usage*, per model and per
deployment, starting with Azure AI (Azure OpenAI / AI Foundry), then GCP Vertex AI and
AWS Bedrock.

### Non-goals (v1)

- General-purpose cloud FinOps for non-AI services (VMs, storage, etc.). We ingest the
  full bill but only model and surface AI services in v1.
- Cost *optimisation* actions (auto-scaling PTUs, switching models). Recommendations only.
- Replacing the gateway: we consume its usage data, we do not proxy traffic ourselves.

## 2. Key design decisions

| # | Decision | Why |
|---|----------|-----|
| D1 | **Two data planes: billed cost and token usage**, joined on (cloud, account, resource, model, day). | Billing is authoritative but late and coarse; metrics are near-real-time and granular but not money. We need both. |
| D2 | **FOCUS 1.x** (FinOps Open Cost & Usage Spec) as the canonical cost schema. | Azure, AWS and GCP all export FOCUS-formatted data, so multi-cloud becomes a mapping exercise rather than three bespoke models. |
| D3 | **Pull from cloud-native exports, not scraping APIs**, where possible. | Exports (Azure Cost Exports, AWS Data Exports, GCP BigQuery billing export) are cheap, complete and not rate limited. The Cost Details / Query APIs are a fallback for backfill and onboarding. |
| D4 | **Secretless, least-privilege connections**: Entra multi-tenant app + admin consent (Azure), Workload Identity Federation (GCP), cross-account IAM role with External ID (AWS). | Customers will not paste long-lived keys into a cost tool, and they should not have to. |
| D5 | **Python / FastAPI / PostgreSQL**, same as `prompt-shields-sdk`. | Shared libraries, auth, tenancy and ops. Time-series sit in Postgres (+ TimescaleDB) until volume justifies ClickHouse. |
| D6 | **Estimate, then true-up.** Show estimated cost (tokens × price list) for today, replace it with billed cost once the bill lands. | Users want today's number today. Estimates are visibly flagged and reconciled. |
| D7 | **Multi-tenant-ready schema, single internal tenant at launch** (tenant_id on every row, row-level security; no self-serve signup in v1). | We start as an internal tool, but the data model must not block offering it as SaaS later. Onboarding polish is deferred, isolation is not. |

## 2a. Decisions taken (2026-10-13)

| Question | Decision | Consequences for this plan |
|----------|----------|----------------------------|
| Product shape | **Internal tool first.** | Phase 1 targets our own Azure tenant(s). One tenant, created by a seed script; no signup flow, billing or tenant self-service. Connections can be set up by an admin with a CLI or a minimal settings page rather than a polished wizard. Customer-facing onboarding moves to a later phase. |
| Hosting | **SaaS only.** | One deployment we operate (Azure Container Apps). No self-hosted, Marketplace managed app or customer-tenant packaging. Config can assume our infra (Key Vault, Entra, Postgres Flexible Server). |
| Frontend | **Standalone app.** | Own Next.js app, own domain (e.g. `costs.promptshields.com`). It doesn't embed in or depend on another Prompt Shields UI; integration with the registry stays API-to-API. |

## 3. System overview

```
                         ┌──────────────────────────────────────────────┐
  Users ──OIDC          │                 Web App (Next.js)            │
  (Entra ID)────────────▶│  dashboards · explorer · budgets · settings  │
                         └───────────────────────┬──────────────────────┘
                                                 │ REST (JSON)
                         ┌───────────────────────▼──────────────────────┐
                         │              API (FastAPI)                   │
                         │  auth · tenants · connections · query · alerts│
                         └───────┬──────────────────────────┬───────────┘
                                 │                          │
                    ┌────────────▼───────────┐   ┌──────────▼──────────┐
                    │ PostgreSQL + Timescale │◀──│  Workers (arq/Redis) │
                    │  raw · normalised ·    │   │  schedulers · ingest │
                    │  aggregates · config   │   │  normalise · reconcile│
                    └────────────────────────┘   │  anomaly · alerts    │
                                                 └──────────┬──────────┘
                                                            │ pull
        ┌───────────────────────────┬───────────────────────┼────────────────────────┐
        ▼                           ▼                       ▼                        ▼
 ┌──────────────┐          ┌─────────────────┐     ┌────────────────┐      ┌──────────────────┐
 │ Azure        │          │ GCP (phase 3)   │     │ AWS (phase 4)  │      │ Prompt Shields   │
 │ Cost Exports │          │ BigQuery billing│     │ Data Exports / │      │ gateway & SDK    │
 │ Cost Details │          │ export, Cloud   │     │ CUR 2.0 (S3),  │      │ per-request      │
 │ Monitor      │          │ Monitoring      │     │ CloudWatch,    │      │ usage w/ app,    │
 │ Resource Grph│          │ (Vertex AI)     │     │ Bedrock logs   │      │ user, team tags  │
 │ Retail Prices│          │ Pricing API     │     │ Pricing API    │      │                  │
 └──────────────┘          └─────────────────┘     └────────────────┘      └──────────────────┘
```

### Components

| Component | Responsibility |
|-----------|----------------|
| **Web app** | Next.js + TypeScript. Overview, cost explorer (group by / filter), model & deployment pages, PTU utilisation, budgets & alerts, connection wizard. |
| **API** | FastAPI. Authn/z, tenant & user management, connection lifecycle, query endpoints over aggregates, alert rules, CSV / API export. |
| **Scheduler + workers** | arq on Redis. One job type per source × cadence (see §5). Idempotent, partitioned by (connection, day). |
| **Connectors** | One Python package per cloud implementing a common `Connector` interface: `discover()`, `pull_cost(day)`, `pull_usage(window)`, `pull_prices()`, `health()`. |
| **Normaliser** | Maps raw rows to FOCUS cost rows and to the token-usage fact table; parses AI meters into (model, version, direction, deployment type, region). |
| **Reconciler** | Joins estimated vs billed cost, computes effective unit prices, flags variance. |
| **Anomaly / alerts** | Daily and intraday rules: budget thresholds, day-over-day spikes, idle PTU, unknown deployments. Delivered to email, Slack, Teams, webhook. |
| **Storage** | PostgreSQL 16 + TimescaleDB hypertables for usage; raw export files retained in object storage (Azure Blob) for replay. |

## 4. Azure integration (phase 1)

### 4.1 What we read

| Data | Source | Granularity / latency | Used for |
|------|--------|-----------------------|----------|
| **Billed cost** | Cost Management **Exports** (FOCUS, daily, to a storage account) | Daily, ~8–24h lag, finalised after month close | Authoritative cost per resource & meter |
| Billed cost (backfill / no-export fallback) | Cost Management **Cost Details API** (async report) | Daily | Onboarding backfill (up to 13 months), tenants who won't create an export |
| **Token usage** | **Azure Monitor metrics** on `Microsoft.CognitiveServices/accounts` (input / output / total token metrics, request counts, PTU utilisation), split by `ModelDeploymentName` and `ModelName` | 1-min, ~3 min lag | Near-real-time tokens per deployment; estimated cost |
| **Inventory** | **Azure Resource Graph** + Cognitive Services management API (accounts, deployments, SKU: Standard / Global / Data Zone / Provisioned, capacity, model + version, tags) | On change / hourly | Map cost and metrics to named deployments, owners and tags |
| **Prices** | **Azure Retail Prices API** (public) + customer price sheet / reservations from billing APIs when permitted | Daily | Estimation; showing list vs negotiated price |
| **Per-request attribution** (optional) | Prompt Shields **gateway** usage events (or Azure diagnostic logs / APIM token metrics if the customer uses APIM) | Real time | Cost by app, user, team, prompt route — something Azure cannot show natively |

### 4.2 Meter parsing

Azure OpenAI billing lines carry meter names such as `gpt-4o-0806 Inp regnl 1M Tokens` or
`gpt-4o-mini-0718-Outp-glbl`. The normaliser maintains a versioned rules table that maps
`(MeterCategory, MeterSubCategory, MeterName)` → `{model, version, direction
(input|cached_input|output|training|hosting|ptu), deployment_type, unit}`. Unknown meters are
surfaced in an admin queue rather than silently dropped, and new mappings ship as data, not
code.

### 4.3 Connecting an Azure tenant

Two supported paths. In the internal phase an admin configures them via CLI / settings page; a guided wizard comes with external onboarding:

1. **Delegated admin consent (recommended)**
   - Prompt Shields registers a **multi-tenant Entra ID app**. A customer admin clicks
     "Connect Azure", signs in, and grants admin consent.
   - The wizard then guides (or, with the admin's token, performs) role assignments on the
     chosen scope (management group, subscription or billing account):
     - `Cost Management Reader` (cost data)
     - `Monitoring Reader` (token metrics)
     - `Reader` (Resource Graph inventory, deployments)
     - `Storage Blob Data Reader` on the export container (if using exports)
   - We authenticate as that service principal using a **federated credential** from our
     own workload identity: no client secret is stored for customer tenants.
   - Optionally we create the FOCUS daily export in the customer's storage account on their
     behalf (requires `Cost Management Contributor` once, at setup).
2. **Bring-your-own export (most restrictive)** — customer creates the export themselves and
   grants us read access to the container only. We get cost but not token metrics or
   inventory; the UI shows which features are degraded.

EA / MCA billing-account scope is preferred so one connection covers all subscriptions;
subscription scope works for teams without billing-account access.

## 5. Ingestion pipeline

| Job | Cadence | Notes |
|-----|---------|-------|
| `inventory.sync` | Hourly + on demand | Upsert accounts and deployments; detect new / deleted deployments. |
| `usage.pull` | Every 5 min | Azure Monitor metrics, 1-min grain, rolled up to 5-min / hourly / daily continuous aggregates. Watermark per (connection, resource). |
| `prices.sync` | Daily | Retail prices for AI meters; diff stored so historical estimates stay reproducible. |
| `cost.pull` | Every 4h | Read new export files (manifest-driven); re-ingest a day whenever its file is rewritten (Azure restates the current month). |
| `cost.backfill` | On connect | Cost Details API, month by month, rate-limit aware. |
| `reconcile` | After each `cost.pull` | Replace estimates with billed figures for closed days; record variance. |
| `alerts.evaluate` | 15 min (usage-based) / daily (cost-based) | Budgets, spikes, idle PTU, unmapped meters. |

All jobs are **idempotent per (connection, source, day)**: a re-run deletes and rewrites
that partition inside one transaction. Raw files and API responses are written to object
storage first ("land, then load") so we can re-normalise when mapping rules change.

## 6. Data model

```
tenant ─┬─ user / role
        ├─ cloud_connection (cloud, scope, auth_method, status, last_success_at)
        ├─ cloud_account      (subscription / GCP project / AWS account)
        ├─ ai_resource        (Azure OpenAI account, Vertex endpoint, Bedrock region)
        │    └─ ai_deployment (name, model, version, sku/type, capacity, tags, owner)
        ├─ cost_line          FOCUS columns + tenant_id, connection_id, ai_* parsed fields
        ├─ token_usage        hypertable: ts, deployment_id, model, input_tokens,
        │                     cached_input_tokens, output_tokens, requests, source
        ├─ price              (cloud, sku/meter, region, unit, unit_price, currency, valid_from)
        ├─ daily_ai_cost      materialised: day, deployment, model, billed_cost,
        │                     estimated_cost, tokens_in/out, effective_$_per_1M
        ├─ allocation_rule    tag / gateway-attribute → cost centre, team, app
        └─ budget / alert_rule / alert_event
```

- **Money**: `NUMERIC(20,10)` in billing currency plus a normalised reporting currency
  (FX table, daily rate). Never floats.
- **Tenancy**: `tenant_id` on every table with Postgres RLS, mirroring `prompt-shields-sdk`.
- **Retention**: 1-min usage for 14 days, 5-min for 90 days, hourly / daily indefinitely.
  Cost lines indefinitely.

## 7. Login, identity and access

- **User login**: OIDC. v1 (internal) supports **Microsoft Entra ID** for our own tenant only,
  restricted to an allow-listed group. Google and other external IdPs are added when the tool
  opens to external users; SAML / generic OIDC for enterprise SSO after that.
- **Sessions**: short-lived JWT access tokens + HTTP-only refresh cookie; API keys for
  programmatic access (scoped, hashed at rest), compatible with the SDK's `ps-…` key format.
- **Tenant mapping**: v1 has one seeded internal tenant, and Entra group membership maps to
  roles. When opened up externally: the first user from an IdP domain creates the tenant;
  later users join via invite or domain claim.
- **RBAC**: `owner`, `admin` (manage connections, budgets), `analyst` (all data, read-only),
  `viewer` (scoped to cost centres / tags they own). Scopes are enforced in the query layer,
  not just the UI.
- **Customer cloud credentials** never leave the backend: federated credentials where
  possible; anything that must be stored (e.g. a BYO client secret) is envelope-encrypted
  with a KMS / Key Vault key per tenant.
- **Audit log** of logins, connection changes, role changes and exports.

## 8. Multi-cloud expansion

Each cloud implements the same `Connector` interface and emits FOCUS cost rows plus
`token_usage` rows. The UI and alerting do not change per cloud.

### Phase 3 — GCP (Vertex AI / Gemini)

| Data | Source |
|------|--------|
| Billed cost | **Cloud Billing export to BigQuery** (detailed usage cost, FOCUS view) — we query the customer's dataset |
| Token usage | **Cloud Monitoring** metrics for Vertex AI / Gemini (token counts and request counts per model / endpoint) |
| Inventory | Vertex AI API (endpoints, models), Asset Inventory |
| Prices | Cloud Billing Catalog API |
| Auth | **Workload Identity Federation**: customer grants our identity `roles/bigquery.dataViewer` + `roles/bigquery.jobUser` on the billing dataset, `roles/monitoring.viewer`, `roles/aiplatform.viewer`. No JSON keys. |

### Phase 4 — AWS (Bedrock)

| Data | Source |
|------|--------|
| Billed cost | **AWS Data Exports** (CUR 2.0 or FOCUS) to S3; read via Athena or direct Parquet |
| Token usage | **CloudWatch** `AWS/Bedrock` metrics (input / output token count, invocations, by model ID); optional Bedrock model-invocation logs for per-request detail |
| Inventory | Bedrock API (provisioned throughput, inference profiles) |
| Prices | AWS Price List API |
| Auth | **Cross-account IAM role** with External ID, deployed by a CloudFormation quick-create link from the wizard; read-only policy scoped to the export bucket, CloudWatch and Bedrock list/describe. |

### Also on the roadmap

Direct providers (OpenAI, Anthropic, Google AI Studio) via their usage / cost admin APIs,
and Databricks / Snowflake AI functions, all landing in the same two tables.

## 9. Relationship to Prompt Shields

`prompt-shields-sdk` builds an **inventory of AI assets** (one vendor, one model, one
business unit, one purpose). The cost tracker adds **what each asset costs**:

- Gateway / SDK usage events already carry model, provider and tenant-defined attributes
  (app, team, purpose). Ingesting them gives per-app and per-user attribution that cloud
  bills cannot.
- Cost rows link to registry assets by (provider, model, deployment / account), so an
  auditor's asset view can show monthly spend and token volume next to risk classification.
- Shared packages: auth, tenancy, API-key handling and the DB layer should be extracted into
  a common library rather than copy-pasted. Until then this repo vendors nothing and talks to
  the registry over its public API.

## 10. Deployment & operations

- **Runtime**: containers on Azure Container Apps (API, workers, web) — dogfoods Azure and
  makes Entra federated credentials trivial. Azure Database for PostgreSQL Flexible Server
  (TimescaleDB extension), Azure Cache for Redis, Blob Storage for raw landing, Key Vault.
- **IaC**: Bicep or Terraform in `infra/`; one environment per stage (dev, staging, prod).
- **Local dev**: `docker compose up` with Postgres + Timescale, Redis, and a **fake cloud**
  connector that replays fixture exports and metrics, so nobody needs an Azure tenant to
  work on the UI.
- **Observability**: OpenTelemetry traces + metrics; per-connection health page (last
  successful pull per source, lag, error) visible to customers.
- **CI**: lint, type-check, unit tests, connector contract tests against recorded fixtures,
  migration check.

## 11. Security & compliance

- Read-only cloud roles only; the wizard shows exactly which permissions are requested and
  why, and a "verify permissions" check after setup.
- Cost data is commercially sensitive: encryption at rest and in transit, tenant isolation
  via RLS, and no cross-tenant aggregates without explicit opt-in.
- No prompt / completion content is ever ingested; only counts and metadata.
- SOC 2-friendly audit trail from day one (§7).

## 12. Proposed repository layout

```
ai-cost-tracker/
├── apps/
│   ├── api/              FastAPI service
│   ├── worker/           arq jobs & scheduler
│   └── web/              Next.js frontend
├── packages/
│   ├── core/             domain models, FOCUS schema, money/FX, tenancy
│   ├── connectors/
│   │   ├── base/         Connector interface + contract tests
│   │   ├── azure/
│   │   ├── gcp/
│   │   ├── aws/
│   │   └── fake/         fixture replay for local dev & tests
│   ├── normalise/        meter parsing rules, FOCUS mapping
│   └── db/               SQLAlchemy models + Alembic migrations
├── infra/                Bicep / Terraform
├── docs/                 architecture, ADRs, runbooks
└── docker-compose.yml
```

## 13. Delivery plan

| Phase | Scope | Exit criteria |
|-------|-------|---------------|
| **0. Foundations** (2 wks) | Repo skeleton, CI, Postgres + Timescale schema (tenant-scoped), Entra ID login for our tenant, RBAC via Entra groups, seeded internal tenant, fake connector, empty dashboard shell. | Can sign in with a work account and see fixture data in a chart. |
| **1. Azure MVP — internal** (4–5 wks) | Azure connection for our own tenant (service principal + federated credential, set up by an admin); inventory sync; Azure Monitor token metrics; Cost Details backfill + FOCUS export ingestion; meter parsing; daily cost by deployment / model; estimated vs billed; cost per 1M tokens. | Internal Azure tenant shows yesterday's AI cost per deployment within 1% of Cost Analysis, and today's tokens within 5 min. |
| **2. Make it useful** (3–4 wks) | Budgets & alerts (email, Slack, Teams), spike detection, PTU utilisation & idle-capacity report, tag-based allocation to cost centres, gateway usage ingestion for per-app / per-user attribution, CSV export & API. | The team uses it instead of Cost Analysis for AI spend reviews. |
| **2b. Open to external users** (3 wks, when we decide to) | Self-serve signup, Entra admin-consent connection wizard, Google login, SAML, tenant admin pages, usage limits. | First design-partner tenant onboarded without engineering help. |
| **3. GCP** (3 wks) | WIF connection, BigQuery billing export, Vertex AI metrics & inventory. | Unified multi-cloud overview. |
| **4. AWS** (3 wks) | CloudFormation role, Data Exports, Bedrock CloudWatch metrics & inventory. | Three clouds in one view. |
| **5. Beyond** | Direct model providers, forecasting, recommendations (model swaps, PTU right-sizing), API-level links to the Prompt Shields registry. | — |

## 14. Risks

| Risk | Mitigation |
|------|------------|
| Billing data lags 24–72h and is restated during the month | Estimate-then-true-up (D6); re-ingest rewritten export days; label numbers as estimated / billed / finalised. |
| Azure meter names change with new models | Data-driven mapping table, unmapped-meter queue and alert, fixture tests per new model family. |
| Token metric names / dimensions differ between classic Azure OpenAI and AI Foundry resources | Connector abstracts metric discovery (list metric definitions per resource) instead of hard-coding names. |
| Customers reluctant to grant tenant-wide roles | Subscription-scope and BYO-export modes; clear permission explanations; read-only only. |
| Negotiated / EA pricing differs from retail | Prefer billed cost; read price sheet when the role allows; show "list vs effective". |
| Postgres volume at scale | Timescale compression + continuous aggregates first; ClickHouse is a planned swap behind the query layer. |

## 15. Open questions

Product shape, hosting and frontend are decided (§2a). Remaining:

1. **Attribution source** — is the Prompt Shields gateway deployed in front of the Azure
   OpenAI traffic we care about, or do we need APIM / diagnostic-log ingestion too?
2. **Billing scope** — do we have EA / MCA billing-account access for our own tenant to
   validate the billing-account-scope path early?

## Appendix A — ADRs to write next

- ADR-001: FOCUS as canonical cost schema
- ADR-002: Postgres + TimescaleDB vs ClickHouse for usage data
- ADR-003: Connector interface and idempotent partitioned ingestion
- ADR-004: Federated, secretless cloud authentication
- ADR-005: Estimated vs billed cost reconciliation semantics
