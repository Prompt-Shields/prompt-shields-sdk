/**
 * PS fork shim for Portkey 2.0.0's `requestCache` (src/services/cache).
 * Upstream backs it with Redis or Cloudflare KV plus a local tier; ported
 * provider code here (Bedrock / AWS credential caching) only needs a
 * per-process key/value store with a TTL in seconds, so that is all this is.
 */
type Entry = { value: unknown; expiresAt: number };

const store = new Map<string, Entry>();
const MAX_ENTRIES = 1000;

const cache = {
  async get<T = unknown>(key: string, _options?: unknown): Promise<T | null> {
    const entry = store.get(key);
    if (!entry) return null;
    if (entry.expiresAt <= Date.now()) {
      store.delete(key);
      return null;
    }
    return entry.value as T;
  },
  async set(
    key: string,
    value: unknown,
    options?: { ttl?: number }
  ): Promise<void> {
    if (store.size >= MAX_ENTRIES) {
      const oldest = store.keys().next().value;
      if (oldest !== undefined) store.delete(oldest);
    }
    const ttlSeconds = options?.ttl ?? 300;
    store.set(key, { value, expiresAt: Date.now() + ttlSeconds * 1000 });
  },
  async delete(key: string): Promise<void> {
    store.delete(key);
  },
};

export function requestCache(_env?: Record<string, any>) {
  return cache;
}
