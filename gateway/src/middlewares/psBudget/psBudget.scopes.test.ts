/**
 * Budgets #29 / #347: team, project, user, service and model scopes, token
 * budgets, threshold notifications and the central policy's budgets section.
 */
import { createHmac } from 'crypto';
import { Hono } from 'hono';
import { psBudget } from './index';
import { parseBudgetConfig, parseBudgetsSection } from './config';
import { counterKey, periodOf } from './keys';
import { getBudgetRuntime, setBudgetRuntime, setBudgetStore } from './runtime';
import { BudgetStore, MemoryBudgetStore } from './store';
import {
  notifyThresholds,
  signPayload,
  thresholdCrossings,
  webhookUrlOf,
} from './notify';
import {
  EgressRefusedError,
  setEgressResolverForTests,
} from '../../utils/egressGuard';
import { defaultRegistry } from '../psMetrics/registry';
import { OIDC_IDENTITY_KEY } from '../psOidc/identity';
import {
  parsePolicyBundle,
  PolicyBundleError,
  setCentralPolicy,
} from '../policyPull/state';

const NOW = Date.parse('2026-11-06T13:45:00.000Z');
const USAGE = { prompt_tokens: 1000, completion_tokens: 1000 };

function install(budgets: any[], store: BudgetStore = new MemoryBudgetStore()) {
  setBudgetRuntime({
    config: parseBudgetConfig({ schema_version: 'budget.v1', budgets }),
    store,
    reservationTtlMs: 60_000,
  });
  return store;
}

function makeApp(subjectHash?: string) {
  const app = new Hono();
  const seen: any[] = [];
  app.use('*', async (c, next) => {
    if (subjectHash)
      c.set(OIDC_IDENTITY_KEY as any, { subjectHash, subject: 'u' } as any);
    return next();
  });
  app.use('*', psBudget({ now: () => NOW }));
  app.post('/v1/chat/completions', async (c) => {
    const body = await c.req.json();
    seen.push({ model: body.model, budget: c.get('psBudget') });
    return c.json({ model: body.model, usage: USAGE });
  });
  return { app, seen };
}

const post = (
  app: Hono,
  headers: Record<string, string> = {},
  model = 'gpt-4o-mini'
) =>
  app.request('/v1/chat/completions', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-portkey-provider': 'openai',
      ...headers,
    },
    body: JSON.stringify({
      model,
      messages: [{ role: 'user', content: 'hi' }],
      max_tokens: 1000,
    }),
  });

const key = (
  scope: string,
  subject: string,
  unit: 'usd' | 'tokens' = 'usd',
  window = 'month'
) =>
  counterKey(
    'default',
    scope as any,
    subject,
    window as any,
    periodOf(window as any, NOW),
    unit
  );

const budget = (b: Record<string, unknown>) => ({
  window: 'month',
  limit_usd: 10,
  mode: 'warn',
  ...b,
});

const tokens = (b: Record<string, unknown>) => {
  const { limit_usd: _drop, ...rest } = budget(b) as any;
  return { limit_tokens: 100_000, ...rest };
};

beforeEach(() => {
  jest.spyOn(console, 'warn').mockImplementation(() => {});
});

afterEach(() => {
  setBudgetRuntime(undefined);
  setBudgetStore(undefined);
  setCentralPolicy(undefined);
  setEgressResolverForTests(null);
  delete process.env.PS_BUDGET_WEBHOOK_URL;
  jest.restoreAllMocks();
});

describe('config', () => {
  test('new scopes and token budgets parse', () => {
    const cfg = parseBudgetConfig({
      schema_version: 'budget.v1',
      budgets: [
        budget({ id: 't', scope: 'team', subject: 'data' }),
        tokens({ id: 'p', scope: 'project', subject: '*', mode: 'block' }),
        budget({ id: 'u', scope: 'user', subject: '*' }),
        budget({ id: 's', scope: 'service', subject: 'billing' }),
        budget({ id: 'm', scope: 'model', subject: 'gpt-4o' }),
      ],
    });
    expect(cfg.budgets[0]).toMatchObject({
      unit: 'usd',
      limit: 10,
      limitUsd: 10,
    });
    expect(cfg.budgets[1]).toMatchObject({
      unit: 'tokens',
      limit: 100_000,
      limitTokens: 100_000,
    });
    expect(cfg.budgets[1].limitUsd).toBeUndefined();
  });

  const one = (b: any) => ({ schema_version: 'budget.v1', budgets: [b] });
  test.each([
    [
      one({ ...budget({ id: 'x', scope: 'global' }), limit_tokens: 5 }),
      'exactly one',
    ],
    [
      one({ id: 'x', scope: 'global', window: 'day', mode: 'warn' }),
      'exactly one',
    ],
    [one(tokens({ id: 'x', scope: 'global', limit_tokens: 1.5 })), 'integer'],
    [
      one(tokens({ id: 'x', scope: 'global', mode: 'downgrade' })),
      'token budget',
    ],
  ])('rejects %#', (doc, msg) => {
    expect(() => parseBudgetConfig(doc)).toThrow(msg);
  });

  test('key vector: token counters get the :tokens suffix', () => {
    expect(key('team', 'data', 'tokens')).toBe(
      'ps:budget:v1:default:team:data:month:2026-11:tokens'
    );
  });
});

describe('scopes', () => {
  test('team, project and service come from the X-PS-* headers', async () => {
    const store = install([
      budget({ id: 'team', scope: 'team', subject: 'data' }),
      budget({ id: 'proj', scope: 'project', subject: '*' }),
      budget({ id: 'svc', scope: 'service', subject: 'billing' }),
    ]);
    const { app } = makeApp();
    await post(app, {
      'x-ps-team': 'data',
      'x-ps-project': 'chat',
      'x-ps-service': 'billing',
    });
    const usage = await store.read(
      [key('team', 'data'), key('project', 'chat'), key('service', 'billing')],
      NOW
    );
    expect(usage.every((u) => u.spent > 0 && u.reserved === 0)).toBe(true);
    await post(app, { 'x-ps-team': 'other' });
    expect((await store.read([key('team', 'other')], NOW))[0].spent).toBe(0);
  });

  test('model budgets match the requested model', async () => {
    const store = install([
      budget({
        id: 'big',
        scope: 'model',
        subject: 'gpt-4o',
        limit_usd: 1e-9,
        mode: 'block',
      }),
    ]);
    const { app } = makeApp();
    expect((await post(app, {}, 'gpt-4o')).status).toBe(200);
    expect((await post(app, {}, 'gpt-4o')).status).toBe(429);
    expect((await post(app, {}, 'gpt-4o-mini')).status).toBe(200);
    expect(
      (await store.read([key('model', 'gpt-4o')], NOW))[0].spent
    ).toBeGreaterThan(0);
  });

  test('user budgets need a verified OIDC identity (its subject hash)', async () => {
    const SUBJECT = 'b'.repeat(32);
    const store = install([
      budget({
        id: 'per-user',
        scope: 'user',
        subject: '*',
        limit_usd: 1e-9,
        mode: 'block',
      }),
    ]);
    const anon = makeApp().app;
    expect((await post(anon)).status).toBe(200);
    expect((await post(anon)).status).toBe(200); // no identity, no user budget
    const verified = makeApp(SUBJECT).app;
    expect((await post(verified)).status).toBe(200);
    expect((await post(verified)).status).toBe(429);
    expect(
      (await store.read([key('user', SUBJECT)], NOW))[0].spent
    ).toBeGreaterThan(0);
  });
});

describe('token budgets', () => {
  test('count the actual tokens and block once exhausted', async () => {
    const store = install([
      tokens({
        id: 'tok',
        scope: 'team',
        subject: 'data',
        limit_tokens: 3000,
        mode: 'block',
      }),
    ]);
    const { app } = makeApp();
    expect((await post(app, { 'x-ps-team': 'data' })).status).toBe(200);
    expect(await store.read([key('team', 'data', 'tokens')], NOW)).toEqual([
      { spent: 2000, reserved: 0 },
    ]);
    expect((await post(app, { 'x-ps-team': 'data' })).status).toBe(200);
    const res = await post(app, { 'x-ps-team': 'data' });
    expect(res.status).toBe(429);
    const body: any = await res.json();
    expect(body.error.message).toContain('4000 tokens of 3000 tokens used');
    // The USD counter for the same subject is untouched.
    expect((await store.read([key('team', 'data')], NOW))[0].spent).toBe(0);
  });

  test('the estimate is reserved in tokens while the call runs', async () => {
    const store = install([
      tokens({
        id: 'tok',
        scope: 'global',
        limit_tokens: 10_000,
        mode: 'block',
      }),
    ]);
    let during: any;
    const app = new Hono();
    app.use('*', psBudget({ now: () => NOW }));
    app.post('/v1/chat/completions', async (c) => {
      during = await store.read(
        [
          counterKey(
            'default',
            'global',
            '_',
            'month',
            periodOf('month', NOW),
            'tokens'
          ),
        ],
        NOW
      );
      return c.json({ model: 'gpt-4o-mini', usage: USAGE });
    });
    await post(app);
    // ceil(2 chars / 4) input + max_tokens 1000.
    expect(during).toEqual([{ spent: 0, reserved: 1001 }]);
  });
});

describe('threshold notifications', () => {
  const crossings = () =>
    defaultRegistry
      .render()
      .split('\n')
      .filter((l) =>
        l.startsWith(
          'ps_gateway_budget_threshold_crossings_total{budget="notify'
        )
      );

  test('a settle crossing warn, then the limit, is reported once each', async () => {
    const log = console.warn as jest.Mock;
    install([
      budget({
        id: 'notify-team',
        scope: 'team',
        subject: 'data',
        limit_usd: 0.002,
        warn_at: 0.5,
      }),
    ]);
    const { app } = makeApp();
    // gpt-4o-mini, 1000 in + 1000 out: about $0.00075 per call.
    for (let i = 0; i < 4; i++) await post(app, { 'x-ps-team': 'data' });
    const lines = crossings();
    expect(lines).toEqual(
      expect.arrayContaining([
        'ps_gateway_budget_threshold_crossings_total{budget="notify-team",window="month",threshold="warn"} 1',
        'ps_gateway_budget_threshold_crossings_total{budget="notify-team",window="month",threshold="exceeded"} 1',
      ])
    );
    const events = log.mock.calls
      .map((c) => c[0])
      .filter((l) => typeof l === 'string' && l.includes('"ps.budget"'))
      .map((l) => JSON.parse(l));
    expect(events.map((e) => e.threshold)).toEqual(['warn', 'exceeded']);
    expect(events[0]).toMatchObject({
      type: 'budget.threshold_crossed',
      budget_id: 'notify-team',
      scope: 'team',
      subject: 'data',
      period: '2026-11',
      unit: 'usd',
      limit: 0.002,
      namespace: 'default',
    });
  });

  test('thresholdCrossings: before < mark <= after; warn_at 1 has no warn', () => {
    const [b] = parseBudgetConfig({
      schema_version: 'budget.v1',
      budgets: [
        budget({ id: 'x', scope: 'global', limit_usd: 10, warn_at: 1 }),
      ],
    }).budgets;
    const on = [{ budget: b, subject: '_', period: '2026-11' }];
    expect(thresholdCrossings(on, 'n', 0, 9.99, NOW)).toEqual([]);
    expect(
      thresholdCrossings(on, 'n', 5, 10, NOW).map((e) => e.threshold)
    ).toEqual(['exceeded']);
    expect(thresholdCrossings(on, 'n', 10, 11, NOW)).toEqual([]);
  });

  test('the webhook gets the event, signed like the collector webhooks', async () => {
    const [b] = parseBudgetConfig({
      schema_version: 'budget.v1',
      budgets: [budget({ id: 'x', scope: 'global', limit_usd: 1 })],
    }).budgets;
    const events = thresholdCrossings(
      [{ budget: b, subject: '_', period: '2026-11' }],
      'acme',
      0.5,
      1.5,
      NOW
    );
    const calls: [string, RequestInit][] = [];
    const fetch = async (url: string, init: RequestInit) => {
      calls.push([url, init]);
      return new Response(null, { status: 204 });
    };
    await notifyThresholds(
      events,
      {
        PS_BUDGET_WEBHOOK_URL: 'https://hooks.example.com/budget',
        PS_BUDGET_WEBHOOK_SECRET: 's3cret',
      },
      fetch
    );
    expect(calls.map((c) => c[0])).toEqual([
      'https://hooks.example.com/budget',
      'https://hooks.example.com/budget',
    ]);
    // The two posts run concurrently, so they can arrive in either order.
    const exceeded = calls.find(
      (c) => JSON.parse(c[1].body as string).threshold === 'exceeded'
    )!;
    expect(exceeded).toBeDefined();
    const headers = exceeded[1].headers as Record<string, string>;
    expect(headers['Prompt-Shields-Event-Type']).toBe(
      'budget.threshold_crossed'
    );
    const sig = headers['Prompt-Shields-Signature'];
    const [, t, v1] = sig.match(/^t=(\d+),v1=([0-9a-f]{64})$/)!;
    const body = exceeded[1].body as string;
    expect(
      createHmac('sha256', 's3cret').update(`${t}.${body}`).digest('hex')
    ).toBe(v1);
    expect(JSON.parse(body)).toMatchObject({
      budget_id: 'x',
      threshold: 'exceeded',
    });
    expect(await signPayload('k', 'b', 1)).toBe(
      `t=1,v1=${createHmac('sha256', 'k').update('1.b').digest('hex')}`
    );
  });

  test('a webhook failure never throws; http needs an explicit opt-in', async () => {
    expect(
      webhookUrlOf({ PS_BUDGET_WEBHOOK_URL: 'http://hooks.example.com' })
    ).toBeUndefined();
    expect(
      webhookUrlOf({
        PS_BUDGET_WEBHOOK_URL: 'http://hooks.example.com',
        PS_BUDGET_WEBHOOK_ALLOW_HTTP: 'true',
      })
    ).toBe('http://hooks.example.com');
    expect(
      webhookUrlOf({ PS_BUDGET_WEBHOOK_URL: 'not a url' })
    ).toBeUndefined();
    const [b] = parseBudgetConfig({
      schema_version: 'budget.v1',
      budgets: [budget({ id: 'y', scope: 'global', limit_usd: 1 })],
    }).budgets;
    const events = thresholdCrossings(
      [{ budget: b, subject: '_', period: 'p' }],
      'n',
      0,
      2,
      NOW
    );
    await expect(
      notifyThresholds(
        events,
        { PS_BUDGET_WEBHOOK_URL: 'https://hooks.example.com' },
        async () => {
          throw new Error('down');
        }
      )
    ).resolves.toBeUndefined();
  });
});

describe('webhook egress', () => {
  test('a webhook host resolving to the metadata service is refused without a request', async () => {
    setEgressResolverForTests(async () => [
      { address: '169.254.169.254', family: 4 },
    ]);
    const fetchSpy = jest.spyOn(globalThis, 'fetch');
    const warn = console.warn as jest.Mock;
    const [b] = parseBudgetConfig({
      schema_version: 'budget.v1',
      budgets: [budget({ id: 'z', scope: 'global', limit_usd: 1 })],
    }).budgets;
    const events = thresholdCrossings(
      [{ budget: b, subject: '_', period: 'p' }],
      'n',
      0,
      2,
      NOW
    );
    await notifyThresholds(events, {
      PS_BUDGET_WEBHOOK_URL: 'https://hooks.example.com/budget',
      PS_BUDGET_WEBHOOK_SECRET: 'never-logged',
    });
    expect(fetchSpy).not.toHaveBeenCalled();
    const logged = warn.mock.calls.map((c) => c.join(' ')).join('\n');
    expect(logged).toContain(
      new EgressRefusedError('hooks.example.com').message
    );
    expect(logged).not.toContain('never-logged');
  });
});

describe('central policy budgets section', () => {
  const bundle = (sections: Record<string, unknown>) => ({
    schema: 'ps-policy-bundle.v1',
    tenant: { version: 'default@1', sections },
    teams: {},
    projects: {},
  });
  const section = (budgets: any[], extra: Record<string, unknown> = {}) => ({
    version: 1,
    budgets,
    ...extra,
  });

  test('replaces the local budgets, keeping the namespace and store', async () => {
    const store = install([
      budget({ id: 'local', scope: 'global', limit_usd: 1e-9, mode: 'block' }),
    ]);
    setCentralPolicy(
      parsePolicyBundle(
        bundle({
          budgets: section(
            [
              budget({
                id: 'central',
                scope: 'team',
                subject: 'data',
                limit_usd: 1e-9,
                mode: 'block',
              }),
            ],
            { on_store_error: 'closed' }
          ),
        })
      )
    );
    const rt = getBudgetRuntime({})!;
    expect(rt.store).toBe(store);
    expect(rt.config).toMatchObject({
      namespace: 'default',
      onStoreError: 'closed',
    });
    expect(rt.config.budgets.map((b) => b.id)).toEqual(['central']);
    const { app } = makeApp();
    expect((await post(app)).status).toBe(200); // the local global budget is gone
    expect((await post(app, { 'x-ps-team': 'data' })).status).toBe(200);
    expect((await post(app, { 'x-ps-team': 'data' })).status).toBe(429);
  });

  test('works without a local document; an empty section turns budgets off', () => {
    setBudgetRuntime(null);
    setCentralPolicy(
      parsePolicyBundle(
        bundle({ budgets: section([budget({ id: 'c', scope: 'global' })]) })
      )
    );
    expect(
      getBudgetRuntime({ PS_BUDGET_NAMESPACE: 'acme' })!.config.namespace
    ).toBe('acme');
    install([budget({ id: 'local', scope: 'global' })]);
    setCentralPolicy(parsePolicyBundle(bundle({ budgets: section([]) })));
    expect(getBudgetRuntime({})).toBeNull();
    setCentralPolicy(parsePolicyBundle(bundle({})));
    expect(getBudgetRuntime({})!.config.budgets.map((b) => b.id)).toEqual([
      'local',
    ]);
  });

  test('an invalid section refuses the whole bundle', () => {
    expect(() =>
      parsePolicyBundle(
        bundle({ budgets: section([budget({ id: 'x', scope: 'galaxy' })]) })
      )
    ).toThrow(PolicyBundleError);
    expect(() =>
      parsePolicyBundle(bundle({ budgets: { version: 2, budgets: [] } }))
    ).toThrow(/version 2 is not supported/);
    expect(() =>
      parseBudgetsSection({ version: 1, budgets: [], namespace: 'x' })
    ).toThrow('unknown field "namespace"');
  });
});
