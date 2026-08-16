# Prompt Shields Gateway Extensions

This directory contains Prompt Shields-specific middleware added on top of the Portkey AI Gateway.

## ps-telemetry.ts

Intercepts all LLM requests flowing through the gateway and sends discovery telemetry
to the Prompt Shields collector service. This enables zero-code-change AI asset discovery
for any application that routes LLM traffic through the gateway.

### How it works

1. **Before request:** Extracts `X-PS-*` headers for business metadata, strips them before forwarding
2. **After response:** Captures vendor, model, token usage, latency, and sends to collector
3. **Fail-open:** Telemetry failures never block LLM requests

### Configuration

Set these environment variables:
- `PS_COLLECTOR_URL` — URL of the Prompt Shields collector (default: `http://localhost:8000`)
- `PS_API_KEY` — Prompt Shields API key for authentication

### X-PS-* Headers

Applications can annotate requests with business context via HTTP headers:
- `X-PS-Business-Unit` — e.g., "HR", "Legal"
- `X-PS-Use-Case` — e.g., "interview-screening"
- `X-PS-Owner` — e.g., "jane.doe@acme.com"
- `X-PS-Data-Classification` — e.g., "confidential"
- `X-PS-Environment` — e.g., "production"

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
| `X-PS-Cache` | `off` | Opt this call out of the semantic cache (creative/non-deterministic). |

A request whose model is `auto` signals "gateway, you choose." A concrete
model name is a *soft preference* the router may downgrade to honor `X-PS-Max-Cost`.

### Response header (gateway → SDK)

- `X-PS-Served-Model` — the concrete model that actually ran. The SDK records
  it as `served_model` alongside `requested_model` so the collector can prove
  routing savings. (Providers also echo the served model in the response body's
  `model` field; the header is the authoritative source when they differ.)

### Resolution precedence

```
X-PS-Route (explicit override)
  >  X-PS-Quality / X-PS-Max-Cost (hints)
  >  gateway default policy (transparent)
```

### router/ (implemented)

`router/` runs **before** ps-telemetry (which strips `X-PS-*`) and **before**
the cache middleware (so the cache key reflects the resolved model). A pluggable
`RouterStrategy` resolves a `RouteRequest` → `RouteDecision`:

| File | Responsibility |
|------|----------------|
| `router/types.ts` | `RouteRequest`, `RouteDecision`, `RouterStrategy`, `ModelTier`, `RoutePolicy` |
| `router/policy.ts` | `defaultPolicy`, token/cost estimation, `pickCheapestIn`, `cheapestUnderBudget` |
| `router/heuristic.ts` | `HeuristicStrategy` — the ship-first decision function |
| `router/index.ts` | `parseRouteHeaders`, `buildRouteRequest`, `resolveRoute` (precedence) |
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
leaves the request unchanged.

Model groups (`cheap` / `balanced` / `frontier`) and the `defaultGroup` are
config-driven via `RoutePolicy`, overridable per virtual-key — this is where
"transparent-by-default" and "per-route override" live. `defaultPolicy` pricing
mirrors the SDK's `pricing.py`.

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

- parses token usage + served model from a non-streaming JSON body (streaming
  responses still emit an event, without usage);
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
