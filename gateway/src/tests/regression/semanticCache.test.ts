/**
 * Semantic cache mode (#33) through the whole gateway app, offline: a stub
 * chat upstream and a stub embeddings endpoint (or the built-in local
 * embedder). Covers the threshold, strict isolation (caller credential,
 * virtual key, tenant, use case, model, sensitivity), the sensitivity and
 * PII bypass, the data policy check on the embedder, refusals, telemetry
 * (`cost_source: cache`) and metrics.
 */
jest.mock('jose', () => ({}));
jest.mock('../../middlewares/router/policy', () => {
  const actual = jest.requireActual('../../middlewares/router/policy');
  return { ...actual, defaultPolicy: { ...actual.defaultPolicy } };
});

import * as fs from 'fs';
import * as path from 'path';
import {
  events,
  json,
  post,
  resetHarness,
  until,
  upstream,
  upstreamCalls,
} from './harness';
import app from '../../index';
import { defaultPolicy } from '../../middlewares/router/policy';
import { fromRoutePolicyDoc } from '../../middlewares/router/constraints';
import { __resetSemanticCacheForTests } from '../../middlewares/cache/semantic';

const EMBED_URL = 'http://embed.test/v1/embeddings';
const CHAT = /api\.openai\.com\/v1\/chat\/completions$/;

const original = { ...defaultPolicy };
function usePolicy(p: any) {
  for (const k of Object.keys(defaultPolicy)) delete (defaultPolicy as any)[k];
  Object.assign(defaultPolicy, p);
}
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

const SEMANTIC_ENV = Object.keys(process.env).filter((k) =>
  k.startsWith('PS_SEMANTIC_CACHE_')
);
const PS_API_KEY = process.env.PS_API_KEY;

let served = 0;
/** A chat completion that says which upstream call produced it. */
const chatOk = (call: any) =>
  json({
    id: `chatcmpl-${++served}`,
    object: 'chat.completion',
    created: 1,
    model: call.body.model,
    choices: [
      {
        index: 0,
        message: { role: 'assistant', content: `answer-${served}` },
        finish_reason: 'stop',
      },
    ],
    usage: { prompt_tokens: 100, completion_tokens: 50, total_tokens: 150 },
  });

const OPENAI_A = {
  'x-portkey-provider': 'openai',
  authorization: 'Bearer sk-tenant-A',
  'x-ps-cache': 'semantic',
};
const OPENAI_B = { ...OPENAI_A, authorization: 'Bearer sk-tenant-B' };

const chat = (content: string, extra: object = {}) => ({
  model: 'gpt-4o-mini',
  messages: [
    { role: 'system', content: 'You are terse.' },
    { role: 'user', content },
  ],
  ...extra,
});

async function ask(
  content: string,
  headers: Record<string, string> = OPENAI_A,
  extra: object = {}
) {
  const res = await post(app, '/v1/chat/completions', chat(content, extra), {
    ...headers,
  });
  const body: any = await res.json();
  return {
    status: res.headers.get('x-portkey-cache-status'),
    answer: body?.choices?.[0]?.message?.content,
    code: res.status,
  };
}

const chatCalls = () => upstreamCalls.filter((c) => CHAT.test(c.url));
const embedCalls = () => upstreamCalls.filter((c) => c.url === EMBED_URL);

beforeEach(() => {
  resetHarness();
  __resetSemanticCacheForTests();
  usePolicy(original);
  for (const k of Object.keys(process.env))
    if (k.startsWith('PS_SEMANTIC_CACHE_') && !SEMANTIC_ENV.includes(k))
      delete process.env[k];
  process.env.PS_API_KEY = PS_API_KEY;
  process.env.PS_SEMANTIC_CACHE_EMBEDDER = 'local';
  process.env.PS_SEMANTIC_CACHE_THRESHOLD = '0.9';
  upstream(CHAT, chatOk);
});
afterAll(() => {
  usePolicy(original);
  process.env.PS_API_KEY = PS_API_KEY;
});

describe('semantic lookup', () => {
  test('a near-duplicate prompt is served from the cache', async () => {
    const first = await ask('What is the capital of France?');
    expect(first).toMatchObject({ status: 'SEMANTIC MISS', code: 200 });
    const again = await ask('what is the capital of france');
    expect(again.status).toBe('SEMANTIC HIT');
    expect(again.answer).toBe(first.answer);
    const other = await ask('Write a haiku about autumn leaves');
    expect(other.status).toBe('SEMANTIC MISS');
    expect(chatCalls()).toHaveLength(2);
  });

  test('a prompt below the threshold misses', async () => {
    process.env.PS_SEMANTIC_CACHE_THRESHOLD = '0.999';
    await ask('What is the capital of France?');
    const close = await ask('what is the capital of france, please');
    expect(close.status).toBe('SEMANTIC MISS');
    expect(chatCalls()).toHaveLength(2);
  });

  test('different history, params or system prompt never match', async () => {
    await ask('What is the capital of France?');
    const temp = await ask('What is the capital of France?', OPENAI_A, {
      temperature: 0.9,
    });
    expect(temp.status).toBe('SEMANTIC MISS');
    const res = await post(
      app,
      '/v1/chat/completions',
      {
        model: 'gpt-4o-mini',
        messages: [
          { role: 'system', content: 'You are verbose.' },
          { role: 'user', content: 'What is the capital of France?' },
        ],
      },
      OPENAI_A
    );
    expect(res.headers.get('x-portkey-cache-status')).toBe('SEMANTIC MISS');
    expect(chatCalls()).toHaveLength(3);
  });

  test('without an embedder the mode reads and writes nothing', async () => {
    delete process.env.PS_SEMANTIC_CACHE_EMBEDDER;
    expect((await ask('hello there')).status).toBe('SEMANTIC BYPASS');
    expect((await ask('hello there')).status).toBe('SEMANTIC BYPASS');
    expect(chatCalls()).toHaveLength(2);
  });

  test('streamed requests bypass the cache', async () => {
    upstream(
      CHAT,
      () =>
        new Response('data: [DONE]\n\n', {
          status: 200,
          headers: { 'content-type': 'text/event-stream' },
        })
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chat('stream me', { stream: true }),
      OPENAI_A
    );
    await res.text();
    expect(res.headers.get('x-portkey-cache-status')).toBe('SEMANTIC BYPASS');
  });
});

describe('isolation (no cross-tenant leakage)', () => {
  test('another credential never sees the first caller’s entry', async () => {
    const a = await ask('Summarise our Q3 board minutes', OPENAI_A);
    const b = await ask('Summarise our Q3 board minutes', OPENAI_B);
    expect(b.status).toBe('SEMANTIC MISS');
    expect(b.answer).not.toBe(a.answer);
    // Each tenant still hits its own entry.
    expect((await ask('summarise our q3 board minutes', OPENAI_A)).answer).toBe(
      a.answer
    );
    expect((await ask('summarise our q3 board minutes', OPENAI_B)).answer).toBe(
      b.answer
    );
    expect(chatCalls()).toHaveLength(2);
  });

  test('virtual key, tenant, use case and model each scope the entry', async () => {
    const base = await ask('Explain our refund policy');
    const variants: [string, Record<string, string>, object?][] = [
      ['virtual key', { ...OPENAI_A, 'x-portkey-virtual-key': 'vk-other' }],
      ['use case', { ...OPENAI_A, 'x-ps-use-case': 'support-bot' }],
      ['model', OPENAI_A, { model: 'gpt-4o' }],
    ];
    for (const [, headers, extra] of variants) {
      const r = await ask('Explain our refund policy', headers, extra);
      expect(r.status).toBe('SEMANTIC MISS');
      expect(r.answer).not.toBe(base.answer);
    }
    // Same credentials, another Prompt Shields tenant (deployment key).
    process.env.PS_API_KEY = 'ps-other-tenant';
    const tenant = await ask('Explain our refund policy');
    expect(tenant.status).toBe('SEMANTIC MISS');
    expect(chatCalls()).toHaveLength(5);
  });

  test('a declared sensitivity level is part of the key', async () => {
    await ask('Explain the onboarding steps', {
      ...OPENAI_A,
      'x-ps-data-classification': 'public',
    });
    const internal = await ask('Explain the onboarding steps', {
      ...OPENAI_A,
      'x-ps-data-classification': 'internal',
    });
    expect(internal.status).toBe('SEMANTIC MISS');
  });
});

describe('sensitivity and PII', () => {
  test.each(['confidential', 'restricted', 'top-secret'])(
    '%s calls never read or write the cache',
    async (level) => {
      const headers = { ...OPENAI_A, 'x-ps-data-classification': level };
      expect((await ask('Draft the merger memo', headers)).status).toBe(
        'SEMANTIC BYPASS'
      );
      expect((await ask('Draft the merger memo', headers)).status).toBe(
        'SEMANTIC BYPASS'
      );
      expect(chatCalls()).toHaveLength(2);
    }
  );

  test('an operator may explicitly allow higher levels', async () => {
    process.env.PS_SEMANTIC_CACHE_MAX_SENSITIVITY = 'restricted';
    const headers = { ...OPENAI_A, 'x-ps-data-classification': 'restricted' };
    await ask('Draft the merger memo', headers);
    expect((await ask('draft the merger memo', headers)).status).toBe(
      'SEMANTIC HIT'
    );
  });

  test('PII in the prompt bypasses unless allowed', async () => {
    const q = 'Email jane.doe@example.com the invoice';
    expect((await ask(q)).status).toBe('SEMANTIC BYPASS');
    expect((await ask(q)).status).toBe('SEMANTIC BYPASS');
    process.env.PS_SEMANTIC_CACHE_ALLOW_PII = 'true';
    await ask(q);
    expect((await ask(q)).status).toBe('SEMANTIC HIT');
  });

  test('under a policy, unclassified calls take its default level', async () => {
    usePolicy(nordic); // unclassified_default: confidential
    const m = { model: 'mistral-small-eu' }; // allowed at confidential
    const res = await ask('What is the capital of France?', OPENAI_A, m);
    expect(res).toMatchObject({ status: 'SEMANTIC BYPASS', code: 200 });
  });
});

describe('embeddings', () => {
  /** Stub embeddings: a fixed vector per known text. */
  const vectors: Record<string, number[]> = {
    'What is the capital of France?': [1, 0, 0],
    'Which city is France’s capital?': [0.96, 0.28, 0],
    'Tell me a joke': [0, 0, 1],
  };
  const embedOk = (call: any) =>
    json({
      object: 'list',
      data: [{ index: 0, embedding: vectors[call.body.input] ?? [0, 1, 0] }],
      model: call.body.model,
    });

  beforeEach(() => {
    process.env.PS_SEMANTIC_CACHE_EMBEDDER = 'http';
    process.env.PS_SEMANTIC_CACHE_EMBEDDING_URL = EMBED_URL;
    process.env.PS_SEMANTIC_CACHE_EMBEDDING_MODEL = 'embed-small';
    process.env.PS_SEMANTIC_CACHE_EMBEDDING_API_KEY = 'emb-key';
    process.env.PS_SEMANTIC_CACHE_THRESHOLD = '0.95';
    upstream(/^http:\/\/embed\.test\//, embedOk);
  });

  test('a paraphrase above the threshold hits; only the last user message is embedded', async () => {
    const first = await ask('What is the capital of France?');
    const para = await ask('Which city is France’s capital?');
    expect(para.status).toBe('SEMANTIC HIT');
    expect(para.answer).toBe(first.answer);
    expect((await ask('Tell me a joke')).status).toBe('SEMANTIC MISS');
    expect(embedCalls()).toHaveLength(3);
    expect(embedCalls()[0].body).toEqual({
      model: 'embed-small',
      input: 'What is the capital of France?',
    });
    expect(embedCalls()[0].headers.authorization).toBe('Bearer emb-key');
    expect(chatCalls()).toHaveLength(2);
  });

  test('an embedding failure fails open to the upstream', async () => {
    upstream(/^http:\/\/embed\.test\//, () => json({ error: 'down' }, 500));
    const r = await ask('What is the capital of France?');
    expect(r).toMatchObject({ status: 'SEMANTIC BYPASS', code: 200 });
    expect(chatCalls()).toHaveLength(1);
  });

  test('an embedder on a metadata or private address is never called', async () => {
    process.env.PS_SEMANTIC_CACHE_EMBEDDING_URL =
      'http://169.254.169.254/v1/embeddings';
    upstream(/169\.254\.169\.254/, embedOk);
    const r = await ask('What is the capital of France?');
    expect(r).toMatchObject({ status: 'SEMANTIC BYPASS', code: 200 });
    expect(upstreamCalls.filter((c) => /169\.254/.test(c.url))).toHaveLength(0);
  });

  test('a redirect from the embedder is not followed', async () => {
    upstream(
      /^http:\/\/embed\.test\//,
      () =>
        new Response(null, {
          status: 307,
          headers: { location: 'http://elsewhere.test/v1/embeddings' },
        })
    );
    upstream(/elsewhere\.test/, embedOk);
    const r = await ask('What is the capital of France?');
    expect(r).toMatchObject({ status: 'SEMANTIC BYPASS', code: 200 });
    expect(
      upstreamCalls.filter((c) => /elsewhere\.test/.test(c.url))
    ).toHaveLength(0);
  });

  describe('under a data policy', () => {
    const headers = {
      ...OPENAI_A,
      'x-ps-data-classification': 'confidential',
    };
    beforeEach(() => {
      usePolicy(nordic);
      process.env.PS_SEMANTIC_CACHE_MAX_SENSITIVITY = 'confidential';
    });

    test('a non-compliant embedder never receives the prompt', async () => {
      process.env.PS_SEMANTIC_CACHE_EMBEDDING_PROVIDER = 'openai';
      process.env.PS_SEMANTIC_CACHE_EMBEDDING_JURISDICTION = 'US';
      const r = await ask('What is the capital of France?', headers, {
        model: 'mistral-small-eu',
      });
      expect(r).toMatchObject({ status: 'SEMANTIC BYPASS', code: 200 });
      expect(embedCalls()).toHaveLength(0);
    });

    test('a compliant embedder is used', async () => {
      process.env.PS_SEMANTIC_CACHE_EMBEDDING_PROVIDER = 'mistral-eu';
      process.env.PS_SEMANTIC_CACHE_EMBEDDING_JURISDICTION = 'FR';
      process.env.PS_SEMANTIC_CACHE_EMBEDDING_ZDR = 'true';
      process.env.PS_SEMANTIC_CACHE_EMBEDDING_US_CLOUD_ACT_EXPOSURE = 'false';
      const m = { model: 'mistral-small-eu' };
      await ask('What is the capital of France?', headers, m);
      const r = await ask('Which city is France’s capital?', headers, m);
      expect(r.status).toBe('SEMANTIC HIT');
    });

    test('the local embedder needs no egress and is always allowed', async () => {
      process.env.PS_SEMANTIC_CACHE_EMBEDDER = 'local';
      process.env.PS_SEMANTIC_CACHE_THRESHOLD = '0.9';
      const m = { model: 'mistral-small-eu' };
      await ask('What is the capital of France?', headers, m);
      const r = await ask('what is the capital of france', headers, m);
      expect(r.status).toBe('SEMANTIC HIT');
      expect(embedCalls()).toHaveLength(0);
    });
  });
});

describe('what is never cached', () => {
  test('refusals', async () => {
    upstream(CHAT, (call) =>
      json({
        id: 'chatcmpl-r',
        object: 'chat.completion',
        created: 1,
        model: call.body.model,
        choices: [
          {
            index: 0,
            message: {
              role: 'assistant',
              content: null,
              refusal: 'I can’t help with that.',
            },
            finish_reason: 'stop',
          },
        ],
        usage: { prompt_tokens: 5, completion_tokens: 5, total_tokens: 10 },
      })
    );
    await ask('How do I pick a lock?');
    expect((await ask('How do I pick a lock?')).status).toBe('SEMANTIC MISS');
    expect(chatCalls()).toHaveLength(2);
  });

  test('upstream errors', async () => {
    upstream(CHAT, () => json({ error: { message: 'overloaded' } }, 500));
    await ask('What is the capital of France?');
    upstream(CHAT, chatOk);
    expect((await ask('What is the capital of France?')).status).toBe(
      'SEMANTIC MISS'
    );
  });
});

describe('telemetry and metrics', () => {
  test('a hit is recorded at cost 0 with cost_source cache and what it saved', async () => {
    const usage = () => events.filter((e) => e.tokens_in !== undefined);
    await ask('What is the capital of France?');
    await until(() => usage().length === 1, 'miss event');
    await ask('what is the capital of france');
    await until(() => usage().length === 2, 'hit event');
    const [miss, hit] = usage();
    expect(miss.cost_source).toBe('pricing_table');
    expect(miss.cost).toBeGreaterThan(0);
    expect(hit).toMatchObject({
      cache_status: 'SEMANTIC HIT',
      cost: 0,
      cost_source: 'cache',
      cache_saved_cost: miss.cost,
    });
  });

  test('metrics count hits, bypass reasons and saved cost', async () => {
    await ask('What is the capital of France?');
    await ask('what is the capital of france');
    await ask('Draft the memo', {
      ...OPENAI_A,
      'x-ps-data-classification': 'restricted',
    });
    const text = await (await app.request('/metrics')).text();
    expect(text).toMatch(
      /ps_gateway_cache_responses_total\{status="semantic_hit"\} [1-9]/
    );
    expect(text).toMatch(
      /ps_gateway_semantic_cache_bypass_total\{reason="sensitivity"\} [1-9]/
    );
    expect(text).toMatch(
      /ps_gateway_cache_writes_total\{mode="semantic",outcome="stored"\} [1-9]/
    );
    expect(text).toMatch(
      /ps_gateway_cache_saved_cost_usd_total\{provider="openai",model="gpt-4o-mini",status="semantic_hit"\} 0\.\d+/
    );
    expect(text).toMatch(/ps_gateway_semantic_cache_entries [1-9]/);
  });
});
