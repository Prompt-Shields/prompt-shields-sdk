/**
 * Policy stages (#18 sensitivity classification, #19 fail-closed data
 * policy): unit cases, a seeded property test, and the middleware / Hono
 * behaviour. The golden vectors are in vectors.test.ts.
 */
import * as fs from 'fs';
import * as path from 'path';
import { Hono } from 'hono';
import {
  buildRouteRequest,
  classifySensitivity,
  detectPiiCategories,
  fromRoutePolicyDoc,
  HeuristicStrategy,
  parseRouteHeaders,
  PolicyViolation,
  resolveRoute,
  RouteDecision,
  RoutePolicy,
  RouteRequest,
  RouterStrategy,
  tierComplies,
} from './index';
import { dataPolicyRule, modelComplies } from './constraints';
import { requestText, scanRequestBody } from './pii';
import { psRouter } from './hono';
import { psRouterMiddleware } from './middleware';
import { ModelTier } from './types';

const nordic = fromRoutePolicyDoc(
  JSON.parse(
    fs.readFileSync(
      path.resolve(
        __dirname,
        '../../../../schemas/route-policy/examples/nordic-sovereign.json'
      ),
      'utf8'
    )
  )
);

const hi = [{ role: 'user', content: 'hi' }];
const req = (over: Partial<RouteRequest> = {}): RouteRequest => ({
  messages: hi,
  requestedModel: 'auto',
  approxInputTokens: 1,
  maxOutputTokens: 100,
  ...over,
});

const tier = (over: Partial<ModelTier> = {}): ModelTier => ({
  model: 'm',
  group: 'g',
  inputCostPer1k: 0.001,
  outputCostPer1k: 0.001,
  provider: 'p',
  jurisdiction: 'SE',
  zdr: true,
  usCloudActExposure: false,
  ...over,
});

describe('classifySensitivity', () => {
  test('an unknown declared level counts as unclassified', () => {
    expect(classifySensitivity(nordic, 'top-secret')).toEqual({
      level: 'confidential',
      source: 'default',
    });
  });

  test('PII escalation is escalate-only', () => {
    expect(classifySensitivity(nordic, 'public', ['email'])).toEqual({
      level: 'internal',
      source: 'pii_escalated',
    });
    expect(classifySensitivity(nordic, 'restricted', ['iban'])).toEqual({
      level: 'restricted',
      source: 'declared',
    });
  });

  test('inherited object keys are not PII categories', () => {
    expect(classifySensitivity(nordic, 'public', ['constructor'])).toEqual({
      level: 'public',
      source: 'declared',
    });
  });

  test('without a sensitivity section the declared value passes through', () => {
    const plain: RoutePolicy = { ...nordic, sensitivity: undefined };
    expect(classifySensitivity(plain, 'internal', ['national_id'])).toEqual({
      level: 'internal',
      source: 'declared',
    });
    expect(classifySensitivity(plain, undefined)).toEqual({
      source: 'default',
    });
  });
});

describe('tierComplies fails closed on missing facts', () => {
  const rule = {
    jurisdictions: ['EEA'],
    providers: ['p'],
    requireZdr: true,
    usCloudActExposure: false,
  };

  test('a tier stating every fact passes', () => {
    expect(tierComplies(tier(), rule)).toBe(true);
  });

  test.each<[string, Partial<ModelTier>]>([
    ['jurisdiction', { jurisdiction: undefined }],
    ['provider', { provider: undefined }],
    ['zdr', { zdr: undefined }],
    ['us_cloud_act_exposure', { usCloudActExposure: undefined }],
  ])('a tier missing %s is not allowed', (_, over) => {
    expect(tierComplies(tier(over), rule)).toBe(false);
  });

  test('a missing jurisdiction still passes "any"', () => {
    expect(
      tierComplies(tier({ jurisdiction: undefined }), {
        jurisdictions: ['any'],
      })
    ).toBe(true);
  });

  test('EEA allows a member country, not the other way round', () => {
    expect(
      tierComplies(tier({ jurisdiction: 'NO' }), { jurisdictions: ['EEA'] })
    ).toBe(true);
    expect(
      tierComplies(tier({ jurisdiction: 'EEA' }), { jurisdictions: ['NO'] })
    ).toBe(false);
    expect(
      tierComplies(tier({ jurisdiction: 'CH' }), { jurisdictions: ['EEA'] })
    ).toBe(false);
  });
});

// --- property: the served model always meets the data policy ---------------

/** Deterministic PRNG (mulberry32), so a failure reproduces. */
function rng(seed: number) {
  return () => {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

describe('property: zero data-policy violations', () => {
  const rand = rng(20261023);
  const pick = <T>(xs: readonly T[]): T => xs[Math.floor(rand() * xs.length)];
  const maybe = <T>(v: T): T | undefined => (rand() < 0.2 ? undefined : v);
  const JURIS = ['US', 'EEA', 'NO', 'SE', 'DE', 'CH', 'any'];
  const PROVIDERS = ['openai', 'berget', 'mistral-eu', 'scaleway'];
  const GROUPS = ['cheap', 'balanced', 'frontier', 'us-hosted', 'nope'];
  const PII = ['email', 'iban', 'national_id', 'health_data', 'phone'];

  function randomPolicy(): RoutePolicy {
    const groups: RoutePolicy['groups'] = {};
    for (const g of ['cheap', 'balanced', 'frontier', 'us-hosted']) {
      groups[g] = Array.from({ length: Math.floor(rand() * 4) }, (_, i) => ({
        model: `${g}-${i}`,
        group: g,
        inputCostPer1k: rand() / 100,
        outputCostPer1k: rand() / 50,
        provider: maybe(pick(PROVIDERS)),
        jurisdiction: maybe(pick(JURIS.filter((j) => j !== 'any'))),
        zdr: maybe(rand() < 0.5),
        usCloudActExposure: maybe(rand() < 0.5),
      }));
    }
    const rule = () => ({
      jurisdictions: [pick(JURIS), pick(JURIS)],
      providers: rand() < 0.4 ? [pick(PROVIDERS), pick(PROVIDERS)] : undefined,
      requireZdr: rand() < 0.5,
      usCloudActExposure: rand() < 0.4 ? false : undefined,
    });
    return {
      ...nordic,
      groups,
      dataPolicy: {
        public: rule(),
        internal: rule(),
        confidential: rule(),
        restricted: rule(),
      },
    };
  }

  test('2000 random policies and requests', async () => {
    let served = 0;
    let refused = 0;
    for (let i = 0; i < 2000; i++) {
      const policy = randomPolicy();
      const r = req({
        quality:
          rand() < 0.4 ? pick(['draft', 'balanced', 'critical']) : undefined,
        explicitGroup: rand() < 0.3 ? pick(GROUPS) : undefined,
        maxCost: rand() < 0.4 ? rand() / 100 : undefined,
        dataClassification: maybe(
          pick(['public', 'internal', 'confidential', 'restricted', 'bogus'])
        ),
        piiDetected: rand() < 0.5 ? [pick(PII), pick(PII)] : [],
        approxInputTokens: Math.floor(rand() * 4000),
      });
      const { level } = classifySensitivity(
        policy,
        r.dataClassification,
        r.piiDetected
      );
      const rule = dataPolicyRule(policy, level);
      let decision: RouteDecision;
      try {
        decision = await resolveRoute(r, policy, new HeuristicStrategy());
      } catch (err) {
        expect(err).toBeInstanceOf(PolicyViolation);
        refused++;
        continue;
      }
      served++;
      const servedTier = (policy.groups[decision.group] || []).find(
        (t) => t.model === decision.model
      );
      expect(servedTier).toBeDefined();
      expect(tierComplies(servedTier!, rule)).toBe(true);
      expect(decision.sensitivityLevel).toBe(level);
    }
    // Both outcomes were exercised.
    expect(served).toBeGreaterThan(100);
    expect(refused).toBeGreaterThan(100);
  });
});

describe('custom strategies', () => {
  test('only ever see tiers the data policy allows', async () => {
    const seen: string[] = [];
    const strategy: RouterStrategy = {
      async route(_req, policy) {
        for (const g of Object.keys(policy.groups))
          for (const t of policy.groups[g]) seen.push(t.model);
        return { model: 'x', group: 'cheap', reason: 'custom', estCost: 0 };
      },
    };
    const decision = await resolveRoute(
      req({ dataClassification: 'confidential' }),
      nordic,
      strategy
    );
    expect(seen).not.toContain('gpt-4o-mini');
    expect(seen).toContain('scaleway-small');
    expect(decision).toMatchObject({
      model: 'scaleway-small',
      reason: 'custom',
    });
  });
});

describe('headers', () => {
  test('X-PS-Data-Classification and X-PS-Use-Case reach the RouteRequest', () => {
    const hints = parseRouteHeaders({
      'x-ps-data-classification': 'restricted',
      'X-PS-Use-Case': 'legal-review',
    });
    const r = buildRouteRequest({ model: 'auto', messages: hi }, hints);
    expect(r.dataClassification).toBe('restricted');
    expect(r.useCase).toBe('legal-review');
  });
});

describe('detectPiiCategories (port of the SDK detector)', () => {
  test.each<[string, string[]]>([
    ['nothing here', []],
    ['mail jane@acme.com', ['email']],
    ['IBAN NO9386011117947', ['iban', 'financial_data', 'phone']],
    ['Mitt personnummer är 19121212-1212.', ['national_id', 'phone']],
    ['fødselsnummer 01818510078', ['national_id', 'phone']],
    ['the diagnosis was clear', ['health_data']],
  ])('%s', (text, expected) => {
    expect(detectPiiCategories(text)).toEqual([...expected].sort());
  });
});

// --- middleware and Hono adapter -------------------------------------------

describe('psRouterMiddleware under a data policy', () => {
  const run = (
    policy: RoutePolicy,
    body: any,
    headers: Record<string, string> = {},
    strategy?: RouterStrategy
  ) =>
    psRouterMiddleware(policy, strategy).beforeRequest({
      url: 'https://gw/v1/chat/completions',
      headers,
      body,
    }) as Promise<any>;

  test('PII in the messages escalates the level and tightens routing', async () => {
    const r = await run(
      nordic,
      {
        model: 'auto',
        messages: [{ role: 'user', content: 'Patient 19121212-1212' }],
      },
      { 'x-ps-data-classification': 'internal', 'x-ps-quality': 'balanced' }
    );
    expect(r._psRoute).toMatchObject({
      servedModel: 'berget-llama-70b',
      sensitivityLevel: 'restricted',
      sensitivitySource: 'pii_escalated',
    });
  });

  test('no compliant candidate: the call is refused, the body untouched', async () => {
    const body = { model: 'auto', messages: hi };
    const r = await run(nordic, body, {
      'x-ps-data-classification': 'restricted',
      'x-ps-route': 'us-hosted',
    });
    expect(r._psRoute).toBeUndefined();
    expect(r._psRouteViolation).toMatchObject({
      group: 'us-hosted',
      sensitivityLevel: 'restricted',
      sensitivitySource: 'declared',
    });
    expect(body.model).toBe('auto');
  });

  test('any routing error under a data policy fails closed', async () => {
    const broken: RouterStrategy = {
      route: async () => {
        throw new Error('boom');
      },
    };
    const r = await run(nordic, { model: 'auto', messages: hi }, {}, broken);
    expect(r._psRouteViolation).toBeDefined();
    expect(r.body.model).toBe('auto');
  });

  test('without a data policy a routing error still fails open', async () => {
    const broken: RouterStrategy = {
      route: async () => {
        throw new Error('boom');
      },
    };
    const plain: RoutePolicy = {
      defaultGroup: nordic.defaultGroup,
      qualityToGroup: nordic.qualityToGroup,
      groups: nordic.groups,
    };
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
    const r = await run(plain, { model: 'auto', messages: hi }, {}, broken);
    warn.mockRestore();
    expect(r._psRouteViolation).toBeUndefined();
    expect(r._psRoute).toBeUndefined();
  });
});

describe('psRouter (Hono) under a data policy', () => {
  function makeApp() {
    const app = new Hono();
    let handled = 0;
    app.use('/v1/chat/completions', psRouter(nordic));
    app.post('/v1/chat/completions', async (c) => {
      handled++;
      const body = await c.req.json();
      return c.json({ model: body.model, route: c.get('psRoute') ?? null });
    });
    return { app, handled: () => handled };
  }
  const post = (app: Hono, headers: Record<string, string>) =>
    app.request('/v1/chat/completions', {
      method: 'POST',
      headers: { 'content-type': 'application/json', ...headers },
      body: JSON.stringify({ model: 'auto', messages: hi }),
    });

  test('a compliant decision carries the sensitivity fields', async () => {
    const { app } = makeApp();
    const res = await post(app, { 'x-ps-quality': 'balanced' });
    const json: any = await res.json();
    expect(json.model).toBe('berget-llama-70b');
    expect(json.route).toMatchObject({
      sensitivityLevel: 'confidential',
      sensitivitySource: 'default',
    });
  });

  test('a violation is a 403 policy_violation and the handler never runs', async () => {
    const { app, handled } = makeApp();
    const res = await post(app, {
      'x-ps-data-classification': 'restricted',
      'x-ps-route': 'us-hosted',
    });
    expect(res.status).toBe(403);
    const json: any = await res.json();
    expect(json.error).toMatchObject({
      type: 'permission_error',
      code: 'policy_violation',
      param: null,
    });
    expect(json.error.message).toContain('restricted');
    expect(handled()).toBe(0);
  });
});

// --- every request shape, ReDoS, directly named models ---------------------

describe('requestText / scanRequestBody', () => {
  const id = '19121212-1212';
  test.each<[string, any]>([
    ['chat string content', { messages: [{ role: 'user', content: id }] }],
    [
      'chat content parts',
      { messages: [{ role: 'user', content: [{ type: 'text', text: id }] }] },
    ],
    [
      'chat tool call arguments',
      {
        messages: [
          {
            role: 'assistant',
            tool_calls: [
              { function: { name: 'f', arguments: `{"pnr":"${id}"}` } },
            ],
          },
        ],
      },
    ],
    [
      'Anthropic system blocks',
      { system: [{ type: 'text', text: id }], messages: hi },
    ],
    [
      'Anthropic tool_use input object',
      {
        messages: [
          {
            role: 'assistant',
            content: [{ type: 'tool_use', name: 'f', input: { pnr: id } }],
          },
        ],
      },
    ],
    [
      'Anthropic tool_result',
      {
        messages: [
          {
            role: 'user',
            content: [{ type: 'tool_result', content: [{ text: id }] }],
          },
        ],
      },
    ],
    ['Responses string input', { input: id }],
    [
      'Responses items',
      {
        instructions: 'be brief',
        input: [{ role: 'user', content: [{ type: 'input_text', text: id }] }],
      },
    ],
    [
      'Responses function_call_output',
      { input: [{ type: 'function_call_output', output: id }] },
    ],
    ['completions prompt', { prompt: [id] }],
    ['embeddings input', { input: ['x', id] }],
    ['rerank', { query: 'q', documents: [{ text: id }] }],
  ])('%s', (_name, body) => {
    expect(scanRequestBody(body)).toContain('national_id');
  });

  test('images, URLs, tools and the model are not request text', () => {
    const text = requestText({
      model: 'jane@acme.com',
      messages: [
        {
          role: 'user',
          content: [
            {
              type: 'image_url',
              image_url: { url: 'https://x/jane@acme.com' },
            },
            {
              type: 'image',
              source: { type: 'base64', data: '4111111111111111' },
            },
          ],
        },
      ],
      tools: [{ function: { description: '19121212-1212' } }],
    });
    expect(scanRequestBody({ t: text, content: text })).toEqual([]);
  });
});

describe('PII detection stays linear on large bodies', () => {
  test.each([
    ['letters', 'a'.repeat(200_000)],
    ['dotted', 'a.'.repeat(100_000)],
    ['digits', '1'.repeat(200_000)],
    ['local part then @', 'a'.repeat(200_000) + '@'],
    ['many @', 'a@'.repeat(100_000)],
  ])('%s', (_name, text) => {
    const t = Date.now();
    detectPiiCategories(text);
    expect(Date.now() - t).toBeLessThan(1000);
  });

  test('the anchored email pattern still finds addresses mid-run', () => {
    expect(detectPiiCategories('xx..jane.doe+1@acme.co.uk')).toContain('email');
    expect(detectPiiCategories('a'.repeat(5000) + '@acme.com')).toContain(
      'email'
    );
    expect(detectPiiCategories('a@b')).not.toContain('email');
  });
});

describe('modelComplies (directly named models)', () => {
  const restricted = dataPolicyRule(nordic, 'restricted');
  test('a listed model uses its tier facts', () => {
    expect(modelComplies(nordic, 'mistral-small-eu', restricted)).toBe(true);
    expect(modelComplies(nordic, 'gpt-4o', restricted)).toBe(false);
  });
  test('an unlisted or missing model fails closed unless the rule allows any', () => {
    expect(modelComplies(nordic, 'unlisted', restricted)).toBe(false);
    expect(modelComplies(nordic, undefined, restricted)).toBe(false);
    const pub = dataPolicyRule(nordic, 'public');
    expect(modelComplies(nordic, 'unlisted', pub)).toBe(true);
  });
  test('no rule, no constraint', () => {
    expect(modelComplies(nordic, 'unlisted', undefined)).toBe(true);
  });
});

describe('psRouter (Hono) realtime under a data policy', () => {
  test('a non-compliant realtime model is refused before the upgrade', async () => {
    const app = new Hono();
    let handled = 0;
    app.use('*', psRouter(nordic));
    app.get('/v1/realtime', (c) => {
      handled++;
      return c.text('upgrade');
    });
    const res = await app.request('/v1/realtime?model=gpt-4o', {
      headers: { 'x-portkey-provider': 'openai' },
    });
    expect(res.status).toBe(403);
    expect(handled).toBe(0);
    const ok = await app.request('/v1/realtime?model=gpt-4o', {
      headers: {
        'x-portkey-provider': 'openai',
        'x-ps-data-classification': 'internal',
      },
    });
    expect(ok.status).toBe(200);
  });
});
