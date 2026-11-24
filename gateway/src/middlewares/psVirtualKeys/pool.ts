/**
 * PS fork (#343): credential pool selection and health for virtual keys.
 *
 * A virtual key's pool holds several inference credentials, possibly for
 * several providers. For one call, the credentials of the target's provider
 * are tried in an order set by the key's strategy:
 *
 *   round_robin             a per (virtual key, provider) counter rotates
 *                           the starting credential on every call
 *   least_recently_limited  credentials never answered 429 first (pool
 *                           order), then the one limited longest ago
 *
 * In both, a credential that answered 401 or 403 within UNHEALTHY_MS is
 * moved to the end (tried only when every other credential failed too), so
 * a revoked or wrong provider key stops taking traffic without being
 * dropped for good. State is per process (per isolate on edge runtimes),
 * bounded (MAX_ENTRIES, least recently used evicted), and keyed by
 * credential id: it never holds a key.
 */

export const ROUND_ROBIN = 'round_robin';
export const LEAST_RECENTLY_LIMITED = 'least_recently_limited';
export const UNHEALTHY_MS = 5 * 60_000;
export const MAX_ENTRIES = 10_000;

/** One decrypted credential of a resolved virtual key. */
export interface PoolCredential {
  id: string;
  provider: string;
  label?: string;
  key_fingerprint: string;
  api_key: string;
}

interface Health {
  limitedAt?: number;
  unhealthyAt?: number;
}

/**
 * Same rule as the collector (collector/virtual_keys.py model_allowed,
 * Python fnmatchcase): `*` any run of characters, `?` one character,
 * `[...]` a set (`[!...]` negated); everything else literal. `undefined`
 * or `null` patterns allow every model.
 */
export function modelAllowed(
  patterns: string[] | null | undefined,
  model: string
): boolean {
  if (patterns === null || patterns === undefined) return true;
  return patterns.some((p) => globToRegExp(p).test(model));
}

const globCache = new Map<string, RegExp>();

export function globToRegExp(glob: string): RegExp {
  const cached = globCache.get(glob);
  if (cached) return cached;
  let out = '';
  for (let i = 0; i < glob.length; i++) {
    const ch = glob[i];
    if (ch === '*') out += '[\\s\\S]*';
    else if (ch === '?') out += '[\\s\\S]';
    else if (ch === '[') {
      const end = glob.indexOf(']', i + 2);
      if (end === -1) {
        out += '\\[';
        continue;
      }
      let body = glob.slice(i + 1, end);
      const negate = body.startsWith('!');
      if (negate) body = body.slice(1);
      body = body.replace(/\\/g, '\\\\').replace(/\^/g, '\\^');
      out += `[${negate ? '^' : ''}${body}]`;
      i = end;
    } else out += ch.replace(/[.+^${}()|\\\/\]-]/g, '\\$&');
  }
  const re = new RegExp(`^${out}$`);
  if (globCache.size > 1000) globCache.clear();
  globCache.set(glob, re);
  return re;
}

export class PoolState {
  private health = new Map<string, Health>();
  private counters = new Map<string, number>();

  constructor(private readonly now: () => number = Date.now) {}

  private touch<V>(map: Map<string, V>, key: string, value: V): void {
    map.delete(key);
    map.set(key, value);
    if (map.size > MAX_ENTRIES) {
      const oldest = map.keys().next().value;
      if (oldest !== undefined) map.delete(oldest);
    }
  }

  markLimited(credentialId: string): void {
    const h = { ...(this.health.get(credentialId) ?? {}) };
    h.limitedAt = this.now();
    this.touch(this.health, credentialId, h);
  }

  markUnhealthy(credentialId: string): void {
    const h = { ...(this.health.get(credentialId) ?? {}) };
    h.unhealthyAt = this.now();
    this.touch(this.health, credentialId, h);
  }

  markHealthy(credentialId: string): void {
    const h = this.health.get(credentialId);
    if (h?.unhealthyAt === undefined) return;
    this.touch(this.health, credentialId, { limitedAt: h.limitedAt });
  }

  isUnhealthy(credentialId: string): boolean {
    const at = this.health.get(credentialId)?.unhealthyAt;
    return at !== undefined && this.now() - at < UNHEALTHY_MS;
  }

  /**
   * The provider's credentials in the order to try them for one call.
   * Advances the round-robin counter unless `advance` is false (a peek).
   */
  order(
    virtualKeyId: string,
    strategy: string,
    provider: string,
    credentials: PoolCredential[],
    advance = true
  ): PoolCredential[] {
    const mine = credentials.filter((c) => c.provider === provider);
    if (mine.length <= 1) return mine;
    let ordered: PoolCredential[];
    if (strategy === LEAST_RECENTLY_LIMITED) {
      const at = (c: PoolCredential) =>
        this.health.get(c.id)?.limitedAt ?? -Infinity;
      // Array.prototype.sort is stable: ties keep pool order.
      ordered = [...mine].sort((a, b) => at(a) - at(b));
    } else {
      const key = `${virtualKeyId}\n${provider}`;
      const n = this.counters.get(key) ?? 0;
      if (advance) this.touch(this.counters, key, (n + 1) % 1_000_000);
      const start = n % mine.length;
      ordered = [...mine.slice(start), ...mine.slice(0, start)];
    }
    const healthy = ordered.filter((c) => !this.isUnhealthy(c.id));
    const unhealthy = ordered.filter((c) => this.isUnhealthy(c.id));
    return [...healthy, ...unhealthy];
  }

  reset(): void {
    this.health.clear();
    this.counters.clear();
  }
}

export const poolState = new PoolState();
