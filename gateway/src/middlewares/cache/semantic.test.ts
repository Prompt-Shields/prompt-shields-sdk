import {
  embedderAllowed,
  localHashEmbedder,
  partitionKey,
  policyVersionOf,
  semanticConfig,
  sensitivityAllows,
  splitPrompt,
} from './semantic';
import { normalize, dot } from './semanticStore';
import { isRefusalResponse } from './index';
import { resolveCacheMode } from './resolveMode';

const ctx = (vars: Record<string, unknown> = {}) =>
  ({
    get: (k: string) => vars[k],
    set: (k: string, v: unknown) => (vars[k] = v),
  }) as any;

const cfg = semanticConfig({});
const user = (content: unknown) => ({ role: 'user', content });

describe('splitPrompt', () => {
  it('embeds only the final user text and pins the rest', () => {
    const r = splitPrompt(
      {
        model: 'm',
        temperature: 0,
        messages: [{ role: 'system', content: 's' }, user('hello')],
      },
      100
    );
    expect(r).toEqual({
      ok: true,
      text: 'hello',
      exact: {
        params: { model: 'm', temperature: 0 },
        history: [{ role: 'system', content: 's' }],
        last: { role: 'user' },
      },
    });
  });

  it('accepts text parts only', () => {
    expect(
      splitPrompt({ messages: [user([{ type: 'text', text: 'a' }])] }, 100)
    ).toMatchObject({ ok: true, text: 'a' });
    expect(
      splitPrompt(
        {
          messages: [
            user([
              { type: 'text', text: 'what is this' },
              { type: 'image_url', image_url: { url: 'x' } },
            ]),
          ],
        },
        100
      )
    ).toEqual({ ok: false, reason: 'non_text_content' });
  });

  it.each([
    [{}, 'no_messages'],
    [{ messages: [{ role: 'assistant', content: 'x' }] }, 'last_not_user'],
    [{ messages: [user('  ')] }, 'empty_text'],
    [{ messages: [user('x'.repeat(101))] }, 'too_long'],
  ])('bypasses %j (%s)', (params, reason) => {
    expect(splitPrompt(params, 100)).toEqual({ ok: false, reason });
  });
});

describe('sensitivityAllows', () => {
  const body = { messages: [user('hi')] };
  const run = (headers: Record<string, string>, vars = {}, c = cfg) =>
    sensitivityAllows(ctx(vars), body, headers, c);

  it('allows unclassified, public and internal by default', () => {
    expect(run({})).toMatchObject({ ok: true, level: 'unclassified' });
    expect(run({ 'x-ps-data-classification': 'internal' })).toMatchObject({
      ok: true,
      level: 'internal',
    });
  });

  it('refuses confidential and above, and unknown levels', () => {
    expect(run({ 'x-ps-data-classification': 'confidential' })).toEqual({
      ok: false,
      reason: 'sensitivity',
    });
    expect(run({ 'x-ps-data-classification': 'secret' })).toEqual({
      ok: false,
      reason: 'unknown_sensitivity',
    });
  });

  it('uses the data policy gate level over the declared one', () => {
    const gate = { policy: { groups: {} }, level: 'restricted' };
    expect(
      run({ 'x-ps-data-classification': 'public' }, { psDataPolicyGate: gate })
    ).toEqual({ ok: false, reason: 'sensitivity' });
  });

  it('uses the router level when there is no gate', () => {
    expect(
      run({}, { psRoute: { sensitivityLevel: 'confidential' } })
    ).toMatchObject({ ok: false });
  });

  it('a max level the policy does not know fails closed', () => {
    const c = semanticConfig({ PS_SEMANTIC_CACHE_MAX_SENSITIVITY: 'nope' });
    expect(run({ 'x-ps-data-classification': 'public' }, {}, c)).toEqual({
      ok: false,
      reason: 'sensitivity',
    });
  });

  it('refuses requests with detected PII', () => {
    expect(
      sensitivityAllows(
        ctx(),
        { messages: [user('mail bob@example.com')] },
        {},
        cfg
      )
    ).toEqual({ ok: false, reason: 'pii' });
  });
});

describe('embedderAllowed', () => {
  const rule = { jurisdictions: ['EEA'], requireZdr: true };
  const gate: any = { policy: { groups: {} }, level: 'confidential', rule };
  const http = (facts: any) => ({
    id: 'h',
    local: false,
    model: 'e',
    facts: {
      model: 'e',
      group: '',
      inputCostPer1k: 0,
      outputCostPer1k: 0,
      ...facts,
    },
    embed: async () => [1],
  });

  it('checks the embedder facts against the rule', () => {
    expect(embedderAllowed(http({ jurisdiction: 'US' }), gate)).toBe(false);
    expect(embedderAllowed(http({ jurisdiction: 'DE' }), gate)).toBe(false);
    expect(embedderAllowed(http({ jurisdiction: 'DE', zdr: true }), gate)).toBe(
      true
    );
  });

  it('always allows the local embedder and calls without a rule', () => {
    expect(embedderAllowed(localHashEmbedder(), gate)).toBe(true);
    expect(embedderAllowed(http({}), undefined)).toBe(true);
  });
});

describe('localHashEmbedder', () => {
  const sim = async (a: string, b: string) => {
    const e = localHashEmbedder();
    return dot(normalize(await e.embed(a))!, normalize(await e.embed(b))!);
  };

  it('scores near-duplicates high and unrelated text low', async () => {
    expect(await sim('Hello, World!', 'hello world')).toBeCloseTo(1, 5);
    expect(
      await sim(
        'What is the capital of France?',
        'what is the capital of france please'
      )
    ).toBeGreaterThan(0.85);
    expect(
      await sim('What is the capital of France?', 'Write a haiku about leaves')
    ).toBeLessThan(0.3);
  });
});

describe('partitionKey', () => {
  const input = {
    endpoint: 'chatComplete',
    params: { model: 'gpt-4o-mini' },
    requestHeaders: {},
    index: 0,
    cacheScope: 'scope',
    forceRefresh: false,
  };
  const key = (vars: Record<string, unknown>, c = cfg, over = {}) =>
    partitionKey(
      ctx(vars),
      { ...input, ...over },
      {},
      'internal',
      undefined,
      localHashEmbedder(),
      c
    );

  it('separates OIDC principals unless sharing is enabled', async () => {
    const alice = { psOidcIdentity: { subjectHash: 'a'.repeat(32) } };
    const bob = { psOidcIdentity: { subjectHash: 'b'.repeat(32) } };
    expect(await key(alice)).not.toBe(await key(bob));
    const share = semanticConfig({
      PS_SEMANTIC_CACHE_SHARE_PRINCIPALS: 'true',
    });
    expect(await key(alice, share)).toBe(await key(bob, share));
  });

  it('changes with scope, use case and model', async () => {
    const base = await key({});
    expect(await key({}, cfg, { cacheScope: 'other' })).not.toBe(base);
    expect(
      await key({}, cfg, { requestHeaders: { 'x-ps-use-case': 'u' } })
    ).not.toBe(base);
    expect(await key({}, cfg, { params: { model: 'gpt-4o' } })).not.toBe(base);
  });
});

describe('policyVersionOf', () => {
  it('prefers policy_version, else digests the policy', async () => {
    expect(await policyVersionOf(undefined)).toBe('none');
    expect(await policyVersionOf({ policyVersion: '7' } as any)).toBe('v:7');
    const a = await policyVersionOf({ defaultGroup: 'a' } as any);
    const b = await policyVersionOf({ defaultGroup: 'b' } as any);
    expect(a).toMatch(/^sha256:/);
    expect(a).not.toBe(b);
  });
});

describe('isRefusalResponse', () => {
  it.each([
    [{ choices: [{ message: { refusal: 'no' } }] }, true],
    [{ choices: [{ finish_reason: 'content_filter', message: {} }] }, true],
    [{ type: 'message', stop_reason: 'refusal', content: [] }, true],
    [{ output: [{ type: 'message', content: [{ type: 'refusal' }] }] }, true],
    [{ choices: [{ message: { content: 'ok', refusal: null } }] }, false],
    [{ type: 'message', stop_reason: 'end_turn' }, false],
  ])('%j -> %s', (body, expected) => {
    expect(isRefusalResponse(body)).toBe(expected);
  });
});

describe('resolveCacheMode semantic', () => {
  it('accepts semantic from the header and the default', () => {
    expect(resolveCacheMode({ 'x-ps-cache': 'semantic' }, {})).toEqual({
      mode: 'semantic',
      forceRefresh: false,
    });
    expect(resolveCacheMode({}, { PS_CACHE_DEFAULT: 'semantic' })?.mode).toBe(
      'semantic'
    );
    expect(
      resolveCacheMode(
        { 'x-ps-cache': 'refresh' },
        { PS_CACHE_DEFAULT: 'semantic' }
      )
    ).toEqual({ mode: 'semantic', forceRefresh: true });
  });
});
