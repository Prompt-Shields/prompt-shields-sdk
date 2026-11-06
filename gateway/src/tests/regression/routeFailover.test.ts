/**
 * Failover between routed models (#21, PRD 0001 §7.5) through the real
 * gateway app, with stub upstreams. The router lists the compliant
 * candidates (nordic-sovereign policy: at `confidential`, the draft group's
 * EEA models scaleway-small then mistral-small-eu; the US gpt-4o-mini is
 * never a candidate) and the chat/messages handlers fail over across them
 * on a 5xx, 429 or timeout. Anything else is returned as is.
 */
jest.mock('jose', () => ({}));
jest.mock('../../middlewares/router/policy', () => {
  const actual = jest.requireActual('../../middlewares/router/policy');
  return { ...actual, defaultPolicy: { ...actual.defaultPolicy } };
});

import * as fs from 'fs';
import * as path from 'path';
import {
  json,
  post,
  resetHarness,
  sse,
  telemetryEvent,
  upstream,
  upstreamCalls,
  UpstreamCall,
} from './harness';
import app from '../../index';
import { defaultPolicy } from '../../middlewares/router/policy';
import { fromRoutePolicyDoc } from '../../middlewares/router/constraints';
import * as routerIndex from '../../middlewares/router/index';
import { _resetRouteBreakersForTests } from '../../middlewares/router/failover';
import { BreakerRegistry } from '../../middlewares/psFailover';

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

const CHAT = /api\.openai\.com\/v1\/chat\/completions$/;
const chatOk = (model: string) =>
  json({
    id: 'chatcmpl-f',
    object: 'chat.completion',
    created: 1,
    model,
    choices: [
      {
        index: 0,
        message: { role: 'assistant', content: `from ${model}` },
        finish_reason: 'stop',
      },
    ],
    usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
  });

/** Answers per model: a Response factory, or a status for a canned error. */
function byModel(
  answers: Record<string, ((call: UpstreamCall) => Response) | number>
) {
  upstream(CHAT, (call) => {
    const a = answers[call.body.model];
    if (typeof a === 'function') return a(call);
    if (typeof a === 'number')
      return json({ error: { message: 'upstream failed', type: 'x' } }, a);
    return chatOk(call.body.model);
  });
}

const models = () => upstreamCalls.map((c) => c.body.model);

const openai = {
  'x-portkey-provider': 'openai',
  authorization: 'Bearer sk-regression',
};
const routed = {
  ...openai,
  'x-ps-quality': 'draft',
  'x-ps-data-classification': 'confidential',
};
const hi = [{ role: 'user', content: 'hi' }];
const chat = (headers: Record<string, string> = routed, body: any = {}) =>
  post(
    app,
    '/v1/chat/completions',
    { model: 'auto', messages: hi, ...body },
    headers
  );

let breakers: BreakerRegistry;
beforeEach(() => {
  resetHarness();
  usePolicy(nordic);
  breakers = _resetRouteBreakersForTests();
});
afterAll(() => {
  usePolicy(original);
  _resetRouteBreakersForTests();
});

describe('fails over to the next compliant candidate', () => {
  test('a 5xx moves to the next candidate; telemetry records the chain', async () => {
    byModel({ 'scaleway-small': 500 });
    const res = await chat();
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.choices[0].message.content).toBe('from mistral-small-eu');
    expect(models()).toEqual(['scaleway-small', 'mistral-small-eu']);

    const ev = await telemetryEvent();
    expect(ev.fallback_chain).toEqual(['server_error']);
    // #44: a failover is the `fallback` outcome class.
    expect(ev.outcome_signals).toEqual(['fallback']);
    expect(ev.served_model).toBe('mistral-small-eu');
    expect(ev.requested_model).toBe('auto');
    expect(ev.retry_count).toBe(1);
    expect(ev.attempts.map((a: any) => [a.model, a.status])).toEqual([
      ['scaleway-small', 500],
      ['mistral-small-eu', 200],
    ]);
  });

  test('a 429 without Retry-After moves on; a gateway timeout too', async () => {
    byModel({ 'scaleway-small': 429 });
    expect((await chat()).status).toBe(200);
    expect(models()).toEqual(['scaleway-small', 'mistral-small-eu']);

    resetHarness();
    byModel({ 'scaleway-small': 504 });
    expect((await chat()).status).toBe(200);
    expect(models()).toEqual(['scaleway-small', 'mistral-small-eu']);
  });

  test('a 429 with a short Retry-After is retried once on the same model', async () => {
    let n = 0;
    byModel({
      'scaleway-small': (call) =>
        n++ === 0
          ? new Response('{"error":{"message":"slow down"}}', {
              status: 429,
              headers: {
                'content-type': 'application/json',
                'retry-after': '0',
              },
            })
          : chatOk(call.body.model),
    });
    expect((await chat()).status).toBe(200);
    expect(models()).toEqual(['scaleway-small', 'scaleway-small']);
  });

  test('never fails over to a non-compliant model: the last error is returned', async () => {
    byModel({ 'scaleway-small': 503, 'mistral-small-eu': 502 });
    const res = await chat();
    expect(res.status).toBe(502);
    // gpt-4o-mini (US) is cheaper but never a candidate at `confidential`.
    expect(models()).toEqual(['scaleway-small', 'mistral-small-eu']);
    const ev = await telemetryEvent();
    expect(ev.fallback_chain).toEqual(['server_error', 'server_error']);
  });

  test('at `internal` the US model is allowed and is a candidate', async () => {
    byModel({ 'scaleway-small': 500 });
    const res = await chat({
      ...routed,
      'x-ps-data-classification': 'internal',
    });
    expect(res.status).toBe(200);
    expect(models()).toEqual(['scaleway-small', 'gpt-4o-mini']);
  });

  test('the messages endpoint fails over too', async () => {
    upstream(/api\.anthropic\.com\/v1\/messages$/, (call) =>
      call.body.model === 'scaleway-small'
        ? json({ type: 'error', error: { type: 'overloaded_error' } }, 529)
        : json({
            id: 'msg_1',
            type: 'message',
            role: 'assistant',
            model: call.body.model,
            content: [{ type: 'text', text: 'ok' }],
            stop_reason: 'end_turn',
            usage: { input_tokens: 1, output_tokens: 1 },
          })
    );
    const res = await post(
      app,
      '/v1/messages',
      { model: 'auto', max_tokens: 16, messages: hi },
      {
        'x-portkey-provider': 'anthropic',
        'x-api-key': 'sk-ant-regression',
        'x-ps-quality': 'draft',
        'x-ps-data-classification': 'confidential',
      }
    );
    expect(res.status).toBe(200);
    expect(models()).toEqual(['scaleway-small', 'mistral-small-eu']);
  });
});

describe('does not fail over', () => {
  test('on a content-filter refusal', async () => {
    byModel({
      'scaleway-small': () =>
        json(
          {
            error: {
              message: 'blocked by the content management policy',
              code: 'content_filter',
            },
          },
          400
        ),
    });
    const res = await chat();
    expect(res.status).toBe(400);
    expect(models()).toEqual(['scaleway-small']);
  });

  test('on another 4xx', async () => {
    byModel({ 'scaleway-small': 401 });
    expect((await chat()).status).toBe(401);
    expect(models()).toEqual(['scaleway-small']);
  });

  test('once a stream has started', async () => {
    byModel({
      'scaleway-small': () =>
        sse(
          'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"scaleway-small","choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}\n\n'
        ),
    });
    const res = await chat(routed, { stream: true });
    expect(res.status).toBe(200);
    expect(await res.text()).toContain('Hi');
    expect(models()).toEqual(['scaleway-small']);
  });

  test('a stream that fails before its first byte does fail over', async () => {
    byModel({
      'scaleway-small': 500,
      'mistral-small-eu': () =>
        sse(
          'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"mistral-small-eu","choices":[{"index":0,"delta":{"content":"Yo"},"finish_reason":null}]}\n\ndata: [DONE]\n\n'
        ),
    });
    const res = await chat(routed, { stream: true });
    expect(res.status).toBe(200);
    expect(await res.text()).toContain('Yo');
    expect(models()).toEqual(['scaleway-small', 'mistral-small-eu']);
  });

  test('a concrete model with no hint is one attempt, as before', async () => {
    byModel({ 'scaleway-small': 500 });
    const res = await chat(
      { ...openai, 'x-ps-data-classification': 'confidential' },
      { model: 'scaleway-small' }
    );
    expect(res.status).toBe(500);
    expect(models()).toEqual(['scaleway-small']);
  });
});

describe('x-portkey-config owns retries when it sets them', () => {
  test('config retries: the router does not fail over (no double retry)', async () => {
    byModel({ 'scaleway-small': 500 });
    const config = JSON.stringify({
      provider: 'openai',
      api_key: 'sk-regression',
      retry: { attempts: 2, on_status_codes: [500] },
    });
    const res = await chat({
      'x-portkey-config': config,
      'x-ps-quality': 'draft',
      'x-ps-data-classification': 'confidential',
    });
    expect(res.status).toBe(500);
    // Portkey's retries, all on the routed model; no router fallback.
    expect(models()).toEqual([
      'scaleway-small',
      'scaleway-small',
      'scaleway-small',
    ]);
  });

  test('config fallback targets: Portkey falls back, the router does not', async () => {
    byModel({ 'scaleway-small': 500 });
    const config = JSON.stringify({
      strategy: { mode: 'fallback' },
      targets: [
        { provider: 'openai', api_key: 'sk-regression' },
        {
          provider: 'openai',
          api_key: 'sk-regression',
          override_params: { model: 'mistral-small-eu' },
        },
      ],
    });
    const res = await chat({
      'x-portkey-config': config,
      'x-ps-quality': 'draft',
      'x-ps-data-classification': 'confidential',
    });
    expect(res.status).toBe(200);
    expect(models()).toEqual(['scaleway-small', 'mistral-small-eu']);
  });

  test('a single-target config without retries still fails over', async () => {
    byModel({ 'scaleway-small': 500 });
    const config = JSON.stringify({
      provider: 'openai',
      api_key: 'sk-regression',
    });
    const res = await chat({
      'x-portkey-config': config,
      'x-ps-quality': 'draft',
      'x-ps-data-classification': 'confidential',
    });
    expect(res.status).toBe(200);
    expect(models()).toEqual(['scaleway-small', 'mistral-small-eu']);
  });
});

describe('per-provider circuit breaker', () => {
  test('an open breaker skips its provider and is counted in /metrics', async () => {
    breakers = _resetRouteBreakersForTests(
      new BreakerRegistry({ config: { failureThreshold: 2, openSeconds: 60 } })
    );
    byModel({ 'scaleway-small': 500 });
    await chat();
    await chat();
    // Keyed `<provider>#<caller scope>`.
    const scaleway = Object.entries(breakers.snapshot()).filter(([k]) =>
      k.startsWith('scaleway#')
    );
    expect(scaleway.map(([, state]) => state)).toEqual(['open']);

    resetHarness();
    byModel({ 'scaleway-small': 500 });
    expect((await chat()).status).toBe(200);
    expect(models()).toEqual(['mistral-small-eu']);

    const metrics = await (await app.request('/metrics')).text();
    expect(metrics).toMatch(
      /ps_gateway_circuit_breaker_transitions_total\{provider="scaleway",to="open"\} [1-9]/
    );
  });

  test('every candidate breaker open: 503, no upstream call', async () => {
    breakers = _resetRouteBreakersForTests(
      new BreakerRegistry({ config: { failureThreshold: 1, openSeconds: 60 } })
    );
    byModel({ 'scaleway-small': 500, 'mistral-small-eu': 500 });
    expect((await chat()).status).toBe(500);
    resetHarness();
    byModel({});
    const res = await chat();
    expect(res.status).toBe(503);
    const body: any = await res.json();
    expect(body.error.code).toBe('no_available_candidate');
    expect(upstreamCalls).toHaveLength(0);
  });
});

describe('the data policy is checked on every attempt', () => {
  // A router extension that adds a US model to the candidates: the engine's
  // allow (the data-policy gate) skips it, so it never reaches upstream.
  const actual = routerIndex.routeCandidates;
  const withUsModel = (keepRest: boolean) =>
    jest.spyOn(routerIndex, 'routeCandidates').mockImplementation((...args) => {
      const [first, ...rest] = actual(...args);
      const us = nordic.groups.cheap.find((t) => t.model === 'gpt-4o-mini')!;
      return keepRest ? [first, us, ...rest] : [first, us];
    });
  afterEach(() => jest.restoreAllMocks());

  test('a non-compliant candidate slipped into the list is never called', async () => {
    withUsModel(true);
    byModel({ 'scaleway-small': 500 });
    const res = await chat();
    expect(res.status).toBe(200);
    expect(models()).toEqual(['scaleway-small', 'mistral-small-eu']);
  });

  test('only non-compliant fallbacks left: the first error is returned', async () => {
    withUsModel(false);
    byModel({ 'scaleway-small': 503 });
    const res = await chat();
    expect(res.status).toBe(503);
    expect(models()).toEqual(['scaleway-small']);
  });
});

describe('without a data policy', () => {
  test('the default policy fails over within the routed group', async () => {
    usePolicy(original);
    byModel({ 'gpt-4o-mini': 500 });
    const res = await chat({ ...openai, 'x-ps-quality': 'draft' });
    expect(res.status).toBe(200);
    expect(models()).toEqual(['gpt-4o-mini', 'claude-3-5-haiku-20241022']);
  });
});

describe('circuit breakers are scoped to the caller', () => {
  test("one caller's failing upstream never opens another caller's breaker", async () => {
    breakers = _resetRouteBreakersForTests(
      new BreakerRegistry({ config: { failureThreshold: 1, openSeconds: 60 } })
    );
    // Caller A (its own key) gets 5xx on every candidate: its breakers open.
    byModel({ 'scaleway-small': 500, 'mistral-small-eu': 500 });
    const a = { ...routed, authorization: 'Bearer sk-caller-a' };
    expect((await chat(a)).status).toBe(500);
    resetHarness();
    byModel({});
    expect((await chat(a)).status).toBe(503);
    expect(upstreamCalls).toHaveLength(0);

    // Caller B, a different key on the same provider, is still served.
    resetHarness();
    byModel({});
    const res = await chat({ ...routed, authorization: 'Bearer sk-caller-b' });
    expect(res.status).toBe(200);
    expect(models()).toEqual(['scaleway-small']);
  });
});

describe('the response cache with failover', () => {
  test('a fallback answer is cached under the model that served it', async () => {
    const config = JSON.stringify({
      provider: 'openai',
      api_key: 'sk-cache',
      cache: { mode: 'simple' },
    });
    const headers = {
      'x-portkey-config': config,
      'x-ps-quality': 'draft',
      'x-ps-data-classification': 'confidential',
    };
    byModel({ 'scaleway-small': 500 });
    let body: any = await (await chat(headers)).json();
    expect(body.choices[0].message.content).toBe('from mistral-small-eu');

    // The primary fails again: the fallback's own entry answers its attempt.
    resetHarness();
    byModel({ 'scaleway-small': 500, 'mistral-small-eu': 500 });
    body = await (await chat(headers)).json();
    expect(models()).toEqual(['scaleway-small']);
    expect(body.choices[0].message.content).toBe('from mistral-small-eu');

    // The primary is healthy again: it answers, not the cached fallback.
    resetHarness();
    byModel({});
    body = await (await chat(headers)).json();
    expect(body.choices[0].message.content).toBe('from scaleway-small');
    expect(models()).toEqual(['scaleway-small']);
  });
});
