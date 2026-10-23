# AI bill teardown — Golden Co

|  |  |
|---|---|
| **Prepared for** | {{customer_name}}, {{recipient_names_roles}} |
| **Prepared by** | {{preparer}}, Prompt Shields |
| **Report id / date** | golden / 2026-10-23 |
| **Window analysed** | 2026-10-20T09:00:00Z – 2026-10-21T11:00:00Z (1.08 days) |
| **Mode** | cost-only |
| **Confidentiality** | {{confidentiality_statement}} |

## 1. Summary

|  | Window | Per month (extrapolated, M§3) |
|---|---|---|
| Baseline spend | USD 0.1484 | USD 4.12 |
| Projected spend, same-model arbitrage | USD 0.1484 (0.0% lower) | USD 4.12 |
| Projected spend, policy at parity | *pending #14* |  |
| Cache-hit potential (upper bound) | USD 0.0046 (3.1% of baseline) | USD 0.1264 |
| Spend that must stay sovereign | *pending #13* |  |

**In one sentence:** Over 1.08 days the logged AI traffic cost USD 0.1484 (about USD 4.12 a month); same-model price arbitrage would save 0.0% of it and exact-match caching up to 3.1%. Savings from routing at quality parity are not measured yet (pending #14).

**Gate 1 reading** (≥ 30% savings at ≥ 95% parity): not measured — pending #14.

**How much to trust these numbers:**

- Baseline reconciliation with the invoice: {{gap_pct}}% → {{reconciled / unreconciled / no invoice}} (M§3.1).
- Priced calls: 76.5% of calls, 96.6% of tokens. Null-cost calls are left out (M§4.5).
- Estimated token counts: 4.6% of baseline spend (M§4.3).
- Same-model arbitrage does not check the cheaper host against a route policy (pending #13).

## 2. Scope and data

| Source | Rows read | Rows used | Dropped (reason) |
|---|---|---|---|
| ps-events (`ps_events.jsonl`) | 19 | 17 | 1 (gateway_cache_event), 1 (invalid_json) |

- Prompt text: not used. Where an input carried messages, they were read on this machine only to compute a request hash and prompt features; no text was kept.
- Sensitivity input: declared data_classification only; PII escalation pending #13; see M§5.3.
- Was the window typical? {{customer answer}}

## 3. Baseline spend

| Provider | Model (served) | Calls | Input tokens | Output tokens | Cached tokens | Spend | Share |
|---|---|---|---|---|---|---|---|
| anthropic | claude-sonnet-4-20250514 | 2 | 40,000 | 2,900 | 36,000 | USD 0.0663 | 44.7% |
| bedrock | anthropic.claude-sonnet-4-20250514-v1:0 | 1 | 9,000 | 1,200 | 0 | USD 0.0450 | 30.3% |
| openai | gpt-4o | 5 | 5,400 | 1,290 | 2,000 | USD 0.0194 | 13.1% |
| anthropic | claude-3-5-haiku-20241022 | 2 | 7,800 | 1,600 | 0 | USD 0.0126 | 8.5% |
| bedrock | anthropic.claude-3-5-haiku-20241022-v1:0 | 1 | 1,500 | 300 | 0 | USD 0.0024 | 1.6% |
| together | mixtral-8x7b | 1 | 2,000 | 400 | 0 | USD 0.0014 | 0.9% |
| mistral | mistral-small-latest | 1 | 6,000 | 800 | 0 | USD 0.0008 | 0.6% |
| openrouter | meta-llama/llama-3.3-70b-instruct | 1 | 3,000 | 600 | 0 | USD 0.0004 | 0.3% |
| custom | acme-llm-7b | 1 | 1,000 | 100 | 0 | — | — |
| openai | auto | 1 | 500 | 100 | 0 | — | — |
| openai | gpt-4o-mini | 1 | 0 | 0 | 0 | — | — |
| **Total** |  | 17 |  |  |  | **USD 0.1484** | 100% |

| Cost source (M§4.3) | Calls | Spend |
|---|---|---|
| provider_reported | 1 | USD 0.0004 |
| pricing_table | 10 | USD 0.1398 |
| pricing_table_estimated_tokens | 1 | USD 0.0068 |
| litellm (LiteLLM's own price) | 1 | USD 0.0014 |
| null (not priced) | 4 | — |

Null-cost calls by reason (M§4.5):

| Reason | Calls | Tokens |
|---|---|---|
| auto_without_served_model | 1 | 600 |
| missing_tokens | 1 | 0 |
| unknown_model | 1 | 1,100 |
| unpriced_service_tier | 1 | 1,200 |

Calls where requested model ≠ served model: 4 (23.5%). Re-priced from tokens at served_model, per M§4.2. Calls with no served model, priced at the requested model: 10.

## 4. Where the money goes

### 4.1 By business unit and use case

| Business unit | Use case | Calls | Spend | Share | Main model |
|---|---|---|---|---|---|
| Legal | contract-review | 3 | USD 0.1113 | 75.0% | claude-sonnet-4-20250514 |
| Customer Care | support-bot | 6 | USD 0.0194 | 13.1% | gpt-4o |
| Operations | ticket-triage | 3 | USD 0.0150 | 10.1% | claude-3-5-haiku-20241022 |
| Product | search-rerank | 4 | USD 0.0018 | 1.2% | mixtral-8x7b |
| (unattributed) |  | 1 | USD 0.0008 | 0.6% | mistral-small-latest |

### 4.2 Top cost drivers

1. Use case `contract-review` on `claude-sonnet-4-20250514`: USD 0.0663 (44.7% of baseline spend), 2 calls, 20,000 input tokens per call on average, 90.0% of input tokens read from the provider's prompt cache.
2. Use case `contract-review` on `anthropic.claude-sonnet-4-20250514-v1:0`: USD 0.0450 (30.3% of baseline spend), 1 call, 9,000 input tokens per call on average, 0.0% of input tokens read from the provider's prompt cache.
3. Use case `support-bot` on `gpt-4o`: USD 0.0194 (13.1% of baseline spend), 4 calls, 1,100 input tokens per call on average, 45.5% of input tokens read from the provider's prompt cache.

## 5. Same-model price arbitrage

No model in the window is sold cheaper by another host in catalog `test-catalog`. Saving: USD 0.00.

## 6. Cache-hit potential

TTL 60 minutes. Calls with a request hash: 8 (47.1% of calls). Exact repeats of the message text: 1 call (5.9% of calls), USD 0.0046 (3.1% of baseline). This is an upper bound (M§6.4). Calls whose sensitivity turns the cache off under the policy are excluded: 3 (USD 0.0731); the cache is off at `confidential` and above, and a call with no declared classification counts as `confidential` (M§5.1).

## 7. Sensitivity mix

*Not measured in this teardown — pending #13.*

## 8. Policy replay — projected spend by strategy

*Not measured in this teardown — pending #13.*

## 9. Quality parity

*Not measured in this teardown — pending #14.*

## 10. Savings at parity

*Not measured in this teardown — pending #14.*

## 11. Recommendations and next steps

1. {{quick win that needs no routing, e.g. turn on provider prompt caching for use case X}}
2. {{routing change, limited to use cases that passed parity}}
3. {{sovereignty finding for the CISO / DPO}}

**Proposed pilot:** {{scope, use cases, gateway or SDK, duration, how savings will be verified against a frozen baseline (PRD §7.10), commercial terms}}.

## 12. Limits of this report

This report is a projection from 1.08 days of past traffic. It does not guarantee future savings, replace an invoice, assess compliance, prove general model quality, or measure latency or reliability. Migration cost is not included. Full list: M§8.

Specific to this teardown:

- Cost-only mode: no prompt text, no policy replay, no quality parity.
- Costs are list prices at the catalog version below, in USD.
- {{run-specific limit}}

## Appendix A — Methodology record

| Field | Value |
|---|---|
| Report id / date | golden / 2026-10-23 |
| Customer (or anonymised label) | Golden Co |
| Consent: signer, date, scope | {{consent}} |
| Mode | cost-only |
| Tool and version | ps-teardown test |
| Input sources and row counts | ps-events ps_events.jsonl: 19 read, 17 used |
| Window (UTC) | 2026-10-20T09:00:00Z – 2026-10-21T11:00:00Z, 1.08 days |
| Window typical? | {{answer}} |
| Catalog version | test-catalog |
| Re-priced from tokens? | yes |
| Currency and FX rate (source, date) | USD, no conversion |
| Baseline definition | Observed historical mix over the window (M§3) |
| Invoice reconciliation | {{gap_pct}}%, {{status}} |
| Calls by cost source | litellm 1, null 4, pricing_table 10, pricing_table_estimated_tokens 1, provider_reported 1 |
| Null-cost calls by reason | auto_without_served_model 1, missing_tokens 1, unknown_model 1, unpriced_service_tier 1 |
| `requested_model ≠ served_model` calls | 4 |
| Route policy file, `policy_version`, SHA-256 | n/a: cost-only mode (pending #13) |
| Sensitivity input | declared data_classification only; PII escalation pending #13 |
| Cache TTL | 3600 seconds |
| Sample size per stratum, seed | pending #14 |
| Scoring method per use case | pending #14 |
| Judge model, provider, jurisdiction, prompt version | pending #14 |
| Re-execution temperature, spend | pending #14 |
| `min_parity`, `min_cell_samples`, `min_cell_calls` | pending #14, pending #14, 1 |
| CI methods | pending #14 |
| Known limits specific to this run | {{limits}} |
| Intermediate data deleted on | {{date}} |

## Appendix B — Data handling statement

The analysis ran on {{where}}, under {{customer}}'s control, with `ps-teardown` reading local files only and making no network calls. Logs did not leave that environment. This report contains aggregates only: no prompt text, response text, request hashes or user identifiers. Intermediate files were deleted on {{date}}.
