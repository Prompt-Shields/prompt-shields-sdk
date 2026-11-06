/**
 * Budget stores over the shared counter format (./keys.ts,
 * schemas/budget/README.md): a counter per key holding settled spend in USD,
 * plus the gateway's in-flight reservations of estimated cost (each with its
 * own expiry, so a replica that dies mid-call cannot hold budget forever).
 *
 * - RedisBudgetStore: shared by every gateway replica and the SDK
 *   BudgetStore. Each operation is one Lua script, so the check-and-reserve
 *   of a block budget is atomic across replicas.
 * - MemoryBudgetStore: one process only (single replica, tests, Workers).
 */
import type Redis from 'ioredis';
import { reservationsKey } from './keys';

export interface BudgetUsage {
  /** Settled spend in the period (the shared counter), USD. */
  spent: number;
  /** Sum of live gateway reservations, USD. */
  reserved: number;
}

export interface ReserveArgs {
  key: string;
  /** Unique per call. */
  id: string;
  amount: number;
  nowMs: number;
  /** How long the reservation holds budget if never settled. */
  reservationTtlMs: number;
  /**
   * Refuse (and reserve nothing) when spent + reserved >= limit. Omitted:
   * always reserve.
   */
  limit?: number;
}

export interface ReserveResult extends BudgetUsage {
  ok: boolean;
}

export interface BudgetStore {
  readonly kind: 'redis' | 'memory';
  read(keys: string[], nowMs: number): Promise<BudgetUsage[]>;
  reserve(args: ReserveArgs): Promise<ReserveResult>;
  /**
   * Drops the reservation and adds `actual` to the counter (with the shared
   * TTL rule: set on the period's first write, never extended).
   */
  settle(
    key: string,
    id: string,
    amount: number,
    actual: number,
    ttlSeconds: number
  ): Promise<void>;
}

const member = (id: string, amount: number) => `${id}|${amount}`;

const SUM_RESERVED = `
local reserved = 0
for _, m in ipairs(redis.call('ZRANGE', KEYS[2], 0, -1)) do
  reserved = reserved + (tonumber(string.match(m, '|([^|]*)$')) or 0)
end
`;

// KEYS: counter, reservations. ARGV: now. -> {spent, reserved}
const READ_LUA = `
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ARGV[1])
local spent = tonumber(redis.call('GET', KEYS[1]) or '0') or 0
${SUM_RESERVED}
return {tostring(spent), tostring(reserved)}
`;

// KEYS: counter, reservations. ARGV: now, member, reservation ttl ms,
// limit ('' for none). -> {ok, spent, reserved}
const RESERVE_LUA = `
local now = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
local spent = tonumber(redis.call('GET', KEYS[1]) or '0') or 0
${SUM_RESERVED}
if ARGV[4] ~= '' and spent + reserved >= tonumber(ARGV[4]) then
  return {0, tostring(spent), tostring(reserved)}
end
redis.call('ZADD', KEYS[2], now + tonumber(ARGV[3]), ARGV[2])
redis.call('PEXPIRE', KEYS[2], ARGV[3])
return {1, tostring(spent), tostring(reserved)}
`;

// KEYS: counter, reservations. ARGV: member, actual, ttl seconds.
// The increment is the shared one (schemas/budget/README.md).
const SETTLE_LUA = `
redis.call('ZREM', KEYS[2], ARGV[1])
if tonumber(ARGV[2]) > 0 then
  redis.call('INCRBYFLOAT', KEYS[1], ARGV[2])
  if redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[3])
  end
end
return 1
`;

type BudgetRedis = Redis & {
  psBudgetRead(k1: string, k2: string, now: number): Promise<string[]>;
  psBudgetReserve(
    k1: string,
    k2: string,
    ...argv: (string | number)[]
  ): Promise<[number, string, string]>;
  psBudgetSettle(
    k1: string,
    k2: string,
    ...argv: (string | number)[]
  ): Promise<number>;
};

export class RedisBudgetStore implements BudgetStore {
  readonly kind = 'redis' as const;
  readonly client: BudgetRedis;

  constructor(client: Redis) {
    client.defineCommand('psBudgetRead', { numberOfKeys: 2, lua: READ_LUA });
    client.defineCommand('psBudgetReserve', {
      numberOfKeys: 2,
      lua: RESERVE_LUA,
    });
    client.defineCommand('psBudgetSettle', {
      numberOfKeys: 2,
      lua: SETTLE_LUA,
    });
    this.client = client as BudgetRedis;
  }

  async read(keys: string[], nowMs: number): Promise<BudgetUsage[]> {
    return Promise.all(
      keys.map(async (key) => {
        const [spent, reserved] = await this.client.psBudgetRead(
          key,
          reservationsKey(key),
          nowMs
        );
        return { spent: Number(spent) || 0, reserved: Number(reserved) || 0 };
      })
    );
  }

  async reserve(a: ReserveArgs): Promise<ReserveResult> {
    const [ok, spent, reserved] = await this.client.psBudgetReserve(
      a.key,
      reservationsKey(a.key),
      a.nowMs,
      member(a.id, a.amount),
      Math.max(1, Math.round(a.reservationTtlMs)),
      a.limit === undefined ? '' : a.limit
    );
    return {
      ok: Number(ok) === 1,
      spent: Number(spent) || 0,
      reserved: Number(reserved) || 0,
    };
  }

  async settle(
    key: string,
    id: string,
    amount: number,
    actual: number,
    ttlSeconds: number
  ): Promise<void> {
    await this.client.psBudgetSettle(
      key,
      reservationsKey(key),
      member(id, amount),
      actual,
      ttlSeconds
    );
  }
}

interface Counter {
  spent: number;
  expiresAt: number;
}

export class MemoryBudgetStore implements BudgetStore {
  readonly kind = 'memory' as const;
  private counters = new Map<string, Counter>();
  private reservations = new Map<
    string,
    Map<string, { amount: number; expiresAt: number }>
  >();
  /** Counter expiry uses this clock (tests); reservations use `nowMs`. */
  constructor(private clock: () => number = Date.now) {}

  private usage(key: string, nowMs: number): BudgetUsage {
    const c = this.counters.get(key);
    if (c && c.expiresAt <= this.clock()) this.counters.delete(key);
    const res = this.reservations.get(key);
    let reserved = 0;
    for (const [id, r] of res ?? []) {
      if (r.expiresAt <= nowMs) res!.delete(id);
      else reserved += r.amount;
    }
    return { spent: this.counters.get(key)?.spent ?? 0, reserved };
  }

  async read(keys: string[], nowMs: number): Promise<BudgetUsage[]> {
    return keys.map((k) => this.usage(k, nowMs));
  }

  async reserve(a: ReserveArgs): Promise<ReserveResult> {
    const { spent, reserved } = this.usage(a.key, a.nowMs);
    if (a.limit !== undefined && spent + reserved >= a.limit)
      return { ok: false, spent, reserved };
    let res = this.reservations.get(a.key);
    if (!res) this.reservations.set(a.key, (res = new Map()));
    res.set(a.id, {
      amount: a.amount,
      expiresAt: a.nowMs + a.reservationTtlMs,
    });
    return { ok: true, spent, reserved };
  }

  async settle(
    key: string,
    id: string,
    _amount: number,
    actual: number,
    ttlSeconds: number
  ): Promise<void> {
    this.reservations.get(key)?.delete(id);
    if (!(actual > 0)) return;
    this.usage(key, 0); // drop an expired counter first
    const c = this.counters.get(key);
    if (c) c.spent += actual;
    else
      this.counters.set(key, {
        spent: actual,
        expiresAt: this.clock() + ttlSeconds * 1000,
      });
  }
}
