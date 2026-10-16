# AI bill teardown — methodology appendix

| | |
|---|---|
| **Status** | v0.1 draft. Parts of the pipeline are not built yet; see [§1](#1-what-is-built-and-what-is-pending). |
| **Applies to** | Concierge teardowns (Gate 1) and the `ps-teardown` CLI once it ships |
| **Spec** | PRD 0001 §7.6 (teardown), §7.10 (baseline), §7.4 (eval harness) |
| **Report template** | [teardown-report-template.md](teardown-report-template.md) |
| **Issue** | #15 |

This appendix explains how a teardown report gets its numbers. It covers what the
report claims and what it does not. Every teardown report links to it and fills in the
[methodology record](#9-methodology-record) for its own run. If a report and this
document disagree, the report's methodology record says what was actually done.

Some sections describe tooling that does not exist on `main` yet. They are marked
**Pending #N**. Those sections give the method the tool must follow. They do not
describe tool behaviour, because there is none yet. A concierge teardown done before
the tool lands must do these steps by hand and say so in the methodology record.

## 1. What is built and what is pending

| Step | Issue | State on `main` |
|---|---|---|
| Per-call cost: cache read/write (5m and 1h), reasoning, service tier (batch/flex), long-context bands, audio, web-search fees, provider-reported cost | #9, #71 (PRs #138, #157) | Built, in `packages/sdk/prompt_shields/pricing.py`. Checked by `schemas/pricing/cost-vectors.v1.json`. |
| Price at `served_model`, not `requested_model` | #6 | **Pending.** See [§4.2](#42-served-model-vs-requested-model). |
| Versioned model catalog with sovereignty fields | #7, #8 | **Pending.** Prices today are the 2026-Q1 snapshot in `pricing.py`, with no catalog version id. |
| Nordic national-ID detectors | #10 | Built (`national_ids.py`, `national_id` PII category). |
| `route-policy.v1` schema and golden vectors | #11 (PR #124) | Built (`schemas/route-policy/`). The gateway and the Python reference router run the routing vectors. Sensitivity, data-policy and quality-bar vectors are `pending`. |
| `ps-teardown` CLI, cost-only mode (ingest, baseline, attribution, arbitrage, cache-hit potential, HTML + JSON) | #12 | **Pending.** |
| Policy replay and sensitivity mix | #13 | **Partly built.** Python reference router (`packages/sdk/prompt_shields/route_policy.py`) for precedence, heuristic strategy, budget clamp and unknown-group fallback. Sensitivity, data policy, quality bar, the replay itself and the sensitivity mix are pending. |
| Eval harness and sample re-execution (quality parity) | #14 | **Pending.** |
| Learning-tuple export | #42 | **Pending.** Out of scope for this appendix. |

Until #12, #13 and #14 land, a report can only contain what can be computed by hand
from the customer's data with `pricing.py`. Every section that depends on a pending
item must say so in the report. It must not be filled in with estimates.

## 2. Data collected and consent

### 2.1 Inputs

A teardown reads one or more of these sources (PRD §7.6). The CLI input formats are
defined in #12. **Pending #12.**

| Source | Typical fields | Prompt text? |
|---|---|---|
| Provider usage exports (OpenAI, Anthropic usage CSV or API) | Date, model, token counts by type, service tier, project or API key | No |
| Prompt Shields SDK or registry events | `requested_model`, `served_model`, token counts, cache, reasoning and audio counts, `service_tier`, `cost`, `cost_source`, `tokens_estimated`, `business_unit`, `use_case_name`, `owner_email`, `data_classification`, `detected_pii_types`, `prompt_hash`, `session_id` | Only if the customer turned on `send_prompt_text` |
| Gateway logs | As above, plus route group, `attempts` | Optional |
| Generic JSONL | `{messages, model, usage, metadata}` | Optional |

### 2.2 Two modes

- **Cost-only.** No prompt text. The report covers baseline spend, attribution, price
  arbitrage on the same model, and cache-hit potential from request hashes. The
  sensitivity mix is limited to declared classifications and any `detected_pii_types`
  the SDK already recorded ([§5.3](#53-without-prompt-text)). Quality parity cannot be
  measured.
- **Full.** Prompt text is available for a sample. This adds PII-based sensitivity
  escalation, re-execution on candidate models and quality scoring. **Pending #13, #14.**

### 2.3 Where the data goes

- The teardown runs on the customer's machine or in their environment (PRD §7.6). Logs
  and prompts do not leave it. In the concierge offer we either run the tool on a
  machine the customer controls or the customer runs it while we watch.
- Re-execution (**pending #14**) uses the customer's own provider keys. A sampled
  prompt is sent only to candidate models, and a judge model, that the customer's
  route policy allows for that prompt's sensitivity level.
- What leaves the customer environment is the report: aggregates by business unit,
  use case, model, sensitivity level and strategy. The report contains no prompt text,
  no response text, no `prompt_hash` values and no user identifiers.
- Small cells can identify people or projects. Any table row that covers fewer than
  `min_cell_calls` calls (record the value used; 10 is a reasonable default) is merged
  into an "Other" row.

### 2.4 Consent checklist

Before data is touched, get written confirmation of each item. Record the date and the
signer in the methodology record.

1. Who authorised the teardown, and that they may authorise processing of these logs.
2. The sources and the time window in scope.
3. Whether prompt text may be read, and if so for which use cases.
4. Whether sample re-execution is allowed, which provider keys to use, and the spend cap
   for re-execution and judging.
5. Where the tool runs, and who on our side can see intermediate files.
6. Retention: intermediate files (normalised logs, samples, judge outputs) are deleted
   when the report is delivered, unless the customer asks in writing for them to be kept.
7. Who receives the report, and whether it may be quoted (anonymised) in sales material.

A teardown that processes personal data needs a lawful basis and, where we act as a
processor, a DPA. This checklist does not replace either.

## 3. Baseline

The **baseline** is what the customer's logged traffic actually cost over the window,
with their own model mix.

- **Window.** One contiguous period, normally 7 days (PRD G1). Record the start and end
  in UTC.
- **Baseline spend** = the sum of per-call cost ([§4](#4-how-cost-is-computed)) over
  every call in the window with a non-null cost. Calls with a null cost are counted and
  shown separately ([§4.5](#45-null-cost-calls)). They are never counted as zero.
- **Baseline mix.** The share of calls and of spend by served model, per use case. In
  P1 this mix is frozen as `baseline_id` together with the catalog version (PRD §7.10).
  A teardown does not freeze a baseline. It reports the mix it measured, so a later
  pilot can freeze the same one.
- **Monthly figures** are the window total × (30 / days in window). This assumes the
  window is typical ([§7](#7-statistical-caveats)).
- PRD open question 6 (historical mix vs. list-price "naive" baseline) is still open.
  Teardowns use the historical mix.

### 3.1 Invoice reconciliation

If the customer shares the provider invoice for the same window, compare it with the
baseline spend for that provider. Report the gap as a percentage. A gap above ±2% marks
the baseline `unreconciled` (PRD G4). The usual causes are:

- negotiated discounts, credits or committed-use pricing (we price at list);
- traffic that is not in the logs (other teams, other keys, dropped telemetry);
- null-cost calls;
- price changes inside the window.

A teardown with an unreconciled baseline can still be delivered. Its headline savings
must then be given as a percentage of the baseline, not as an absolute amount.

## 4. How cost is computed

### 4.1 Per-call cost

Cost is in USD and comes from `prompt_shields.pricing.usage_cost` /
`estimate_cost`. Every case below has a worked example in
`schemas/pricing/cost-vectors.v1.json`.

| Dimension | Treatment |
|---|---|
| Input | `tokens_in` is the whole prompt, including cache reads and writes. Only the uncached remainder is charged at the input rate. |
| Output | `tokens_out` at the output rate. Reasoning / thinking tokens are already inside `tokens_out` and are charged at the output rate. They are reported separately (`tokens_reasoning`) for information only. |
| Cache read | `tokens_cached` at the model's cached rate, or the vendor multiplier on the input rate (OpenAI 0.5, Anthropic 0.1, Google 0.25; o3 / o4-mini and Gemini 2.5 have their own rates). |
| Cache write | `tokens_cache_write` at the 5-minute write rate (Anthropic 1.25× input). `tokens_cache_write_1h`, which is a subset, is charged at the 1-hour rate (Anthropic 2× input). |
| Service tier | `batch` and `flex` multiply every token rate, cache rates included (OpenAI batch/flex, Anthropic batch, Gemini batch: 0.5). Standard tiers are 1.0. Any other tier (e.g. OpenAI `priority`, `scale`) has no known price and gives a null cost. |
| Long context | When `tokens_in` is above a model's band threshold (Gemini 2.5 Pro and Claude Sonnet 4: 200k), the **whole** request is priced at the band rates. This applies to the default table only, not to a custom `pricing_table`. |
| Audio | `tokens_audio_in` / `tokens_audio_out` (subsets of input and output) at the model's audio rates. Audio tokens on a model with no audio rate give a null cost. |
| Tool fees | Per-request fees (Anthropic web search, $0.01 each). Not discounted by service tier. |
| Provider-reported cost | If the provider reports the cost (e.g. OpenRouter `usage.cost`), that number is used as it is, even for models not in the table. |

### 4.2 Served model vs. requested model

A call can ask for one model and be served by another: a gateway route, `model="auto"`,
an alias, a dated snapshot, or an Azure deployment name. The teardown prices each call
at the model that **served** it (`served_model`), because that is what the provider bills.

- Re-pricing in the teardown: use `served_model`. If it is missing, use
  `requested_model` and count the call as `priced_at_requested`. If neither names a
  priced model (e.g. `auto` with no served model), the cost is null.
- **Known gap on `main` (#6, pending).** SDK events carry both fields, but
  the SDK prices at the requested model. Only the Azure adapter falls back to the
  served model, for unmapped deployments. The Gemini and Bedrock adapters only normalise
  the requested name. So an event's `cost` can be wrong when the two differ, and
  `model="auto"` events have a null cost. Until #6 lands, a teardown
  that reads SDK events must **re-price** from token counts and `served_model`. It must
  not sum the `cost` field. Record "re-priced from tokens" in the methodology record.
- The report gives the number and share of calls where `requested_model ≠ served_model`.

### 4.3 Provider-reported, estimated and null

Every priced call has a cost source. The report gives spend and call counts for each one.

| `cost_source` | Meaning | Reliability |
|---|---|---|
| `provider_reported` | The provider returned the cost | Highest; used as is |
| `pricing_table` | Provider-reported token counts × list price | Exact at list price, up to the catalog's accuracy |
| `pricing_table_estimated_tokens` | Token counts were estimated (~4 characters per token) because a stream ended without usage (`tokens_estimated=true`) | Rough. If this is above 5% of baseline spend, say so in the summary. |
| `litellm` | The SDK table has no price, so the LiteLLM integration used LiteLLM's own `response_cost` | LiteLLM's price list, not ours; not tied to the catalog version. Report separately. |
| *(none — cost null)* | See [§4.5](#45-null-cost-calls) | Not priced |

Provider usage exports count as `pricing_table` when they give token counts by type,
and `provider_reported` when they give the billed amount.

### 4.4 Catalog version

- **Until #7 / #8 land:** prices are the 2026-Q1 snapshot in `pricing.py`, which has no
  version id. Record the SDK version (`prompt-shields` package version) and the git
  commit of `pricing.py` as the catalog version, e.g. `sdk-0.2.0@<commit>`.
- **After #7:** record the `price_catalog_version` the catalog reports.
- List prices change often. A report is valid for the prices at its catalog version. If
  a provider changed a price during the window, both baseline and projected costs use
  the catalog version, not the price in force on each day. Say so if it matters.
- Amounts are in USD. If the report shows EUR or NOK, record the exchange rate, its
  source and its date. Convert only at presentation time.

### 4.5 Null-cost calls

A call's cost is null ("unmetered"), never zero, when:

- the model is unknown to the catalog (custom fine-tunes, self-hosted, unlisted providers);
- the model name is null, or is `auto` with no served model ([§4.2](#42-served-model-vs-requested-model));
- input or output token counts are missing;
- the service tier has no known price (e.g. OpenAI `priority`, `scale`);
- the call has audio tokens on a model with no audio rate.

The report gives the null-cost calls as a count, a share of all calls and a share of
tokens, grouped by reason. They are left out of baseline spend, projected spend and
savings. If they are more than 10% of tokens, the summary must say that the savings
figure covers only the priced part of the traffic.

### 4.6 Projected cost on another model

**Pending #13.** Projected spend prices each call's token usage at the candidate
model's rates, with these rules:

- **Same tokens.** The call keeps its observed input and output counts. Tokenizers
  differ between model families, and output length depends on the model. This is a
  known bias ([§6.3](#63-limits)). Once re-execution exists (#14), the sampled calls'
  observed token ratio per (baseline, candidate) pair can correct it. If a correction
  is applied, the report must say so.
- **Same dimensions on both sides.** A cached-read token on the baseline is priced at
  the candidate's cache-read rate only if the candidate offers prompt caching. If it
  does not, it is priced at the full input rate. A batch call keeps the batch discount
  only if the candidate offers batch. Otherwise it is priced at the standard rate. When
  we are unsure, we take the more expensive option, so projected savings are understated
  rather than overstated.
- **Policy rates.** The `modelTier` entries in `route-policy.v1` carry only
  `input_cost_per_1k` and `output_cost_per_1k`. When a candidate is not in the catalog,
  projected cost uses those two rates only, with no cache, tier or long-context pricing,
  and the report flags it.
- **No price for the candidate** means the projected cost for that call is null and the
  call is left out of the strategy's savings. It is not priced at zero.

## 5. Sensitivity mix

**Pending #13** for the tooling. The method follows the shared contract in
`schemas/route-policy/` (`route-policy.v1`, `vectors/sensitivity.json`,
`vectors/data-policy.json`).

### 5.1 Effective level per call

```
effective = max( declared data_classification, or sensitivity.unclassified_default if none,
                 every level in sensitivity.pii_escalation for each detected PII category )
```

- **Escalate-only.** Detection can raise a level, never lower it.
- `unclassified_default` is `confidential` unless the customer's policy says otherwise.
  A call with no declared classification and no detection is counted as
  `confidential`, not `public`.
- Each call gets a `sensitivity_source`: `declared`, `pii_escalated` (a detection
  raised the level) or `default`.

### 5.2 What "must stay sovereign" means

A call **must stay sovereign** if the data-policy rule for its effective level excludes
every candidate outside the allowed jurisdictions. Usually that is a rule with
`jurisdictions` limited to `EEA` / `NO`, or `us_cloud_act_exposure: false`. The
definition depends on the customer's policy, so the report names the policy file and
its `policy_version`.

The report shows, for each level: number of calls, share of calls, spend, share of
spend, and the split by `sensitivity_source`. The headline "sovereign share" is the
share of **spend** that must stay sovereign, with the share of calls next to it.

### 5.3 Without prompt text

In cost-only mode PII escalation can only use `detected_pii_types` that the SDK already
recorded on its events. The SDK detects locally and sends only category names. Provider
usage exports have no per-call classification. So their calls fall back to
`unclassified_default` or to a classification the customer gives per project or API key.
Record which applies. With the default policy this pushes the sovereign share **up**.

### 5.4 Limits of detection

PII detection is regex and keyword based. The README says it is "a signal, not a data
loss prevention control". It misses names, addresses and free-text identifiers, and it
flags some harmless numbers. Nordic national IDs (#10) are checksum-validated, which
cuts false positives. There is no measured recall yet (PRD backlog #30). So:

- missed detections push the sensitivity mix **down**. The escalate-only rule and the
  `confidential` default limit the damage;
- false positives push it **up**;
- the report gives the share of calls whose level comes from `pii_escalated`, so the
  reader can see how much of the mix depends on detection.

The sensitivity mix describes the customer's traffic **under their stated policy**. It
is not a data classification audit ([§8](#8-what-a-teardown-does-not-claim)).

## 6. Policy replay

**Pending #13.** Policy replay runs every logged call through a Python reference router
that implements `route-policy.v1`, then prices the routed decision. The router must pass
the golden vectors in `schemas/route-policy/vectors/` before a report can use it.
The router (`prompt_shields.route_policy`) passes the active vectors
(`packages/sdk/tests/test_route_policy_vectors.py`). It does not yet apply sensitivity,
the data policy or the use-case quality bar ([§6.1](#61-per-call)); those vectors are
`pending`, and until they pass a replay must not be presented as policy-compliant.

### 6.1 Per call

1. Normalise the call ([§2.1](#21-inputs)).
2. Work out its effective sensitivity ([§5.1](#51-effective-level-per-call)).
3. Apply the data-policy constraint. If no compliant candidate is left, the call's
   outcome is `no_compliant_candidate`. It is counted and shown, and its projected cost
   is not lowered.
4. Apply the use-case quality bar (`min_group`, `eligible_models`), then the strategy,
   with the precedence in the schema: data policy > explicit group > hints > use-case
   bar > default strategy.
5. Price the chosen model ([§4.6](#46-projected-cost-on-another-model)).

### 6.2 Strategies reported

Each report shows projected spend for:

- **Baseline:** the observed mix ([§3](#3-baseline)).
- **Same-model arbitrage:** the same model from the cheapest provider that serves it
  and is allowed by the call's data policy (e.g. an open-weight model offered by
  several hosts, or a regional endpoint). Cost-only mode can compute this (**pending #12**).
- **Policy, cheapest:** the cheapest compliant candidate in the group the policy picks.
- **Policy, at parity:** like *Policy, cheapest*, but only models that passed the
  parity bar for that use case ([§7.2](#72-quality-parity)) are allowed. **Pending #14.**
  Without eval results this row reads "not measured". It is never filled in with the
  *Policy, cheapest* number.
- **Cache:** extra savings from exact-match caching ([§6.4](#64-cache-hit-potential)),
  shown separately and not added to routing savings.

### 6.3 Limits

Replay is a counterfactual on logged traffic. It assumes:

- **Same tokens** on every model ([§4.6](#46-projected-cost-on-another-model)).
- **No failures.** It does not simulate capacity limits, 429s, failover or circuit
  breakers. EU sovereign supply is known to be capacity-constrained (PRD P5). A
  strategy that relies on one sovereign provider is optimistic.
- **No behaviour change.** Users and applications do not change what they send because
  the model changed (more retries, longer prompts, different tool use).
- **The logged traffic is the traffic.** Calls missing from the logs are not replayed.
  For SDK sources, report the SDK's dropped-event counters if they are available.
- **Complexity is heuristic.** The `heuristic` strategy uses the gateway heuristic
  (tokens, code, schema). It is not a measure of task difficulty.
- **Policy is as given.** The report evaluates the customer's policy file, or the
  example policy we agreed with them. A different policy gives different numbers.

### 6.4 Cache-hit potential

Cache-hit potential is the share of calls, and their cost, whose request repeats an
earlier one exactly within a chosen TTL (record it; default 1 hour). **Pending #12.**

- On SDK events the key is `prompt_hash`: SHA-256 of the concatenated string message
  contents. It does **not** include the model, system parameters, tools, temperature or
  non-text content. Two calls with the same text but different settings count as a hit.
  So the figure is an **upper bound** for an exact-match cache.
- A repeat call is counted as saving its whole cost. It is not counted when the policy
  would turn the cache off for it. The PRD default is off for sensitivity ≥
  `confidential`.
- Provider prompt caching (already in `tokens_cached`) is a different thing. It is part
  of the baseline, not of this figure.

## 7. Statistical caveats

### 7.1 Window and extrapolation

- One week may not be typical: month-end jobs, launches, holidays, incidents. Ask the
  customer whether the window was normal, and record the answer.
- Monthly and annual figures are linear extrapolations. Show them as rounded figures
  next to the measured window total.
- If traffic grew a lot inside the window, say so. The extrapolation then understates
  future spend in absolute terms. The savings percentage is less sensitive.

### 7.2 Quality parity

**Pending #14.** Parity is measured on a stratified sample, re-run on candidate models.

- **Sampling.** Stratify by use case × sensitivity level × complexity band. Sample at
  random within each stratum, with a recorded seed. Record the sample size per stratum,
  and the population size it stands for.
- **Score.** One of: exact match, schema validity, or pairwise LLM-as-judge against the
  baseline model's answer. The method is chosen per use case and recorded.
- **Parity** for a (use case, candidate model) cell is the share of sampled prompts
  where the candidate's answer is acceptable: correct for exact match, valid for
  schema, win or tie for the judge.
- **Interval.** Give a two-sided 95% Wilson interval (z = 1.96) for each cell. Do not
  give a point estimate without its interval.
- **Sample size.** With zero failures, the lower bound of the two-sided 95% Wilson
  interval is n / (n + 1.96²), which reaches 0.95 only at n ≥ 73 (0.9500 at 73, 0.9493
  at 72). Other intervals give other thresholds (one-sided 95% Clopper–Pearson,
  0.05^(1/n): n ≥ 59), so record the method with the threshold. A cell with fewer samples cannot show that parity ≥ 95% is likely, even if
  every answer passed. Cells below `min_cell_samples` (record it; default 30) are shown
  as "insufficient sample", and their model is not counted as at parity.
- **Headline rule.** "At parity" in the headline means the point estimate is ≥
  `min_parity` (default 0.95, from the policy's quality bar) **and** n ≥
  `min_cell_samples`. The report also gives the savings figure that uses the Wilson
  lower bound instead. That is the conservative number.
- **Savings at parity CI.** The headline savings percentage gets a bootstrap interval
  (resample calls within strata; record the number of resamples and the seed).

### 7.3 Judge

**Pending #14.**

- The judge model is subject to the same data policy as the candidates. A prompt at
  `restricted` is never sent to a judge that the policy would not allow to serve it.
  Record the judge model, its provider and jurisdiction, and the version of the judge
  prompt.
- LLM judges prefer certain answer positions and their own family's style. Show the
  two answers in random order, judge every pair in both orders, and count a
  disagreement as a tie. Do not use as a judge a model from the same family as a
  candidate it is scoring, where an alternative exists. If none exists, record it.
- Generation is not deterministic. Record the temperature used for re-execution.
  Where the baseline call's temperature is known, use it.

### 7.4 Aggregation

- Parity and savings are given **per use case**. An overall figure is a spend-weighted
  average of the use-case figures. A use case with no eval set adds nothing to "savings
  at parity". It is not assumed to be at parity.
- Comparing many (use case, model) cells will produce some false passes by chance. The
  report lists every cell that was tested, not only the ones that passed.

## 8. What a teardown does not claim

A teardown report is a projection from a sample of past traffic. It does **not**:

- **Guarantee future savings.** Prices, traffic and models change. Realised savings
  are measured later against a frozen baseline (PRD §7.10), not taken from this report.
- **Replace an invoice.** Costs are list prices from a catalog version. Negotiated
  discounts, committed spend, credits, taxes and currency effects are not included
  unless the methodology record says otherwise.
- **Assess compliance.** The sensitivity mix applies the customer's own policy and a
  regex-based PII signal. It is not a DPIA, a data classification audit, DLP evidence or
  a legal opinion on jurisdiction or CLOUD Act exposure.
- **Prove general model quality.** Parity holds only for the sampled prompts, the
  chosen scoring method and the stated judge. It says nothing about other tasks, other
  languages or the next model version.
- **Measure latency, availability or reliability.** No calls are simulated under load
  and failover is not modelled.
- **Count migration cost.** Engineering work, prompt re-tuning, contract changes and the
  cost of running the eval and the gateway are left out.
- **Cover traffic it did not see.** Spend outside the supplied logs is not included.
  Null-cost calls are not priced.
- **Recommend anonymise-then-route.** Prompt rewriting is gated on a measured PII recall
  target (PRD §7.9). A teardown never assumes sensitive prompts can be scrubbed and sent
  to a cheaper non-sovereign model.

## 9. Methodology record

Every report fills in this table. A field that does not apply reads "n/a" with a reason.
A field for a step that has not been built reads "pending #N".

| Field | Value |
|---|---|
| Report id / date | |
| Customer (or anonymised label) | |
| Consent: signer, date, scope ([§2.4](#24-consent-checklist)) | |
| Mode | cost-only / full |
| Tool and version | `ps-teardown x.y.z` (pending #12) or "manual, sdk-0.2.0" |
| Input sources and row counts | per source: rows read, rows kept, rows dropped and why |
| Window (UTC) | start – end, days |
| Window typical? (customer answer) | |
| Catalog version ([§4.4](#44-catalog-version)) | |
| Re-priced from tokens? ([§4.2](#42-served-model-vs-requested-model)) | yes / no |
| Currency and FX rate (source, date) | |
| Baseline definition ([§3](#3-baseline)) | observed historical mix, window as above |
| Invoice reconciliation | gap %, reconciled / unreconciled / not available |
| Calls by cost source ([§4.3](#43-provider-reported-estimated-and-null)) | provider_reported / pricing_table / estimated tokens / litellm / null |
| Null-cost calls by reason ([§4.5](#45-null-cost-calls)) | |
| `requested_model ≠ served_model` calls | |
| Route policy file, `policy_version`, SHA-256 | |
| Sensitivity input ([§5.3](#53-without-prompt-text)) | declared / detected_pii_types / prompt text / per-key mapping |
| Cache TTL used ([§6.4](#64-cache-hit-potential)) | |
| Sample size per stratum and seed ([§7.2](#72-quality-parity)) | pending #14 |
| Scoring method per use case | pending #14 |
| Judge model, provider, jurisdiction, prompt version ([§7.3](#73-judge)) | pending #14 |
| Re-execution temperature, spend | pending #14 |
| `min_parity`, `min_cell_samples`, `min_cell_calls` | |
| CI methods (Wilson; bootstrap resamples and seed) | |
| Known limits specific to this run | |
| Intermediate data deleted on (date) | |
