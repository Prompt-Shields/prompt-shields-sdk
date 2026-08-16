# Gateway Exact-Match Cache — Design

**Date:** 2026-08-10
**Status:** Approved (design)
**Repo:** `prompt-shields-sdk` (`gateway/`)
**Branch:** `feat/gateway-exact-cache`

## Context

The Prompt Shields Gateway is a discovery-focused fork of the Portkey AI Gateway.
The fork intentionally disabled Portkey's optimization features (smart caching, cost
routing) because discovery is read-only. We are now reversing that decision for
caching under strategy **A1: own the gateway, keep Portkey's provider plumbing, build
the optimization ourselves** — so the gateway both observes *and* reduces token spend,
without depending on an upstream Portkey.

This is **sub-project #1** of a larger effort ("replace Portkey with our own optimizing
gateway"). It is deliberately the smallest slice that ships real token savings and lays
the store/telemetry plumbing that later sub-projects reuse.

### Larger effort — sub-projects (for context only, not in scope here)

1. **Exact-match cache** — this spec.
2. **Semantic cache** — embed requests, vector store (collector already ships pgvector),
   cosine match, reuse the same middleware seam.
3. **Cost-aware routing** — honor `RouteHint` / `X-PS-*` headers to select cheaper model
   tiers. (A cost-routing commit already exists on `feat/cost-aware-routing`.)
4. **Widget wiring** (separate repo, `prompt-shields-safari-widget`) — point the Safari
   extension's `apiBaseURL` at the gateway, pass route hints, surface savings in the
   popup and the AI-SPM dashboard.

### Current state of the cache code

The Portkey cache pipeline is **present but dormant**, not deleted:

- `src/middlewares/cache/index.ts` — `memoryCache()` middleware, `getFromCache`,
  `putInCache`. Registered globally at `src/index.ts:109` (`app.use('*', memoryCache())`).
- Store is a bare module-level object: `const inMemoryCache: any = {}` — **unbounded, no
  LRU, no size cap**, per-instance, wiped on restart. TTL is checked lazily on read only.
- Key = `SHA-256(JSON.stringify(requestBody) + '-' + url)` — exact match. This matches the
  agreed "normalized exact match" strategy; no semantic matching.
- Read path is fully wired: `src/handlers/services/cacheService.ts` → `getCachedResponse()`
  calls the `getFromCache` function the middleware sets on context. Activates only when
  `context.cacheConfig.mode` is truthy.
- `cacheConfig` is derived in `src/handlers/services/requestContext.ts:162` from
  `providerOption?.cache` (mode `simple` | `DISABLED`, plus `maxAge`).
- `src/handlers/services/logsService.ts` already carries `cacheStatus` / `cacheMode` /
  `cacheMaxAge` fields.
- Config-disabled by default: `conf.example.json` has `"cache": false`.
- Streaming responses are already excluded from caching (`putInCache` early-returns on
  `requestBody.stream`). **Quirk:** the middleware write guard is
  `requestParams.stream === (false || undefined)`, i.e. `stream === undefined` — a request
  that sends `stream: false` *explicitly* is currently **not** written to cache.
- **`ps-telemetry` is defined but NOT wired.** `src/middlewares/ps-telemetry.ts` exports
  `psTelemetryMiddleware` / `sendTelemetry` / `TelemetryEvent`, but nothing imports or
  registers it anywhere in `src/`. Its functions take plain `{url, headers, body}` objects,
  not a Hono `Context`, so as written it has no handle to the `cacheStatus` computed in the
  request pipeline (`logsService.addCache`, called from `handlerUtils.ts`). Any telemetry
  emission this spec relies on must be *newly wired*, not merely extended.

## Goal

Turn the dormant simple cache into a production token-saver Prompt Shields owns:
**bounded, TTL'd, header-controllable, and observable through `ps-telemetry`** — saving
tokens on repeated/identical non-streaming LLM requests, with an interface ready for a
shared/distributed backend later.

## Non-goals (explicitly out of scope)

- Semantic / near-match caching (sub-project #2).
- Cost-aware routing (sub-project #3).
- Safari widget wiring (sub-project #4).
- A shared/distributed backend (Redis, pgvector). We ship **in-memory LRU** now, but the
  store interface is designed so a shared backend drops in without touching call sites.
- Caching of streaming responses (remains bypassed).

## Design

### Component 1 — Bounded store (`src/middlewares/cache/store.ts`, new)

A small, self-contained LRU + TTL store replacing the bare `inMemoryCache` object.

- **Interface** (the swappable seam):
  ```ts
  interface CacheStore {
    get(key: string): Promise<CacheEntry | null>;   // returns null on miss OR expired
    set(key: string, value: CacheEntry): Promise<void>;
    delete(key: string): Promise<void>;
    clear(): Promise<void>;                          // test/maintenance
  }
  interface CacheEntry { responseBody: string; maxAge: number | null; } // maxAge = absolute expiry ms
  ```
  Async signatures from the start so a Redis/pgvector backend implements the same
  interface without call-site changes.
- **`InMemoryLruStore implements CacheStore`:**
  - Bounded by entry count: `PS_CACHE_MAX_ENTRIES` (default 1000). On `set` past cap,
    evict least-recently-used.
  - LRU recency updated on `get` and `set` (e.g. `Map` insertion-order reordering, or a
    ring — implementation detail, covered by tests, not fixed here).
  - Lazy TTL: `get` returns `null` and deletes the entry when `maxAge && maxAge < now`.
    (Absolute expiry is stamped at `set` time by the caller, as today.)
- **`cache/index.ts` delegates** to a single module-level `InMemoryLruStore` instance
  instead of the bare object. `getFromCache` / `putInCache` keep their existing exported
  signatures so `cacheService.ts` and the middleware are unchanged. The SHA-256 keying is
  unchanged.
- **Time source:** injectable `now()` (default `Date.now`) so tests control TTL/eviction
  deterministically.

### Component 2 — Enable + header control

- **`X-PS-Cache` header** parsed to drive `cacheConfig.mode`:
  - `on` → mode `simple` (read + write).
  - `off` → mode `DISABLED`.
  - `refresh` → mode `simple` **and** inject the existing `x-portkey-cache-force-refresh`
    request header. Both are required: `getFromCache` returns REFRESH (skips the read) only
    when that header is present (`index.ts:38-40`), while `putInCache` writes back only when
    `cacheMode === 'simple'` (`index.ts:98`). So `refresh` = bypass read, still repopulate.
  - malformed / unknown value → treated as the resolved default (below).
  Parsing lives alongside the other `X-PS-*` header handling; the resolved mode flows into
  `requestContext.cacheConfig` so the existing read/write path activates. This maps onto
  the SDK's existing contract: `RouteHint(allow_cache=False)` already emits `X-PS-Cache: off`.
- **Mode-resolution precedence** (highest wins) — pinned to remove ambiguity between the
  three existing inputs and the new ones:
  1. `X-PS-Cache` request header, if present and valid.
  2. Else `PS_CACHE_DEFAULT` env (`on` | `off`, default `off`).
  3. `providerOption.cache` (the config-file path via `requestContext.ts:163`) is honored as
     today only when neither of the above applies; when it yields a mode we keep it.
- **Master gate — must be handled.** The top-level `conf` `"cache"` boolean is **not** a dead
  flag: `src/index.ts:108` mounts `memoryCache()` only when `conf.cache === true`, and
  `memoryCache()` is the sole thing that sets `getFromCache` on context — without it
  `CacheService.getCachedResponse` bails (`cacheService.ts:89`). Since `conf.example.json`
  ships `"cache": false`, the whole read/write path (and therefore all `X-PS-Cache` /
  `PS_CACHE_DEFAULT` resolution above) is **inert by default**. This sub-project must make the
  middleware always mount and move the on/off decision into per-request mode resolution:
  **remove the `index.ts:108` `conf.cache` gate so `memoryCache()` mounts unconditionally**
  (the store is cheap and idle when every request resolves to `DISABLED`). `conf.cache` is
  then retired from the enablement path; `PS_CACHE_DEFAULT` becomes the deployment-level
  default. (Alternative: keep the gate but default `conf.cache=true`; rejected because it
  leaves two competing global switches — `conf.cache` and `PS_CACHE_DEFAULT`.)
- **Streaming stays uncached** regardless of header. As part of this sub-project, fix the
  write guard from `stream === (false || undefined)` to `stream !== true`, so an explicit
  `stream: false` is cached like an absent field. (Without this, the "identical requests →
  HIT" integration test is client-payload-dependent.)
- **Default via env:** `PS_CACHE_DEFAULT` (`off` default) so caching is opt-in per deployment.

### Component 3 — Observability via `ps-telemetry`

The gateway computes `cacheStatus` (HIT / MISS / SEMANTIC\* / REFRESH / DISABLED) and
threads it through `logsService` (`addCache`). But `ps-telemetry.ts` is **not currently
wired**, and its functions take plain objects with no handle to the Hono context where
`cacheStatus` lives. This component therefore does real wiring, not a one-line extension:

- **Emission point:** emit the cache event from where `cacheStatus` is already known — the
  request handler / `logsService` path that calls `addCache` — rather than from the
  context-less `ps-telemetry` functions. Concretely: register a telemetry emission seam
  (Hono middleware or a `logsService` post-hook) that reads the resolved `cacheStatus` from
  context and calls `sendTelemetry`. The plan must choose one of these two mechanisms
  explicitly; the design constraint is only that the emitter has access to both the Hono
  `Context` and the computed `cacheStatus`.
- **New event fields** (extend `TelemetryEvent` in `ps-telemetry.ts` and the collector
  contract):
  - `cache_status` — the resolved status string.
  - `est_tokens_saved` — on HIT only, estimated as `ceil(chars / 4)` over the request body
    (dependency-free rough estimate; good enough for relative savings roll-ups). MISS/other → 0.
- **Fail-open:** any telemetry/store error is swallowed and never blocks or alters the
  proxied response. (Existing `getFromCache` already try/catches to MISS; the store and
  telemetry additions follow the same rule.)

## Data flow

```
memoryCache() mounts unconditionally (conf.cache gate removed)
request → [X-PS-Cache parse → cacheConfig.mode]
        → CacheService.getCachedResponse()
             → getFromCache() → InMemoryLruStore.get()
                  ├─ HIT     → return cached Response, cacheStatus=HIT, skip provider
                  └─ MISS    → proceed to provider
        → provider response
        → memoryCache() middleware → putInCache() → InMemoryLruStore.set() (non-stream, mode=simple)
        → ps-telemetry: { cache_status, est_tokens_saved } → collector
```

## Error handling / fail-open

- Store `get`/`set` errors → treated as MISS / no-op; request proceeds to provider.
- Telemetry errors → swallowed; response unaffected.
- Malformed `X-PS-Cache` value → treated as default (`off`).
- Never cache when `mode` is falsy or endpoint is non-cacheable (existing
  `isEndpointCacheable` guard retained).

## Testing

**Unit**
- `InMemoryLruStore`: LRU eviction at cap; recency updated on get and set; lazy TTL expiry
  (with injected `now`); `delete` / `clear`; miss returns null.
- `X-PS-Cache` mapping: `on`→simple, `off`/absent→DISABLED, `refresh`→REFRESH bypass;
  malformed→default.
- Key stability: identical body+url → identical key; differing body → different key.
- Stream bypass: `stream:true` never written; `stream:false` **is** written (after the
  `stream !== true` guard fix); absent `stream` is written.

**Integration**
- Two identical non-stream requests → 2nd is HIT, provider called once.
- `X-PS-Cache: off` → always MISS, provider called each time.
- `X-PS-Cache: refresh` → read bypassed, provider called, cache repopulated (mode `simple`
  + `x-portkey-cache-force-refresh` injected).
- Mode precedence: header overrides `PS_CACHE_DEFAULT`; `PS_CACHE_DEFAULT=on` caches with no
  header present.
- Telemetry event carries `cache_status` and non-zero `est_tokens_saved` on HIT.

## Config summary (new env vars)

| Var | Default | Purpose |
|---|---|---|
| `PS_CACHE_DEFAULT` | `off` | Default cache mode when no `X-PS-Cache` header. Replaces `conf.cache` as the deployment default. |
| `PS_CACHE_MAX_ENTRIES` | `1000` | LRU entry cap for the in-memory store. |

The `conf.cache` boolean gate at `src/index.ts:108` is removed; `memoryCache()` mounts
unconditionally and idles when requests resolve to `DISABLED`.

## Open questions for implementation (resolve in plan)

- Exact LRU data structure (ordered `Map` vs explicit list) — behavior fixed by tests.
- Precise location of `X-PS-Cache` parsing relative to existing `X-PS-*` header handling.
- Telemetry emission mechanism (Hono middleware vs `logsService` post-hook) — either is
  acceptable per Component 3; pick one in the plan.
