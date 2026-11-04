# AI bill teardown — Golden Co

|  |  |
|---|---|
| **Prepared for** | {{customer_name}}, {{recipient_names_roles}} |
| **Prepared by** | {{preparer}}, Prompt Shields |
| **Report id / date** | golden / 2026-10-23 |
| **Window analysed** | 2026-10-20T08:00:00Z – 2026-10-20T12:00:00Z (0.17 days) |
| **Mode** | cost-only |
| **Confidentiality** | {{confidentiality_statement}} |

## 1. Summary

|  | Window | Per month (extrapolated, M§3) |
|---|---|---|
| Baseline spend | USD 0.0150 | USD 2.64 |
| Projected spend, same-model arbitrage | USD 0.0150 (0.0% lower) | USD 2.64 |
| Projected spend, policy at parity | *pending #14* |  |
| Cache-hit potential (upper bound) | USD 0.0001 (0.9% of baseline) | USD 0.0236 |
| Spend that must stay sovereign | *pending #13* |  |

**In one sentence:** Over 0.17 days the logged AI traffic cost USD 0.0150 (about USD 2.64 a month); same-model price arbitrage would save 0.0% of it and exact-match caching up to 0.9%. Savings from routing at quality parity are not measured yet (pending #14).

**Gate 1 reading** (≥ 30% savings at ≥ 95% parity): not measured — pending #14.

**How much to trust these numbers:**

- Baseline reconciliation with the invoice: {{gap_pct}}% → {{reconciled / unreconciled / no invoice}} (M§3.1).
- Priced calls: 85.7% of calls, 100.0% of tokens. Null-cost calls are left out (M§4.5).
- Estimated token counts: 0.0% of baseline spend (M§4.3).
- Same-model arbitrage does not check the cheaper host against a route policy (pending #13).

## 2. Scope and data

| Source | Rows read | Rows used | Dropped (reason) |
|---|---|---|---|
| generic-jsonl (`generic.jsonl`) | 9 | 7 | 1 (no_model), 1 (not_an_object) |

- Prompt text: not used. Where an input carried messages, they were read on this machine only to compute a request hash and prompt features; no text was kept.
- Sensitivity input: declared data_classification only; PII escalation pending #13; see M§5.3.
- Was the window typical? {{customer answer}}

## 3. Baseline spend

| Provider | Model (served) | Calls | Input tokens | Output tokens | Cached tokens | Spend | Share |
|---|---|---|---|---|---|---|---|
| anthropic | claude-3-5-sonnet-20241022 | 2 | 1,700 | 580 | 1,200 | USD 0.0106 | 70.6% |
| google | gemini-2.5-flash | 1 | 5,000 | 1,000 | 0 | USD 0.0040 | 26.7% |
| openai | gpt-4o-mini | 3 | 1,260 | 359 | 0 | USD 0.0004 | 2.7% |
| openai | gpt-4o | 1 | 0 | 0 | 0 | — | — |
| **Total** |  | 7 |  |  |  | **USD 0.0150** | 100% |

| Cost source (M§4.3) | Calls | Spend |
|---|---|---|
| provider_reported | 0 | USD 0.00 |
| pricing_table | 6 | USD 0.0150 |
| pricing_table_estimated_tokens | 0 | USD 0.00 |
| litellm (LiteLLM's own price) | 0 | USD 0.00 |
| null (not priced) | 1 | — |

Null-cost calls by reason (M§4.5):

| Reason | Calls | Tokens |
|---|---|---|
| missing_tokens | 1 | 0 |

Calls where requested model ≠ served model: 0 (0.0%). Re-priced from tokens at served_model, per M§4.2. Calls with no served model, priced at the requested model: 7.

## 4. Where the money goes

### 4.1 By business unit and use case

| Business unit | Use case | Calls | Spend | Share | Main model |
|---|---|---|---|---|---|
| Engineering | code-assist | 3 | USD 0.0106 | 70.6% | claude-3-5-sonnet-20241022 |
| (none) | summaries | 1 | USD 0.0040 | 26.7% | gemini-2.5-flash |
| Customer Care | faq | 3 | USD 0.0004 | 2.7% | gpt-4o-mini |

### 4.2 Top cost drivers

1. Use case `code-assist` on `claude-3-5-sonnet-20241022`: USD 0.0106 (70.6% of baseline spend), 2 calls, 850 input tokens per call on average, 70.6% of input tokens read from the provider's prompt cache.
2. Use case `summaries` on `gemini-2.5-flash`: USD 0.0040 (26.7% of baseline spend), 1 call, 5,000 input tokens per call on average, 0.0% of input tokens read from the provider's prompt cache.
3. Use case `faq` on `gpt-4o-mini`: USD 0.0004 (2.7% of baseline spend), 3 calls, 420 input tokens per call on average, 0.0% of input tokens read from the provider's prompt cache.

## 5. Same-model price arbitrage

No model in the window is sold cheaper by another host in catalog `test-catalog`. Saving: USD 0.00.

## 6. Cache-hit potential

TTL 60 minutes. Calls with a request hash: 6 (85.7% of calls). Exact repeats of the message text: 1 call (14.3% of calls), USD 0.0001 (0.9% of baseline). This is an upper bound (M§6.4). Calls whose sensitivity turns the cache off under the policy are excluded: 2 (USD 0.0040); the cache is off at `confidential` and above, and a call with no declared classification counts as `confidential` (M§5.1).

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

This report is a projection from 0.17 days of past traffic. It does not guarantee future savings, replace an invoice, assess compliance, prove general model quality, or measure latency or reliability. Migration cost is not included. Full list: M§8.

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
| Input sources and row counts | generic-jsonl generic.jsonl: 9 read, 7 used |
| Window (UTC) | 2026-10-20T08:00:00Z – 2026-10-20T12:00:00Z, 0.17 days |
| Window typical? | {{answer}} |
| Catalog version | test-catalog |
| Re-priced from tokens? | yes |
| Currency and FX rate (source, date) | USD, no conversion |
| Baseline definition | Observed historical mix over the window (M§3) |
| Invoice reconciliation | {{gap_pct}}%, {{status}} |
| Calls by cost source | null 1, pricing_table 6 |
| Null-cost calls by reason | missing_tokens 1 |
| `requested_model ≠ served_model` calls | 0 |
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
