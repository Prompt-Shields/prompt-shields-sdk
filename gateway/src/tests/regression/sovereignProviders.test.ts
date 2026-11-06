/**
 * OpenAI-compatible EU sovereign providers end to end (#20): the call
 * reaches the provider's fixed base URL with bearer auth, the telemetry
 * event reports `served_provider` and the policy's `served_jurisdiction`,
 * and the data policy only applies a tier's facts to the provider (and base
 * URL) it names. Offline: the harness stubs every upstream and the
 * collector. The policy is the nordic-sovereign example.
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
  telemetryEvent,
  upstream,
  upstreamCalls,
} from './harness';
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
    id: 'chatcmpl-eu',
    object: 'chat.completion',
    created: 1,
    model: call.body.model,
    choices: [
      {
        index: 0,
        message: { role: 'assistant', content: 'hej' },
        finish_reason: 'stop',
      },
    ],
    usage: {
      prompt_tokens: 12,
      completion_tokens: 3,
      total_tokens: 15,
      prompt_tokens_details: { cached_tokens: 4 },
    },
  });

const hi = [{ role: 'user', content: 'hi' }];
const confidential = { 'x-ps-data-classification': 'confidential' };
const as = (provider: string) => ({
  'x-portkey-provider': provider,
  authorization: 'Bearer eu-key',
});

beforeEach(() => {
  resetHarness();
  usePolicy(nordic);
  upstream(/api\.scaleway\.ai\/v1\/chat\/completions$/, chatOk);
  upstream(/api\.infercom\.ai\/v1\/chat\/completions$/, chatOk);
  upstream(/api\.openai\.com\/v1\/chat\/completions$/, chatOk);
});
afterAll(() => usePolicy(original));

async function expectViolation(res: Response) {
  expect(res.status).toBe(403);
  const body: any = await res.json();
  expect(body.error.code).toBe('policy_violation');
  expect(upstreamCalls).toHaveLength(0);
}

test('a confidential call is served by the sovereign provider', async () => {
  const res = await post(
    app,
    '/v1/chat/completions',
    { model: 'scaleway-small', messages: hi },
    { ...as('scaleway'), ...confidential }
  );
  expect(res.status).toBe(200);
  expect(upstreamCalls).toHaveLength(1);
  expect(upstreamCalls[0].url).toBe(
    'https://api.scaleway.ai/v1/chat/completions'
  );
  expect(upstreamCalls[0].headers.authorization).toBe('Bearer eu-key');
  const event = await telemetryEvent();
  expect(event).toMatchObject({
    vendor: 'scaleway',
    served_provider: 'scaleway',
    served_jurisdiction: 'EEA',
    tokens_in: 12,
    tokens_out: 3,
    tokens_cached: 4,
  });
});

test("a tier's facts do not vouch for the same model at another provider", async () => {
  // scaleway-small is EEA only at scaleway; at OpenAI nothing vouches for it.
  await expectViolation(
    await post(
      app,
      '/v1/chat/completions',
      { model: 'scaleway-small', messages: hi },
      { ...as('openai'), ...confidential }
    )
  );
});

test("a custom host gets no facts from the provider's tier", async () => {
  const res = await post(
    app,
    '/v1/chat/completions',
    { model: 'scaleway-small', messages: hi },
    {
      ...as('scaleway'),
      ...confidential,
      'x-portkey-custom-host': 'https://inference.example.com/v1',
    }
  );
  await expectViolation(res);
});

test("the provider's own base URL as custom host keeps its facts", async () => {
  const res = await post(
    app,
    '/v1/chat/completions',
    { model: 'scaleway-small', messages: hi },
    {
      ...as('scaleway'),
      ...confidential,
      'x-portkey-custom-host': 'https://api.scaleway.ai/v1',
    }
  );
  expect(res.status).toBe(200);
  expect((await telemetryEvent()).served_jurisdiction).toBe('EEA');
});

test('a model the policy does not state facts for has no jurisdiction', async () => {
  const res = await post(
    app,
    '/v1/chat/completions',
    { model: 'DeepSeek-V3.1', messages: hi },
    { ...as('infercom'), 'x-ps-data-classification': 'public' }
  );
  expect(res.status).toBe(200);
  expect(upstreamCalls[0].url).toBe(
    'https://api.infercom.ai/v1/chat/completions'
  );
  const event = await telemetryEvent();
  expect(event.served_provider).toBe('infercom');
  expect(event.served_jurisdiction).toBeUndefined();
});

test('the served provider is the fallback target that answered', async () => {
  upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
    json({ error: { message: 'down' } }, 500)
  );
  const config = JSON.stringify({
    strategy: { mode: 'fallback' },
    targets: [
      { provider: 'openai', api_key: 'sk-us' },
      { provider: 'scaleway', api_key: 'eu-key' },
    ],
  });
  const res = await post(
    app,
    '/v1/chat/completions',
    { model: 'gpt-4o', messages: hi },
    { 'x-portkey-config': config, 'x-ps-data-classification': 'public' }
  );
  expect(res.status).toBe(200);
  const event = await telemetryEvent();
  expect(event.served_provider).toBe('scaleway');
  // gpt-4o's only tiers name openai: no stated jurisdiction at scaleway.
  expect(event.served_jurisdiction).toBeUndefined();
  expect(events).toHaveLength(1);
});

describe("the router's choice is held to the provider too", () => {
  // berget-llama-70b is the only restricted-compliant balanced tier; it
  // names berget. Before, the router rewrote the model and the call ran at
  // whatever provider the request named, here a US one.
  const restricted = {
    'x-ps-data-classification': 'restricted',
    'x-ps-quality': 'balanced',
  };

  test('a routed EEA tier is not sent to another provider', async () => {
    upstream(/api\.together\.xyz\/v1\/chat\/completions$/, chatOk);
    await expectViolation(
      await post(
        app,
        '/v1/chat/completions',
        { model: 'auto', messages: hi },
        { ...as('together-ai'), ...restricted }
      )
    );
  });

  test('a routing hint on a concrete model does not skip the check', async () => {
    await expectViolation(
      await post(
        app,
        '/v1/chat/completions',
        { model: 'berget-llama-70b', messages: hi },
        { ...as('openai'), ...restricted }
      )
    );
  });

  test('a routed tier is served at its own provider', async () => {
    upstream(/api\.berget\.ai\/v1\/chat\/completions$/, chatOk);
    const res = await post(
      app,
      '/v1/chat/completions',
      { model: 'auto', messages: hi },
      { ...as('berget'), ...restricted }
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].url).toBe(
      'https://api.berget.ai/v1/chat/completions'
    );
    expect(upstreamCalls[0].body.model).toBe('berget-llama-70b');
    expect((await telemetryEvent()).served_jurisdiction).toBe('EEA');
  });
});
