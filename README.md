# Prompt Shields SDK

[![Licence](https://img.shields.io/badge/licence-Apache--2.0-blue.svg)](LICENSE)

Prompt Shields discovers and catalogues every AI system running inside an organisation — sanctioned or not — and publishes that inventory to your Enterprise Architecture tooling.

## The problem

Most organisations cannot answer a basic governance question: which business processes depend on which models, holding which data, owned by whom. Procurement records miss the API key a team expensed last quarter, CMDB entries miss the LLM call buried in a microservice, and neither sees the employee pasting customer records into a browser chatbot. Without a live inventory, EU AI Act classification, ISO 42001 scoping, and third-party risk review are all performed against a guess — and the controls you design protect a system you have never actually enumerated.

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

If you already run PostgreSQL on port 5432, the container will not receive the connection and you will see `role "ps_user" does not exist`. Publish the database on another port instead, and point the tooling at it:

```bash
export PS_DB_PORT=5433
export PS_DATABASE_URL="postgresql+asyncpg://ps_user:ps_local_dev@localhost:5433/prompt_shields"
```

Seeding is one-shot. It creates a demo tenant each time it runs, so re-run it only against a fresh database.

## How does it work?

Prompt Shields separates *collection* from *inventory*. Several independent collectors observe AI usage and emit events; one collector service turns that stream of events into a deduplicated register of assets.

**An AI asset is a single AI use case as an auditor would describe it** — one vendor, one model, one business unit, one purpose, for example "HR screening interview transcripts with GPT-4o". **A discovery source is a mechanism that observes AI usage and reports it.** This repository contains two of them, the SDK and the gateway; the browser extensions and macOS desktop app that capture shadow AI live in separate repositories and feed the same collector.

```
  your code                        your code (unmodified)
      |                                     |
      | ShieldsOpenAI /                     | OPENAI_BASE_URL=gateway
      | ShieldsAnthropic                    v
      v                             +---------------+
  +----------+                      |  AI Gateway   |--> model provider
  | Python   |--> model provider    |  (TypeScript, |
  |   SDK    |                      |   Portkey fork)|
  +-----+----+                      +-------+-------+
        |                                   |
        |        structured events          |
        +----------------+------------------+
                         v
             +-----------------------+
             |  Telemetry Collector  |  fail-open: a collector
             |       (FastAPI)       |  outage never blocks a
             +-----------+-----------+  model call
                         v
             +-----------------------+
             | PostgreSQL + pgvector |  deduplication,
             |    AI Asset Registry  |  confidence scoring
             +-----------+-----------+
                         v
          +--------------+---------------+
          v                              v
   Registry REST API              Partner API
   (tenant bearer token)          (OAuth 2.0, delta sync,
          |                        bulk export, rate limited)
          v                              v
     your queries                 Ardoq, ServiceNow,
                                  custom REST consumers
```

**The gateway is an HTTP proxy on the path between an application and a model provider, so calls can be observed without the application being modified.** Its telemetry middleware is registered in the request pipeline at [`gateway/src/index.ts:115`](gateway/src/index.ts).

**Fail-open means the telemetry path is allowed to fail silently.** Events buffer locally in a bounded in-memory queue and the model call proceeds regardless of whether the collector is reachable.

**Confidence is a count of distinct discovery sources**, computed in [`packages/collector/collector/dedup.py`](packages/collector/collector/dedup.py): two or more sources yields `verified`; a single SDK or gateway sighting yields `high`; a browser extension, macOS app, or platform signal yields `medium`; anything else `low`.

### What does the SDK capture?

| Captured | Notes |
|---|---|
| Vendor, model, tokens, latency, cost | Bundled model catalog: OpenAI, Anthropic, Google, plus EU-hosted Mistral, Scaleway and OVHcloud models |
| Ownership metadata | Business unit, use case, owner, environment, data classification |
| PII categories | `email`, `phone`, `ssn`, `credit_card`, `ip_address`, `iban`, `health_data`, `financial_data` — categories only, never the matched value |
| Tool and function calls | OpenAI `tool_calls` and Anthropic `tool_use` blocks |
| API key fingerprint | Truncated SHA-256, never the raw key |
| Requested versus served model | Makes cost-routing savings provable |

Prompt text is never transmitted unless `send_prompt_text=True` is explicitly set.

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

response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Summarise this candidate..."}],
)
```

`AsyncShieldsOpenAI` and `ShieldsAnthropic` present the same surface. A deeper walkthrough is in [SDK_GUIDE.md](SDK_GUIDE.md).

## What this does not do

This is a discovery and inventory tool. It is not a control, and the statements below are the boundary of what it can be relied upon for.

**It does not block anything.** No prompt, response, tool call, or model is ever filtered, redacted, rewritten, or refused. Every call proceeds exactly as it would without Prompt Shields; the only change is that a record is written. Despite the product name, there is no prompt-injection, jailbreak, or unsafe-output defence anywhere in this repository.

**PII detection is a signal, not a data loss prevention control.** It is regular-expression and keyword matching over prompt text, run locally, and it is wrong in both directions. `10.2.14.3` in a release note is reported as `ip_address`; a sixteen-digit order number is reported as both `credit_card` and `phone`; `Jane Doe, 14 Rue Lafayette, Paris` is reported as nothing at all. Names, postal addresses, free-text identifiers, and anything in a language or format not covered by the patterns are missed. Treat the output as a hint about where to look, never as evidence that a payload was clean.

**Discovery is opt-in per code path, and silent where it is not applied.** The SDK requires a code change at the call site; the gateway requires traffic to be routed through it. Any code path that does neither is invisible to the register, and the register cannot tell you that it is missing. There is no network scanning, no eBPF, and no agent that finds AI usage you have not instrumented. An empty result means nothing was reported, not that nothing is running.

**`verified` does not mean a human verified it.** Confidence is a count of distinct discovery sources. Two automated sources reporting the same asset yields `verified`; no human review is involved at any point, and a persistently wrong owner or classification will be scored `verified` just the same.

**The register is not an audit log.** Telemetry is fail-open and lossy by design. Events buffer in memory, up to 1,000 per client, and the oldest are dropped on overflow; the buffer is not persisted, so anything unsent is lost when the process exits. Nothing reconciles what was sent against what was received. Do not present the register as a complete record of AI usage in a regulatory or evidentiary context.

**The collector is not hardened for production.** It authenticates by looking up the bearer token as plaintext in the tenant record, which the source itself flags as a Phase 1 shortcut that must never be deployed to production or pilot environments. The `rate_limit_per_minute` setting in `config.py` is defined but not enforced anywhere — only the Partner API has a working rate limiter. There is no TLS termination, no role-based access control, no encryption at rest, no data-retention or deletion policy, and no audit logging of registry reads. Tenant isolation is a `tenant_id` predicate applied per query, not database row-level security, so it holds only as far as the application code is correct.

**Semantic search calls OpenAI.** When `OPENAI_API_KEY` is set on the collector, asset metadata — vendor, model, use case, business unit — is sent to OpenAI's embeddings API (`text-embedding-3-small`) to build the search index. Leave the variable unset and embeddings are skipped, disabling `/search` but keeping asset metadata inside your network.

**Cost figures are estimates.** They are computed from a bundled price catalog against observed token counts. They will not reconcile to a provider invoice. Cached and reasoning tokens are not yet priced separately. The catalog is a dated snapshot of the LiteLLM price map; it is regenerated with `scripts/build_catalog.py`, not refreshed at runtime.

**Shadow AI capture is not in this repository.** The browser extensions and macOS app that detect employees using ChatGPT, Gemini, and Copilot are separate products. Cloning this repository gives you developer-side and infrastructure-side discovery only.

## Coming next: Prompt Shields Route

> **Status: in development. None of this is in the current release.** Everything in
> the section above still describes what this repository does today. Scope, phasing and
> success criteria are in [PRD 0001](docs/prd/0001-model-cost-optimizer.md). Progress is
> tracked in the [project milestones](https://github.com/Prompt-Shields/prompt-shields-sdk/milestones).

**Cut your AI bill without letting sensitive data go anywhere it shouldn't.**

### Why

AI spend keeps rising even as the price per token falls. Most of the waste comes from
sending every request to the most expensive model "just in case." Routers that pick a
cheaper model already exist. The question they cannot answer for a regulated business is
*whether that cheaper model is allowed to see this data.* A router that knows prices but
not sensitivity hands the CFO a saving and the CISO a breach.

Prompt Shields already sits in the request path and already knows what kind of data each
call carries. Route turns that knowledge into savings.

### What it delivers

| For | Value |
|---|---|
| **The CFO and FinOps team** | Each request goes to the cheapest model that still meets your quality bar. Spend is attributed to business units and use cases. Budgets can warn, downgrade or block. Savings are reported against a baseline frozen at onboarding and reconciled with your provider invoice. |
| **The CISO and DPO** | Routing is constrained by data sensitivity *before* cost is considered. Confidential and personal data stays on EU or Nordic sovereign models, under your policy. Every call records where it was served and under which policy version, as evidence. If no compliant model is available, the call fails instead of being sent somewhere cheaper. |
| **Platform teams** | One policy file, enforced at the gateway with no application changes, or in the SDK. Failover across providers covers capacity shortages, and a semantic cache serves repeat questions. |
| **Developers** | Use `model="auto"` and stop hand-picking models and writing retry code. |

### It gets cheaper on its own

Route learns from your own traffic. Each call produces outcome signals: failures,
retries, your users' feedback, and occasional side-by-side quality checks. A per-use-case
learner uses them to find the cheapest model that actually solves *your* requests. It
learns inside your environment; prompts are never shipped off for training. It can only
choose between models your policy already allows. It recommends changes before it acts on
them, and it rolls itself back if quality drops.

### See the savings before you install anything

The **AI bill teardown** runs on your own machine against a week of your logs. It
replays them through the routing policy, re-tests a sample against cheaper compliant
models, and reports what you would have saved at measured quality parity. It also shows
how much of your traffic must stay sovereign. Your logs never leave your environment.

### How it compares

| | Price/latency routers | Enterprise AI gateways | AI FinOps tools | **Prompt Shields Route** |
|---|---|---|---|---|
| Lowers the cost of each request | Yes | Yes | No (they report spend) | **Yes** |
| Routes by data sensitivity, fails closed | No | Partly (guardrails, no sovereignty policy) | No | **Yes** |
| Keeps prompts in your environment, uses your own keys | Usually a hosted proxy | Varies | n/a | **Yes** |
| Proves savings against an audited baseline | No | No | Partly | **Yes** |
| Learns from your outcomes, within policy | Some (global models) | No | No | **Yes, per tenant** |
| Ties every call to an owner, use case and data class in an AI asset registry | No | No | Partly | **Yes** |

The evidence that routing works is well established. The peer-reviewed RouteLLM study
reported cost reductions of up to 85% while keeping 95% of GPT-4's quality on MT-Bench
([paper](https://arxiv.org/abs/2406.18665)). Real savings depend on your workload,
which is why the teardown measures yours rather than quoting ours.

### What it will not do

Route will not resell tokens or run GPUs. It is not a hosted public router. It will not
rewrite your prompts until the PII engine passes a measured accuracy target. Its
"same quality" claims are always measured against your own eval sets, never assumed.

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

## Links

- [Documentation](docs/) — Partner API reference, integration guides, and SDK docs
- [Introduction and key concepts](docs/introduction.mdx)
- [Developer guide](SDK_GUIDE.md) — architecture, configuration, debugging, FastAPI patterns
- [Security policy](SECURITY.md) — how to report a vulnerability; report privately to security@promptshields.com, never via a public issue
- [Contributing](CONTRIBUTING.md) — development setup, tests, and pull request expectations
- [Code of conduct](CODE_OF_CONDUCT.md)
- [Changelog](CHANGELOG.md)
- [Gateway fork notice](gateway/FORK_NOTICE.md) — what was changed in the vendored Portkey gateway, and its MIT licence

## Licence

Apache 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

The `gateway/` directory is a fork of the [Portkey AI Gateway](https://github.com/Portkey-AI/gateway), licensed under the MIT Licence. That licence continues to govern the upstream code and is retained at [`gateway/LICENSE`](gateway/LICENSE). MIT is compatible with Apache 2.0, so the combined work may be distributed under these terms provided both notices are preserved.
