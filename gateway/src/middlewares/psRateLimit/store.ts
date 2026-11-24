/**
 * PS fork (#344): rate-limit counters.
 *
 * Algorithm: a sliding window counter. Each limit keeps one counter per
 * fixed window (a minute for rpm/tpm, a UTC day for tpd); the usage seen at
 * time t is
 *
 *   used = previous window * (1 - elapsed fraction of this window) + this window
 *
 * so a "60 per minute" rule admits about 60 in any rolling minute, without
 * the 2x burst a fixed window allows at its boundary and without a token
 * bucket's full-capacity burst on top of its refill rate. It needs two
 * integers per limit and subject (a sliding log would need one entry per
 * request, or per token), and it takes arbitrary units, so a tokens/minute
 * counter can be charged an estimate first and corrected after the call by
 * adding the difference to the window the estimate went into.
 *
 * The vendored token bucket (shared/services/cache/utils/rateLimiter.ts) is
 * not used: it cannot refund or charge after the fact, it zeroes the bucket
 * when it refuses a call, and it drops fractional refill.
 *
 * Concurrency limits are a set of leases (call id -> expiry): a call holds
 * one until it ends, and a lease whose replica died lapses after the lease
 * time.
 *
 * A call's limits are checked and charged together: either every limit
 * admits it and all are charged, or none is. In Redis that is one Lua
 * script (all keys share one hash tag, so the script also runs on Redis
 * Cluster); MemoryRateLimitStore runs the same arithmetic in process.
 */
import type Redis from 'ioredis';

export interface WindowBucket {
  kind: 'window';
  /** This window's counter. */
  key: string;
  /** The previous window's counter. */
  prevKey: string;
  limit: number;
  windowMs: number;
  /** Start of the current window (epoch ms). */
  windowStart: number;
  units: number;
}

export interface ConcurrencyBucket {
  kind: 'concurrency';
  key: string;
  limit: number;
}

export type Bucket = WindowBucket | ConcurrencyBucket;

export interface BucketResult {
  allowed: boolean;
  /** Units left after this call (when admitted) or now (when refused). */
  remaining: number;
  /** Ms until this limit is back to its full value. */
  resetMs: number;
  /** When refused: ms until the call would fit; -1 if it never can. */
  retryAfterMs: number;
}

export interface AcquireResult {
  ok: boolean;
  results: BucketResult[];
}

export interface Adjustment {
  key: string;
  /** Units to add (negative: refund). The counter never goes below 0. */
  delta: number;
}

export interface RateLimitStore {
  readonly kind: 'redis' | 'memory';
  acquire(
    buckets: Bucket[],
    nowMs: number,
    leaseId: string,
    leaseMs: number
  ): Promise<AcquireResult>;
  /** Corrects token counters and drops the call's concurrency leases. */
  settle(
    adjustments: Adjustment[],
    leaseKeys: string[],
    leaseId: string
  ): Promise<void>;
}

/** A window counter lives for its window and the next (it is "previous" then). */
export const counterTtlMs = (windowMs: number) => windowMs * 2 + 60_000;

/** The sliding window arithmetic shared by both stores (and the Lua script). */
export function windowDecision(
  cur: number,
  prev: number,
  b: Omit<WindowBucket, 'kind' | 'key' | 'prevKey'>,
  nowMs: number
): BucketResult {
  const { limit, windowMs, windowStart, units } = b;
  const elapsed = Math.min(Math.max(nowMs - windowStart, 0), windowMs);
  const decaying = prev * (1 - elapsed / windowMs);
  const used = decaying + cur;
  const allowed = used + units <= limit;
  const toEnd = windowStart + windowMs - nowMs;
  const curAfter = cur + (allowed ? units : 0);
  const resetMs = curAfter > 0 ? toEnd + windowMs : prev > 0 ? toEnd : 0;
  let retryAfterMs = 0;
  if (!allowed) {
    if (units > limit) retryAfterMs = -1;
    else {
      const need = used + units - limit;
      if (prev > 0 && need <= decaying)
        retryAfterMs = Math.ceil((need * windowMs) / prev);
      else {
        const need2 = cur + units - limit;
        retryAfterMs =
          need2 <= 0 ? toEnd : toEnd + Math.ceil((need2 * windowMs) / cur);
      }
    }
  }
  return {
    allowed,
    remaining: Math.max(0, Math.floor(limit - used - (allowed ? units : 0))),
    resetMs: Math.max(0, Math.ceil(resetMs)),
    retryAfterMs,
  };
}

/** Retry hint for a refused concurrency limit: leases end when calls do. */
export const CONCURRENCY_RETRY_MS = 1000;

export function concurrencyDecision(held: number, limit: number): BucketResult {
  const allowed = held < limit;
  return {
    allowed,
    remaining: Math.max(0, limit - held - (allowed ? 1 : 0)),
    resetMs: 0,
    retryAfterMs: allowed ? 0 : CONCURRENCY_RETRY_MS,
  };
}

// KEYS: per window bucket (cur, prev), per concurrency bucket (set).
// ARGV: now, lease id, lease ms, n, then per bucket: kind ('w'|'c'),
// limit, window ms, window start, units.
// -> {ok, then per bucket: allowed, remaining, reset ms, retry ms}
// Mirrors windowDecision / concurrencyDecision above.
const ACQUIRE_LUA = `
local now = tonumber(ARGV[1])
local lease = ARGV[2]
local leaseMs = tonumber(ARGV[3])
local n = tonumber(ARGV[4])
local ki = 1
local ok = 1
local out = {}
local plan = {}
for i = 0, n - 1 do
  local a = 5 + i * 5
  local kind = ARGV[a]
  local limit = tonumber(ARGV[a + 1])
  local win = tonumber(ARGV[a + 2])
  local start = tonumber(ARGV[a + 3])
  local units = tonumber(ARGV[a + 4])
  local allowed, remaining, reset, retry
  if kind == 'w' then
    local cur = tonumber(redis.call('GET', KEYS[ki]) or '0') or 0
    local prev = tonumber(redis.call('GET', KEYS[ki + 1]) or '0') or 0
    local elapsed = math.min(math.max(now - start, 0), win)
    local decaying = prev * (1 - elapsed / win)
    local used = decaying + cur
    local toEnd = start + win - now
    allowed = (used + units <= limit) and 1 or 0
    local curAfter = cur
    if allowed == 1 then curAfter = cur + units end
    if curAfter > 0 then reset = toEnd + win elseif prev > 0 then reset = toEnd else reset = 0 end
    retry = 0
    if allowed == 0 then
      if units > limit then
        retry = -1
      else
        local need = used + units - limit
        if prev > 0 and need <= decaying then
          retry = math.ceil(need * win / prev)
        else
          local need2 = cur + units - limit
          if need2 <= 0 then retry = toEnd else retry = toEnd + math.ceil(need2 * win / cur) end
        end
      end
    end
    local left = limit - used
    if allowed == 1 then left = left - units end
    remaining = math.max(0, math.floor(left))
    reset = math.max(0, math.ceil(reset))
    plan[#plan + 1] = {'w', KEYS[ki], units, win}
    ki = ki + 2
  else
    redis.call('ZREMRANGEBYSCORE', KEYS[ki], '-inf', now)
    local held = redis.call('ZCARD', KEYS[ki])
    allowed = (held < limit) and 1 or 0
    remaining = limit - held
    if allowed == 1 then remaining = remaining - 1 end
    remaining = math.max(0, remaining)
    reset = 0
    if allowed == 1 then retry = 0 else retry = ${CONCURRENCY_RETRY_MS} end
    plan[#plan + 1] = {'c', KEYS[ki]}
    ki = ki + 1
  end
  if allowed == 0 then ok = 0 end
  out[#out + 1] = allowed
  out[#out + 1] = remaining
  out[#out + 1] = reset
  out[#out + 1] = retry
end
if ok == 1 then
  for _, p in ipairs(plan) do
    if p[1] == 'w' then
      if p[3] > 0 then
        redis.call('INCRBY', p[2], p[3])
      end
      redis.call('PEXPIRE', p[2], p[4] * 2 + 60000)
    else
      redis.call('ZADD', p[2], now + leaseMs, lease)
      redis.call('PEXPIRE', p[2], leaseMs)
    end
  end
end
table.insert(out, 1, ok)
return out
`;

// KEYS: counters to adjust, then concurrency sets. ARGV: number of
// counters, lease id, then one delta per counter. A counter that already
// expired is left alone; none goes below 0.
const SETTLE_LUA = `
local n = tonumber(ARGV[1])
for i = 1, n do
  local d = tonumber(ARGV[2 + i])
  if d ~= 0 and redis.call('EXISTS', KEYS[i]) == 1 then
    local v = redis.call('INCRBY', KEYS[i], d)
    if v < 0 then
      local ttl = redis.call('PTTL', KEYS[i])
      redis.call('SET', KEYS[i], 0)
      if ttl > 0 then redis.call('PEXPIRE', KEYS[i], ttl) end
    end
  end
end
for i = n + 1, #KEYS do
  redis.call('ZREM', KEYS[i], ARGV[2])
end
return 1
`;

type RateLimitRedis = Redis & {
  psRateLimitAcquire(
    numKeys: number,
    ...args: (string | number)[]
  ): Promise<number[]>;
  psRateLimitSettle(
    numKeys: number,
    ...args: (string | number)[]
  ): Promise<number>;
};

export class RedisRateLimitStore implements RateLimitStore {
  readonly kind = 'redis' as const;
  readonly client: RateLimitRedis;

  constructor(client: Redis) {
    // numberOfKeys is passed per call (it varies with the rules).
    client.defineCommand('psRateLimitAcquire', { lua: ACQUIRE_LUA });
    client.defineCommand('psRateLimitSettle', { lua: SETTLE_LUA });
    this.client = client as RateLimitRedis;
  }

  async acquire(
    buckets: Bucket[],
    nowMs: number,
    leaseId: string,
    leaseMs: number
  ): Promise<AcquireResult> {
    const keys: string[] = [];
    const argv: (string | number)[] = [
      nowMs,
      leaseId,
      Math.max(1, Math.round(leaseMs)),
      buckets.length,
    ];
    for (const b of buckets) {
      if (b.kind === 'window') {
        keys.push(b.key, b.prevKey);
        argv.push('w', b.limit, b.windowMs, b.windowStart, b.units);
      } else {
        keys.push(b.key);
        argv.push('c', b.limit, 0, 0, 1);
      }
    }
    const raw = await this.client.psRateLimitAcquire(
      keys.length,
      ...keys,
      ...argv
    );
    const results: BucketResult[] = buckets.map((_, i) => ({
      allowed: Number(raw[1 + i * 4]) === 1,
      remaining: Number(raw[2 + i * 4]),
      resetMs: Number(raw[3 + i * 4]),
      retryAfterMs: Number(raw[4 + i * 4]),
    }));
    return { ok: Number(raw[0]) === 1, results };
  }

  async settle(
    adjustments: Adjustment[],
    leaseKeys: string[],
    leaseId: string
  ): Promise<void> {
    if (!adjustments.length && !leaseKeys.length) return;
    const keys = [...adjustments.map((a) => a.key), ...leaseKeys];
    await this.client.psRateLimitSettle(
      keys.length,
      ...keys,
      adjustments.length,
      leaseId,
      ...adjustments.map((a) => Math.round(a.delta))
    );
  }
}

interface Counter {
  value: number;
  expiresAt: number;
}

/** One process only: a single replica, Workers, tests, or a Redis outage. */
export class MemoryRateLimitStore implements RateLimitStore {
  readonly kind = 'memory' as const;
  private counters = new Map<string, Counter>();
  private leases = new Map<string, Map<string, number>>();
  private ops = 0;

  constructor(private clock: () => number = Date.now) {}

  private sweep(): void {
    if (++this.ops % 1000) return;
    const now = this.clock();
    for (const [k, c] of this.counters)
      if (c.expiresAt <= now) this.counters.delete(k);
    for (const [k, set] of this.leases) {
      for (const [id, exp] of set) if (exp <= now) set.delete(id);
      if (!set.size) this.leases.delete(k);
    }
  }

  private value(key: string): number {
    const c = this.counters.get(key);
    if (!c) return 0;
    if (c.expiresAt <= this.clock()) {
      this.counters.delete(key);
      return 0;
    }
    return c.value;
  }

  private held(key: string, nowMs: number): Map<string, number> {
    let set = this.leases.get(key);
    if (!set) this.leases.set(key, (set = new Map()));
    for (const [id, exp] of set) if (exp <= nowMs) set.delete(id);
    return set;
  }

  async acquire(
    buckets: Bucket[],
    nowMs: number,
    leaseId: string,
    leaseMs: number
  ): Promise<AcquireResult> {
    this.sweep();
    const results = buckets.map((b) =>
      b.kind === 'window'
        ? windowDecision(this.value(b.key), this.value(b.prevKey), b, nowMs)
        : concurrencyDecision(this.held(b.key, nowMs).size, b.limit)
    );
    const ok = results.every((r) => r.allowed);
    if (ok) {
      for (const b of buckets) {
        if (b.kind === 'window') {
          this.counters.set(b.key, {
            value: this.value(b.key) + b.units,
            expiresAt: this.clock() + counterTtlMs(b.windowMs),
          });
        } else this.held(b.key, nowMs).set(leaseId, nowMs + leaseMs);
      }
    }
    return { ok, results };
  }

  async settle(
    adjustments: Adjustment[],
    leaseKeys: string[],
    leaseId: string
  ): Promise<void> {
    for (const a of adjustments) {
      const c = this.counters.get(a.key);
      if (!c || c.expiresAt <= this.clock() || !a.delta) continue;
      c.value = Math.max(0, c.value + Math.round(a.delta));
    }
    for (const k of leaseKeys) {
      const set = this.leases.get(k);
      set?.delete(leaseId);
      if (set && !set.size) this.leases.delete(k);
    }
  }
}
