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

The SDK never picks a model. It expresses *intent*; the gateway owns the
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
   `pii_escalation` level of every PII category detected in the messages).
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
routing error also fails closed. A concrete model sent without a routing
hint is not routed, so it is not checked against the data policy.

### router/ (implemented)

`router/` runs **before** ps-telemetry and **before**
the cache middleware (so the cache key reflects the resolved model). A pluggable
`RouterStrategy` resolves a `RouteRequest` → `RouteDecision`:

| File | Responsibility |
|------|----------------|
| `router/types.ts` | `RouteRequest`, `RouteDecision`, `RouterStrategy`, `ModelTier`, `RoutePolicy` |
| `router/policy.ts` | `defaultPolicy`, token/cost estimation, `pickCheapestIn`, `cheapestUnderBudget` |
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
`defaultPolicy` pricing mirrors the SDK's `pricing.py`.

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
- `route_sensitivity_level` / `route_sensitivity_source` — the call's
  effective sensitivity level and `declared` / `default` / `pii_escalated`
  (only under a policy with the policy stages; OTel: `ps.route.sensitivity_level`,
  `ps.route.sensitivity_source`)
