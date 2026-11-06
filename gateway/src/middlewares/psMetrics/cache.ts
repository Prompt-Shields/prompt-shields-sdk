import { Counter, defaultRegistry, Gauge } from './registry';

/**
 * Response cache metrics (#33). Kept apart from `./index` so the cache
 * modules can count without importing the request middleware.
 *
 * Label values are bounded: statuses, modes, outcomes and bypass reasons
 * come from fixed lists; provider and model go through `labelValue` and the
 * registry's series cap.
 */
export const cacheResponsesTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_cache_responses_total',
    'Responses by cache status (hit, miss, semantic_hit, semantic_miss, semantic_bypass, refresh).',
    ['status']
  )
);
export const cacheWritesTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_cache_writes_total',
    'Cache write-backs by mode (simple, semantic) and outcome: stored, or skipped as refusal, incomplete, too_large or error.',
    ['mode', 'outcome']
  )
);
export const semanticBypassTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_semantic_cache_bypass_total',
    'Requests in semantic cache mode that neither read nor wrote the cache, by reason.',
    ['reason']
  )
);
export const cacheSavedCostTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_cache_saved_cost_usd_total',
    'Upstream cost avoided by cache hits (USD), priced from the cached response usage.',
    ['provider', 'model', 'status']
  )
);
export const semanticEntries = defaultRegistry.register(
  new Gauge(
    'ps_gateway_semantic_cache_entries',
    'Entries in the in-memory semantic cache.',
    []
  )
);
export const semanticBytes = defaultRegistry.register(
  new Gauge(
    'ps_gateway_semantic_cache_bytes',
    'Approximate bytes held by the in-memory semantic cache.',
    []
  )
);

/** `x-portkey-cache-status` value as a metrics label. */
export const cacheStatusLabel = (status: string): string =>
  status.trim().toLowerCase().replace(/\s+/g, '_');
