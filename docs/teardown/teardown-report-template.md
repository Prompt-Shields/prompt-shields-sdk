# AI bill teardown — customer report template

Copy this file for each teardown and replace every `{{placeholder}}`. Methodology:
[methodology.md](methodology.md). Section numbers like "M§4" point to it.

Rules for filling it in:

- A section that depends on a step that is not built yet stays in the report, with the
  line *"Not measured in this teardown — pending #N."* It is never filled in with an
  estimate. Today that means #12 (cost-only CLI), #13 (policy replay and sensitivity
  mix) and #14 (quality parity); see M§1.
- Every amount gives its currency. Every percentage says what it is a percentage of.
- Rows that cover fewer than `{{min_cell_calls}}` calls are merged into "Other" (M§2.3).
- No prompt text, response text, `prompt_hash` values or user identifiers appear anywhere.
- Delete this instruction block before sending.

---

# AI bill teardown — {{customer_label}}

| | |
|---|---|
| **Prepared for** | {{customer_name}}, {{recipient_names_roles}} |
| **Prepared by** | {{preparer}}, Prompt Shields |
| **Report id / date** | {{report_id}} / {{report_date}} |
| **Window analysed** | {{window_start_utc}} – {{window_end_utc}} ({{window_days}} days) |
| **Mode** | {{cost-only / full}} |
| **Confidentiality** | {{confidentiality_statement}} |

## 1. Summary

| | Window | Per month (extrapolated, M§3) |
|---|---|---|
| Baseline spend | {{baseline_window}} | {{baseline_month}} |
| Projected spend, same-model arbitrage | {{arbitrage_window}} ({{arbitrage_pct}}% lower) | {{arbitrage_month}} |
| Projected spend, policy at parity | {{parity_window}} ({{parity_pct}}% lower, 95% CI {{parity_ci}}) — or *pending #14* | {{parity_month}} |
| Cache-hit potential (upper bound) | {{cache_window}} ({{cache_pct}}% of baseline) | {{cache_month}} |
| Spend that must stay sovereign | {{sovereign_spend_pct}}% of spend, {{sovereign_calls_pct}}% of calls — or *pending #13* | |

**In one sentence:** {{headline_sentence}}

**Gate 1 reading** (≥ 30% savings at ≥ 95% parity): {{met / not met / not measured — pending #14}}.
Based on: {{point estimate and conservative (Wilson lower bound) figures, M§7.2}}.

**How much to trust these numbers:**

- Baseline reconciliation with the invoice: {{gap_pct}}% → {{reconciled / unreconciled / no invoice}} (M§3.1).
- Priced calls: {{priced_calls_pct}}% of calls, {{priced_tokens_pct}}% of tokens. Null-cost calls are left out (M§4.5).
- Estimated token counts: {{estimated_spend_pct}}% of baseline spend (M§4.3).
- {{any other caveat that changes the headline}}

## 2. Scope and data

| Source | Rows read | Rows used | Dropped (reason) |
|---|---|---|---|
| {{source}} | {{n}} | {{n}} | {{n (reason)}} |

- Prompt text: {{not used / used for a sample of N calls in use cases …}}
- Sensitivity input: {{declared / detected_pii_types / prompt text / per-key mapping}} (M§5.3)
- Was the window typical? {{customer answer}}

## 3. Baseline spend

| Provider | Model (served) | Calls | Input tokens | Output tokens | Cached tokens | Spend | Share |
|---|---|---|---|---|---|---|---|
| {{provider}} | {{model}} | {{n}} | {{n}} | {{n}} | {{n}} | {{amount}} | {{pct}} |
| **Total** | | | | | | **{{amount}}** | 100% |

| Cost source (M§4.3) | Calls | Spend |
|---|---|---|
| provider_reported | {{n}} | {{amount}} |
| pricing_table | {{n}} | {{amount}} |
| pricing_table_estimated_tokens | {{n}} | {{amount}} |
| litellm (LiteLLM's own price) | {{n}} | {{amount}} |
| null (not priced) | {{n}} | — |

Calls where requested model ≠ served model: {{n}} ({{pct}}%). {{Re-priced from tokens at served_model, per M§4.2.}}

## 4. Where the money goes

*Not measured in this teardown — pending #12.* (Remove this line once filled in by hand or by the CLI.)

### 4.1 By business unit and use case

| Business unit | Use case | Calls | Spend | Share | Main model |
|---|---|---|---|---|---|
| {{bu}} | {{use_case}} | {{n}} | {{amount}} | {{pct}} | {{model}} |
| (unattributed) | | {{n}} | {{amount}} | {{pct}} | |

### 4.2 Top cost drivers

1. {{driver — e.g. "use case X sends 40k-token prompts to model Y with no caching"}}
2. {{driver}}
3. {{driver}}

## 5. Same-model price arbitrage

*Not measured in this teardown — pending #12.*

| Model | Current provider | Cheaper compliant provider | Spend now | Spend after | Saving |
|---|---|---|---|---|---|
| {{model}} | {{provider}} | {{provider}} | {{amount}} | {{amount}} | {{amount}} ({{pct}}) |

## 6. Cache-hit potential

*Not measured in this teardown — pending #12.*

TTL {{ttl}}. Exact repeats of the message text: {{n}} calls ({{pct}}% of calls), {{amount}}
({{pct}}% of baseline). This is an upper bound (M§6.4). Calls whose sensitivity turns the
cache off under the policy are excluded: {{n}}.

## 7. Sensitivity mix

*Not measured in this teardown — pending #13.*

Policy: `{{policy_file}}`, `policy_version` {{policy_version}}. `unclassified_default` = {{level}}.

| Level | Calls | Share of calls | Spend | Share of spend | declared / pii_escalated / default | Must stay sovereign? |
|---|---|---|---|---|---|---|
| public | {{n}} | {{pct}} | {{amount}} | {{pct}} | {{a / b / c}} | {{yes/no}} |
| internal | | | | | | |
| confidential | | | | | | |
| restricted | | | | | | |

{{Short reading, e.g. "62% of spend must stay in the EEA; 18 points of that come from the confidential default for calls with no declared classification."}}

## 8. Policy replay — projected spend by strategy

*Not measured in this teardown — pending #13.*

| Strategy (M§6.2) | Projected spend | vs. baseline | Calls with no compliant candidate |
|---|---|---|---|
| Baseline (observed) | {{amount}} | — | — |
| Same-model arbitrage | {{amount}} | {{pct}} | {{n}} |
| Policy, cheapest (not quality-checked) | {{amount}} | {{pct}} | {{n}} |
| Policy, at parity | {{amount}} or *pending #14* | {{pct}} | {{n}} |
| Cache (separate, not additive) | {{amount}} | {{pct}} | — |

"Policy, cheapest" is not a savings claim. It shows the ceiling before quality is checked.

## 9. Quality parity

*Not measured in this teardown — pending #14.*

Scoring: {{method per use case}}. Judge: {{judge_model}} ({{provider}}, {{jurisdiction}}), prompt {{judge_prompt_version}}.

| Use case | Candidate model | Sample n | Parity | 95% Wilson CI | At parity? |
|---|---|---|---|---|---|
| {{use_case}} | {{model}} | {{n}} | {{pct}} | {{lo – hi}} | {{yes / no / insufficient sample}} |

Every tested cell is listed, including the ones that failed (M§7.4).

## 10. Savings at parity

*Not measured in this teardown — pending #14.*

| Use case | Baseline spend | At-parity spend | Saving | Model change |
|---|---|---|---|---|
| {{use_case}} | {{amount}} | {{amount}} | {{pct}} | {{from → to}} |
| **Total (spend-weighted)** | **{{amount}}** | **{{amount}}** | **{{pct}} (95% CI {{ci}})** | |
| Conservative (Wilson lower bound) | | {{amount}} | {{pct}} | |

Use cases with no eval set count as zero savings here (M§7.4).

## 11. Recommendations and next steps

1. {{quick win that needs no routing, e.g. turn on provider prompt caching for use case X}}
2. {{routing change, limited to use cases that passed parity}}
3. {{sovereignty finding for the CISO / DPO}}

**Proposed pilot:** {{scope, use cases, gateway or SDK, duration, how savings will be
verified against a frozen baseline (PRD §7.10), commercial terms}}.

## 12. Limits of this report

This report is a projection from {{window_days}} days of past traffic. It does not
guarantee future savings, replace an invoice, assess compliance, prove general model
quality, or measure latency or reliability. Migration cost is not included. Full list:
M§8.

Specific to this teardown:

- {{run-specific limit}}

## Appendix A — Methodology record

Fill in every field from M§9. "n/a" needs a reason. Unbuilt steps read "pending #N".

| Field | Value |
|---|---|
| Report id / date | {{report_id}} / {{report_date}} |
| Customer (or anonymised label) | {{customer_label}} |
| Consent: signer, date, scope | {{consent}} |
| Mode | {{cost-only / full}} |
| Tool and version | {{tool_version}} |
| Input sources and row counts | {{sources}} |
| Window (UTC) | {{window}} |
| Window typical? | {{answer}} |
| Catalog version | {{catalog_version}} |
| Re-priced from tokens? | {{yes / no}} |
| Currency and FX rate (source, date) | {{fx}} |
| Baseline definition | Observed historical mix over the window (M§3) |
| Invoice reconciliation | {{gap_pct}}%, {{status}} |
| Calls by cost source | {{counts}} |
| Null-cost calls by reason | {{counts}} |
| `requested_model ≠ served_model` calls | {{n}} |
| Route policy file, `policy_version`, SHA-256 | {{policy}} |
| Sensitivity input | {{input}} |
| Cache TTL | {{ttl}} |
| Sample size per stratum, seed | {{sample}} or pending #14 |
| Scoring method per use case | {{method}} or pending #14 |
| Judge model, provider, jurisdiction, prompt version | {{judge}} or pending #14 |
| Re-execution temperature, spend | {{reexec}} or pending #14 |
| `min_parity`, `min_cell_samples`, `min_cell_calls` | {{0.95}}, {{30}}, {{10}} |
| CI methods | Wilson 95% two-sided per cell; bootstrap {{n_resamples}} resamples, seed {{seed}} |
| Known limits specific to this run | {{limits}} |
| Intermediate data deleted on | {{date}} |

## Appendix B — Data handling statement

The analysis ran on {{where}}, under {{customer}}'s control. Logs{{ and prompt text}} did
not leave that environment. {{Sampled prompts were re-run only on models, and judged only
by models, that the customer's policy allows for their sensitivity level, using the
customer's own provider keys.}} This report contains aggregates only. Intermediate files
were deleted on {{date}}.
