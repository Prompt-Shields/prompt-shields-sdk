# Backlog — Prompt Shields Route (PRD 0001 v2)

These are ready-to-file issues for [PRD 0001](0001-model-cost-optimizer.md). Each group
below matches a GitHub milestone:

| Milestone | Due | Link |
|---|---|---|
| P0 — Teardown MVP | 2026-10-31 | [milestone/1](https://github.com/Prompt-Shields/prompt-shields-sdk/milestone/1) |
| Gate 1 — Market test decision | 2026-11-21 | [milestone/2](https://github.com/Prompt-Shields/prompt-shields-sdk/milestone/2) |
| P1 — Sensitivity-aware routing | 2027-01-02 | [milestone/3](https://github.com/Prompt-Shields/prompt-shields-sdk/milestone/3) |
| P2 — Spend governance & semantic cache | 2027-01-30 | [milestone/4](https://github.com/Prompt-Shields/prompt-shields-sdk/milestone/4) |
| P3 — Autonomous learning & anonymise-then-route | — | [milestone/5](https://github.com/Prompt-Shields/prompt-shields-sdk/milestone/5) |

Sizes: S ≤ 2 days, M ≤ 1 week, L ≤ 2 weeks.

## P0 — Teardown MVP (weeks 0–3)

| # | Title | Size | Acceptance criteria |
|---|---|---|---|
| 1 | Price events at `served_model`, not `requested_model` | S | Sync and async `_build_event` price at the served model. `model="auto"` no longer yields `cost=None`. Regression test. |
| 2 | Catalog v1: LiteLLM-compatible prices + sovereignty fields | M | `catalog.json` + loader covering cache read/write, reasoning, `above_200k`, batch and capabilities. Adds `jurisdiction`, `hosting_countries`, `provider_hq_country`, `us_cloud_act_exposure`, `zdr` and `certifications`. Seeded with current OpenAI/Anthropic/Google models and the EU sovereign supply (Berget, Mistral EU, Infercom, Regolo, IONOS, STACKIT, Scaleway, OVHcloud). `estimate_cost()` stays backward compatible. |
| 3 | Catalog generator + gateway drift check | M | `scripts/build_catalog.py` emits the SDK JSON and the gateway policy defaults. CI fails on drift. `FORK_NOTICE.md` is updated if an upstream file is touched. |
| 4 | Cached, reasoning and streaming usage accounting | M | Parses OpenAI `cached_tokens`/`reasoning_tokens`, Anthropic `cache_*` and Gemini `cachedContentTokenCount`. Adds `include_usage` on OpenAI streams and accumulates Anthropic `message_delta`. Sets `cost_source`. Fixture deltas are ≤2%. |
| 5 | Nordic national-ID detectors | S | Norwegian fødselsnummer (mod-11), Swedish personnummer (Luhn), Danish CPR and Finnish HETU, each checksum-validated. A new `national_id` category. Tests include negatives. |
| 6 | `route-policy.v1` JSON Schema + golden decision vectors | M | Covers sensitivity levels, PII escalation, data policy, groups, quality bar and fallback. Shared vectors (`schemas/route-policy/vectors/*.json`) define the expected decisions both implementations must pass. |
| 7 | `ps-teardown` CLI — cost-only mode | M | Ingests OpenAI/Anthropic usage exports, SDK/registry events, gateway logs and generic JSONL. Reports baseline spend, attribution by BU/use case, same-model price arbitrage and cache-hit potential from request hashes. Outputs HTML + JSON. Runs fully locally. |
| 8 | `ps-teardown` — policy replay + sensitivity mix | M | Applies the policy to each logged call (Python reference router). Reports the share of traffic that must stay sovereign and the projected spend per strategy. |
| 9 | Eval harness v0 + sample re-execution | L | Builds a stratified sample, re-runs it on candidate models with customer keys, and scores by exact match, schema validity or LLM-as-judge (the judge is itself policy-constrained). Parity per (use case, model) with a CI. Feeds savings-at-parity into the report. |
| 10 | Teardown methodology appendix + report template | S | Records catalog version, sample size, judge model, baseline definition and known limits. Produces a pitch-ready report for the memo's concierge offer. |
| 32 | Teardown exports learning tuples (warm start) | S | Replay emits `(prompt features, model, quality score, cost)` tuples in the §7.13 schema, so a pilot's learner starts warm. Features are computed locally, and the export contains no prompt text. |

## Gate 1 — Market test decision (weeks 4–6)

| # | Title | Size | Acceptance criteria |
|---|---|---|---|
| 11 | Run concierge teardowns for 2+ pipeline accounts | — | Reports delivered. Savings % at parity recorded per account. |
| 12 | Go/kill decision record | S | A decision document against the memo criteria (≥3/10 spend >€5k/mo; ≥2 teardowns ≥30% at ≥95% parity; ≥1 pilot or LOI). On kill: re-plan P1 out of scope and pull P2 forward. |

## P1 — Sensitivity-aware routing (weeks 6–12, only after GO)

| # | Title | Size | Acceptance criteria |
|---|---|---|---|
| 13 | Gateway: sensitivity classification stage | M | Sensitivity = max(declared `X-PS-Data-Classification`, PII-escalated level), escalate-only. `unclassified_default` is applied. Emits `sensitivity_level` and `sensitivity_source`. |
| 14 | Gateway: fail-closed jurisdiction constraint stage | M | Filters candidates by the data policy before any strategy runs. No compliant candidate → `PolicyViolation` (HTTP 451/403 shape TBD). Passes the golden vectors. Property tests show zero violations. |
| 15 | OpenAI-compatible sovereign provider adapter | M | One adapter, configured per provider (base URL, auth, region), for gateway and SDK. Records `served_provider` and `served_jurisdiction`. |
| 16 | Failover engine + per-provider circuit breaker | L | Implements the PRD §7.5 matrix. Fallback stays within the constraint set. No retry after a stream has started. No fallback on a content-filter refusal. Records `fallback_chain`. |
| 17 | SDK `ShieldsRouter` (sync + async), local mode | L | BYOK multi-provider. OpenAI-shape responses with `raw`. `resp.ps` exposes route, cost and attempts. Python router passes the golden vectors. `mode="gateway"` delegates. Existing clients unchanged. |
| 18 | Per-use-case quality bar | M | `min_group` and eval-set eligibility in the policy. The router excludes models that have not passed parity. |
| 19 | Frozen baseline + counterfactual cost | M | `baseline_id` frozen at onboarding. `counterfactual_cost` on every routed event. Invoice reconciliation report flags a gap above ±2%. |
| 20 | Collector/DB: route + cost fields (Alembic 004) | M | Nullable columns for the PRD §7.12 fields. Ingest validation. The PR includes the CONTRIBUTING privacy statement. |
| 21 | Registry API: savings + sovereign share | M | `usage-summary` returns `savings_usd`, `sovereign_share` and `fallback_rate`. OpenAPI + Mintlify docs updated. |
| 22 | Docs: routing guide, README limits, demo | M | SDK Guide §6 rewrite, gateway PS_README, and a README "What this does not do" update. `demo/demo_route.py` shows sensitivity routing, failover and savings. |
| 33 | `call_id` + feedback API | M | Every routed call gets a `call_id`. SDK `client.feedback(call_id, score, reason=None)` and gateway `POST /v1/feedback` are rate-limited per user. Feedback is stored as metadata only. |
| 34 | Implicit outcome signal capture | M | Records fallback, schema/tool-call parse failure, refusal, `max_tokens` truncation, and same-session regenerate/retry detection as `outcome_signals` classes on the event. |
| 35 | Local prompt feature extraction | M | A local embedding plus complexity features (tokens, code, schema, language, use case), computed in the gateway or SDK process and kept there. Adds under 10 ms p50. A test proves nothing beyond the §7.12 metadata reaches the collector. |
| 36 | Shadow-eval sampler | M | Samples a configurable share of calls (default 1–2%, capped at 2% of routed spend) and runs each on one alternative **compliant** model. A policy-constrained judge scores the pair. The user only sees the primary answer. Shadow cost is reported separately. |

## P2 — Spend governance & semantic cache (weeks 10–16)

| # | Title | Size | Acceptance criteria |
|---|---|---|---|
| 23 | Budgets: gateway shared store | M | Per BU, use case or team, by day, week or month. Modes `warn`, `downgrade` (within constraints) and `block`. Emits `budget_state`. |
| 24 | Budgets: SDK in-process + Redis `BudgetStore` | M | Same semantics as the gateway. The per-process limitation is documented. Ships as an `[optimizer-redis]` extra. |
| 25 | Spend anomaly alerts | M | Per-use-case EWMA/σ detector. Webhook delivery. Thresholds configurable. |
| 26 | FOCUS-compatible cost export | S | CSV/Parquet export of cost by BU, use case, model and provider, aligned with the FinOps FOCUS spec. |
| 27 | Purview/Defender audit export | M | Per-call routing evidence (jurisdiction, policy version, sensitivity) in an ingestible format. |
| 28 | Gateway semantic cache mode | L | Embeddings + threshold. The key includes tenant, use case, model and policy version. Off for sensitivity ≥ `confidential`. Never caches refusals. Records `cost_source="cache"`. |
| 37 | Contextual-bandit learner — `recommend` mode | L | Per (tenant, use case) Thompson sampling over the policy-safe set, with reward = quality − λ·cost − μ·latency. Off-policy evaluation over logged traffic gives the projected savings and quality of each recommended policy change. Runs in shadow and never routes live traffic. |
| 38 | Learner snapshots + recommendation review | M | Versioned learner state (`learner_version`) in the gateway or SDK store. The registry API lists recommendations with projections, and a human accepts or rejects each one. Realised vs. projected savings are tracked after acceptance. |

## P3 — Autonomous learning & anonymise-then-route (weeks 16+)

| # | Title | Size | Acceptance criteria |
|---|---|---|---|
| 29 | Learner `auto` mode: exploration budget, auto-rollback, periodic retrain | L | The learner routes live, but only inside the policy-safe set. `explore_rate` defaults to 5%, and is 0 for `restricted` and `min_group: frontier` use cases. It auto-rolls back to the last good snapshot when parity or implicit-failure thresholds are breached. Drift or poisoning detection freezes learning. A periodic RouteLLM-style retrain provides the cold-start prior. Enabled per use case only after `recommend` mode has proven accurate. |
| 30 | Labelled Nordic/EU PII benchmark + recall gate | M | A labelled dataset and a recall/precision report for `pii.py`. The gate threshold is agreed with security. |
| 31 | Reversible pseudonymisation (anonymise-then-route) | L | Entity → token before the call, restored locally after. Enabled per tenant only once #30 passes. Off by default. |

## Suggested labels

`area:sdk`, `area:gateway`, `area:collector`, `area:teardown`, `route`, `size:S|M|L`.
