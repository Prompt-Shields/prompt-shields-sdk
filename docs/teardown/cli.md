# `ps-teardown` CLI — cost-only mode

`ps-teardown` builds the AI bill teardown report ([template](teardown-report-template.md),
[methodology](methodology.md), "M§" below) from a customer's usage logs. It ships with the
SDK (`pip install prompt-shields`), reads local files only and makes no network calls.

```
ps-teardown run usage-openai.json usage-anthropic.json events.jsonl \
  --key-map key-map.json --customer-label "Acme" --out-dir report/ \
  --learning-tuples report/tuples.jsonl
```

This writes `report/teardown-report.md`, `.html` and `.json`. The Markdown and HTML follow
the template section by section. Fields only a person can fill (customer name, consent,
invoice reconciliation, recommendations) keep their `{{placeholder}}`. Sections that need
policy replay or quality parity read *"Not measured in this teardown — pending #13 / #14."*

| Option | Default | Meaning |
|---|---|---|
| `--input-format` | detected per file | Force one of the input formats below |
| `--key-map FILE` | none | JSON: project / workspace / API key id → `{business_unit, use_case, data_classification}` |
| `--min-cell-calls N` | 10 | Rows with fewer calls are merged into "Other" (M§2.3) |
| `--cache-ttl SECONDS` | 3600 | Exact-match cache TTL (M§6.4) |
| `--cache-off-at LEVEL` | `confidential` | The cache is off at this level and above |
| `--unclassified-default LEVEL` | `confidential` | Level of a call with no declared classification (M§5.1) |
| `--allow-provider P` | any | Limit same-model arbitrage to these providers (repeatable) |
| `--format F` / `--stdout F` | all three | Which report formats to write, or print one to stdout |
| `--learning-tuples PATH` | off | Also write the learning-tuple export (#42) |

## Input formats

| Format | Files | One record per | Notes |
|---|---|---|---|
| `openai-usage` | OpenAI `GET /v1/organization/usage/completions` pages (JSON), or CSV with the result column names plus `start_time` / `end_time` | bucket × project × key × model | `calls` = `num_model_requests`; `batch: true` → batch tier |
| `anthropic-usage` | Anthropic `GET /v1/organizations/usage_report/messages` pages (JSON), or CSV with the result fields flattened (`ephemeral_5m_input_tokens`, `ephemeral_1h_input_tokens`, `web_search_requests`) plus `starting_at` / `ending_at` | bucket × workspace × key × model | No request count (`calls` unknown). `context_window` picks the long-context band. |
| `ps-events` | Prompt Shields SDK events, gateway telemetry events, registry (`ai_usage_events`) rows: JSONL, JSON array or CSV | call | Re-priced from tokens at `served_model` (at `requested_model` when the served id has no price, as `pricing.resolve_pricing_model` does); the event's own `cost` is not summed (M§4.2). `provider_reported` costs are kept. Gateway cache events are dropped. |
| `generic-jsonl` | `{"messages", "model", "usage", "metadata"}` per line | call | `usage` may be OpenAI, Anthropic or Gemini shaped. Messages are read only to compute the request hash and prompt features. |

Every input row is either used or counted as dropped with a reason (report §2). Input files
are untrusted: a malformed row (invalid JSON or CSV, an out-of-range timestamp, a negative
count or cost) is dropped or has that field treated as missing; it does not abort the run.

## Normalised record

Each row becomes a `prompt_shields.teardown.TeardownRecord`. Other modes (policy replay
and sensitivity mix, #13) work on these records, never on the raw input.

| Field | Meaning |
|---|---|
| `source`, `source_file` | Input format and file name |
| `timestamp`, `timestamp_end` | Call time (UTC); bucket start and end for an aggregated row |
| `aggregated`, `calls` | Aggregated usage-export row, and the requests it stands for (1 for a call, None when unknown) |
| `provider` | Normalised slug (`openai`, `anthropic`, `azure_openai`, `bedrock`, `google`, `mistral`, …) |
| `requested_model`, `served_model` | As logged. Usage exports report the serving model, so they set `served_model`. |
| `tokens_in`, `tokens_out` | Whole prompt (cache reads and writes included) and whole completion |
| `tokens_cached`, `tokens_cache_write`, `tokens_cache_write_1h`, `tokens_reasoning`, `tokens_audio_in`, `tokens_audio_out` | Subsets, as in `pricing.py` |
| `web_search_requests`, `service_tier`, `usage_units`, `usage_unit_type`, `image_quality`, `image_size`, `context_window` | Other priced dimensions |
| `tokens_estimated` | Token counts were estimated, not reported |
| `provider_cost` | Amount the provider billed (used as is) |
| `framework_cost`, `framework_cost_source` | A cost from another price list (LiteLLM), used only when the catalog has no price |
| `business_unit`, `use_case`, `data_classification`, `detected_pii_types` | Attribution and declared sensitivity |
| `project`, `api_key_id` | Opaque usage-export ids, used only for `--key-map` |
| `route_group` | Gateway route group |
| `prompt_hash` | SHA-256 of the message text (the SDK's algorithm). Used for cache-hit analysis only; never in a report or export. |
| `features` | Local prompt features (see below), when the input had message text |

The record never holds prompt text, response text, user ids, session ids or owner emails.

Python API, in pipeline order:

```python
from prompt_shields.teardown import (
    ReportOptions, build_report, load_records, price_records, render_report,
)

loaded = load_records(["events.jsonl"])
priced = price_records(loaded.records)          # list[PricedRecord]
report = build_report(priced, loaded.sources, ReportOptions(customer_label="Acme"))
markdown = render_report(report, "markdown")    # or "html", "json"
```

A new mode adds its sections to the `report` dict and removes its entry from
`report["pending"]`. Prices come through the `PriceSource` protocol
(`prompt_shields/teardown/catalog.py`); `SdkPriceSource` wraps `prompt_shields.pricing`
and will be replaced by the versioned catalog (#7 / #8).

## What the cost-only report computes

- **Baseline** (M§3, M§4): every record priced at `served_model` (dated snapshots and
  Bedrock inference profiles resolve to their priced id). Null costs are counted by
  reason, never as zero. Aggregated export rows are priced at base rates unless the export
  reports a long-context band: a row's summed `tokens_in` says nothing about one request.
- **Attribution**: by business unit and use case, with `--key-map` for usage exports.
  Top cost drivers are the largest (use case, model) cells.
- **Same-model arbitrage** (M§6.2): the same model from a cheaper host in the catalog's
  equivalence groups, limited by `--allow-provider`. A host is only a candidate if its
  price list covers every dimension the call uses (web-search fees, 1-hour cache writes,
  long-context bands, audio, batch). Provider-billed amounts are not re-priced. The cheaper
  host is not checked against a route policy (pending #13). With the 2026-Q1 price table,
  hosts in a group charge the same, so this is usually zero until the catalog adds hosts.
- **Cache-hit potential** (M§6.4): calls whose `prompt_hash` repeats within the TTL of the
  entry's write. Usage exports and gateway events carry no hash, so they are not covered;
  the report gives the hashed share. Calls at or above `--cache-off-at` are excluded, and in
  cost-only mode a call's level is its declared classification or `--unclassified-default`.
- **Window and monthly figures**: first to last timestamp (bucket end for exports);
  monthly = window × 30 / days.

## Learning tuples (#42)

`--learning-tuples` writes one JSON object per call (aggregated export rows are skipped):

```json
{"schema": "ps-learning-tuple.v0", "timestamp": "2026-10-20T08:00:00Z",
 "business_unit": "Customer Care", "use_case": "faq", "data_classification": "public",
 "features": {"approx_input_tokens": 19, "message_count": 2, "has_code": false,
              "has_json_schema": false, "complexity": "simple"},
 "features_source": "messages", "provider": "openai", "model": "gpt-4o-mini",
 "quality_score": null, "quality_source": "not_measured",
 "cost_usd": 0.000135, "cost_source": "pricing_table", "tokens_in": 420, "tokens_out": 120}
```

Features are the gateway heuristic's signals (PRD §6.2): approximate input tokens, code
fences, JSON-schema wording, message count and the resulting complexity. They are computed
on the customer's machine. Inputs without message text get `features_source:
"token_counts"`, with only the token count and a complexity label from it. No prompt text,
hash or embedding is exported. `quality_score` stays null until the eval harness (#14)
scores sampled calls, and the routed decision per call comes with policy replay (#13).
