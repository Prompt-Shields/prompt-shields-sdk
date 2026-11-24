/**
 * RedisRateLimitStore against a real Redis (integration suite: needs
 * PS_TEST_REDIS_URL, default redis://127.0.0.1:6379; run with
 * `npm run test:integration`). Three clients stand in for three gateway
 * replicas sharing one Redis.
 */
import Redis from 'ioredis';
import { Hono } from 'hono';
import { parseRateLimitSection } from './config';
import { psRateLimit } from './index';
import {
  resetRateLimitRuntime,
  setLocalRateLimits,
  setRateLimitStores,
} from './runtime';
import {
  Bucket,
  MemoryRateLimitStore,
  RedisRateLimitStore,
  WindowBucket,
} from './store';

const URL = process.env.PS_TEST_REDIS_URL || 'redis://127.0.0.1:6379';
const PREFIX = `ps:rl:test:{t${process.pid}-${Date.now()}}`;
const MINUTE = 60_000;

const windowBucket = (
  name: string,
  now: number,
  limit: number,
  units = 1
): WindowBucket => {
  const idx = Math.floor(now / MINUTE);
  return {
    kind: 'window',
    key: `${PREFIX}:${name}:${idx}`,
    prevKey: `${PREFIX}:${name}:${idx - 1}`,
    limit,
    windowMs: MINUTE,
    windowStart: idx * MINUTE,
    units,
  };
};

describe('RedisRateLimitStore (three replicas, one Redis)', () => {
  let clients: Redis[];
  let replicas: RedisRateLimitStore[];

  beforeAll(() => {
    clients = [new Redis(URL), new Redis(URL), new Redis(URL)];
    replicas = clients.map((c) => new RedisRateLimitStore(c));
  });
  afterAll(async () => {
    const keys = await clients[0].keys(`${PREFIX}*`);
    if (keys.length) await clients[0].del(...keys);
    await Promise.all(clients.map((c) => c.quit()));
  });

  test('a 60 rpm rule admits 60 in a minute in total (load test)', async () => {
    const t0 = Math.floor(Date.now() / MINUTE) * MINUTE;
    // 300 concurrent calls spread over the three replicas, same minute.
    const results = await Promise.all(
      Array.from({ length: 300 }, (_, i) =>
        replicas[i % 3].acquire(
          [windowBucket('load', t0 + 10 + (i % 50), 60)],
          t0 + 10 + (i % 50),
          `l${i}`,
          60_000
        )
      )
    );
    expect(results.filter((r) => r.ok)).toHaveLength(60);
    // In the next minute's first second the previous 60 still count.
    const next = await replicas[1].acquire(
      [windowBucket('load', t0 + MINUTE + 500, 60)],
      t0 + MINUTE + 500,
      'n',
      60_000
    );
    expect(next.ok).toBe(false);
  });

  test('admits about 60 per rolling minute at a steady overload', async () => {
    const t0 = Math.floor(Date.now() / MINUTE) * MINUTE;
    const admittedAt: number[] = [];
    for (let t = 0; t < 3 * MINUTE; t += 250) {
      const r = await replicas[(t / 250) % 3].acquire(
        [windowBucket('steady', t0 + t, 60)],
        t0 + t,
        `s${t}`,
        60_000
      );
      if (r.ok) admittedAt.push(t);
    }
    // Every rolling minute, sampled each 5 s, admits no more than ~60.
    for (let start = 0; start + MINUTE <= 3 * MINUTE; start += 5000) {
      const n = admittedAt.filter(
        (t) => t >= start && t < start + MINUTE
      ).length;
      expect(n).toBeLessThanOrEqual(62);
    }
    expect(admittedAt.length).toBeGreaterThanOrEqual(170);
  });

  test('all limits or none: a refused tpm charges no request', async () => {
    const now = Math.floor(Date.now() / MINUTE) * MINUTE + 100;
    const rpm = windowBucket('both-r', now, 10);
    const tpm = windowBucket('both-t', now, 100, 150);
    const r = await replicas[0].acquire([rpm, tpm], now, 'x', 1000);
    expect(r.ok).toBe(false);
    expect(r.results[0].allowed).toBe(true);
    expect(r.results[1]).toMatchObject({ allowed: false, retryAfterMs: -1 });
    expect(await clients[0].get(rpm.key)).toBeNull();
  });

  test('settle corrects tokens (never below 0) and releases leases', async () => {
    const now = Math.floor(Date.now() / MINUTE) * MINUTE + 100;
    const tpm = windowBucket('settle', now, 1000, 100);
    const conc: Bucket = {
      kind: 'concurrency',
      key: `${PREFIX}:conc`,
      limit: 1,
    };
    expect((await replicas[0].acquire([tpm, conc], now, 'a', 60_000)).ok).toBe(
      true
    );
    expect((await replicas[1].acquire([conc], now, 'b', 60_000)).ok).toBe(
      false
    );
    await replicas[2].settle([{ key: tpm.key, delta: 150 }], [conc.key], 'a');
    expect(await clients[0].get(tpm.key)).toBe('250');
    expect((await replicas[1].acquire([conc], now, 'b', 60_000)).ok).toBe(true);
    await replicas[0].settle([{ key: tpm.key, delta: -1000 }], [], 'x');
    expect(await clients[0].get(tpm.key)).toBe('0');
    expect(await clients[0].pttl(tpm.key)).toBeGreaterThan(0);
  });

  test('an expired lease no longer holds a slot', async () => {
    const now = Date.now();
    const conc: Bucket = {
      kind: 'concurrency',
      key: `${PREFIX}:lapse`,
      limit: 1,
    };
    expect((await replicas[0].acquire([conc], now, 'dead', 50)).ok).toBe(true);
    expect((await replicas[1].acquire([conc], now + 10, 'b', 50)).ok).toBe(
      false
    );
    expect((await replicas[1].acquire([conc], now + 100, 'b', 50)).ok).toBe(
      true
    );
  });

  test('the Lua script decides as the memory store does', async () => {
    const memory = new MemoryRateLimitStore();
    const t0 = Math.floor(Date.now() / MINUTE) * MINUTE;
    let seed = 7;
    const rand = () => (seed = (seed * 48271) % 2147483647) / 2147483647;
    for (let i = 0, t = t0; i < 200; i++) {
      t += Math.floor(rand() * 2000);
      const units = Math.floor(rand() * 30);
      const b = windowBucket('parity', t, 300, units);
      const [r, m] = await Promise.all([
        replicas[i % 3].acquire([b], t, `p${i}`, 1000),
        memory.acquire([b], t, `p${i}`, 1000),
      ]);
      expect(r).toEqual(m);
    }
  });
});

describe('three gateway apps sharing one Redis', () => {
  afterAll(() => {
    resetRateLimitRuntime();
    delete process.env.PS_RATELIMIT_NAMESPACE;
  });

  test('the 61st call in a minute is refused, on whichever app', async () => {
    const ns = `t${process.pid}${Date.now()}`;
    process.env.PS_RATELIMIT_NAMESPACE = ns;
    const client = new Redis(URL);
    setRateLimitStores({
      primaryFactory: () => new RedisRateLimitStore(client),
    });
    setLocalRateLimits(
      parseRateLimitSection({
        version: 1,
        rules: [{ id: 'g', scope: 'global', rpm: 60 }],
      })
    );
    const t0 = Math.floor(Date.now() / MINUTE) * MINUTE + 1000;
    const apps = [0, 1, 2].map(() => {
      const app = new Hono();
      app.use('*', psRateLimit({ now: () => t0 }));
      app.post('/v1/chat/completions', (c) => c.json({ ok: true }));
      return app;
    });
    const statuses: number[] = await Promise.all(
      Array.from({ length: 90 }, async (_, i) => {
        const res = await apps[i % 3].request('/v1/chat/completions', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ model: 'm', messages: [] }),
        });
        return res.status;
      })
    );
    expect(statuses.filter((s) => s === 200)).toHaveLength(60);
    expect(statuses.filter((s) => s === 429)).toHaveLength(30);
    const keys = await client.keys(`ps:rl:v1:{${ns}}*`);
    expect(keys.length).toBeGreaterThan(0);
    await client.del(...keys);
    await client.quit();
  });
});
