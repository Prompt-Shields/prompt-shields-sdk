/**
 * Rate limits (#344): the rate_limits v1 section, the sliding window
 * arithmetic, the in-memory store and the middleware (headers, 429,
 * token settlement, concurrency, store errors, central policy layers).
 * The Redis store is covered by psRateLimit.redis.test.ts.
 */
import { Hono } from 'hono';
import {
  globToRegExp,
  parseRateLimitSection,
  RateLimitConfigError,
  rateLimitConfigFromText,
} from './config';
import {
  estimateTokens,
  formatDuration,
  psRateLimit,
  rateLimitHealth,
} from './index';
import { loadRateLimitsFromEnv } from './loader';
import {
  resetRateLimitRuntime,
  setLocalRateLimits,
  setRateLimitStores,
} from './runtime';
import { MemoryRateLimitStore, RateLimitStore, windowDecision } from './store';
import { defaultRegistry } from '../psMetrics/registry';
import { parsePolicyBundle, setCentralPolicy } from '../policyPull/state';

const T0 = Date.UTC(2026, 10, 7, 12, 0, 0); // a minute boundary

const section = (rules: any[], extra: Record<string, any> = {}) => ({
  version: 1,
  rules,
  ...extra,
});

afterEach(() => {
  resetRateLimitRuntime();
  setCentralPolicy(undefined);
});

describe('rate_limits v1 section', () => {
  test('parses rules, match globs and defaults', () => {
    const cfg = parseRateLimitSection(
      section([
        {
          id: 'chat',
          scope: 'team',
          subjects: ['team-a'],
          match: { model: 'gpt-4o*', provider: ['OpenAI'] },
          rpm: 10,
          tpm: 1000,
        },
        { id: 'all', scope: 'global', concurrency: 5, tpd: 100000 },
      ])
    );
    expect(cfg.onStoreError).toBe('open');
    expect(cfg.rules).toHaveLength(2);
    expect(cfg.rules[0].match.model![0].test('gpt-4o-mini')).toBe(true);
    expect(cfg.rules[0].match.model![0].test('xgpt-4o')).toBe(false);
    expect(cfg.rules[0].match.provider![0].test('openai')).toBe(true);
  });

  test.each([
    [{ version: 2, rules: [] }, 'version must be 1'],
    [section([], { extra: 1 }), 'unknown keys'],
    [section([], { on_store_error: 'maybe' }), 'on_store_error'],
    [section([{ id: 'a', scope: 'global' }]), 'needs at least one'],
    [section([{ id: 'a', scope: 'org', rpm: 1 }]), 'scope must be'],
    [section([{ id: 'a b', scope: 'global', rpm: 1 }]), 'id must be'],
    [
      section([
        { id: 'a', scope: 'global', rpm: 1 },
        { id: 'a', scope: 'global', rpm: 2 },
      ]),
      'used twice',
    ],
    [section([{ id: 'a', scope: 'global', rpm: 0 }]), 'positive integer'],
    [section([{ id: 'a', scope: 'global', rpm: 1.5 }]), 'positive integer'],
    [
      section([{ id: 'a', scope: 'global', subjects: ['x'], rpm: 1 }]),
      'subjects is not allowed',
    ],
    [
      section([{ id: 'a', scope: 'user', match: { tool: 'x' }, rpm: 1 }]),
      'unknown keys',
    ],
    [
      section([{ id: 'a', scope: 'user', match: { model: [] }, rpm: 1 }]),
      'non-empty',
    ],
  ])('refuses %j', (raw, message) => {
    expect(() => parseRateLimitSection(raw)).toThrow(RateLimitConfigError);
    expect(() => parseRateLimitSection(raw)).toThrow(message);
  });

  test('local text: empty is none, bad JSON is an error', () => {
    expect(rateLimitConfigFromText('')).toBeUndefined();
    expect(() => rateLimitConfigFromText('{')).toThrow('not valid JSON');
  });

  test('globs escape regex characters', () => {
    expect(globToRegExp('a.b*').test('a.bc')).toBe(true);
    expect(globToRegExp('a.b*').test('axbc')).toBe(false);
  });

  test('loader: inline document, memory store without a Redis URL', async () => {
    const r = await loadRateLimitsFromEnv({
      PS_RATE_LIMITS: JSON.stringify(
        section([{ id: 'g', scope: 'global', rpm: 5 }])
      ),
    });
    expect(r.store).toBe('memory');
    expect(r.config?.rules).toHaveLength(1);
    await expect(
      loadRateLimitsFromEnv({ PS_RATE_LIMITS: '{"version":1}' })
    ).rejects.toThrow('rules must be an array');
    await expect(
      loadRateLimitsFromEnv({ PS_RATE_LIMITS_FILE: '/nonexistent/rl.json' })
    ).rejects.toThrow('cannot read PS_RATE_LIMITS_FILE');
  });
});

describe('sliding window', () => {
  const b = { limit: 60, windowMs: 60_000, windowStart: T0, units: 1 };

  test('admits the limit within a window, then refuses with a wait', () => {
    expect(windowDecision(59, 0, b, T0 + 1000)).toMatchObject({
      allowed: true,
      remaining: 0,
    });
    const r = windowDecision(60, 0, b, T0 + 1000);
    expect(r.allowed).toBe(false);
    // Nothing decays before the window ends; then 60 + 1 - 60 = 1 must
    // decay from the previous window: 1 s into the next one.
    expect(r.retryAfterMs).toBe(59_000 + 1000);
  });

  test("the previous window's count decays across this one", () => {
    // Full previous window, half way through this one: 30 used.
    const half = windowDecision(0, 60, b, T0 + 30_000);
    expect(half).toMatchObject({ allowed: true, remaining: 29 });
    // At the start of the next window nothing is free.
    expect(windowDecision(0, 60, b, T0).allowed).toBe(false);
    const r = windowDecision(30, 60, b, T0 + 30_000);
    expect(r.allowed).toBe(false);
    expect(r.retryAfterMs).toBe(1000);
  });

  test('a request larger than the limit never fits', () => {
    const r = windowDecision(0, 0, { ...b, units: 61 }, T0);
    expect(r).toMatchObject({ allowed: false, retryAfterMs: -1 });
  });

  test('a 60 rpm rule admits 60 per rolling minute in the memory store', async () => {
    const store = new MemoryRateLimitStore(() => T0);
    let admitted = 0;
    // 3 calls a second for two minutes.
    for (let t = 0; t < 120_000; t += 333) {
      const now = T0 + t;
      const idx = Math.floor(now / 60_000);
      const r = await store.acquire(
        [
          {
            kind: 'window',
            key: `k:${idx}`,
            prevKey: `k:${idx - 1}`,
            limit: 60,
            windowMs: 60_000,
            windowStart: idx * 60_000,
            units: 1,
          },
        ],
        now,
        'l',
        1000
      );
      if (r.ok) admitted++;
    }
    expect(admitted).toBeGreaterThanOrEqual(110);
    expect(admitted).toBeLessThanOrEqual(120);
  });
});

test('formatDuration', () => {
  expect(formatDuration(20)).toBe('20ms');
  expect(formatDuration(1500)).toBe('1.5s');
  expect(formatDuration(360_000)).toBe('6m0s');
});

test('estimateTokens: input estimate plus max_tokens or the default', () => {
  const messages = [{ role: 'user', content: 'x'.repeat(400) }];
  expect(
    estimateTokens('/v1/chat/completions', { messages, max_tokens: 50 })
  ).toBe(150);
  expect(estimateTokens('/v1/chat/completions', { messages })).toBe(100 + 512);
  expect(estimateTokens('/v1/embeddings', { input: 'x'.repeat(40) })).toBe(10);
});

// ---------------------------------------------------------------- middleware

interface AppOpts {
  now?: () => number;
  upstream?: (c: any) => Response | Promise<Response>;
}

function makeApp(opts: AppOpts = {}) {
  const app = new Hono();
  app.use('*', psRateLimit({ now: opts.now ?? (() => T0 + 1000) }));
  app.post(
    '/v1/chat/completions',
    opts.upstream ??
      ((c) =>
        c.json({
          choices: [],
          usage: { prompt_tokens: 10, completion_tokens: 5 },
        }))
  );
  return app;
}

const chat = (
  app: Hono,
  body: Record<string, any> = {},
  headers: Record<string, string> = {}
) =>
  app.request('/v1/chat/completions', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      authorization: 'Bearer sk-test-key',
      'x-portkey-provider': 'openai',
      ...headers,
    },
    body: JSON.stringify({
      model: 'gpt-4o-mini',
      messages: [{ role: 'user', content: 'hello' }],
      max_tokens: 10,
      ...body,
    }),
  });

const errorOf = async (res: Response): Promise<any> =>
  ((await res.json()) as any).error;

const metric = (line: string) => {
  const m = defaultRegistry
    .render()
    .split('\n')
    .find((l) => l.startsWith(line + ' '));
  return m ? Number(m.split(' ')[1]) : 0;
};

/** A store that records calls and delegates to memory. */
function spyStore(inner: RateLimitStore = new MemoryRateLimitStore()) {
  const calls = { acquire: 0, settle: [] as any[] };
  const store: RateLimitStore = {
    kind: 'redis',
    acquire: (...a) => {
      calls.acquire++;
      return inner.acquire(...a);
    },
    settle: (...a) => {
      calls.settle.push(a);
      return inner.settle(...a);
    },
  };
  return { store, calls };
}

describe('psRateLimit middleware', () => {
  test('no rules: no headers and no store calls', async () => {
    let created = 0;
    setRateLimitStores({
      primaryFactory: () => {
        created++;
        return spyStore().store;
      },
    });
    setLocalRateLimits(null);
    const res = await chat(makeApp());
    expect(res.status).toBe(200);
    expect(res.headers.get('x-ratelimit-limit-requests')).toBeNull();
    expect(created).toBe(0);
    expect(rateLimitHealth({})).toEqual({});
  });

  test('a rule that matches nothing makes no store calls', async () => {
    const spy = spyStore();
    setRateLimitStores({ primaryFactory: () => spy.store });
    setLocalRateLimits(
      parseRateLimitSection(
        section([
          {
            id: 'claude',
            scope: 'global',
            match: { model: 'claude*' },
            rpm: 1,
          },
        ])
      )
    );
    expect((await chat(makeApp())).status).toBe(200);
    expect(spy.calls.acquire).toBe(0);
  });

  test('rpm: headers on every call, then 429 with rule id and Retry-After', async () => {
    setLocalRateLimits(
      parseRateLimitSection(
        section([{ id: 'per-key', scope: 'virtual_key', rpm: 2 }])
      )
    );
    const app = makeApp();
    const line =
      'ps_gateway_rate_limit_hits_total{scope="virtual_key",rule="per-key",limit="rpm"}';
    const before = metric(line);
    const first = await chat(app);
    expect(first.status).toBe(200);
    expect(first.headers.get('x-ratelimit-limit-requests')).toBe('2');
    expect(first.headers.get('x-ratelimit-remaining-requests')).toBe('1');
    expect(first.headers.get('x-ratelimit-reset-requests')).toBe('1m59s');
    expect((await chat(app)).status).toBe(200);
    const third = await chat(app);
    expect(third.status).toBe(429);
    // At the next window's start both calls still count (they decay over
    // that minute): one has to, 59 s + 30 s from now.
    expect(third.headers.get('retry-after')).toBe('89');
    expect(third.headers.get('x-ratelimit-remaining-requests')).toBe('0');
    expect(await errorOf(third)).toMatchObject({
      type: 'rate_limit_error',
      code: 'rate_limit_exceeded',
      rule_id: 'per-key',
      scope: 'virtual_key',
      limit: 'rpm',
    });
    expect(metric(line)).toBe(before + 1);
    // Another key has its own counter.
    expect(
      (await chat(app, {}, { authorization: 'Bearer sk-other' })).status
    ).toBe(200);
    expect(rateLimitHealth({})).toEqual({
      rate_limits: { source: 'local', rules: 1, store: 'memory' },
    });
  });

  test('match and subjects narrow a rule', async () => {
    setLocalRateLimits(
      parseRateLimitSection(
        section([
          {
            id: 'team-a-mini',
            scope: 'team',
            subjects: ['team-a'],
            match: { model: '*-mini', route: '/v1/chat/*' },
            rpm: 1,
          },
        ])
      )
    );
    const app = makeApp();
    const a = { 'x-ps-team': 'team-a' };
    expect((await chat(app, {}, a)).status).toBe(200);
    expect((await chat(app, {}, a)).status).toBe(429);
    expect((await chat(app, { model: 'gpt-4o' }, a)).status).toBe(200);
    expect((await chat(app, {}, { 'x-ps-team': 'team-b' })).status).toBe(200);
    expect((await chat(app)).status).toBe(200); // no team: not counted
  });

  test('tpm blocks the request that would exceed it and settles to real usage', async () => {
    setLocalRateLimits(
      parseRateLimitSection(section([{ id: 'tok', scope: 'global', tpm: 100 }]))
    );
    const app = makeApp(); // usage: 15 tokens per call
    // Estimate: 2 input + 10 max_tokens = 12 reserved, settled to 15.
    const r1 = await chat(app);
    expect(r1.status).toBe(200);
    expect(r1.headers.get('x-ratelimit-limit-tokens')).toBe('100');
    expect(r1.headers.get('x-ratelimit-remaining-tokens')).toBe('88');
    // 15 charged; a request estimated at 2 + 90 = 92 would exceed 100.
    const big = await chat(app, { max_tokens: 90 });
    expect(big.status).toBe(429);
    expect((await errorOf(big)).limit).toBe('tpm');
    // 2 + 80 = 82 fits (15 + 82 <= 100).
    const ok = await chat(app, { max_tokens: 80 });
    expect(ok.status).toBe(200);
    expect(ok.headers.get('x-ratelimit-remaining-tokens')).toBe('3');
  });

  test('a request estimated above the limit is refused without Retry-After', async () => {
    setLocalRateLimits(
      parseRateLimitSection(section([{ id: 'tok', scope: 'global', tpm: 50 }]))
    );
    const res = await chat(makeApp(), { max_tokens: 1000 });
    expect(res.status).toBe(429);
    expect(res.headers.get('retry-after')).toBeNull();
    expect((await errorOf(res)).message).toContain('lower max_tokens');
  });

  test('failed calls are refunded their tokens but count as requests', async () => {
    setLocalRateLimits(
      parseRateLimitSection(
        section([{ id: 'r', scope: 'global', rpm: 5, tpm: 30 }])
      )
    );
    const app = makeApp({
      upstream: (c) => c.json({ error: { message: 'bad' } }, 500),
    });
    for (let i = 0; i < 3; i++) expect((await chat(app)).status).toBe(500);
    const res = await chat(app);
    expect(res.headers.get('x-ratelimit-remaining-tokens')).toBe('18');
    expect(res.headers.get('x-ratelimit-remaining-requests')).toBe('1');
  });

  test('a streamed call is charged its real usage once, at the end', async () => {
    const spy = spyStore();
    setRateLimitStores({ primaryFactory: () => spy.store });
    setLocalRateLimits(
      parseRateLimitSection(
        section([{ id: 'tok', scope: 'global', tpm: 1000, concurrency: 1 }])
      )
    );
    const enc = new TextEncoder();
    const chunks = [
      'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
      'data: {"choices":[],"usage":{"prompt_tokens":40,"completion_tokens":60}}\n\n',
      'data: [DONE]\n\n',
    ];
    const app = makeApp({
      upstream: () =>
        new Response(
          new ReadableStream({
            start(ctrl) {
              for (const ch of chunks) ctrl.enqueue(enc.encode(ch));
              ctrl.close();
            },
          }),
          { headers: { 'content-type': 'text/event-stream' } }
        ),
    });
    const res = await chat(app, { stream: true });
    expect(res.status).toBe(200);
    // The lease is held while the stream is open.
    expect((await chat(app)).status).toBe(429);
    expect(spy.calls.settle).toHaveLength(0);
    await res.text();
    await new Promise((r) => setTimeout(r, 0));
    expect(spy.calls.settle).toHaveLength(1);
    const [adjustments, leaseKeys] = spy.calls.settle[0];
    // Reserved 2 + 10 = 12; real usage 100.
    expect(adjustments).toEqual([expect.objectContaining({ delta: 88 })]);
    expect(leaseKeys).toHaveLength(1);
    // Released: the next call fits and sees 1000 - 100 - its own 12.
    const next = await chat(app);
    expect(next.status).toBe(200);
    expect(next.headers.get('x-ratelimit-remaining-tokens')).toBe('888');
  });

  test('concurrency leases are released when the handler throws', async () => {
    setLocalRateLimits(
      parseRateLimitSection(
        section([{ id: 'c', scope: 'global', concurrency: 1 }])
      )
    );
    let fail = true;
    const app = new Hono();
    app.use('*', psRateLimit({ now: () => T0 }));
    app.post('/v1/chat/completions', (c) => {
      if (fail) throw new Error('boom');
      return c.json({});
    });
    app.onError((_e, c) => c.json({ error: 'x' }, 500));
    expect((await chat(app)).status).toBe(500);
    fail = false;
    expect((await chat(app)).status).toBe(200);
  });

  describe('store errors', () => {
    const broken: RateLimitStore = {
      kind: 'redis',
      acquire: async () => {
        throw new Error('ECONNREFUSED');
      },
      settle: async () => undefined,
    };

    test('open (default): counted in memory instead, with a metric', async () => {
      setRateLimitStores({ primaryFactory: () => broken });
      setLocalRateLimits(
        parseRateLimitSection(section([{ id: 'g', scope: 'global', rpm: 1 }]))
      );
      const line =
        'ps_gateway_rate_limit_store_errors_total{op="acquire",outcome="memory_fallback"}';
      const before = metric(line);
      const app = makeApp();
      expect((await chat(app)).status).toBe(200);
      expect((await chat(app)).status).toBe(429); // the memory store counted
      expect(metric(line)).toBe(before + 2);
    });

    test('closed: 503 rate_limit_store_unavailable', async () => {
      setRateLimitStores({ primaryFactory: () => broken });
      setLocalRateLimits(
        parseRateLimitSection(
          section([{ id: 'g', scope: 'global', rpm: 1 }], {
            on_store_error: 'closed',
          })
        )
      );
      const res = await chat(makeApp());
      expect(res.status).toBe(503);
      expect((await errorOf(res)).code).toBe('rate_limit_store_unavailable');
    });
  });

  describe('central policy', () => {
    const rl = (rules: any[]) => ({ version: 1, rules });
    const bundle = (tenant: any, teams: any = {}, projects: any = {}) =>
      parsePolicyBundle({
        schema: 'ps-policy-bundle.v1',
        tenant: { version: 'default@1', sections: tenant },
        teams,
        projects,
      });

    test('replaces the local config', async () => {
      setLocalRateLimits(
        parseRateLimitSection(
          section([{ id: 'local', scope: 'global', rpm: 1 }])
        )
      );
      setCentralPolicy(
        bundle({
          rate_limits: rl([{ id: 'central', scope: 'global', rpm: 3 }]),
        })
      );
      const res = await chat(makeApp());
      expect(res.headers.get('x-ratelimit-limit-requests')).toBe('3');
      expect(rateLimitHealth({})).toEqual({
        rate_limits: { source: 'central', rules: 1, store: 'memory' },
      });
    });

    test("a team's own section adds to the tenant's; an inherited one is not counted twice", async () => {
      const tenant = rl([{ id: 'all', scope: 'global', rpm: 3 }]);
      setCentralPolicy(
        bundle(
          { rate_limits: tenant },
          {
            't-1': {
              name: 'payments',
              version: 'default@1+pay@1',
              sections: {
                rate_limits: rl([{ id: 'team', scope: 'team', rpm: 1 }]),
              },
            },
            't-2': {
              name: 'search',
              version: 'default@1+search@1',
              sections: { rate_limits: tenant },
            },
          }
        )
      );
      const app = makeApp();
      // By name: the team rule (1 rpm) applies on top of the tenant's.
      const pay = await chat(app, {}, { 'x-ps-team': 'payments' });
      expect(pay.headers.get('x-ratelimit-limit-requests')).toBe('1');
      expect((await chat(app, {}, { 'x-ps-team': 't-1' })).status).toBe(429);
      // t-2 inherits the tenant's section: only the tenant counter (3 rpm),
      // which has 1 call so far (the refused one was not charged).
      const search = await chat(app, {}, { 'x-ps-team': 'search' });
      expect(search.status).toBe(200);
      expect(search.headers.get('x-ratelimit-remaining-requests')).toBe('1');
      expect(search.headers.get('x-ratelimit-limit-requests')).toBe('3');
    });

    test("a project's team section applies to calls naming the project", async () => {
      const team = rl([{ id: 'team', scope: 'team', rpm: 1 }]);
      setCentralPolicy(
        bundle(
          {},
          {
            't-1': {
              name: 'payments',
              version: 'pay@1',
              sections: { rate_limits: team },
            },
          },
          {
            'p-1': {
              name: 'checkout',
              team_id: 't-1',
              version: 'pay@1',
              sections: { rate_limits: team },
            },
          }
        )
      );
      const app = makeApp();
      expect((await chat(app, {}, { 'x-ps-project': 'checkout' })).status).toBe(
        200
      );
      // Same team counter (the project inherits its team's section).
      expect((await chat(app, {}, { 'x-ps-team': 'payments' })).status).toBe(
        429
      );
    });

    test('a bundle with a bad rate_limits section is refused whole', () => {
      expect(() =>
        bundle({ rate_limits: rl([{ id: 'x', scope: 'global' }]) })
      ).toThrow('rate_limits');
    });
  });
});
