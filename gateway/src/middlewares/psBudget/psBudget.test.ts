import { Hono } from 'hono';
import { readFileSync } from 'fs';
import { join } from 'path';
import { budgetState, decide, matchBudgets, psBudget, Status } from './index';
import { budgetConfigFromText, parseBudgetConfig } from './config';
import {
  counterKey,
  counterTtl,
  encodeSegment,
  periodOf,
  reservationsKey,
} from './keys';
import { setBudgetRuntime } from './runtime';
import { BudgetStore, MemoryBudgetStore } from './store';
import { pickDowngrade } from './downgrade';
import { estimateCost } from '../psPricing';
import { defaultRegistry } from '../psMetrics/registry';
import { RoutePolicy } from '../router/types';
import { estimateInputTokens } from '../router/policy';

const vectorFile = (name: string) =>
  JSON.parse(
    readFileSync(
      join(__dirname, '../../../../schemas/budget/vectors', name),
      'utf8'
    )
  );
const keyVectors = vectorFile('keys.json');
const decisionVectors = vectorFile('decisions.json');

const NOW = Date.parse('2026-11-06T13:45:00.000Z');

describe('shared key format (schemas/budget/vectors/keys.json)', () => {
  test.each(keyVectors.vectors as any[])('$id', ({ input, expected }) => {
    const nowMs = Date.parse(input.now);
    const key = counterKey(
      input.namespace,
      input.scope,
      input.subject,
      input.window,
      periodOf(input.window, nowMs)
    );
    expect(key).toBe(expected.key);
    expect(counterTtl(input.window, nowMs)).toBe(expected.ttl_seconds);
  });

  test('reservations share the counter key as hash tag', () => {
    expect(reservationsKey('ps:budget:v1:a:global:_:day:2026-11-06')).toBe(
      '{ps:budget:v1:a:global:_:day:2026-11-06}:res'
    );
    expect(encodeSegment('a:{b}~')).toBe('a%3A%7Bb%7D~');
  });
});

describe('decisions (schemas/budget/vectors/decisions.json)', () => {
  const config = parseBudgetConfig(decisionVectors.budgets);
  test.each(decisionVectors.vectors as any[])('$id', (v) => {
    const matched = matchBudgets(config, v.attributes, NOW);
    const statuses: Status[] = matched.map((m) => {
      const spent = v.spent[m.budget.id] ?? null;
      return { ...m, spent, state: budgetState(m.budget, spent) };
    });
    const d = decide(statuses, v.on_store_error ?? config.onStoreError)!;
    const e = v.expected;
    if ('matched' in e)
      expect(matched.map((m) => m.budget.id)).toEqual(e.matched);
    if ('state' in e) expect(d.state).toBe(e.state);
    if ('budget_id' in e) expect(d.budgetId).toBe(e.budget_id);
    if ('action' in e) expect(d.action).toBe(e.action);
    if ('ceiling_usd' in e) expect(d.ceilingUsd).toBe(e.ceiling_usd);
  });
});

describe('parseBudgetConfig', () => {
  test('applies defaults', () => {
    const cfg = parseBudgetConfig({
      schema_version: 'budget.v1',
      budgets: [
        {
          id: 'all',
          scope: 'global',
          window: 'day',
          limit_usd: 10,
          mode: 'warn',
        },
        {
          id: 'bu',
          scope: 'business_unit',
          subject: 'marketing',
          window: 'month',
          limit_usd: 5,
          mode: 'downgrade',
          downgrade_model: 'gpt-4o-mini',
        },
      ],
    });
    expect(cfg).toMatchObject({ namespace: 'default', onStoreError: 'open' });
    expect(cfg.budgets[0]).toMatchObject({ warnAt: 0.8, limitUsd: 10 });
    expect(cfg.budgets[1]).toMatchObject({ subject: 'marketing' });
  });

  const one = (b: any) => ({ schema_version: 'budget.v1', budgets: [b] });
  const ok = {
    id: 'x',
    scope: 'global',
    window: 'day',
    limit_usd: 1,
    mode: 'warn',
  };
  test.each([
    [{ schema_version: 'budget.v2', budgets: [] }, 'schema_version'],
    [one({ ...ok, scope: 'tenant' }), 'scope'],
    [one({ ...ok, window: 'year' }), 'window'],
    [one({ ...ok, limit_usd: -1 }), 'limit_usd'],
    [one({ ...ok, mode: 'throttle' }), 'mode'],
    [one({ ...ok, id: 'Upper' }), 'budget id'],
    [one({ ...ok, subject: 'x' }), 'no subject'],
    [one({ ...ok, scope: 'use_case' }), 'needs a subject'],
    [one({ ...ok, downgrade_model: 'm' }), 'downgrade_model'],
    [{ ...one(ok), on_store_error: 'maybe' }, 'on_store_error'],
    [{ schema_version: 'budget.v1', budgets: [ok, ok] }, 'duplicate'],
  ])('rejects %j', (doc, msg) => {
    expect(() => parseBudgetConfig(doc)).toThrow(msg);
  });

  test('empty text means no budgets; bad JSON throws', () => {
    expect(budgetConfigFromText('')).toBeUndefined();
    expect(() => budgetConfigFromText('{')).toThrow('not JSON');
    expect(
      budgetConfigFromText('{"schema_version":"budget.v1","budgets":[]}')
    ).toBeUndefined();
  });
});

describe('MemoryBudgetStore', () => {
  const base = { key: 'k', nowMs: 1000, reservationTtlMs: 1000 };

  test('a limited reservation is an atomic check', async () => {
    const s = new MemoryBudgetStore();
    expect(
      (await s.reserve({ ...base, id: 'a', amount: 6, limit: 10 })).ok
    ).toBe(true);
    expect(
      (await s.reserve({ ...base, id: 'b', amount: 6, limit: 10 })).ok
    ).toBe(true);
    const denied = await s.reserve({ ...base, id: 'c', amount: 1, limit: 10 });
    expect(denied).toEqual({ ok: false, spent: 0, reserved: 12 });
    expect((await s.reserve({ ...base, id: 'd', amount: 5 })).ok).toBe(true);
    expect(await s.read(['k'], 1000)).toEqual([{ spent: 0, reserved: 17 }]);
  });

  test('settle swaps the reservation for the actual cost', async () => {
    const s = new MemoryBudgetStore();
    await s.reserve({ ...base, id: 'a', amount: 6 });
    await s.settle('k', 'a', 6, 2.5, 100);
    expect(await s.read(['k'], 1000)).toEqual([{ spent: 2.5, reserved: 0 }]);
  });

  test('reservations lapse; the counter expires with its TTL', async () => {
    let clock = 0;
    const s = new MemoryBudgetStore(() => clock);
    await s.reserve({ ...base, id: 'a', amount: 6 });
    expect(await s.read(['k'], 2000)).toEqual([{ spent: 0, reserved: 0 }]);
    await s.settle('k', 'b', 0, 1, 10);
    clock = 9_999;
    expect((await s.read(['k'], 0))[0].spent).toBe(1);
    clock = 10_000;
    expect((await s.read(['k'], 0))[0].spent).toBe(0);
  });
});

// ── middleware ────────────────────────────────────────────────────────────

const USAGE = { prompt_tokens: 1000, completion_tokens: 1000 };
const MINI_COST = estimateCost('openai', 'gpt-4o-mini', {
  tokens_in: 1000,
  tokens_out: 1000,
})!;

function install(
  budgets: any[],
  store: BudgetStore = new MemoryBudgetStore(),
  extra: Record<string, unknown> = {}
) {
  setBudgetRuntime({
    config: parseBudgetConfig({
      schema_version: 'budget.v1',
      budgets,
      ...extra,
    }),
    store,
    reservationTtlMs: 60_000,
  });
  return store;
}

function makeApp(
  opts: { policy?: RoutePolicy; status?: number; stream?: boolean } = {}
) {
  const app = new Hono();
  const seen: any[] = [];
  app.use('*', psBudget({ policy: opts.policy, now: () => NOW }));
  app.post('/v1/chat/completions', async (c) => {
    const body = await c.req.json();
    seen.push({
      model: body.model,
      budget: c.get('psBudget'),
      route: c.get('psRoute'),
    });
    if (opts.stream) {
      const chunks = [
        `data: ${JSON.stringify({ model: body.model, choices: [{ delta: { content: 'hi' } }] })}\n\n`,
        `data: ${JSON.stringify({ model: body.model, choices: [], usage: USAGE })}\n\n`,
        'data: [DONE]\n\n',
      ];
      return new Response(chunks.join(''), {
        headers: { 'content-type': 'text/event-stream' },
      });
    }
    return c.json(
      { model: body.model, usage: USAGE },
      (opts.status ?? 200) as any
    );
  });
  app.post('/v1/files', (c) => c.json({ ok: true }));
  return { app, seen };
}

const post = (
  app: Hono,
  headers: Record<string, string> = {},
  body: any = {
    model: 'gpt-4o-mini',
    messages: [{ role: 'user', content: 'hi' }],
    max_tokens: 1000,
  }
) =>
  app.request('/v1/chat/completions', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-portkey-provider': 'openai',
      ...headers,
    },
    body: JSON.stringify(body),
  });

const key = (scope: string, subject: string, window = 'month') =>
  counterKey(
    'default',
    scope as any,
    subject,
    window as any,
    periodOf(window as any, NOW)
  );
const spend = (store: BudgetStore, k: string, usd: number) =>
  store.settle(k, 'seed', 0, usd, 86_400);

afterEach(() => {
  setBudgetRuntime(undefined);
  jest.restoreAllMocks();
});

describe('psBudget middleware', () => {
  test('counts the actual cost of each call against matching budgets', async () => {
    const store = install([
      {
        id: 'mkt',
        scope: 'business_unit',
        subject: 'marketing',
        window: 'month',
        limit_usd: 10,
        mode: 'warn',
      },
      {
        id: 'other',
        scope: 'business_unit',
        subject: 'sales',
        window: 'month',
        limit_usd: 10,
        mode: 'warn',
      },
    ]);
    const { app, seen } = makeApp();
    const res = await post(app, { 'x-ps-business-unit': 'marketing' });
    expect(res.status).toBe(200);
    expect(res.headers.get('x-ps-budget-state')).toBe('ok');
    expect(seen[0].budget).toEqual({
      state: 'ok',
      budgetId: 'mkt',
      action: 'none',
    });
    const [mkt, sales] = await store.read(
      [key('business_unit', 'marketing'), key('business_unit', 'sales')],
      NOW
    );
    expect(mkt.spent).toBeCloseTo(MINI_COST, 12);
    expect(mkt.reserved).toBe(0);
    expect(sales).toEqual({ spent: 0, reserved: 0 });
  });

  test('calls without the attribute, and non-billed paths, are not counted', async () => {
    const store = install([
      {
        id: 'uc',
        scope: 'use_case',
        subject: '*',
        window: 'month',
        limit_usd: 10,
        mode: 'warn',
      },
    ]);
    const { app } = makeApp();
    const res = await post(app);
    expect(res.headers.get('x-ps-budget-state')).toBeNull();
    expect((await app.request('/v1/files', { method: 'POST' })).status).toBe(
      200
    );
    expect(await store.read([key('use_case', 'x')], NOW)).toEqual([
      { spent: 0, reserved: 0 },
    ]);
  });

  test('block: refuses with 429 budget_exceeded once exhausted', async () => {
    const store = install([
      {
        id: 'support',
        scope: 'use_case',
        subject: 'support',
        window: 'month',
        limit_usd: 1,
        mode: 'block',
      },
    ]);
    await spend(store, key('use_case', 'support'), 1);
    const { app, seen } = makeApp();
    const res = await post(app, { 'x-ps-use-case': 'support' });
    expect(res.status).toBe(429);
    const json: any = await res.json();
    expect(json.error).toMatchObject({
      code: 'budget_exceeded',
      budget_id: 'support',
      resets_at: '2026-12-01T00:00:00.000Z',
    });
    expect(json.error.message).toContain('Budget "support"');
    expect(Number(res.headers.get('retry-after'))).toBe(
      Math.ceil((Date.parse('2026-12-01T00:00:00Z') - NOW) / 1000)
    );
    expect(seen).toHaveLength(0);
    expect(defaultRegistry.render()).toMatch(
      /ps_gateway_budget_decisions_total\{budget="support",state="exceeded",action="block"\} \d+/
    );
  });

  test('block: concurrent calls see each other through reservations', async () => {
    const est = estimateCost('openai', 'gpt-4o-mini', {
      tokens_in: estimateInputTokens([{ role: 'user', content: 'hi' }]),
      tokens_out: 1000,
    })!;
    install([
      {
        id: 'g',
        scope: 'global',
        window: 'day',
        limit_usd: est * 2.5,
        mode: 'block',
      },
    ]);
    const app = new Hono();
    app.use('*', psBudget({ now: () => NOW }));
    app.post('/v1/chat/completions', async (c) => {
      await new Promise((r) => setTimeout(r, 20));
      return c.json({
        model: 'gpt-4o-mini',
        usage: { prompt_tokens: 0, completion_tokens: 0 },
      });
    });
    const codes = (
      await Promise.all(Array.from({ length: 8 }, () => post(app)))
    ).map((r) => r.status);
    // In flight: 0, est, 2est are under the limit; the 4th sees 3est.
    expect(codes.filter((s) => s === 200)).toHaveLength(3);
    expect(codes.filter((s) => s === 429)).toHaveLength(5);
  });

  test('two gateway replicas share one store', async () => {
    const shared = install([
      {
        id: 'g',
        scope: 'global',
        window: 'day',
        limit_usd: MINI_COST * 0.5,
        mode: 'block',
      },
    ]);
    expect((await post(makeApp().app)).status).toBe(200);
    expect((await post(makeApp().app)).status).toBe(429);
    const [usage] = await shared.read([key('global', '_', 'day')], NOW);
    expect(usage.spent).toBeCloseTo(MINI_COST, 12);
  });

  test('warn: serves the call and reports the state', async () => {
    const store = install([
      {
        id: 'mkt',
        scope: 'owner',
        subject: '*',
        window: 'month',
        limit_usd: 1,
        mode: 'warn',
        warn_at: 0.5,
      },
    ]);
    await spend(store, key('owner', 'a@x.com'), 0.6);
    const { app, seen } = makeApp();
    let res = await post(app, { 'x-ps-owner': 'a@x.com' });
    expect(res.status).toBe(200);
    expect(res.headers.get('x-ps-budget-state')).toBe('warn');
    expect(seen[0].budget).toEqual({
      state: 'warn',
      action: 'none',
      budgetId: 'mkt',
    });

    await spend(store, key('owner', 'a@x.com'), 1);
    res = await post(app, { 'x-ps-owner': 'a@x.com' });
    expect(res.status).toBe(200);
    expect(res.headers.get('x-ps-budget-state')).toBe('exceeded');
  });

  test('a failed call or a cache hit costs nothing', async () => {
    const store = install([
      { id: 'g', scope: 'global', window: 'day', limit_usd: 1, mode: 'warn' },
    ]);
    await post(makeApp({ status: 500 }).app);
    const cached = new Hono();
    cached.use('*', psBudget({ now: () => NOW }));
    cached.post('/v1/chat/completions', (c) => {
      c.header('x-portkey-cache-status', 'HIT');
      return c.json({ model: 'gpt-4o-mini', usage: USAGE });
    });
    expect((await post(cached)).status).toBe(200);
    expect(await store.read([key('global', '_', 'day')], NOW)).toEqual([
      { spent: 0, reserved: 0 },
    ]);
  });

  test('a stream settles with the usage at its end', async () => {
    const store = install([
      { id: 'g', scope: 'global', window: 'day', limit_usd: 1, mode: 'warn' },
    ]);
    const res = await post(makeApp({ stream: true }).app);
    expect(res.headers.get('x-ps-budget-state')).toBe('ok');
    await res.text();
    await new Promise((r) => setTimeout(r, 0));
    const [usage] = await store.read([key('global', '_', 'day')], NOW);
    expect(usage.reserved).toBe(0);
    expect(usage.spent).toBeCloseTo(MINI_COST, 12);
  });

  const broken: BudgetStore = {
    kind: 'redis',
    read: async () => {
      throw new Error('down');
    },
    reserve: async () => {
      throw new Error('down');
    },
    settle: async () => {
      throw new Error('down');
    },
  };
  const blockG = {
    id: 'g',
    scope: 'global',
    window: 'day',
    limit_usd: 1,
    mode: 'block',
  };

  test('store down, on_store_error open: serve with budget_state unknown', async () => {
    jest.spyOn(console, 'warn').mockImplementation(() => {});
    install([blockG], broken);
    const { app, seen } = makeApp();
    const res = await post(app);
    expect(res.status).toBe(200);
    expect(res.headers.get('x-ps-budget-state')).toBe('unknown');
    expect(seen[0].budget).toEqual({
      state: 'unknown',
      budgetId: 'g',
      action: 'none',
    });
  });

  test('store down, on_store_error closed: block budgets answer 503', async () => {
    jest.spyOn(console, 'warn').mockImplementation(() => {});
    install([blockG], broken, { on_store_error: 'closed' });
    const res = await post(makeApp().app);
    expect(res.status).toBe(503);
    expect(((await res.json()) as any).error.code).toBe(
      'budget_store_unavailable'
    );
  });
});

// ── downgrade ─────────────────────────────────────────────────────────────

const SOVEREIGN: RoutePolicy = {
  defaultGroup: 'cheap',
  groupOrder: ['cheap', 'frontier'],
  qualityToGroup: { draft: 'cheap', balanced: 'cheap', critical: 'frontier' },
  sensitivity: { unclassifiedDefault: 'internal' },
  dataPolicy: {
    internal: { jurisdictions: ['any'] },
    confidential: { jurisdictions: ['EEA'] },
  },
  groups: {
    cheap: [
      {
        model: 'us-mini',
        group: 'cheap',
        inputCostPer1k: 0.0001,
        outputCostPer1k: 0.0001,
        jurisdiction: 'US',
      },
      {
        model: 'eu-small',
        group: 'cheap',
        inputCostPer1k: 0.001,
        outputCostPer1k: 0.001,
        jurisdiction: 'SE',
      },
      {
        model: 'eu-medium',
        group: 'cheap',
        inputCostPer1k: 0.003,
        outputCostPer1k: 0.003,
        jurisdiction: 'DE',
      },
    ],
    frontier: [
      {
        model: 'eu-large',
        group: 'frontier',
        inputCostPer1k: 0.01,
        outputCostPer1k: 0.01,
        jurisdiction: 'FR',
      },
    ],
  },
};

describe('downgrade', () => {
  test('pickDowngrade: most capable cheaper tier under the ceiling, compliant only', () => {
    const req = { approxInputTokens: 1000, maxOutputTokens: 1000 };
    const eea = { jurisdictions: ['EEA'] };
    // eu-large costs 0.02; a 0.007 ceiling fits eu-medium (0.006).
    expect(pickDowngrade(SOVEREIGN, req, eea, 0.02, 0.007)?.tier.model).toBe(
      'eu-medium'
    );
    // Nothing fits: the cheapest compliant one, never us-mini.
    expect(pickDowngrade(SOVEREIGN, req, eea, 0.02, 0)?.tier.model).toBe(
      'eu-small'
    );
    expect(pickDowngrade(SOVEREIGN, req, undefined, 0.02, 0)?.tier.model).toBe(
      'us-mini'
    );
    // Already the cheapest compliant model.
    expect(pickDowngrade(SOVEREIGN, req, eea, 0.002, 0)).toBeNull();
  });

  test('pickDowngrade keeps to a use case eval set', () => {
    const policy: RoutePolicy = {
      ...SOVEREIGN,
      useCases: {
        legal: {
          evalSet: 'legal-v1',
          eligibleModels: ['eu-medium', 'eu-large'],
        },
      },
    };
    expect(
      pickDowngrade(
        policy,
        { approxInputTokens: 1000, maxOutputTokens: 1000, useCase: 'legal' },
        undefined,
        1,
        0
      )?.tier.model
    ).toBe('eu-medium');
  });

  const body = (model: string) => ({
    model,
    messages: [{ role: 'user', content: 'hi' }],
    max_tokens: 1000,
  });
  const team = {
    id: 'team',
    scope: 'use_case',
    subject: 'rag',
    window: 'month',
    limit_usd: 1,
    mode: 'downgrade',
  };

  test('an exhausted budget moves the call to a cheaper compliant model', async () => {
    const store = install([team]);
    await spend(store, key('use_case', 'rag'), 1);
    const { app, seen } = makeApp({ policy: SOVEREIGN });
    const res = await post(
      app,
      { 'x-ps-use-case': 'rag', 'x-ps-data-classification': 'confidential' },
      body('eu-large')
    );
    expect(res.status).toBe(200);
    expect(seen[0].model).toBe('eu-small');
    expect(seen[0].budget).toEqual({
      state: 'exceeded',
      budgetId: 'team',
      action: 'downgraded',
    });
    expect(seen[0].route.reason).toBe('budget+budget-downgrade');
  });

  test('a ceiling below the estimate clamps before the budget is spent', async () => {
    const store = install([team]);
    await spend(store, key('use_case', 'rag'), 1 - 0.007);
    const { app, seen } = makeApp({ policy: SOVEREIGN });
    await post(app, { 'x-ps-use-case': 'rag' }, body('eu-large'));
    expect(seen[0].model).toBe('eu-medium');
  });

  test('with no cheaper compliant model the call keeps its model', async () => {
    const store = install([team]);
    await spend(store, key('use_case', 'rag'), 1);
    const { app, seen } = makeApp({ policy: SOVEREIGN });
    const res = await post(
      app,
      { 'x-ps-use-case': 'rag', 'x-ps-data-classification': 'confidential' },
      body('eu-small')
    );
    expect(res.status).toBe(200);
    expect(seen[0].model).toBe('eu-small');
    expect(seen[0].budget.action).toBe('none');
  });

  test('a budget with room left keeps the requested model', async () => {
    install([{ ...team, limit_usd: 100 }]);
    const { app, seen } = makeApp({ policy: SOVEREIGN });
    await post(app, { 'x-ps-use-case': 'rag' }, body('eu-large'));
    expect(seen[0].model).toBe('eu-large');
    expect(seen[0].budget).toEqual({
      state: 'ok',
      budgetId: 'team',
      action: 'none',
    });
  });
});
