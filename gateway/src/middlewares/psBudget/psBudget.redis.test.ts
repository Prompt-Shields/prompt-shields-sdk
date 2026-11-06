/**
 * RedisBudgetStore against a real Redis (integration suite: needs
 * PS_TEST_REDIS_URL, default redis://127.0.0.1:6379; run with
 * `npm run test:integration`). Two clients stand in for two gateway replicas.
 */
import Redis from 'ioredis';
import { Hono } from 'hono';
import { counterKey, reservationsKey } from './keys';
import { RedisBudgetStore } from './store';
import { loadBudgetsFromEnv } from './loader';
import { psBudget } from './index';
import { setBudgetRuntime } from './runtime';

const URL = process.env.PS_TEST_REDIS_URL || 'redis://127.0.0.1:6379';
const NOW = Date.now();

describe('RedisBudgetStore (two replicas, one Redis)', () => {
  let a: Redis;
  let b: Redis;
  let replicaA: RedisBudgetStore;
  let replicaB: RedisBudgetStore;
  const key = counterKey(
    `test-${process.pid}-${NOW}`,
    'global',
    '_',
    'day',
    '2026-11-06'
  );
  const base = { key, nowMs: NOW, reservationTtlMs: 60_000 };

  beforeAll(() => {
    a = new Redis(URL);
    b = new Redis(URL);
    replicaA = new RedisBudgetStore(a);
    replicaB = new RedisBudgetStore(b);
  });
  afterAll(async () => {
    await a.del(key, reservationsKey(key));
    await a.quit();
    await b.quit();
  });

  test('concurrent limited reservations from both replicas never overrun', async () => {
    const results = await Promise.all(
      Array.from({ length: 20 }, (_, i) =>
        (i % 2 ? replicaA : replicaB).reserve({
          ...base,
          id: `call-${i}`,
          amount: 0.3,
          limit: 1,
        })
      )
    );
    // 0, 0.3, 0.6, 0.9 are under the limit; then 1.2 is not.
    expect(results.filter((r) => r.ok)).toHaveLength(4);
    const [usage] = await replicaB.read([key], NOW);
    expect(usage.spent).toBe(0);
    expect(usage.reserved).toBeCloseTo(1.2, 10);
  });

  test('settle uses the shared counter format, seen by the other replica', async () => {
    const ok = [];
    for (let i = 0; i < 20; i++) ok.push(`call-${i}`);
    for (const id of ok) await replicaA.settle(key, id, 0.3, 0, 100);
    expect((await replicaB.read([key], NOW))[0]).toEqual({
      spent: 0,
      reserved: 0,
    });
    expect(await a.exists(key)).toBe(0); // a zero settle writes nothing

    await replicaA.settle(key, 'x', 0, 0.1, 100);
    await replicaB.settle(key, 'y', 0, 0.2, 5000);
    expect(Number(await a.get(key))).toBeCloseTo(0.3, 10); // a plain string
    const ttl = await a.ttl(key);
    expect(ttl).toBeGreaterThan(90); // set on the first write ...
    expect(ttl).toBeLessThanOrEqual(100); // ... never extended
  });

  test('a reservation lapses when its holder never settles', async () => {
    await replicaA.reserve({
      ...base,
      id: 'crashed',
      amount: 0.5,
      reservationTtlMs: 1000,
    });
    expect((await replicaB.read([key], NOW))[0].reserved).toBeCloseTo(0.5, 10);
    expect((await replicaB.read([key], NOW + 1001))[0].reserved).toBe(0);
    expect(await a.zcard(reservationsKey(key))).toBe(0);
  });

  test('an exhausted budget refuses even a zero estimate', async () => {
    await replicaA.settle(key, 'z', 0, 0.7, 100); // spent = 1.0
    const r = await replicaB.reserve({
      ...base,
      id: 'z2',
      amount: 0,
      limit: 1,
    });
    expect(r.ok).toBe(false);
    expect(r.spent).toBeCloseTo(1, 10);
  });
});

describe('loadBudgetsFromEnv + psBudget on Redis', () => {
  const ns = `test-mw-${process.pid}-${NOW}`;
  afterAll(async () => {
    setBudgetRuntime(undefined);
    const c = new Redis(URL);
    const keys = [
      ...(await c.keys(`ps:budget:v1:${ns}:*`)),
      ...(await c.keys(`{ps:budget:v1:${ns}:*`)),
    ];
    if (keys.length) await c.del(...keys);
    await c.quit();
  });

  test('a spent block budget refuses calls on every replica', async () => {
    const loaded = await loadBudgetsFromEnv({
      PS_BUDGET_REDIS_URL: URL,
      PS_BUDGETS: JSON.stringify({
        schema_version: 'budget.v1',
        namespace: ns,
        budgets: [
          {
            id: 'g',
            scope: 'global',
            window: 'day',
            limit_usd: 0.0001,
            mode: 'block',
          },
        ],
      }),
    });
    expect(loaded?.store.kind).toBe('redis');
    const app = new Hono();
    app.use('*', psBudget());
    app.post('/v1/chat/completions', (c) =>
      c.json({
        model: 'gpt-4o-mini',
        usage: { prompt_tokens: 1000, completion_tokens: 1000 },
      })
    );
    const call = () =>
      app.request('/v1/chat/completions', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'x-portkey-provider': 'openai',
        },
        body: JSON.stringify({
          model: 'gpt-4o-mini',
          messages: [{ role: 'user', content: 'hi' }],
          max_tokens: 10,
        }),
      });
    expect((await call()).status).toBe(200); // spends $0.00075
    const refused = await call();
    expect(refused.status).toBe(429);
    expect(((await refused.json()) as any).error.code).toBe('budget_exceeded');
    await (loaded!.store as RedisBudgetStore).client.quit();
  });
});
