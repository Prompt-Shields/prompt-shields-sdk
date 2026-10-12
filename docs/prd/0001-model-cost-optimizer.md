# PRD 0001 — Prompt Shields Route: sensitivity-aware model & cost optimizer

| | |
|---|---|
| **Status** | Draft v2 — for review |
| **Owner** | @Jun (product); engineering owner TBD |
| **Guiding memo** | *Inference Layer Opportunity for Prompt Shields* (Oct 7, 2026) |
| **Milestones** | [P0](https://github.com/Prompt-Shields/prompt-shields-sdk/milestone/1) · [Gate 1](https://github.com/Prompt-Shields/prompt-shields-sdk/milestone/2) · [P1](https://github.com/Prompt-Shields/prompt-shields-sdk/milestone/3) · [P2](https://github.com/Prompt-Shields/prompt-shields-sdk/milestone/4) · [P3](https://github.com/Prompt-Shields/prompt-shields-sdk/milestone/5) — backlog in [0001-milestone-backlog.md](0001-milestone-backlog.md) |
| **Packages** | `packages/sdk`, `gateway/src/middlewares/{router,cache}`, `packages/collector`, `packages/db`, new `packages/teardown` |
| **Last updated** | 2026-10-10 |

## 1. Summary

Prompt Shields already sits in the request path through the SDK and the gateway, and
already knows *what kind of data* each call carries. This PRD turns that position into
a **cost-optimisation layer**. It covers five capabilities:

- **Sensitivity-aware routing.** Regulated or PII-heavy calls go to a sovereign EU/Nordic
  model. Routine calls go to the cheapest model that passes the customer's quality bar.
  Hard calls go to a frontier model.
- **Semantic caching.**
- **Per-team spend governance.**
- **Verified savings reporting.**
- **A self-learning loop.** The router learns from each tenant's own traffic outcomes
  which compliant model solves each kind of request most cheaply. It learns inside
  the customer's environment and stays within the policy's guardrails (§7.13).

It is built as a **module of the existing product, not a pivot**. Security stays the
moat. Savings open a second budget, so the buyer becomes **CISO + CFO** instead of the CISO alone.

The first deliverable is not the router. It is an **AI bill teardown** toolkit. It
replays a customer's anonymised logs offline through the routing policy and an
evaluation harness, and reports savings at measured quality parity. That toolkit backs
the 6-week market test in the memo. Online routing, which is most of the engineering,
is built only once the memo's go criteria are met.

## 2. Guiding principles (from the memo)

These are product constraints. Every design decision below traces to one of them.

| # | Principle | Design consequence |
|---|---|---|
| P1 | **Extend, don't pivot.** We are not an inference provider and not a generic EU router. | No token resale and no hosted multi-tenant inference endpoint. The router is a module of the SDK and gateway we already ship. |
| P2 | **Security is the moat.** We route by *data sensitivity*; other routers route by price and latency. | Data-policy constraints run **before** any cost optimisation, and they fail closed. |
| P3 | **Bring your own keys.** No working-capital risk, and no race over a 5% margin. | The customer's provider keys stay in the customer's process or gateway. The catalog lists providers but never proxies billing. |
| P4 | **Prove savings, don't claim them.** The business model is a share of verified savings against an audited baseline. | Counterfactual cost, price-catalog versioning and per-customer quality evals are first-class features, not reporting add-ons. |
| P5 | **Capacity is not our game.** EU providers run short of capacity. | Multi-provider failover is mandatory, not optional. The catalog treats Berget, Infercom, Mistral EU, Regolo, IONOS, STACKIT, Melious and EUrouter as **supply**. |
| P6 | **Test before building.** | Phase 0 serves the teardown offer. Online routing is gated on the memo's go/kill criteria (§8). |
| P7 | **Conservative on quality.** A cheaper model that fails on a regulated task is a liability. | Defaults favour quality. Downgrades require a per-customer eval pass. A content-filter refusal never triggers a fallback to a laxer model. |
| P8 | **Learn from the traffic, keep the data home.** Routing should get cheaper and better on its own. | The learner runs per tenant inside the gateway or SDK. Only decision and outcome metadata leave. It can re-rank only models the policy already allows (§7.13). |

## 3. Context

### 3.1 Market (summary of the memo; figures from secondary reports, validate before external use)

- **Prices are falling fast.** Equivalent-capability inference prices fall about 50x per
  year at the median. A usage-weighted token index reached about $0.97 per million tokens
  in September 2026. Open-weight models carry about half of production tokens.
- **Spend pain is still rising.** 98% of FinOps teams now manage AI spend, 72% of
  organisations had a surprise AI bill, and about 26% of AI spend is estimated to be
  wasted. Granular attribution is the top unmet need.
- **Routing works.** RouteLLM reported 85% savings at 95% of GPT-4 quality on MT-Bench,
  and industry reports range from 30% to 85%. *The cheapest token is rarely the cheapest
  solved task.*
- **The competitive lane:**
  - Global routers (OpenRouter, OrcaRouter) lead on breadth and price.
  - EU routers (Melious, EUrouter, Cortecs, Eden AI) route on price and latency, not on
    sensitivity, and have no enterprise DLP.
  - Portkey is closest to us (gateway + guardrails), but has no EU sovereignty or Purview story.
  - AI FinOps tools (Vantage, Datadog and similar) report spend but cannot lower the
    cost per request.

**Our wedge:** we are the only player that is (a) already in the request path with
CISO trust, (b) already classifying data sensitivity on every call, and (c) already
attributing each call to a business unit and use case in a registry an auditor can read.

### 3.2 What exists in this repository today

| Capability | Where | State |
|---|---|---|
| Route hints (`RouteHint` → `X-PS-Quality`, `X-PS-Max-Cost`, `X-PS-Route`, `X-PS-Cache`) | `packages/sdk/prompt_shields/types.py` | Shipped. The SDK never routes. |
| Cost-aware router (`HeuristicStrategy`, `RoutePolicy`, budget clamp) | `gateway/src/middlewares/router/` | Shipped behind `PS_ROUTER_ENABLED`. Groups are cheap / balanced / frontier, with **no sensitivity input**. |
| Exact-match response cache with `X-PS-Cache` on/off/refresh | `gateway/src/middlewares/cache/` | Shipped (simple mode only). |
| PII category detection | `packages/sdk/prompt_shields/pii.py` | Regex and keyword based. The README states it is "a signal, not a DLP control". There are no Nordic identifiers (e.g. Norwegian fødselsnummer). |
| Ownership attribution (`business_unit`, `use_case`, `owner`, `data_classification`) | SDK, gateway `X-PS-*` headers, registry | Shipped. This is the basis for spend attribution. |
| `requested_model` / `served_model` | SDK + gateway telemetry | Shipped. |
| Pricing | `pricing.py`, hand-copied into `router/policy.ts` | 14 models, Q1-2026, input/output only. |

**Gaps:**
1. **Cost is priced at the *requested* model, not the served one** (`client.py`,
   `_build_event`). A `model="auto"` call records `cost=None`.
2. **No cached-token, reasoning-token, long-context-tier or batch pricing.** Streaming
   calls record no usage. Both make cost figures unfit for a savings-share contract.
3. **No jurisdiction or hosting metadata** on models or providers, and no EU-sovereign providers.
4. **The router has no sensitivity input,** and each SDK client is locked to one vendor.
5. **No offline replay or eval tooling,** so savings cannot be shown before deployment.
6. **No budgets and no anomaly alerts.**

## 4. Goals and non-goals

### Goals

- **G1 Teardown.** A customer (or we, as a concierge service) can replay a week of logs
  and get a savings report at measured quality parity within one working day.
- **G2 Sensitivity-aware routing.** No call with declared or detected sensitivity
  above the policy threshold is ever served outside its allowed jurisdiction. This
  holds in the gateway and in SDK local mode.
- **G3 Cheapest capable model.** Routine traffic is routed to the cheapest model that
  passes the customer's quality bar, with multi-provider failover.
- **G4 Auditable savings.** Every routed call carries a counterfactual cost against a
  baseline frozen at onboarding. The savings report reconciles to provider invoices within ±2%.
- **G5 Spend governance.** Per-team budgets, attribution by business unit and use case,
  anomaly alerts, and an export for the Purview/Defender story.
- **G6 Drop-in.** Existing `ShieldsOpenAI`, `ShieldsAnthropic` and gateway users see no
  behaviour change unless they opt in.

### Non-goals

- Operating GPUs, reselling tokens, prepaid credits or a hosted public router endpoint (P1, P3).
- Competing on model breadth with OpenRouter. The catalog covers models our customers use,
  plus sovereign supply.
- **Prompt rewriting or anonymisation in v1.** "Anonymise, then route" is the memo's
  long-term unlock. The current PII engine is not reliable enough to *transform* prompts
  safely. It is scoped as a gated Phase 3 item (§7.9).
- A TypeScript application SDK, and routing for embeddings, images or audio.

## 5. Users

| Persona | Job to be done | What they buy |
|---|---|---|
| **CISO / DPO** | "Confidential and personal data never leaves the EEA. I can prove it per call." | The sensitivity policy, evidence of where each call was served |
| **CFO / FinOps** | "Cut the AI bill, attribute it to teams, and stop surprise bills." | Teardown report, budgets, anomaly alerts, savings report |
| **Platform engineer** | "One policy, enforced at the gateway, with no per-team code changes." | Gateway router + cache + policy file |
| **App developer** | "Cheapest good-enough model, with failover, and no retry code." | SDK `ShieldsRouter` / `model="auto"` |

## 6. Architecture

### 6.1 One policy, three enforcement points

```
                 route-policy.v1  (YAML/JSON, one schema, versioned)
                 model catalog    (prices + capabilities + jurisdiction)
                        |
      +-----------------+------------------+
      v                 v                  v
 Offline replay     Gateway router      SDK local mode
 (teardown CLI)     (org-wide,          (teams without the gateway;
  Phase 0           zero code change)    ShieldsRouter)
                     Phase 1             Phase 1
```

The policy schema, the catalog and a set of **golden decision vectors** are the
contract. The Python implementation (SDK and teardown) and the TypeScript implementation
(gateway) must both pass the same vectors in CI. Code is never copied across the
MIT/Apache boundary (see CONTRIBUTING). Only data files are shared.

### 6.2 Request flow

```
Intercept -> Classify -> Constrain -> Route -> Cache -> Call (+failover) -> Record -> Learn
             sensitivity  jurisdiction  learner      tenant-   provider        cost,       outcome
             + complexity allow-list    re-ranks     scoped    failover        jurisdiction signals ->
             + features                 safe set                               learner_ver  bandit update
```

1. **Intercept.** Through the SDK, the gateway, or (offline) a log file.
2. **Classify.**
   - *Sensitivity* = max(declared `data_classification`, PII-escalated level). It is
     **escalate-only**: detection can raise sensitivity but never lower it. An unknown
     or failed classification counts as the policy's `unclassified_default`, which is
     `confidential` out of the box.
   - *Complexity* uses the existing heuristic (tokens, code, schema), with a learned
     classifier later. Target under 10 ms.
3. **Constrain.** The sensitivity level maps to allowed jurisdictions, providers, ZDR
   and residency. Candidates outside the allow-list are removed. **If none remain, the
   call fails closed** with `PolicyViolation`. It is never downgraded to a non-compliant model.
4. **Route.** Among the remaining candidates that meet the use case's quality bar, pick
   by strategy (cheapest, latency, or balanced) within `max_cost` and the budget.
5. **Cache.** The cache is tenant-scoped. It is off by default for sensitivity ≥
   `confidential`. Flagged responses are never cached.
6. **Call + failover.** Fall back within the same constraint set only (§7.5).
7. **Record.** Telemetry carries the served model, provider and jurisdiction, the cost
   breakdown, the counterfactual cost, the policy version and the learner version.
8. **Learn.** Outcome signals update that tenant's learner in place (§7.13).

## 7. Components

### 7.1 Model & provider catalog (`catalog.json`, shared data file)

- **Prices.** The schema is field-compatible with LiteLLM's
  `model_prices_and_context_window.json`. It covers input, output, cache read, cache
  write (5m/1h), reasoning, `above_200k` tiers and batch/flex.
- **Capabilities:** `tools`, `json_schema`, `vision`, `reasoning`, `max_input_tokens`.
- **New sovereignty fields:**
  - `provider_hq_country`, `hosting_countries`, `jurisdiction` (`EEA` / `NO` / `US` / …)
  - `us_cloud_act_exposure` (bool)
  - `zdr`, `trains_on_data`
  - `certifications` (ISO 27001, C5, SecNumCloud…)
- **Initial sovereign supply** (OpenAI-compatible endpoints, so one adapter covers them):
  Berget AI (SE), Mistral regional EU endpoints, Infercom, Regolo (IT), IONOS, STACKIT,
  Scaleway, OVHcloud. Melious and EUrouter are listed as aggregators. There is a
  Norway-hosted slot for when a partner exists (memo open question).
- **Built by** `scripts/build_catalog.py`. It emits the SDK file and the gateway policy
  defaults, and a CI drift check fails the build if they differ. Every event records
  `price_catalog_version`.
- An unknown model has cost `None` ("unmetered"), never 0.

### 7.2 Sensitivity classification & data policy

```yaml
sensitivity:
  unclassified_default: confidential
  pii_escalation:                    # detected category -> minimum level
    health_data: restricted
    national_id: restricted          # incl. NO fødselsnummer, SE personnummer, DK CPR
    iban: confidential
    email: internal
data_policy:
  public:       { jurisdictions: [any] }
  internal:     { jurisdictions: [EEA, US], require_zdr: false }
  confidential: { jurisdictions: [EEA], require_zdr: true, us_cloud_act_exposure: false }
  restricted:   { jurisdictions: [NO, EEA], providers: [berget, mistral-eu], require_zdr: true }
```

- Add Nordic identifier detectors to `pii.py`: Norwegian fødselsnummer with mod-11
  checksum, Swedish personnummer with Luhn, Danish CPR, and Finnish HETU. Checksums cut
  the false positives that the README already admits to.
- Documentation must state plainly that detection only *escalates*. A missed detection
  falls back to the declared classification and `unclassified_default`, never to
  "public". This is how the router stays safe with a regex engine.

### 7.3 Routing strategies & quality bar

- A Python port of the gateway's `RouterStrategy` interface. Strategies: `Heuristic`
  (existing), `Cheapest`, `Latency`, and `Learned` (the self-learning loop, §7.13).
- **Quality bar per use case:**
  - The policy can pin `min_group` (e.g. `frontier` for `legal-review`).
  - Optionally, an `eval_set` id. A model is eligible for that use case only after it
    passes the set at ≥ the configured parity (default 95%).
- Precedence is unchanged from the gateway: explicit group > hints > policy default.
  Data-policy constraints sit above all of them.

### 7.4 Eval harness (`prompt_shields.evals`)

- **Inputs:** a golden set (customer-provided prompt/expected pairs, or prompts with a
  rubric) and a list of candidate models.
- **Scoring:** exact match, schema validity, or LLM-as-judge (pairwise against the
  baseline model's answer). The judge model is itself policy-constrained, so restricted
  data is never sent to a non-compliant judge.
- **Output:** a parity score per (use case, model) with a confidence interval. The result
  is written back into the policy as eligibility and is versioned. This is what turns
  "same performance" from a claim into evidence (P4).

### 7.5 Failover engine

| Condition | Action |
|---|---|
| 429 | Respect `Retry-After` up to `max_wait`, then move to the next candidate **within the same constraint set** |
| 5xx, timeout, connection error | Next candidate. A circuit breaker per provider handles EU capacity shortages (P5) |
| Context length exceeded | Next candidate with a larger window that is still compliant |
| 400 / 401 / 403 | Raise. No fallback. |
| Provider content-filter refusal | Raise by default. Falling back to a laxer model is a policy-evasion path (P7). |
| Stream already emitting tokens | Never retry |
| No compliant candidate left | `PolicyViolation`. Fail closed. |

### 7.6 AI bill teardown toolkit (`packages/teardown`, Phase 0)

This is the concierge MVP from the memo, and later a self-serve product.

- **Inputs, any of:**
  - provider usage exports (OpenAI and Anthropic usage CSV/API)
  - gateway logs
  - SDK/registry events
  - a JSONL of `{messages, model, usage, metadata}`
- **Prompt-text handling:** prompt text is optional. Without it, the toolkit produces a
  *cost-only* teardown: attribution, cache-hit potential from request hashes, and price
  arbitrage on the same model.
- **Runs on the customer's machine** (`ps-teardown run logs.jsonl --policy policy.yaml`),
  so logs never leave their environment. This supports the CISO sale and the memo's
  "anonymised logs" constraint.
- **Pipeline:**
  1. Normalise.
  2. Classify sensitivity and complexity.
  3. Apply the policy and produce a routing decision per call.
  4. Optionally re-execute a stratified sample on the candidate models with the
     customer's keys.
  5. Score with the eval harness.
  6. Price with the catalog.
- **Output:** a report in HTML and JSON with these sections:
  - baseline spend
  - projected spend by strategy
  - savings at parity ≥ 95%
  - sensitivity mix (share of traffic that *must* stay sovereign)
  - cache-hit potential
  - top cost drivers by business unit and use case
  - quality-parity table
  - methodology appendix: catalog version, sample size, judge model
- The memo's go criterion ("≥ 30% savings at ≥ 95% parity") is read straight off this report.

### 7.7 Semantic cache (Phase 2)

- Extend the gateway cache (`middlewares/cache`, simple mode today) with a `semantic`
  mode: embeddings plus a similarity threshold.
- **The key always includes** tenant, use case, served model and policy version.
- Off by default for sensitivity ≥ `confidential`. Never caches content-filter
  responses. The embedding model is policy-constrained too.
- Cache hits are reported as savings with `cost_source="cache"`.

### 7.8 Spend governance (Phase 2; folds in the memo's idea B)

- **Budgets** per business unit, use case or team, by day, week or month. Modes:
  `warn` / `downgrade` / `block`.
  - The gateway enforces them org-wide with a shared store.
  - The SDK enforces them per process, or through a Redis `BudgetStore`.
- **Attribution.** The registry already holds `business_unit` / `use_case` / `owner`.
  Add cost roll-ups and a FOCUS-compatible export so FinOps tools can ingest it. Follow
  the Linux Foundation Tokenomics work for alignment.
- **Anomaly alerts** on spend-rate deviations per use case (e.g. more than 3σ above a
  7-day EWMA), via webhooks.
- **Audit export.** Per-call routing evidence (jurisdiction, policy version) in a
  shape that can feed the Purview/Defender integration.

### 7.9 Anonymise-then-route (Phase 3, gated)

This is the memo's biggest unlock: once a prompt is scrubbed, a cheaper external model
becomes acceptable. The design is reversible pseudonymisation. Entities are replaced
with tokens before the call, and the original values are restored in the response
locally. **The gate:** it ships only after the detection engine reaches a measured
recall target on a labelled Nordic and EU PII set. Until then, sensitive traffic is
*routed* to a sovereign model and is *not rewritten*. That matches the README's honest
framing of the current PII engine.

### 7.10 Verified savings & baseline

- **Baseline.** At onboarding, freeze the customer's model mix plus the catalog version
  as `baseline_id`. The counterfactual cost of each call equals the call's actual token
  usage priced at the baseline model for its use case.
- Savings = Σ(counterfactual − actual), with cache hits and routed calls reported separately.
- The reconciliation report compares our figures with the provider invoice. A gap of
  more than 2% marks the period as `unreconciled`. This is what makes a share-of-savings
  contract defensible.

### 7.11 SDK developer experience

```python
from prompt_shields import ShieldsRouter, RouteHint

client = ShieldsRouter(
    providers={                                   # BYOK; keys never leave the process
        "openai":     {"api_key": ...},
        "anthropic":  {"api_key": ...},
        "berget":     {"api_key": ..., "base_url": "https://api.berget.ai/v1"},
        "mistral-eu": {"api_key": ...},
    },
    ps_api_key="ps-...",
    business_unit="Claims", use_case="claim-summary",
    data_classification="confidential",           # -> EEA-only, ZDR
    policy="ps-route.yaml",
)

resp = client.chat.completions.create(model="auto", messages=[...],
                                      route=RouteHint(quality="balanced"))
resp.ps.route        # model, provider, jurisdiction, reason, policy_version
resp.ps.cost         # breakdown, counterfactual, source
resp.ps.attempts     # failover chain
```

- Responses always come back in the OpenAI chat-completions shape, with `raw` kept for
  native access. `ShieldsRouter(mode="gateway")` delegates the decision to the gateway
  through the existing `X-PS-*` headers.

### 7.12 Telemetry additions (metadata only, per CONTRIBUTING)

| Field | Purpose |
|---|---|
| `sensitivity_level`, `sensitivity_source` (`declared` / `pii_escalated` / `default`) | CISO evidence |
| `served_provider`, `served_jurisdiction`, `policy_version` | Proof of where each call was served |
| `route_group`, `route_reason`, `route_mode` (`gateway` / `local` / `none`) | Routing evidence |
| `fallback_chain` (error class only) | Reliability |
| `cost_breakdown`, `cost_source` (`reported` / `estimated` / `cache`), `price_catalog_version` | Accurate cost |
| `counterfactual_cost`, `baseline_id` | Verified savings |
| `budget_state` | Governance |
| `call_id`, `learner_version`, `explored` (bool), `outcome_signals` (classes only), `feedback_score`, `shadow_eval_score` | Self-learning loop (§7.13) |

These need nullable columns via Alembic `004_route.py`. Usage-summary APIs are extended
with `savings_usd`, `sovereign_share` and `fallback_rate`.

### 7.13 Self-learning routing loop

The router improves continuously from the traffic that passes through it. Static
groups and offline evals give it a safe starting point. After that, each tenant's
router learns which compliant model actually solves each kind of request most
cheaply, using that tenant's own outcomes.

```
 prompt ──> features ──> learner picks among ──> call ──> outcome signals ──> learner update
           (local)      policy-allowed models            implicit / explicit /   (per tenant,
                        (explore ≤ budget)               shadow eval              versioned)
```

**Where learning runs (principles P2 and P8).** Prompts are turned into features inside the
customer's environment, in the gateway or in SDK local mode. Features are a local
embedding plus the complexity features from §6.2 (tokens, code, schema, language,
use case). The learner state lives there too. The collector and registry receive only
the decision and outcome metadata in §7.12, never prompt text or embeddings.
Cross-tenant learning is **off**; see open question 7.

**Outcome signals (collected from phase P1, used from phase P2):**

| Signal | Source | Strength |
|---|---|---|
| Implicit failure | Fallback triggered, invalid JSON/schema, tool-call parse error, refusal, truncation at `max_tokens` | Strong negative |
| Implicit dissatisfaction | Same `session_id` re-asks a near-duplicate prompt within N seconds (regenerate), or the caller retries | Weak negative |
| Explicit feedback | `client.feedback(call_id, score, reason=None)` in the SDK; `POST /v1/feedback` on the gateway | Strong, sparse |
| Shadow eval | A sampled share of calls (default 1–2%, capped by a budget) is also run on one alternative **compliant** model. A policy-constrained judge compares the two answers. The user only ever sees the primary answer. | Strong, costs money |
| Cost and latency | Reported usage (§7.10), measured latency | Exact |

**Learner.**
- A **contextual bandit** per (tenant, use case), with the prompt features as context
  and the eligible models as arms.
- Reward = quality score − λ·cost − μ·latency. λ and μ are set per use case from the
  `quality` intent: `critical` weights quality almost entirely.
- Thompson sampling is the default. It is interpretable and cheap: under 1 ms on the
  hot path.
- The RouteLLM-style matrix-factorisation router is retrained periodically from the
  accumulated (prompt features, model, score) tuples. It serves as a prior for cold-start
  use cases.

**Guardrails.** The learner can only *re-rank* inside the safe set. It never widens that set.
- The arms are only models that pass the data-policy constraint (§6.2 step 3) **and**
  the use case's quality bar (§7.3). The policy, not the learner, decides eligibility.
- **Exploration budget:** at most `explore_rate` of traffic (default 5%, and 0 for
  `restricted` or `min_group: frontier` use cases) is routed off the current best arm.
- **Auto-rollback:** if a use case's rolling quality score falls below its parity
  threshold, or its implicit-failure rate rises by more than X points, the learner
  reverts to the last good snapshot and alerts.
- **Versioned and auditable:** every learner snapshot gets a `learner_version`. Every
  decision records the version and whether it was an exploration. Savings reports state
  which versions were live, so the audited baseline (§7.10) stays valid.
- **Poisoning resistance:** explicit feedback is rate-limited per user, and implicit
  signals are weighted below shadow-eval scores. A shift in reward distribution
  freezes learning and raises an alert.

**Rollout modes (per tenant and use case):**
1. `observe`: collect signals only (P1).
2. `recommend`: the learner runs in shadow and proposes policy changes, with projected
   savings and quality from off-policy evaluation over logged traffic. A human accepts
   or rejects each one in the registry (P2).
3. `auto`: the learner routes live inside the guardrails above (P3). It is enabled only
   after `recommend` has been accurate for that use case for a set period.

**Offline/online symmetry.** The teardown toolkit (§7.6) produces the same
(features, model, score) tuples from historical logs. A pilot therefore starts from a
warm learner instead of a cold one, and the teardown's projected savings can be compared
directly against what the live learner later delivers.

## 8. Phases, gates and the 6-week test

| Phase | Weeks | Scope | Exit / gate |
|---|---|---|---|
| **P0 — Teardown MVP** | 0–3 | Fix served-model pricing; catalog v1 with cache, reasoning and sovereignty fields; streaming usage; Nordic ID detectors; policy schema v1 and golden vectors; teardown CLI (cost-only mode first, then replay + eval harness v0) | Two concierge teardowns delivered to pipeline accounts (memo: Storebrand, Tetra Tech, CluePoints, Svanemerket, SELENCIA) |
| **Gate 1 (memo)** | 4–6 | — | **Go:** ≥3 of 10 prospects spend more than €5k/month; ≥2 teardowns show ≥30% savings at ≥95% parity; ≥1 paid pilot or LOI. **Kill:** buyers see cost as Microsoft's problem, or teardown savings are <15% → drop routing, keep P0 cost accuracy, move straight to P2 governance (idea B). |
| **P1 — Sensitivity-aware routing** | 6–12 | Gateway router gets a sensitivity input and the constraint stage; OpenAI-compatible sovereign provider adapter; failover engine + circuit breakers; SDK `ShieldsRouter` local mode; counterfactual cost + baseline; telemetry + migration; **learning signals in `observe` mode**: `call_id` + feedback API, implicit outcome capture, local prompt features, shadow-eval sampler | A pilot customer runs in production behind the gateway; zero jurisdiction violations in property tests and in the pilot; invoice reconciliation within ±2% |
| **P2 — Governance & cache** | 10–16 | Budgets (gateway shared store, SDK Redis store); anomaly alerts; FOCUS export; semantic cache mode; savings dashboards in the registry API; **contextual-bandit learner in `recommend` mode** with off-policy evaluation, versioned snapshots and teardown warm-start | The pilot's FinOps owner uses budgets and alerts; cache savings reported; ≥1 learner recommendation accepted, with realised savings within ±5 points of projection |
| **P3 — Autonomous learning & anonymise-then-route** | 16+ | Learner `auto` mode with exploration budget and auto-rollback; periodic RouteLLM-style retrain as cold-start prior; reversible pseudonymisation behind a recall gate | In `auto` mode the learner beats the static policy on cost at equal parity for 4 consecutive weeks with zero rollbacks caused by quality; the recall target is met before anonymisation is enabled for any tenant |

## 9. Success metrics

- **Teardown:** turnaround of 1 working day or less; ≥30% savings at ≥95% parity on ≥2 of
  the first 3 teardowns (the memo's go criterion).
- **Safety:** zero calls served outside the allowed jurisdiction. This is measured
  continuously from `served_jurisdiction` against `sensitivity_level`.
- **Accuracy:** cost within ±2% of the invoice for the reconciled period.
- **Reliability:** ≥99% success with one provider forced down (failover test).
- **Overhead:** under 10 ms p50 for classify + route at the gateway, with no network I/O on the hot path.
- **Learning:** month-over-month cost per solved task falls for each pilot use case at
  constant parity. Fewer than 1 auto-rollback per use case per month. Exploration stays
  within budget.
- **Commercial:** paid pilots or LOIs; share of revenue from savings-share contracts.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Commoditisation (OpenRouter, hyperscalers, Microsoft Foundry routing) | Compete on sensitivity + evidence + attribution, not on breadth or price (P2) |
| EU provider capacity shortages | Mandatory multi-provider failover within the constraint set; circuit breakers; at least two sovereign providers per restricted policy |
| Quality liability on regulated tasks | Per-use-case eval eligibility; conservative `min_group`; no fallback after a content-filter refusal |
| A regex PII engine misses sensitive data | Escalate-only design; `unclassified_default: confidential`; checksummed Nordic IDs; no prompt rewriting until the recall gate is met |
| Savings claims disputed | Frozen baseline, versioned catalog, invoice reconciliation, methodology appendix |
| Focus dilution versus the ASPM roadmap | Phase 0 is mostly tooling (catalog, cost fix, teardown), which is useful even on the kill path; online routing is built only after Gate 1 |
| Breaking the "the SDK never picks a model" contract | Local routing is opt-in through `ShieldsRouter`; existing clients unchanged; docs updated in the P1 PR |
| The learner drifts or is gamed (feedback poisoning, reward hacking toward cheap models) | Only re-ranks the policy-safe set; exploration budget; auto-rollback; shadow evals outweigh implicit signals; versioned snapshots; `recommend` before `auto` |
| Learning data becomes a privacy liability | Features and learner state stay in the customer's environment; only metadata reaches the collector; no cross-tenant learning by default |
| Secondary-source market data | Validate before investor or external use (memo risk list) |

## 11. Open questions

1. *(Memo)* Is this a new SKU, or the reason to buy Prompt Shields at all? This decides
   whether the teardown is free, which would make it a lead magnet.
2. *(Memo)* Which two pipeline accounts will share logs within 3 weeks? Will they accept
   running the CLI locally?
3. *(Memo)* Partner with Melious and EUrouter as supply, or only integrate the
   underlying providers directly?
4. *(Memo)* Is there a Norway-hosted provider or Nscale capacity for a `NO` jurisdiction
   in the restricted tier?
5. Does a US-headquartered provider's EU region count as `EEA` for `confidential`?
   Proposal: allowed only when `us_cloud_act_exposure` is accepted by the customer policy.
6. Savings-share baseline: the customer's actual historical mix, or a list-price
   "naive" baseline? Proposal: the historical mix, frozen at onboarding.
7. Cross-tenant learning: should anonymised outcome statistics (model × task-type win
   rates, no prompt features) be pooled across opted-in tenants, to warm-start new
   customers? Proposal: no in P1–P2, revisit with legal in P3.
8. Shadow-eval spend: is it paid by the customer (their keys), or credited against the
   savings share? Proposal: the customer's keys, capped at 2% of routed spend and shown
   in the savings report.

## Appendix A — Research notes

- **OpenRouter:**
  - `models[]` ordered fallback
  - `provider { order, only, ignore, sort, max_price, zdr, data_collection, quantizations }`
  - exact `usage.cost` on every response
  - `:floor` / `:nitro` variants
  - Auto Router with a candidate allow-list
  - sticky provider routing to keep caches warm

  We reuse this vocabulary in `ProviderPrefs`.
- **LiteLLM:** price-map schema (adopted); separate fallback classes (context window,
  content policy); budgets per key, user and team. The 2026 breach supports the memo's
  "trusted, hardened gateway" pitch.
- **Portkey** (our gateway's upstream): `strategy.mode` fallback / loadbalance /
  conditional; simple and semantic cache.
- **RouteLLM:** strong-versus-weak learned router with a calibrated threshold. This is
  the P3 starting point.
- **Melious** (memo): 60+ open-weight models across 11 EU providers, routing variants
  by name suffix, no visible enterprise governance layer. It is *supply*, not a template.

Sources: see the guiding memo's source list, plus
https://openrouter.ai/docs/features/provider-routing ·
https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json ·
https://portkey.ai/docs/api-reference/config-object ·
https://github.com/lm-sys/RouteLLM ·
https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans
