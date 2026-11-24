/**
 * PS fork (#344): the process's local rate-limit config and its stores.
 *
 * On Node, ./loader.ts sets the local config (PS_RATE_LIMITS or
 * PS_RATE_LIMITS_FILE) at startup and, with PS_RATELIMIT_REDIS_URL (else
 * REDIS_CONNECTION_STRING), a factory for the shared Redis store. The Redis
 * client is only created when a call first meets a rule, so a gateway with
 * no rules makes no Redis calls. Elsewhere (Workers, tests) the first
 * request reads an inline PS_RATE_LIMITS and counts in memory.
 *
 * Store errors: with Redis configured, a failed acquire is decided by the
 * section's `on_store_error`:
 *   - `open` (default): the call is counted in this replica's memory store
 *     instead (limits hold per replica during the outage), logged and
 *     counted in ps_gateway_rate_limit_store_errors_total; Redis is skipped
 *     for RETRY_AFTER_ERROR_MS so an outage costs one timeout per interval,
 *     not one per call (as the collector's limiter, #360).
 *   - `closed`: the call is refused with 503 rate_limit_store_unavailable.
 */
import { RateLimitConfig, rateLimitConfigFromText } from './config';
import { rateLimitStoreErrorsTotal } from './metrics';
import {
  AcquireResult,
  Adjustment,
  Bucket,
  MemoryRateLimitStore,
  RateLimitStore,
} from './store';

export const RETRY_AFTER_ERROR_MS = 5_000;
export const DEFAULT_LEASE_MS = 10 * 60 * 1000;

let localConfig: RateLimitConfig | null | undefined;
let memory: RateLimitStore = new MemoryRateLimitStore();
let primaryFactory: (() => RateLimitStore) | undefined;
let primary: RateLimitStore | undefined;
let skipPrimaryUntil = 0;
let leaseMs = DEFAULT_LEASE_MS;

export class RateLimitStoreUnavailable extends Error {
  constructor(cause: unknown) {
    super(`rate-limit store unavailable: ${(cause as Error)?.message}`);
    this.name = 'RateLimitStoreUnavailable';
  }
}

export function leaseMsOf(env: Record<string, string | undefined>): number {
  const n = Number(env.PS_RATELIMIT_LEASE_MS);
  return Number.isFinite(n) && n > 0 ? n : DEFAULT_LEASE_MS;
}

/** Sets (null: none) the local config, from PS_RATE_LIMITS(_FILE). */
export function setLocalRateLimits(config: RateLimitConfig | null): void {
  localConfig = config;
}

/** The local config, reading an inline PS_RATE_LIMITS on first use. */
export function localRateLimits(
  env: Record<string, string | undefined>
): RateLimitConfig | null {
  if (localConfig === undefined) {
    try {
      localConfig = rateLimitConfigFromText(env.PS_RATE_LIMITS) ?? null;
    } catch (err) {
      // Node refuses to start with a bad PS_RATE_LIMITS (./loader.ts); on
      // Workers there is no startup, so say so and enforce nothing.
      console.error(`[PS RateLimit] ${(err as Error).message}`);
      localConfig = null;
    }
    leaseMs = leaseMsOf(env);
  }
  return localConfig ?? null;
}

export function setRateLimitStores(opts: {
  primaryFactory?: () => RateLimitStore;
  memory?: RateLimitStore;
  leaseMs?: number;
}): void {
  primaryFactory = opts.primaryFactory;
  primary = undefined;
  skipPrimaryUntil = 0;
  if (opts.memory) memory = opts.memory;
  if (opts.leaseMs) leaseMs = opts.leaseMs;
}

/** Back to the defaults (tests). */
export function resetRateLimitRuntime(): void {
  localConfig = undefined;
  memory = new MemoryRateLimitStore();
  primaryFactory = undefined;
  primary = undefined;
  skipPrimaryUntil = 0;
  leaseMs = DEFAULT_LEASE_MS;
}

export const rateLimitLeaseMs = () => leaseMs;

/** 'redis' when every replica shares counters, else 'memory'. */
export const rateLimitStoreKind = (): 'redis' | 'memory' =>
  primaryFactory ? 'redis' : 'memory';

function primaryStore(): RateLimitStore | undefined {
  if (!primaryFactory) return undefined;
  if (!primary) primary = primaryFactory();
  return primary;
}

export interface Acquired extends AcquireResult {
  /** The store that holds this call's charges (settle there). */
  store: RateLimitStore;
}

export async function acquire(
  buckets: Bucket[],
  nowMs: number,
  leaseId: string,
  onStoreError: 'open' | 'closed'
): Promise<Acquired> {
  const p = primaryStore();
  if (p && Date.now() >= skipPrimaryUntil) {
    try {
      return {
        ...(await p.acquire(buckets, nowMs, leaseId, leaseMs)),
        store: p,
      };
    } catch (err) {
      skipPrimaryUntil = Date.now() + RETRY_AFTER_ERROR_MS;
      console.warn(
        '[PS RateLimit] store acquire failed:',
        (err as Error)?.message
      );
      if (onStoreError === 'closed') {
        rateLimitStoreErrorsTotal.inc({ op: 'acquire', outcome: 'refused' });
        throw new RateLimitStoreUnavailable(err);
      }
      rateLimitStoreErrorsTotal.inc({
        op: 'acquire',
        outcome: 'memory_fallback',
      });
    }
  } else if (p) {
    // Redis is being skipped after a recent failure.
    if (onStoreError === 'closed') {
      rateLimitStoreErrorsTotal.inc({ op: 'acquire', outcome: 'refused' });
      throw new RateLimitStoreUnavailable(new Error('recent failure'));
    }
    rateLimitStoreErrorsTotal.inc({
      op: 'acquire',
      outcome: 'memory_fallback',
    });
  }
  return {
    ...(await memory.acquire(buckets, nowMs, leaseId, leaseMs)),
    store: memory,
  };
}

export async function settle(
  store: RateLimitStore,
  adjustments: Adjustment[],
  leaseKeys: string[],
  leaseId: string
): Promise<void> {
  try {
    await store.settle(adjustments, leaseKeys, leaseId);
  } catch (err) {
    // Leases lapse after the lease time; a lost correction leaves the
    // estimate charged.
    rateLimitStoreErrorsTotal.inc({ op: 'settle', outcome: 'skipped' });
    console.warn(
      '[PS RateLimit] store settle failed:',
      (err as Error)?.message
    );
  }
}
