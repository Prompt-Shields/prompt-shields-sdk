# Prompt Shields Gateway Extensions

This directory contains Prompt Shields-specific middleware added on top of the Portkey AI Gateway.

## ps-telemetry.ts

Intercepts all LLM requests flowing through the gateway and sends discovery telemetry
to the Prompt Shields collector service. This enables zero-code-change AI asset discovery
for any application that routes LLM traffic through the gateway.

### How it works

1. **Before request:** Reads `X-PS-*` headers for business metadata from the incoming request. The
   gateway strips every `X-PS-*` header (any case) from the upstream request where upstream headers
   are finalised (`stripPSInternalHeaders` in `src/utils/psInternalHeaders.ts`, applied in
   `constructRequestHeaders` and the realtime handlers), so they never reach the provider
2. **After response:** Captures vendor, model, token usage, latency, and sends to collector
3. **Fail-open:** Telemetry failures never block LLM requests

### Configuration

Set these environment variables:
- `PS_COLLECTOR_URL` — URL of the Prompt Shields collector (default: `http://localhost:8000`)
  It must pass `isSafeUpstreamURL`, and redirects are not followed. Public
  hosts and in-cluster service names (`http://prompt-shields-collector:8000`,
  `*.svc.cluster.local`) work as they are. A loopback host (`localhost`,
  `127.0.0.1`) works only while `TRUSTED_CUSTOM_HOSTS` is unset or lists it,
  and an IP literal in a private range (`10.0.0.5`) only when it is listed.
  Otherwise events are dropped (`send_failed`, one warning in the log).
- `PS_API_KEY` — Prompt Shields API key for authentication
- `PS_TELEMETRY_MAX_ATTEMPTS` — collector send attempts per event (default 3);
  `PS_TELEMETRY_RETRY_BASE_MS` — first retry delay, doubled per attempt
  (default 200; a 429 waits its `Retry-After`, at most 300 s);
  `PS_TELEMETRY_MAX_PENDING` — events awaiting delivery before new ones are
  dropped (default 1000)

### Event-loss accounting

Like the Python SDK, every collector request carries a `metadata` object
(`psTelemetrySender.ts`): `client_id` (random, stable for the process),
`seq` (monotonic per request body, a retry included), `enqueued_total` and
`dropped` — cumulative events that never reached the collector, by reason
(`buffer_overflow`, `send_failed`, `rate_limited`, `server_skipped`,
`serialization_failed`) — with `sdk: "gateway"`. The collector reconciles
them in `GET /telemetry/loss` and reports gateway coverage as its own source.

### X-PS-* Headers

Applications can annotate requests with business context via HTTP headers:
- `X-PS-Business-Unit` — e.g., "HR", "Legal"
- `X-PS-Use-Case` — e.g., "interview-screening"
- `X-PS-Owner` — e.g., "jane.doe@acme.com"
- `X-PS-Data-Classification` — e.g., "confidential"
- `X-PS-Environment` — e.g., "production"

## psOtel.ts (OpenTelemetry GenAI spans, #70)

Opt-in. With `PS_OTEL_ENDPOINT` (or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`) set,
the gateway emits one CLIENT span per LLM call as OTLP/HTTP JSON, using GenAI
semantic conventions 1.40: `gen_ai.operation.name`, `gen_ai.provider.name`
(`aws.bedrock`, `gcp.vertex_ai`, ...), `gen_ai.request.model` and sampling
params, `gen_ai.response.model`/`id`/`finish_reasons` (normalised to `stop`,
`length`, `tool_call`, `content_filter`, `error`), `gen_ai.usage.*` and
`error.type`. `X-PS-*` business context goes to `ps.*` span attributes and
`X-PS-Environment` to the `deployment.environment.name` resource attribute.
An inbound `traceparent` makes the span a child of the caller's span.

- `PS_OTEL_ENDPOINT` — full traces URL, e.g. `http://collector:8000/ingest/otel/v1/traces`
- `PS_OTEL_HEADERS` / `OTEL_EXPORTER_OTLP_TRACES_HEADERS` — `k=v,k2=v2`; when the endpoint comes from `PS_OTEL_ENDPOINT`, `PS_API_KEY` is sent as a bearer token unless an `Authorization` header is given. It is never added for an endpoint taken from `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`.
- `OTEL_SERVICE_NAME` — default `prompt-shields-gateway`
- `PS_OTEL_CAPTURE_CONTENT` / `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` — `true` adds `gen_ai.input.messages`, `gen_ai.output.messages` and `gen_ai.system_instructions` (non-streamed calls only). Off by default.
- `PS_OTEL_MAX_BATCH` (64), `PS_OTEL_FLUSH_MS` (5000), `PS_OTEL_MAX_QUEUE` (2048) — batching on Node. On Workers each request flushes the queue under `waitUntil`.

Export never blocks or fails a request; failed batches are dropped.
The endpoint must pass `isSafeUpstreamURL` (list a private or loopback host in
`TRUSTED_CUSTOM_HOSTS`), and redirects are never followed. Spans for a refused
endpoint are dropped and counted in
`ps_gateway_telemetry_dropped_total{sink="otel",reason="unsafe_endpoint"}`.

**Do not point this exporter at the Prompt Shields collector while `PS_API_KEY`
telemetry (`ps-telemetry.ts`) is also on.** Both paths ingest the same call, so
usage and token counts are doubled, and because this exporter uses semconv
provider names (`aws.bedrock`) while `/ingest/events` uses gateway ids
(`bedrock`), some calls also show up as two separate assets. Use OTel for a
third-party backend, or for the collector instead of `PS_API_KEY` telemetry. The
gateway logs a warning at startup when both target the collector.

## Cost-aware routing (spec)

User-facing guide, with a worked policy and the current limits:
`docs/guides/routing.mdx`. In short, what the gateway router does **not** do
yet:

- **Load a route policy from configuration.** `src/index.ts` mounts
  `psRouter()` with `defaultPolicy`, which has no `sensitivity`, `dataPolicy`
  or `useCases` section, so the policy stages and the data-policy gate below
  are off in a stock gateway. They run only with
  `psRouter(fromRoutePolicyDoc(doc))` in code.
- **Fail over between routed models (#21).** `psFailover/` (engine and
  per-provider circuit breaker) is not in the request path; the router picks
  one model. Fallback targets in a gateway config still work, are checked by
  the data-policy gate when one is active, and are recorded in
  `fallback_chain`.

The SDK's ordinary clients never pick a model (`ShieldsRouter` is the opt-in
exception). They express *intent*; the gateway owns the
decision, the model catalog, pricing, and policy. This keeps routing in one
place instead of scattered across every service.

**Design:** explicit hints in the SDK, **transparent-by-default at the gateway
with a per-route override.**

### Routing headers (SDK → gateway)

| Header | Values | Meaning |
|--------|--------|---------|
| `X-PS-Quality` | `draft` \| `balanced` \| `critical` | Intent, not a model name. Gateway maps it to a model group. |
| `X-PS-Max-Cost` | USD float, e.g. `0.02` | Per-call ceiling. The router downgrades to stay under it. |
| `X-PS-Route` | group name, e.g. `frontier` | Explicit override — bypasses the router and pins a group. |
| `X-PS-Cache` | `on` \| `off` \| `refresh` | The exact-match response cache (`PS_CACHE_DEFAULT` applies when absent); not part of routing. |
| `X-PS-Data-Classification` | a sensitivity level, e.g. `confidential` | Declared level for the policy stages below (also telemetry). Does not engage routing by itself. |
| `X-PS-Use-Case` | use-case name | Selects the policy's per-use-case quality bar (also telemetry). |

A request whose model is `auto` signals "gateway, you choose." A concrete
model name is a *soft preference* the router may downgrade to honor `X-PS-Max-Cost`.

### Response header (gateway → SDK)

- `X-PS-Served-Model` — **not implemented.** The gateway does not set this
  header. The served model is in the response body's `model` field, and the
  gateway's own telemetry records it as `served_model` alongside
  `requested_model`.

### Resolution precedence

```
data policy (sensitivity level -> allowed candidates; fail closed)
  >  X-PS-Route (explicit override)
  >  X-PS-Quality / X-PS-Max-Cost (hints)
  >  use-case quality bar (X-PS-Use-Case)
  >  gateway default policy (transparent)
```

### Policy stages (sensitivity, data policy, quality bar)

They run only when the `RoutePolicy` has a `sensitivity`, `dataPolicy` or
`useCases` section (route-policy.v1 `sensitivity`, `data_policy`,
`use_cases`; `fromRoutePolicyDoc` converts a v1 document). Without them
routing is unchanged. `resolveRoute` then calls `resolvePolicyRoute`, a port
of `resolve_policy_route` in `packages/sdk/prompt_shields/route_policy.py`;
both pass the golden vectors in `schemas/route-policy/vectors/`.

1. **Sensitivity (#18).** Level = max(`X-PS-Data-Classification`, or
   `unclassified_default` when it is missing or not a known level, the
   `pii_escalation` level of every PII category detected in the request text).
   Escalate-only. Detection (`router/pii.ts`) is a port of the SDK's pattern
   detector (`pii.py`, `national_ids.py`) and runs only when the policy maps
   PII categories. The source is `declared`, `default` or `pii_escalated`.
2. **Data policy (#19).** Only candidates that meet the level's rule are
   considered, in every group and in the `X-PS-Max-Cost` clamp. Facts come
   from the policy's model tiers (`provider`, `jurisdiction`, `zdr`,
   `usCloudActExposure`); a missing fact fails closed (`any` jurisdiction
   aside). `EEA` in a rule allows EEA member countries. A custom
   `RouterStrategy` only sees allowed tiers.
3. **Quality bar (#23).** Off the explicit path, `minGroup` raises the group
   (by `groupOrder`) and an `evalSet` limits candidates to `eligibleModels`.

No allowed candidate is a `PolicyViolation`: `router/hono.ts` answers 403
(`error.type: permission_error`, `error.code: policy_violation`) before any
upstream call and records a `policy.denied` security event
(`no_compliant_candidate`). Under a policy with a `dataPolicy`, any other
routing error also fails closed.

The data policy also applies to calls the router does not decide
(`router/dataPolicyGate.ts`): a concrete model with no hint, a config
`override_params.model` (or fallback / load-balance target) that replaces
the routed model, and every model endpoint (Responses, Messages,
completions, embeddings, images, audio, proxied bodies naming a model;
realtime via its `model` query). Under a policy with a `dataPolicy` section
the Hono adapter classifies every request (PII found in any request text:
messages, `system`, Responses `input` / `instructions`, `prompt`, tool
arguments and outputs) and `tryPost` checks the model each target actually
runs (after `override_params`, plus the Azure deployment name) before hooks,
cache or upstream. The facts come from the policy tiers that list that
model name; a model the policy does not list has no facts and fails closed
(only a rule allowing `any` jurisdiction with no other condition lets it
through), so list Azure deployment names as tier models. Same 403 and
security event as above. Calls with no model on a non-model endpoint
(files, batches) are not checked.

### router/ (implemented)

`router/` runs **before** ps-telemetry and **before**
the cache middleware (so the cache key reflects the resolved model). A pluggable
`RouterStrategy` resolves a `RouteRequest` → `RouteDecision`:

| File | Responsibility |
|------|----------------|
| `router/types.ts` | `RouteRequest`, `RouteDecision`, `RouterStrategy`, `ModelTier`, `RoutePolicy` |
| `router/policy.ts` | `defaultPolicy` (from the generated `router/defaultPolicy.json`), token/cost estimation, `pickCheapestIn`, `cheapestUnderBudget` |
| `router/heuristic.ts` | `HeuristicStrategy` — the ship-first decision function |
| `router/index.ts` | `parseRouteHeaders`, `buildRouteRequest`, `resolveRoute` (precedence), `resolvePolicyRoute` |
| `router/constraints.ts` | `classifySensitivity`, `tierComplies`, `PolicyViolation`, `fromRoutePolicyDoc` |
| `router/pii.ts` | `detectPiiCategories` — PII categories for sensitivity escalation |
| `router/middleware.ts` | `psRouterMiddleware` — `beforeRequest` hook that rewrites `body.model` |

- `HeuristicStrategy` (shipped): input-token count, code-fence / JSON-schema
  detection → cheap vs. frontier group; clamps down across groups under
  `X-PS-Max-Cost`. <1ms, explainable.
- `LearnedStrategy` (later): embed → classifier (RouteLLM-style), same
  `RouterStrategy` interface, trained on the gateway's own telemetry. Swap it
  into `psRouterMiddleware(policy, strategy)` with no handler changes.

**Engagement:** transparent routing engages only when the request model is
`auto`; a concrete model with no hint is left untouched. Any `X-PS-*` hint
(quality / max-cost / route) also engages it. Fail-open — any routing error
leaves the request unchanged, except under a data policy (fail closed, above).

Model groups (`cheap` / `balanced` / `frontier`) and the `defaultGroup` are
defined by a `RoutePolicy`. The mounted router uses `defaultPolicy`; there is
no per-deployment or per-key policy configuration yet (`psRouter(policy)`
takes one in code). The per-request override is `X-PS-Route`.
`defaultPolicy` is generated from the model catalog
(`router/defaultPolicy.json`, written by `scripts/build_catalog.py` from
`schemas/catalog/source.v1.json`), so its prices match the SDK's. Do not
edit the JSON by hand; CI fails on drift (`docs/guides/model-catalog.mdx`).

**Wiring (done):** `router/hono.ts` exports `psRouter()`, a Hono middleware
mounted in `src/index.ts` via `app.use('*', psRouter())`, gated on
`PS_ROUTER_ENABLED=true`. It runs **before** `hooks` and `memoryCache` so the
resolved model flows into cache-key computation and the downstream handler.

Hono 4.x re-parses the body on each `c.req.json()`, so in-place mutation does
not propagate; the adapter overwrites `c.req.bodyCache.json` with the rewritten
object (only when a decision was made). The decision is also exposed on the
context as `c.get('psRoute')` for a telemetry middleware to read.

`ps-telemetry.ts` also exports `psTelemetry()`, a Hono middleware mounted in
`src/index.ts` (`app.use('*', psTelemetry())`) **after** `psRouter`, gated on
`PS_API_KEY` being set. After the response is produced it:

- parses token usage + served model from a non-streaming JSON body; for a
  streamed response it reads them from the stream's final events and sends the
  event once the stream has ended (`psStreamObserver.ts`);
- merges business `X-PS-*` headers (`extractPSHeaders`);
- folds in the routing decision from `c.get('psRoute')` →
  `requested_model` / `served_model` / `route_group` / `route_reason` / `route_est_cost`.

Fire-and-forget and fail-open — never affects the response. Only POST traffic is
reported. The original `psTelemetryMiddleware` (`beforeRequest`/`afterResponse`)
is retained for non-Hono callers.

### Routing telemetry fields (gateway → collector)

When the router made a decision, the telemetry event carries:

- `requested_model` — what the caller asked for (e.g. `auto`)
- `served_model` — what actually ran (provider-echoed model, else the decision)
- `route_group` — the resolved group (`cheap` / `frontier` / ...)
- `route_reason` — why (`heuristic:simple`, `quality:critical`, `override`, `...+budget-clamp`)
- `route_est_cost` — the router's pre-flight USD estimate
- `baseline_id` / `counterfactual_cost` — with `PS_BASELINE_FILE` set to a
  frozen `baseline.v1` document (`schemas/baseline/`, written by the SDK's
  `freeze_baseline`), the baseline's id and what the call's tokens would have
  cost on the use case's baseline model at the baseline's frozen prices
  (`router/baseline.ts`, same rules and shared vectors as the SDK). Added at
  send time, so streamed calls are priced on their final usage. An invalid
  file (or one over 1 MiB) stops the gateway at startup.
- `route_sensitivity_level` / `route_sensitivity_source` — the call's
  effective sensitivity level and `declared` / `default` / `pii_escalated`
  (only under a policy with the policy stages; OTel: `ps.route.sensitivity_level`,
  `ps.route.sensitivity_source`)
- `route_mode` — `gateway` (the PRD 0001 §7.12 value for a gateway decision)

When the gateway recorded upstream attempts (retries, fallback targets), the
event also carries `fallback_chain`: the error class of each failed attempt,
in order (`["server_error", "rate_limited"]`), or `[]` when the first attempt
succeeded. Classes only, never error messages. The collector stores all of
these on `ai_usage_events` (#25).

## psBudget/ (spend budgets, #28)

A `budget.v1` document (`schemas/budget/`, shared with the SDK: same
document, Redis counter keys, TTL rule and decision rules, checked against
`schemas/budget/vectors/`) inline in `PS_BUDGETS` or in a file named by
`PS_BUDGETS_FILE`; an invalid document stops the gateway at startup. Budgets
are per business unit (`X-PS-Business-Unit`), use case (`X-PS-Use-Case`),
owner (`X-PS-Owner`, for team budgets) or the whole deployment, by UTC day,
ISO week or month.

| Variable | Default | Meaning |
|---|---|---|
| `PS_BUDGET_REDIS_URL` | `REDIS_CONNECTION_STRING` | The Redis every replica and SDK process shares counters through. Unset: in-memory, per replica |
| `PS_BUDGET_NAMESPACE` | the document's `namespace` | Overrides the counter namespace |
| `PS_BUDGET_RESERVATION_TTL_MS` | `900000` | How long an unsettled reservation holds budget (a replica that dies mid-call) |
| `PS_BUDGET_REDIS_TIMEOUT_MS` | `500` | Per-command Redis timeout |

Per billed call (chat, completions, messages, responses, embeddings, rerank,
images, audio, OCR) the middleware, mounted after the router and before
telemetry, reads the matching counters, decides, reserves the call's
estimate (the router's `route_est_cost`, else the pricing table on the
request) and, once the response or stream ends, settles the actual cost,
priced exactly as telemetry prices it (a failed call or a cache hit costs 0;
an unpriced call keeps its estimate).

- `warn`: never changes the call.
- `downgrade`: the least remaining amount of the matching downgrade budgets
  is a cost ceiling; a call estimated above it moves to the most capable
  cheaper model under it, else the cheapest, among the models the data policy
  (and a use-case eval set) allows; never a non-compliant one. The route
  reason gets `+budget-downgrade`.
- `block`: an exhausted budget answers HTTP 429 `budget_exceeded` with
  `budget_id`, `resets_at` and `Retry-After`.
- `on_store_error`: `open` serves calls with `budget_state: unknown` while
  Redis is unreachable; `closed` answers 503 `budget_store_unavailable` for
  block budgets and treats downgrade budgets as exhausted.

Gateway extension to the shared format: each call's estimate is reserved in a
sorted set at `{<counter key>}:res` (members `<uuid>|<usd>`, scored by expiry;
the hash tag keeps it in the counter's Redis Cluster slot). Reads add live
reservations to the counter, and a block budget's reservation is an atomic
check in one Lua script, so calls racing on several replicas see each other
and a limit is overrun by at most the estimate error of the calls admitted
before it. The SDK ignores the set.

Responses carry `x-ps-budget-state`; telemetry events carry `budget_state`
(`ok`, `warn`, `unknown`, `exceeded`), `budget_id` and `budget_action`
(`none`, `downgraded`). Prometheus (with `PS_METRICS_ENABLED`):
`ps_gateway_budget_decisions_total{budget,state,action}`,
`ps_gateway_budget_spend_usd{budget,window}` (budgets with a concrete
subject), `ps_gateway_budget_limit_usd{budget,window}` and
`ps_gateway_budget_store_errors_total{store,op}`.

The attributes are declared by the caller; a caller that can change them can
move spend between budgets. A `global` budget cannot be dodged that way.
