# AI bill teardown — Golden Co

|  |  |
|---|---|
| **Prepared for** | {{customer_name}}, {{recipient_names_roles}} |
| **Prepared by** | {{preparer}}, Prompt Shields |
| **Report id / date** | golden / 2026-10-23 |
| **Window analysed** | 2026-10-20T08:00:00Z – 2026-10-21T11:00:00Z (1.12 days) |
| **Mode** | full |
| **Confidentiality** | {{confidentiality_statement}} |

## 1. Summary

|  | Window | Per month (extrapolated, M§3) |
|---|---|---|
| Baseline spend | USD 0.1634 | USD 4.38 |
| Projected spend, same-model arbitrage | USD 0.1634 (0.0% lower) | USD 4.38 |
| Projected spend, policy at parity | USD 0.1451 (11.2% lower, 95% CI 0.0% – 11.2%) | USD 3.89 |
| Cache-hit potential (upper bound) | USD 0.0047 (2.9% of baseline) | USD 0.1255 |
| Spend that must stay sovereign | 77.8% of spend, 58.3% of calls |  |

**In one sentence:** Over 1.12 days the logged AI traffic cost USD 0.1634 (about USD 4.38 a month); same-model price arbitrage would save 0.0% of it and exact-match caching up to 2.9%. Routing at quality parity under the policy would save 11.2% of the compared spend (conservative: 0.0%). 77.8% of spend must stay sovereign under the policy.

**Gate 1 reading** (≥ 30% savings at ≥ 95% parity): not met.
Based on: point estimate 11.2% (95% CI 0.0% – 11.2%), conservative (Wilson lower bound) 0.0% (M§7.2).

**How much to trust these numbers:**

- Baseline reconciliation with the invoice: {{gap_pct}}% → {{reconciled / unreconciled / no invoice}} (M§3.1).
- Priced calls: 79.2% of calls, 97.0% of tokens. Null-cost calls are left out (M§4.5).
- Estimated token counts: 4.2% of baseline spend (M§4.3).
- judge mistral-eu:mistral-large-eu is from the same family as candidate mistral-eu:mistral-small-eu (M§7.3)
- Same-model arbitrage does not check the cheaper host against a data policy (the price catalog has no jurisdiction data yet, #7 / #8).

## 2. Scope and data

| Source | Rows read | Rows used | Dropped (reason) |
|---|---|---|---|
| ps-events (`ps_events.jsonl`) | 19 | 17 | 1 (gateway_cache_event), 1 (invalid_json) |
| generic-jsonl (`generic.jsonl`) | 9 | 7 | 1 (no_model), 1 (not_an_object) |

- Prompt text: read on this machine only, to compute a request hash and prompt features, to scan for PII (only category names were kept) and to run the policy heuristic. No text is in this report.
- Quality parity: a stratified sample was re-run by a separate `ps-eval` run (Appendix A); this report reads its results file and executed no prompt.
- Sensitivity input: declared data_classification, recorded detected_pii_types and a local PII scan of message text, escalated per the route policy; see M§5.3.
- Was the window typical? {{customer answer}}

## 3. Baseline spend

| Provider | Model (served) | Calls | Input tokens | Output tokens | Cached tokens | Spend | Share |
|---|---|---|---|---|---|---|---|
| anthropic | claude-sonnet-4-20250514 | 2 | 40,000 | 2,900 | 36,000 | USD 0.0663 | 40.6% |
| bedrock | anthropic.claude-sonnet-4-20250514-v1:0 | 1 | 9,000 | 1,200 | 0 | USD 0.0450 | 27.6% |
| openai | gpt-4o | 6 | 5,400 | 1,290 | 2,000 | USD 0.0194 | 11.9% |
| anthropic | claude-3-5-haiku-20241022 | 2 | 7,800 | 1,600 | 0 | USD 0.0126 | 7.7% |
| anthropic | claude-3-5-sonnet-20241022 | 2 | 1,700 | 580 | 1,200 | USD 0.0106 | 6.5% |
| google | gemini-2.5-flash | 1 | 5,000 | 1,000 | 0 | USD 0.0040 | 2.5% |
| bedrock | anthropic.claude-3-5-haiku-20241022-v1:0 | 1 | 1,500 | 300 | 0 | USD 0.0024 | 1.5% |
| together | mixtral-8x7b | 1 | 2,000 | 400 | 0 | USD 0.0014 | 0.9% |
| mistral | mistral-small-latest | 1 | 6,000 | 800 | 0 | USD 0.0008 | 0.5% |
| openrouter | meta-llama/llama-3.3-70b-instruct | 1 | 3,000 | 600 | 0 | USD 0.0004 | 0.3% |
| openai | gpt-4o-mini | 4 | 1,260 | 359 | 0 | USD 0.0004 | 0.2% |
| custom | acme-llm-7b | 1 | 1,000 | 100 | 0 | — | — |
| openai | auto | 1 | 500 | 100 | 0 | — | — |
| **Total** |  | 24 |  |  |  | **USD 0.1634** | 100% |

| Cost source (M§4.3) | Calls | Spend |
|---|---|---|
| provider_reported | 1 | USD 0.0004 |
| pricing_table | 16 | USD 0.1547 |
| pricing_table_estimated_tokens | 1 | USD 0.0068 |
| litellm (LiteLLM's own price) | 1 | USD 0.0014 |
| null (not priced) | 5 | — |

Null-cost calls by reason (M§4.5):

| Reason | Calls | Tokens |
|---|---|---|
| auto_without_served_model | 1 | 600 |
| missing_tokens | 2 | 0 |
| unknown_model | 1 | 1,100 |
| unpriced_service_tier | 1 | 1,200 |

Calls where requested model ≠ served model: 4 (16.7%). Re-priced from tokens at served_model, per M§4.2. Calls with no served model, priced at the requested model: 17.

## 4. Where the money goes

### 4.1 By business unit and use case

| Business unit | Use case | Calls | Spend | Share | Main model |
|---|---|---|---|---|---|
| Legal | contract-review | 3 | USD 0.1113 | 68.1% | claude-sonnet-4-20250514 |
| Customer Care | support-bot | 6 | USD 0.0194 | 11.9% | gpt-4o |
| Operations | ticket-triage | 3 | USD 0.0150 | 9.2% | claude-3-5-haiku-20241022 |
| Engineering | code-assist | 3 | USD 0.0106 | 6.5% | claude-3-5-sonnet-20241022 |
| (none) | summaries | 1 | USD 0.0040 | 2.5% | gemini-2.5-flash |
| Product | search-rerank | 4 | USD 0.0018 | 1.1% | mixtral-8x7b |
| (unattributed) |  | 1 | USD 0.0008 | 0.5% | mistral-small-latest |
| Customer Care | faq | 3 | USD 0.0004 | 0.2% | gpt-4o-mini |

### 4.2 Top cost drivers

1. Use case `contract-review` on `claude-sonnet-4-20250514`: USD 0.0663 (40.6% of baseline spend), 2 calls, 20,000 input tokens per call on average, 90.0% of input tokens read from the provider's prompt cache.
2. Use case `contract-review` on `anthropic.claude-sonnet-4-20250514-v1:0`: USD 0.0450 (27.6% of baseline spend), 1 call, 9,000 input tokens per call on average, 0.0% of input tokens read from the provider's prompt cache.
3. Use case `support-bot` on `gpt-4o`: USD 0.0194 (11.9% of baseline spend), 4 calls, 1,100 input tokens per call on average, 45.5% of input tokens read from the provider's prompt cache.

## 5. Same-model price arbitrage

No model in the window is sold cheaper by another host in catalog `test-catalog`. Saving: USD 0.00.

## 6. Cache-hit potential

TTL 60 minutes. Calls with a request hash: 14 (58.3% of calls). Exact repeats of the message text: 2 calls (8.3% of calls), USD 0.0047 (2.9% of baseline). This is an upper bound (M§6.4). Calls whose sensitivity turns the cache off under the policy are excluded: 5 (USD 0.0771); the cache is off at `confidential` and above, and a call's level is its effective level under the route policy, PII escalation included; with no declared classification it starts at `confidential` (M§5.1).

## 7. Sensitivity mix

Policy: `nordic-sovereign.json`, `policy_version` nordic-sovereign-1. `unclassified_default` = confidential.

| Level | Calls | Share of calls | Spend | Share of spend | declared / pii_escalated / default | Must stay sovereign? |
|---|---|---|---|---|---|---|
| public | 4 | 16.7% | USD 0.0062 | 3.8% | 4 / 0 / 0 | no |
| internal | 6 | 25.0% | USD 0.0300 | 18.3% | 6 / 0 / 0 | no |
| confidential | 13 | 54.2% | USD 0.0933 | 57.1% | 1 / 0 / 12 | yes |
| restricted | 1 | 4.2% | USD 0.0339 | 20.8% | 0 / 1 / 0 | yes |

Must stay sovereign: USD 0.1272 (77.8% of spend), 14 calls (58.3%). PII escalation raised 4.2% of calls; categories seen: email 1, national_id 1, phone 1.

Today's traffic outside the policy: 5 calls (USD 0.0663) ran on a model the data policy does not allow at their level; 12 calls ran on a model the policy does not list.

{{Short reading, e.g. "62% of spend must stay in the EEA; 18 points of that come from the confidential default for calls with no declared classification."}}

## 8. Policy replay — projected spend by strategy

| Strategy (M§6.2) | Projected spend | vs. baseline | Calls with no compliant candidate |
|---|---|---|---|
| Baseline (observed) | USD 0.1634 | — | — |
| Same-model arbitrage | USD 0.1634 | 0.0% lower | — |
| Policy, cheapest (not quality-checked) | USD 0.4494 (vs. USD 0.1634 compared) | 175.1% higher | 0 |
| Policy, at parity | USD 0.1451 (vs. USD 0.1634 compared) | 11.2% lower | 4 |
| Cache (separate, not additive) | USD 0.0047 | 2.9% of baseline | — |

"Policy, cheapest" is not a savings claim. It shows the ceiling before quality is checked. A negative saving means the policy moves traffic to more expensive, compliant models.

Outcomes: 21 calls routed to another model, 3 unchanged, 0 with no compliant candidate (kept at baseline cost). Calls with a null baseline or projected cost are left out of the comparison: 2 projected null; 12 priced at the policy's per-1k rates because the catalog has no price for the model (M§4.6).

| Routed model | Calls | Projected spend |
|---|---|---|
| claude-sonnet-4-20250514 | 2 | USD 0.0307 |
| gpt-4o-mini | 8 | USD 0.0016 |
| mistral-large-eu | 7 | USD 0.4160 |
| scaleway-small | 7 | USD 0.0018 |

## 9. Quality parity

Scoring: *: exact_match, contract-review: llm_judge. Judge: mistral-eu:mistral-large-eu (prompt pairwise-v1), jurisdiction EEA per the route policy.

| Use case | Candidate model | Sample n | Parity | 95% Wilson CI | At parity? |
|---|---|---|---|---|---|
| contract-review | mistral-eu:mistral-small-eu | 40 | 100.0% | 91.2% – 100.0% | yes |
| contract-review | openai:gpt-4o-mini | 0 | — | — | insufficient sample |
| faq | mistral-eu:mistral-small-eu | 80 | 100.0% | 95.4% – 100.0% | yes |
| faq | openai:gpt-4o-mini | 80 | 100.0% | 95.4% – 100.0% | yes |
| support-bot | mistral-eu:mistral-small-eu | 40 | 75.0% | 59.8% – 85.8% | no |
| support-bot | openai:gpt-4o-mini | 40 | 97.5% | 87.1% – 99.6% | yes |

Every tested cell is listed, including the ones that failed (M§7.4). At parity: point estimate ≥ 95.0% and n ≥ 30.

## 10. Savings at parity

| Use case | Baseline spend | At-parity spend | Saving | Model change |
|---|---|---|---|---|
| code-assist | USD 0.0106 | USD 0.0106 | 0.0% | no eval set |
| contract-review | USD 0.1113 | USD 0.1113 | 0.0% | no change |
| faq | USD 0.0004 | USD 0.0004 | 0.0% | no change |
| search-rerank | USD 0.0018 | USD 0.0018 | 0.0% | no eval set |
| summaries | USD 0.0040 | USD 0.0040 | 0.0% | no eval set |
| support-bot | USD 0.0194 | USD 0.0012 | 94.0% | gpt-4o-2024-08-06 → gpt-4o-mini |
| ticket-triage | USD 0.0150 | USD 0.0150 | 0.0% | no eval set |
| (unattributed) | USD 0.0008 | USD 0.0008 | 0.0% | no eval set |
| **Total (spend-weighted)** | **USD 0.1634** | **USD 0.1451** | **11.2% (95% CI 0.0% – 11.2%)** |  |
| Conservative (Wilson lower bound) |  | USD 0.1634 | 0.0% |  |

Use cases with no eval set count as zero savings here (M§7.4). The at-parity strategy replays the route policy with each use case limited to the models that reached parity for it, plus the model it runs on today (M§6.2). The interval resamples the parity sample; the logged traffic is the whole population.

Cross-check: `ps-eval`'s own estimate (per-use-case model swap over its 160 records) is 70.5% (95% CI 47.0% – 70.5%).

## 11. Recommendations and next steps

1. {{quick win that needs no routing, e.g. turn on provider prompt caching for use case X}}
2. {{routing change, limited to use cases that passed parity}}
3. {{sovereignty finding for the CISO / DPO}}

**Proposed pilot:** {{scope, use cases, gateway or SDK, duration, how savings will be verified against a frozen baseline (PRD §7.10), commercial terms}}.

## 12. Limits of this report

This report is a projection from 1.12 days of past traffic. It does not guarantee future savings, replace an invoice, assess compliance, prove general model quality, or measure latency or reliability. Migration cost is not included. Full list: M§8.

Specific to this teardown:

- Policy replay assumes the same tokens on every model, no failures or capacity limits, and no change in what users send (M§6.3).
- Parity holds only for the sampled prompts, the scoring method and the judge recorded below (M§8).
- Costs are list prices at the catalog version below, in USD.
- {{run-specific limit}}

## Appendix A — Methodology record

| Field | Value |
|---|---|
| Report id / date | golden / 2026-10-23 |
| Customer (or anonymised label) | Golden Co |
| Consent: signer, date, scope | {{consent}} |
| Mode | full |
| Tool and version | ps-teardown test |
| Input sources and row counts | ps-events ps_events.jsonl: 19 read, 17 used; generic-jsonl generic.jsonl: 9 read, 7 used |
| Window (UTC) | 2026-10-20T08:00:00Z – 2026-10-21T11:00:00Z, 1.12 days |
| Window typical? | {{answer}} |
| Catalog version | test-catalog |
| Re-priced from tokens? | yes |
| Currency and FX rate (source, date) | USD, no conversion |
| Baseline definition | Observed historical mix over the window (M§3) |
| Invoice reconciliation | {{gap_pct}}%, {{status}} |
| Calls by cost source | litellm 1, null 5, pricing_table 16, pricing_table_estimated_tokens 1, provider_reported 1 |
| Null-cost calls by reason | auto_without_served_model 1, missing_tokens 2, unknown_model 1, unpriced_service_tier 1 |
| `requested_model ≠ served_model` calls | 4 |
| Route policy file, `policy_version`, SHA-256 | `nordic-sovereign.json`, nordic-sovereign-1, 82a2c33e46f258b8c38979f8928a6b4e7e83fb08d4d17a6bf6981d74e6b88c23 |
| Sensitivity input | declared data_classification, recorded detected_pii_types and a local PII scan of message text, escalated per the route policy |
| Cache TTL | 3600 seconds |
| Sample size per stratum, seed | up to 100 per stratum, seed 7 (contract-review/confidential/short: 40 of 40; faq/public/short: 80 of 80; support-bot/internal/short: 40 of 40) |
| Scoring method per use case | *: exact_match, contract-review: llm_judge |
| Judge model, provider, jurisdiction, prompt version | mistral-eu:mistral-large-eu (prompt pairwise-v1), jurisdiction EEA per the route policy |
| Re-execution temperature, spend | 0.0 unless the logged call's is known; USD 0.0003; 280 calls, 80 judge calls; no prompt sent to a provider (mock or recorded answers) |
| `min_parity`, `min_cell_samples`, `min_cell_calls` | 0.95, 30, 1 |
| CI methods | Wilson 95% two-sided per cell (ps-eval); bootstrap over the parity sample, 200 resamples, seed 3 |
| Known limits specific to this run | {{limits}} |
| Intermediate data deleted on | {{date}} |

## Appendix B — Data handling statement

The analysis ran on {{where}}, under {{customer}}'s control, with `ps-teardown` reading local files only and making no network calls. Logs did not leave that environment. Sampled prompts were re-run by `ps-eval` only on models, and judged only by models, that the route policy allows for their sensitivity level, using the customer's own provider keys. This report contains aggregates only: no prompt text, response text, request hashes or user identifiers. Intermediate files were deleted on {{date}}.
