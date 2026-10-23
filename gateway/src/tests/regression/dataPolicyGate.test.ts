/**
 * The routing data policy (#19) on calls the router does not decide: a
 * concrete model, a config `override_params.model` that replaces the routed
 * model, and non-chat endpoints. Each is refused with a 403
 * `policy_violation` before any upstream call when the model it would run
 * is not allowed at the call's sensitivity level (PRD G2), and served
 * otherwise. The app's router uses `defaultPolicy`, swapped here for the
 * nordic-sovereign example policy.
 */
jest.mock('jose', () => ({}));
jest.mock('../../middlewares/router/policy', () => {
  const actual = jest.requireActual('../../middlewares/router/policy');
  return { ...actual, defaultPolicy: { ...actual.defaultPolicy } };
});

import * as fs from 'fs';
import * as path from 'path';
import { json, post, resetHarness, upstream, upstreamCalls } from './harness';
import app from '../../index';
import { defaultPolicy } from '../../middlewares/router/policy';
import { fromRoutePolicyDoc } from '../../middlewares/router/constraints';

const original = { ...defaultPolicy };
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

function usePolicy(p: any) {
  for (const k of Object.keys(defaultPolicy)) delete (defaultPolicy as any)[k];
  Object.assign(defaultPolicy, p);
}

const chatOk = (call: any) =>
  json({
    id: 'chatcmpl-g',
    object: 'chat.completion',
    created: 1,
    model: call.body.model,
    choices: [
      {
        index: 0,
        message: { role: 'assistant', content: 'ok' },
        finish_reason: 'stop',
      },
    ],
    usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
  });

const openai = {
  'x-portkey-provider': 'openai',
  authorization: 'Bearer sk-regression',
};
const hi = [{ role: 'user', content: 'hi' }];

beforeEach(() => {
  resetHarness();
  usePolicy(nordic);
  upstream(/api\.openai\.com\/v1\/chat\/completions$/, chatOk);
  upstream(/api\.openai\.com\/v1\/embeddings$/, () =>
    json({
      object: 'list',
      data: [{ object: 'embedding', index: 0, embedding: [0.1] }],
      model: 'text-embedding-3-small',
      usage: { prompt_tokens: 1, total_tokens: 1 },
    })
  );
});
afterAll(() => usePolicy(original));

async function expectViolation(res: Response) {
  expect(res.status).toBe(403);
  const body: any = await res.json();
  expect(body.error).toMatchObject({
    type: 'permission_error',
    code: 'policy_violation',
  });
  expect(upstreamCalls).toHaveLength(0);
  return body;
}

describe('a concrete model, no routing hint', () => {
  test('a model not allowed at the declared level is refused', async () => {
    const res = await post(
      app,
      '/v1/chat/completions',
      { model: 'gpt-4o', messages: hi },
      { ...openai, 'x-ps-data-classification': 'restricted' }
    );
    const body = await expectViolation(res);
    expect(body.error.message).toContain('restricted');
  });

  test('detected PII escalates the level for a concrete model too', async () => {
    // gpt-4o is allowed at `internal`; the personnummer makes it restricted.
    const res = await post(
      app,
      '/v1/chat/completions',
      {
        model: 'gpt-4o',
        messages: [{ role: 'user', content: 'Patient 19121212-1212' }],
      },
      { ...openai, 'x-ps-data-classification': 'internal' }
    );
    await expectViolation(res);
  });

  test('the default level applies when nothing is declared', async () => {
    // unclassified_default is confidential: US, non-ZDR gpt-4o is out.
    await expectViolation(
      await post(
        app,
        '/v1/chat/completions',
        { model: 'gpt-4o', messages: hi },
        openai
      )
    );
  });

  test('a model the policy does not list has no facts: refused', async () => {
    await expectViolation(
      await post(
        app,
        '/v1/chat/completions',
        { model: 'some-unlisted-model', messages: hi },
        { ...openai, 'x-ps-data-classification': 'internal' }
      )
    );
  });

  test('an allowed model is served unchanged', async () => {
    const res = await post(
      app,
      '/v1/chat/completions',
      { model: 'gpt-4o', messages: hi },
      { ...openai, 'x-ps-data-classification': 'internal' }
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].body.model).toBe('gpt-4o');
  });

  test('public allows any model, listed or not', async () => {
    const res = await post(
      app,
      '/v1/chat/completions',
      { model: 'some-unlisted-model', messages: hi },
      { ...openai, 'x-ps-data-classification': 'public' }
    );
    expect(res.status).toBe(200);
  });
});

describe('a config cannot override the routed model', () => {
  test('override_params.model to a non-compliant model is refused', async () => {
    const config = JSON.stringify({
      provider: 'openai',
      api_key: 'sk-regression',
      override_params: { model: 'gpt-4o' },
    });
    await expectViolation(
      await post(
        app,
        '/v1/chat/completions',
        { model: 'auto', messages: hi },
        {
          'x-portkey-config': config,
          'x-ps-quality': 'balanced',
          'x-ps-data-classification': 'confidential',
        }
      )
    );
  });

  test('a fallback target that is not compliant is refused before it runs', async () => {
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
      json({ error: { message: 'down' } }, 500)
    );
    const config = JSON.stringify({
      strategy: { mode: 'fallback' },
      targets: [
        { provider: 'openai', api_key: 'sk-regression' },
        {
          provider: 'openai',
          api_key: 'sk-regression',
          override_params: { model: 'gpt-4o' },
        },
      ],
    });
    const res = await post(
      app,
      '/v1/chat/completions',
      { model: 'berget-llama-70b', messages: hi },
      { 'x-portkey-config': config, 'x-ps-data-classification': 'confidential' }
    );
    expect(res.status).toBe(403);
    // Only the compliant first target reached the provider.
    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].body.model).toBe('berget-llama-70b');
  });
});

describe('other endpoints and request shapes', () => {
  test('embeddings with a non-compliant model are refused', async () => {
    await expectViolation(
      await post(
        app,
        '/v1/embeddings',
        { model: 'text-embedding-3-small', input: 'hello' },
        { ...openai, 'x-ps-data-classification': 'confidential' }
      )
    );
  });

  test('PII in an embeddings input escalates the level', async () => {
    const internalOk = {
      ...nordic,
      groups: {
        ...nordic.groups,
        embed: [
          {
            model: 'text-embedding-3-small',
            group: 'embed',
            inputCostPer1k: 0,
            outputCostPer1k: 0,
            provider: 'openai',
            jurisdiction: 'US',
            zdr: false,
            usCloudActExposure: true,
          },
        ],
      },
    };
    usePolicy(internalOk);
    const ok = await post(
      app,
      '/v1/embeddings',
      { model: 'text-embedding-3-small', input: ['hello'] },
      { ...openai, 'x-ps-data-classification': 'internal' }
    );
    expect(ok.status).toBe(200);
    resetHarness();
    await expectViolation(
      await post(
        app,
        '/v1/embeddings',
        { model: 'text-embedding-3-small', input: ['ssn 19121212-1212'] },
        { ...openai, 'x-ps-data-classification': 'internal' }
      )
    );
  });

  test('Responses API input with PII is refused for a US model', async () => {
    await expectViolation(
      await post(
        app,
        '/v1/responses',
        {
          model: 'gpt-4o',
          input: [
            {
              role: 'user',
              content: [{ type: 'input_text', text: 'id 19121212-1212' }],
            },
          ],
        },
        { ...openai, 'x-ps-data-classification': 'internal' }
      )
    );
  });
});

describe('without a data policy nothing changes', () => {
  test('a concrete model is served as before', async () => {
    usePolicy(original);
    const res = await post(
      app,
      '/v1/chat/completions',
      {
        model: 'gpt-4o',
        messages: [{ role: 'user', content: 'Patient 19121212-1212' }],
      },
      { ...openai, 'x-ps-data-classification': 'restricted' }
    );
    expect(res.status).toBe(200);
  });
});
