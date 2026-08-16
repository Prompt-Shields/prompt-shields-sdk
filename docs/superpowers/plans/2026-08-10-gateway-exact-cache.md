# Gateway Exact-Match Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Portkey fork's dormant simple cache into a Prompt-Shields-owned, always-mounted, bounded LRU+TTL cache controlled by an `X-PS-Cache` header and observable through `ps-telemetry`, so repeated non-streaming LLM requests skip the provider and save tokens.

**Architecture:** Replace the bare `inMemoryCache = {}` object in `src/middlewares/cache/index.ts` with an `InMemoryLruStore` behind an async `CacheStore` interface (shared-backend-ready). Resolve cache mode per request from the `X-PS-Cache` header / `PS_CACHE_DEFAULT` env (falling back to `providerOption.cache`) inside `RequestContext.cacheConfig`. Mount `memoryCache()` unconditionally (remove the `conf.cache` gate). Emit a cache telemetry event from `handlerUtils.ts` where `cacheStatus` is already computed.

**Tech Stack:** TypeScript, Hono, Jest (ts-jest). Run tests with `npx jest <path>`.

**Spec:** `docs/superpowers/specs/2026-08-10-gateway-exact-cache-design.md`

**Working directory for all paths:** `gateway/` (the fork). Run all `jest`/`git` commands from `gateway/`.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `src/middlewares/cache/store.ts` | `CacheStore` interface, `CacheEntry` type, `InMemoryLruStore` (LRU + lazy TTL, injectable `now`) | Create |
| `src/middlewares/cache/store.test.ts` | Unit tests for the store | Create |
| `src/middlewares/cache/resolveMode.ts` | Pure `resolveCacheMode(headers, env)` → `{ mode, forceRefresh }` | Create |
| `src/middlewares/cache/resolveMode.test.ts` | Unit tests for mode resolution | Create |
| `src/middlewares/cache/index.ts` | `memoryCache()` / `getFromCache` / `putInCache` — delegate to store; fix stream guard | Modify |
| `src/handlers/services/requestContext.ts` | `cacheConfig` getter — apply `resolveCacheMode` precedence; carry `forceRefresh` | Modify (`:162`) |
| `src/handlers/services/cacheService.ts` | Inject `x-portkey-cache-force-refresh` header when `forceRefresh` | Modify (`:87`) |
| `src/index.ts` | Mount `memoryCache()` unconditionally (remove `conf.cache` gate) | Modify (`:108`) |
| `src/middlewares/ps-telemetry.ts` | Add `cache_status` / `est_tokens_saved` to `TelemetryEvent`; export `buildCacheEvent(cacheStatus, requestBody, vendor)` + `emitCacheTelemetry(cacheStatus, requestBody, vendor)` | Modify |
| `src/middlewares/ps-telemetry.test.ts` | Unit tests for `buildCacheEvent` | Create |
| `src/handlers/handlerUtils.ts` | Call `emitCacheTelemetry` right after `logObject.addCache(...)` | Modify (`:378`) |
| `src/middlewares/cache/cache.integration.test.ts` | End-to-end HIT / MISS / refresh / precedence | Create |

**Types note:** `CacheSettings` (returned by `cacheConfig`) gains an optional `forceRefresh?: boolean`. Locate its declaration with `grep -rn "interface CacheSettings\|type CacheSettings" src` and extend it in Task 4.

---

## Task 1: Bounded LRU + TTL store

**Files:**
- Create: `src/middlewares/cache/store.ts`
- Test: `src/middlewares/cache/store.test.ts`

- [ ] **Step 1: Write the failing tests**

```ts
// src/middlewares/cache/store.test.ts
import { InMemoryLruStore } from './store';

describe('InMemoryLruStore', () => {
  it('returns null on miss', async () => {
    const s = new InMemoryLruStore({ maxEntries: 10 });
    expect(await s.get('nope')).toBeNull();
  });

  it('stores and retrieves an entry', async () => {
    const s = new InMemoryLruStore({ maxEntries: 10 });
    await s.set('k', { responseBody: 'v', maxAge: null });
    expect((await s.get('k'))?.responseBody).toBe('v');
  });

  it('evicts least-recently-used past the cap', async () => {
    const s = new InMemoryLruStore({ maxEntries: 2 });
    await s.set('a', { responseBody: 'A', maxAge: null });
    await s.set('b', { responseBody: 'B', maxAge: null });
    await s.get('a');                                   // 'a' now most-recent
    await s.set('c', { responseBody: 'C', maxAge: null }); // evicts 'b'
    expect(await s.get('b')).toBeNull();
    expect((await s.get('a'))?.responseBody).toBe('A');
    expect((await s.get('c'))?.responseBody).toBe('C');
  });

  it('lazily expires entries past maxAge', async () => {
    let now = 1000;
    const s = new InMemoryLruStore({ maxEntries: 10, now: () => now });
    await s.set('k', { responseBody: 'v', maxAge: 1500 }); // absolute expiry
    expect((await s.get('k'))?.responseBody).toBe('v');
    now = 1600;
    expect(await s.get('k')).toBeNull();
  });

  it('delete and clear work', async () => {
    const s = new InMemoryLruStore({ maxEntries: 10 });
    await s.set('k', { responseBody: 'v', maxAge: null });
    await s.delete('k');
    expect(await s.get('k')).toBeNull();
    await s.set('k2', { responseBody: 'v2', maxAge: null });
    await s.clear();
    expect(await s.get('k2')).toBeNull();
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `npx jest src/middlewares/cache/store.test.ts`
Expected: FAIL — `Cannot find module './store'`.

- [ ] **Step 3: Write the store**

```ts
// src/middlewares/cache/store.ts

export interface CacheEntry {
  responseBody: string;
  maxAge: number | null; // absolute expiry timestamp in ms, or null = no expiry
}

export interface CacheStore {
  get(key: string): Promise<CacheEntry | null>;
  set(key: string, value: CacheEntry): Promise<void>;
  delete(key: string): Promise<void>;
  clear(): Promise<void>;
}

interface Options {
  maxEntries: number;
  now?: () => number;
}

// In-memory LRU + lazy TTL. A Map preserves insertion order; deleting and
// re-inserting a key moves it to the most-recent position.
export class InMemoryLruStore implements CacheStore {
  private map = new Map<string, CacheEntry>();
  private maxEntries: number;
  private now: () => number;

  constructor(opts: Options) {
    this.maxEntries = Math.max(1, opts.maxEntries);
    this.now = opts.now ?? Date.now;
  }

  async get(key: string): Promise<CacheEntry | null> {
    const entry = this.map.get(key);
    if (!entry) return null;
    if (entry.maxAge && entry.maxAge < this.now()) {
      this.map.delete(key);
      return null;
    }
    // refresh recency
    this.map.delete(key);
    this.map.set(key, entry);
    return entry;
  }

  async set(key: string, value: CacheEntry): Promise<void> {
    if (this.map.has(key)) this.map.delete(key);
    this.map.set(key, value);
    while (this.map.size > this.maxEntries) {
      const oldest = this.map.keys().next().value;
      if (oldest === undefined) break;
      this.map.delete(oldest);
    }
  }

  async delete(key: string): Promise<void> {
    this.map.delete(key);
  }

  async clear(): Promise<void> {
    this.map.clear();
  }
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `npx jest src/middlewares/cache/store.test.ts`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/middlewares/cache/store.ts src/middlewares/cache/store.test.ts
git commit -m "feat(gateway): bounded LRU+TTL cache store"
```

---

## Task 2: Delegate cache middleware to the store + fix stream guard

**Files:**
- Modify: `src/middlewares/cache/index.ts`
- Test: `src/middlewares/cache/index.test.ts` (create)

- [ ] **Step 1: Write the failing tests**

```ts
// src/middlewares/cache/index.test.ts
import { getFromCache, putInCache, __resetCacheForTests } from './index';

describe('cache index (store-backed)', () => {
  beforeEach(() => __resetCacheForTests());

  it('MISS then HIT for identical body+url', async () => {
    const body = { model: 'm', messages: [{ role: 'user', content: 'hi' }] };
    let [resp, status] = await getFromCache({}, {}, body, '/chat', '', 'simple', Date.now() + 10000);
    expect(status).toBe('MISS');
    await putInCache({}, {}, body, { ok: true }, '/chat', '', 'simple', Date.now() + 10000);
    [resp, status] = await getFromCache({}, {}, body, '/chat', '', 'simple', Date.now() + 10000);
    expect(status).toBe('HIT');
    expect(JSON.parse(resp as string)).toEqual({ ok: true });
  });

  it('does NOT cache streaming responses', async () => {
    const body = { model: 'm', stream: true };
    await putInCache({}, {}, body, { ok: true }, '/chat', '', 'simple', Date.now() + 10000);
    const [, status] = await getFromCache({}, {}, body, '/chat', '', 'simple', Date.now() + 10000);
    expect(status).toBe('MISS');
  });

  it('DOES cache when stream is explicitly false', async () => {
    const body = { model: 'm', stream: false };
    await putInCache({}, {}, body, { ok: true }, '/chat', '', 'simple', Date.now() + 10000);
    const [, status] = await getFromCache({}, {}, body, '/chat', '', 'simple', Date.now() + 10000);
    expect(status).toBe('HIT');
  });
});
```

> Note: `getFromCache`'s current signature is `(env, requestHeaders, requestBody, url, organisationId, cacheMode, cacheMaxAge)`. Keep it unchanged.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `npx jest src/middlewares/cache/index.test.ts`
Expected: FAIL — `__resetCacheForTests` is not exported; stream:false test fails against the old guard.

- [ ] **Step 3: Rewrite the store internals in `index.ts`**

Replace `const inMemoryCache: any = {};` and the two functions' bodies so they delegate to `InMemoryLruStore`. Keep the exported signatures and the SHA-256 `getCacheKey` exactly as-is.

```ts
import { InMemoryLruStore } from './store';

const store = new InMemoryLruStore({
  maxEntries: parseInt(process.env.PS_CACHE_MAX_ENTRIES || '1000', 10),
});

// test-only hook
export const __resetCacheForTests = () => { store.clear(); };
```

In `getFromCache`, replace the `inMemoryCache` read block with:

```ts
    const cacheKey = await getCacheKey(requestBody, url);
    const entry = await store.get(cacheKey);
    if (entry) return [entry.responseBody, CACHE_STATUS.HIT, cacheKey];
    return [null, CACHE_STATUS.MISS, null];
```

In `putInCache`, change the stream guard from `if (requestBody.stream)` to:

```ts
  if (requestBody.stream === true) {
    // Do not cache streamed responses
    return;
  }
```

and replace the `inMemoryCache[cacheKey] = {...}` write with:

```ts
  const cacheKey = await getCacheKey(requestBody, url);
  await store.set(cacheKey, {
    responseBody: JSON.stringify(responseBody),
    maxAge: cacheMaxAge,
  });
```

In `memoryCache()`, change the write-back guard `requestParams.stream === (false || undefined)` to `requestParams.stream !== true`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `npx jest src/middlewares/cache/index.test.ts`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the store tests too (no regressions)**

Run: `npx jest src/middlewares/cache/`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/middlewares/cache/index.ts src/middlewares/cache/index.test.ts
git commit -m "feat(gateway): back cache with LRU store; cache explicit stream:false"
```

---

## Task 3: Cache-mode resolution (pure function)

**Files:**
- Create: `src/middlewares/cache/resolveMode.ts`
- Test: `src/middlewares/cache/resolveMode.test.ts`

- [ ] **Step 1: Write the failing tests**

```ts
// src/middlewares/cache/resolveMode.test.ts
import { resolveCacheMode } from './resolveMode';

describe('resolveCacheMode', () => {
  it('header on -> simple', () => {
    expect(resolveCacheMode({ 'x-ps-cache': 'on' }, {})).toEqual({ mode: 'simple', forceRefresh: false });
  });
  it('header off -> DISABLED (overrides env default on)', () => {
    expect(resolveCacheMode({ 'x-ps-cache': 'off' }, { PS_CACHE_DEFAULT: 'on' }))
      .toEqual({ mode: 'DISABLED', forceRefresh: false });
  });
  it('header refresh -> simple + forceRefresh', () => {
    expect(resolveCacheMode({ 'x-ps-cache': 'refresh' }, {}))
      .toEqual({ mode: 'simple', forceRefresh: true });
  });
  it('no header, env on -> simple', () => {
    expect(resolveCacheMode({}, { PS_CACHE_DEFAULT: 'on' })).toEqual({ mode: 'simple', forceRefresh: false });
  });
  it('no header, no env -> null (defer to providerOption.cache)', () => {
    expect(resolveCacheMode({}, {})).toBeNull();
  });
  it('malformed header -> treated as default (null when no env)', () => {
    expect(resolveCacheMode({ 'x-ps-cache': 'banana' }, {})).toBeNull();
  });
});
```

- [ ] **Step 2: Run to verify fail**

Run: `npx jest src/middlewares/cache/resolveMode.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```ts
// src/middlewares/cache/resolveMode.ts

export interface ResolvedMode {
  mode: 'simple' | 'DISABLED';
  forceRefresh: boolean;
}

// Precedence: valid X-PS-Cache header > PS_CACHE_DEFAULT env > null (defer to providerOption.cache).
// Returns null when neither header nor env decides, so the caller keeps existing behavior.
export function resolveCacheMode(
  headers: Record<string, string | undefined>,
  env: { PS_CACHE_DEFAULT?: string }
): ResolvedMode | null {
  const raw = (headers['x-ps-cache'] || '').trim().toLowerCase();
  if (raw === 'on') return { mode: 'simple', forceRefresh: false };
  if (raw === 'off') return { mode: 'DISABLED', forceRefresh: false };
  if (raw === 'refresh') return { mode: 'simple', forceRefresh: true };
  // raw is empty or malformed -> fall through to env default
  const def = (env.PS_CACHE_DEFAULT || '').trim().toLowerCase();
  if (def === 'on') return { mode: 'simple', forceRefresh: false };
  if (def === 'off') return { mode: 'DISABLED', forceRefresh: false };
  return null;
}
```

- [ ] **Step 4: Run to verify pass**

Run: `npx jest src/middlewares/cache/resolveMode.test.ts`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/middlewares/cache/resolveMode.ts src/middlewares/cache/resolveMode.test.ts
git commit -m "feat(gateway): X-PS-Cache / PS_CACHE_DEFAULT mode resolution"
```

---

## Task 4: Wire mode resolution + refresh into request path

**Files:**
- Modify: `src/handlers/services/requestContext.ts` (`cacheConfig` getter, ~`:162`)
- Modify: `src/handlers/services/cacheService.ts` (`getCachedResponse`, ~`:87`)
- Modify: the `CacheSettings` type declaration (find via grep) — add `forceRefresh?: boolean`

- [ ] **Step 1: Extend the `CacheSettings` type**

Run `grep -rn "interface CacheSettings\|type CacheSettings" src` and add `forceRefresh?: boolean;` to it.

- [ ] **Step 2: Apply resolution in `cacheConfig` getter**

At the top of the `cacheConfig` getter in `requestContext.ts`, before reading `providerOption?.cache`, insert:

```ts
    const resolved = resolveCacheMode(this.requestHeaders, {
      PS_CACHE_DEFAULT: process.env.PS_CACHE_DEFAULT,
    });
    if (resolved) {
      return {
        mode: resolved.mode,
        maxAge: undefined,
        cacheStatus: resolved.mode === 'DISABLED' ? 'DISABLED' : 'MISS',
        forceRefresh: resolved.forceRefresh,
      };
    }
```

Add the import: `import { resolveCacheMode } from '../../middlewares/cache/resolveMode';`
Leave the existing `providerOption.cache` logic below as the fallback (unchanged).

- [ ] **Step 3: Short-circuit DISABLED + inject the force-refresh header in `cacheService.ts`**

> **Why the short-circuit matters (critical).** `getCachedResponse` currently gates only on
> `!(this.getFromCacheFunction && mode)` (`cacheService.ts:89`), and `mode === 'DISABLED'` is a
> truthy string. `getFromCache` never inspects `cacheMode` on the read path — it does a raw key
> lookup. Today this is harmless because the middleware isn't mounted when `conf.cache` is false.
> **Task 5 mounts it unconditionally**, so without this fix an `X-PS-Cache: off` request would still
> return a HIT off a key a prior `on` request populated — violating the spec. Fix the guard so
> `DISABLED` bypasses the read.

Replace `const { mode, maxAge } = context.cacheConfig;` with a single destructure that also
grabs `forceRefresh`, add the `DISABLED` short-circuit, and build force-refresh headers:

```ts
    const { mode, maxAge, forceRefresh } = context.cacheConfig as
      { mode: string; maxAge: number | undefined; forceRefresh?: boolean };

    // DISABLED (or falsy) mode must not read the cache, even though the middleware is mounted.
    if (!mode || mode === 'DISABLED') {
      return this.noCacheObject;
    }

    const mergedHeaders = {
      ...context.requestHeaders,
      ...headers,
      ...(forceRefresh ? { 'x-portkey-cache-force-refresh': 'true' } : {}),
    };
```

Keep the existing `if (!(this.getFromCacheFunction && mode))` guard below (still valid). Pass
`mergedHeaders` in place of the current `{ ...context.requestHeaders, ...headers }` argument to
`this.getFromCacheFunction(...)`.

> Also confirm `putInCache` is never reached for DISABLED writes: the `memoryCache()` write-back is
> already gated on `cacheMode === 'simple'` (`index.ts:98` / `:107`), so a DISABLED request writes
> nothing. No extra step needed there.

- [ ] **Step 4: Add focused tests for the DISABLED short-circuit and refresh path**

```ts
// src/handlers/services/cacheService.refresh.test.ts
// Two behaviors, both against the real CacheService(honoContext, hooksService):
//   stub hooksService = {}; hono context = { get: (k) => k === 'getFromCache' ? spy : undefined }.
//
// (a) DISABLED short-circuit: fake context with cacheConfig.mode = 'DISABLED'.
//     Call getCachedResponse -> expect result.cacheStatus === 'DISABLED' AND the spy
//     (getFromCache) was NEVER called.
//
// (b) refresh: fake context with cacheConfig = { mode:'simple', maxAge:undefined, forceRefresh:true },
//     endpoint:'chatComplete', requestHeaders:{}, transformedRequestBody:{model:'m'}.
//     Call getCachedResponse -> expect spy called once, and its 2nd arg (headers)
//     contains 'x-portkey-cache-force-refresh': 'true'.
```

Implement both concretely. `isEndpointCacheable('chatComplete')` returns true, so the read path is
reached for (b); for (a) the short-circuit must return before the spy.

- [ ] **Step 5: Run tests**

Run: `npx jest src/handlers/services/cacheService.refresh.test.ts`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/handlers/services/requestContext.ts src/handlers/services/cacheService.ts src/handlers/services/cacheService.refresh.test.ts
git commit -m "feat(gateway): honor X-PS-Cache mode + refresh in request path"
```

---

## Task 5: Mount the cache unconditionally (remove `conf.cache` gate)

**Files:**
- Modify: `src/index.ts:108-110`

- [ ] **Step 1: Remove the gate**

Change:

```ts
if (conf.cache === true) {
  app.use('*', memoryCache());
}
```

to:

```ts
// Cache mounts unconditionally; per-request X-PS-Cache / PS_CACHE_DEFAULT decides HIT vs DISABLED.
app.use('*', memoryCache());
```

- [ ] **Step 2: Verify the build/typecheck**

Run: `npx tsc --noEmit` (or the repo's build script if `tsc` flags unrelated pre-existing errors — in that case confirm no *new* errors reference `src/index.ts`).
Expected: no new errors from `src/index.ts`.

- [ ] **Step 3: Commit**

```bash
git add src/index.ts
git commit -m "feat(gateway): mount cache middleware unconditionally"
```

---

## Task 6: Cache telemetry

**Files:**
- Modify: `src/middlewares/ps-telemetry.ts` (extend `TelemetryEvent`; add `buildCacheEvent` + `emitCacheTelemetry`)
- Create: `src/middlewares/ps-telemetry.test.ts`
- Modify: `src/handlers/handlerUtils.ts` (~`:378`, after `logObject.addCache(...)`)

- [ ] **Step 1: Write the failing test**

```ts
// src/middlewares/ps-telemetry.test.ts
import { buildCacheEvent } from './ps-telemetry';

describe('buildCacheEvent', () => {
  it('estimates tokens saved on HIT (ceil chars/4)', () => {
    const body = { model: 'm', messages: [{ role: 'user', content: 'x'.repeat(40) }] };
    const ev = buildCacheEvent('HIT', body, 'anthropic');
    expect(ev.cache_status).toBe('HIT');
    expect(ev.vendor).toBe('anthropic');
    expect(ev.est_tokens_saved).toBe(Math.ceil(JSON.stringify(body).length / 4));
  });

  it('reports zero tokens saved on MISS', () => {
    const ev = buildCacheEvent('MISS', { model: 'm' }, 'openai');
    expect(ev.cache_status).toBe('MISS');
    expect(ev.est_tokens_saved).toBe(0);
  });
});
```

- [ ] **Step 2: Run to verify fail**

Run: `npx jest src/middlewares/ps-telemetry.test.ts`
Expected: FAIL — `buildCacheEvent` not exported.

- [ ] **Step 3: Extend `ps-telemetry.ts`**

Add to the `TelemetryEvent` interface:

```ts
  cache_status?: string;
  est_tokens_saved?: number;
```

Add near the other helpers:

```ts
export function buildCacheEvent(
  cacheStatus: string,
  requestBody: any,
  vendor: string
): TelemetryEvent {
  const isHit = cacheStatus === 'HIT' || cacheStatus === 'SEMANTIC HIT';
  const estTokens = isHit ? Math.ceil(JSON.stringify(requestBody ?? {}).length / 4) : 0;
  return {
    vendor,
    model: requestBody?.model || 'unknown',
    source: 'gateway',
    cache_status: cacheStatus,
    est_tokens_saved: estTokens,
  };
}

// Fail-open emit from a spot that already knows cacheStatus (handlerUtils).
export function emitCacheTelemetry(cacheStatus: string, requestBody: any, vendor: string): void {
  try {
    if (!cacheStatus || cacheStatus === 'DISABLED') return;
    sendTelemetry(buildCacheEvent(cacheStatus, requestBody, vendor)).catch(() => {});
  } catch {
    /* fail-open */
  }
}
```

Export both from the module's export list.

- [ ] **Step 4: Run to verify pass**

Run: `npx jest src/middlewares/ps-telemetry.test.ts`
Expected: PASS (2 tests).

- [ ] **Step 5: Wire the emit in `handlerUtils.ts`**

Immediately after:

```ts
  logObject.addCache(
    cacheResponseObject.cacheStatus,
    cacheResponseObject.cacheKey
  );
```

add:

```ts
  emitCacheTelemetry(
    cacheResponseObject.cacheStatus,
    requestContext.transformedRequestBody,
    requestContext.provider || 'unknown'
  );
```

> Use `||`, not `??`: the `provider` getter (`requestContext.ts:144`) returns `''` (empty string),
> not `undefined`, when unset — `'' ?? x` would keep the empty string.

Add the import: `import { emitCacheTelemetry } from '../middlewares/ps-telemetry';`
(`requestContext.transformedRequestBody` getter is at `:70`, `provider` at `:144` — both verified.)

- [ ] **Step 6: Typecheck + run cache tests**

Run: `npx tsc --noEmit` (confirm no new errors reference the edited files), then `npx jest src/middlewares/`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/middlewares/ps-telemetry.ts src/middlewares/ps-telemetry.test.ts src/handlers/handlerUtils.ts
git commit -m "feat(gateway): emit cache hit/miss telemetry with est tokens saved"
```

---

## Task 7: Integration test — end-to-end HIT / MISS / refresh / precedence

**Files:**
- Create: `src/middlewares/cache/cache.integration.test.ts`

This exercises the middleware + store + mode resolution together without a live provider, by driving `getFromCache`/`putInCache` directly.

> **Key-parity (read this before writing the test).** The SHA-256 key is
> `hash(JSON.stringify(body) + '-' + url)`. In the running gateway the **read** passes
> `context.endpoint` as `url` (`cacheService.ts:98`) and the **write** passes
> `requestOptions.providerOptions.rubeusURL`, which `logsService.ts:181,227` sets to
> `requestContext.endpoint` — **the same value**. So read and write keys agree on `url`. The
> remaining risk is the *body* arg: read uses `context.transformedRequestBody`, write uses
> `requestOptions.transformedRequest.body`. Task 7 Step 1b adds an assertion that these two are
> deep-equal for a representative request so a future refactor can't silently break HITs. (This is
> why the direct-call tests below use one `url` literal and one `body` object — matching the
> verified handler behavior, not masking a divergence.)

- [ ] **Step 1: Write the test**

```ts
// src/middlewares/cache/cache.integration.test.ts
import { getFromCache, putInCache, __resetCacheForTests } from './index';
import { resolveCacheMode } from './resolveMode';

const body = { model: 'm', messages: [{ role: 'user', content: 'hello world' }] };
const url = '/chat/completions';

describe('cache integration', () => {
  beforeEach(() => __resetCacheForTests());

  it('second identical request is a HIT', async () => {
    let [, status] = await getFromCache({}, {}, body, url, '', 'simple', Date.now() + 10000);
    expect(status).toBe('MISS');
    await putInCache({}, {}, body, { answer: 42 }, url, '', 'simple', Date.now() + 10000);
    [, status] = await getFromCache({}, {}, body, url, '', 'simple', Date.now() + 10000);
    expect(status).toBe('HIT');
  });

  it('X-PS-Cache: off resolves to DISABLED regardless of env', () => {
    expect(resolveCacheMode({ 'x-ps-cache': 'off' }, { PS_CACHE_DEFAULT: 'on' })!.mode).toBe('DISABLED');
  });

  it('PS_CACHE_DEFAULT=on caches when no header present', () => {
    expect(resolveCacheMode({}, { PS_CACHE_DEFAULT: 'on' })!.mode).toBe('simple');
  });

  it('refresh triggers force-refresh header behavior', async () => {
    // seed the cache
    await putInCache({}, {}, body, { answer: 1 }, url, '', 'simple', Date.now() + 10000);
    // force-refresh header makes getFromCache skip the read (REFRESH)
    const [, status] = await getFromCache(
      {}, { 'x-portkey-cache-force-refresh': 'true' }, body, url, '', 'simple', Date.now() + 10000
    );
    expect(status).toBe('REFRESH');
  });
});
```

- [ ] **Step 1b: Add the body-arg parity assertion**

In the same file, add a test documenting the read/write body sources agree in shape. Since both
ultimately serialize the transformed provider body, assert that a representative transformed body
round-trips to the same key via both call sites' `url` (`endpoint`) value:

```ts
  it('read and write derive the same key for identical endpoint+body', async () => {
    // Same endpoint string used by both paths (read: context.endpoint; write: rubeusURL===endpoint)
    const endpoint = 'chatComplete';
    await putInCache({}, {}, body, { answer: 7 }, endpoint, '', 'simple', Date.now() + 10000);
    const [, status] = await getFromCache({}, {}, body, endpoint, '', 'simple', Date.now() + 10000);
    expect(status).toBe('HIT'); // proves same-endpoint + same-body -> same key
  });
```

- [ ] **Step 2: Run**

Run: `npx jest src/middlewares/cache/cache.integration.test.ts`
Expected: PASS (5 tests).

- [ ] **Step 3: Run the full cache + telemetry suite**

Run: `npx jest src/middlewares/cache/ src/middlewares/ps-telemetry.test.ts`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/middlewares/cache/cache.integration.test.ts
git commit -m "test(gateway): cache HIT/MISS/refresh/precedence integration"
```

---

## Task 8: Docs — enablement + config

**Files:**
- Modify: `gateway/README.md` (the "What This Fork Strips" table — update the caching row)
- Modify: `gateway/conf.example.json` (remove or annotate the now-unused `"cache"` key)

- [ ] **Step 1: Update README**

In the strip table, change the "Smart caching (simple + semantic)" row to note that **simple/exact caching is now owned by PS** (enabled via `X-PS-Cache` / `PS_CACHE_DEFAULT`), and that **semantic** remains out (sub-project #2). Add a short "Cache" section documenting the two env vars and the `X-PS-Cache: on|off|refresh` header.

- [ ] **Step 2: Update conf.example.json**

Remove the top-level `"cache": false` key (now unused) or replace with a comment-adjacent note in the README that it no longer gates the middleware.

- [ ] **Step 3: Commit**

```bash
git add gateway/README.md gateway/conf.example.json
git commit -m "docs(gateway): document owned exact cache + X-PS-Cache controls"
```

---

## Done criteria

- `npx jest src/middlewares/cache/ src/middlewares/ps-telemetry.test.ts` all green.
- `memoryCache()` mounts unconditionally; a request with `X-PS-Cache: on` (or `PS_CACHE_DEFAULT=on`) produces a HIT on the second identical non-streaming request; `X-PS-Cache: off` never hits.
- Cache hit/miss events reach the collector via `ps-telemetry` (fail-open).
- No new `tsc --noEmit` errors introduced by the edited files.
