/**
 * The "Fallbacks and Retries" guide (#390), tested: every titled JSON block
 * in docs/guides/fallbacks-and-retries.mdx (```json fallback.json) is sent
 * as `x-portkey-config` through the real gateway app against mocked
 * providers, and the behaviour the guide describes is checked: which
 * targets are called, the one usage event per request with its `attempts`,
 * and the one GenAI span per request with a child span per attempt.
 */
jest.mock('jose', () => ({}));
// Real backoff waits (1 s, 2 s, ...) are not under test; run retries
// without delay. Production retry timing is unchanged.
jest.mock('async-retry', () => {
  const real = jest.requireActual('async-retry');
  return (fn: any, opts: any) =>
    real(fn, { ...opts, minTimeout: 0, maxTimeout: 0 });
});

import * as fs from 'fs';
import * as path from 'path';
import {
  events,
  json,
  otelSpans,
  post,
  resetHarness,
  telemetryEvent,
  until,
  upstream,
  upstreamCalls,
  UpstreamCall,
} from './harness';
import app from '../../index';
import { __resetCacheForTests } from '../../middlewares/cache';

const GUIDE = path.resolve(
  __dirname,
  '../../../../docs/guides/fallbacks-and-retries.mdx'
);

/** The guide's titled JSON blocks, by title. */
function guideConfigs(): Record<string, any> {
  const text = fs.readFileSync(GUIDE, 'utf8');
  const out: Record<string, any> = {};
  for (const m of text.matchAll(/```json ([\w.-]+\.json)\n([\s\S]*?)```/g)) {
    out[m[1]] = JSON.parse(m[2]);
  }
  return out;
}

const configs = guideConfigs();
const config = (name: string) => {
  expect(configs[name]).toBeDefined();
  return JSON.parse(JSON.stringify(configs[name]));
};

const OPENAI = /api\.openai\.com\/v1\/chat\/completions$/;
const MISTRAL = /api\.mistral\.ai\/v1\/chat\/completions$/;

const chatOk = (model: string, content = `from ${model}`) =>
  json({
    id: 'chatcmpl-guide',
    object: 'chat.completion',
    created: 1,
    model,
    choices: [
      {
        index: 0,
        message: { role: 'assistant', content },
        finish_reason: 'stop',
      },
    ],
    usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
  });

const failure = (status: number) =>
  json({ error: { message: 'upstream unavailable', type: 'x' } }, status);

/** Answers each call with the next item; the last one repeats. */
function sequence(...answers: ((call: UpstreamCall) => Response)[]) {
  let i = 0;
  return (call: UpstreamCall) =>
    answers[Math.min(i++, answers.length - 1)](call);
}

const TRACE_ID = '4bf92f3577b34da6a3ce929d0e0e4736';
const PARENT = '00f067aa0ba902b7';

const ask = (cfg: any, extra: Record<string, string> = {}) =>
  post(
    app,
    '/v1/chat/completions',
    { model: 'gpt-4o-mini', messages: [{ role: 'user', content: 'hi' }] },
    {
      'x-portkey-config': JSON.stringify(cfg),
      'x-portkey-trace-id': 'guide-trace',
      traceparent: `00-${TRACE_ID}-${PARENT}-01`,
      ...extra,
    }
  );

const attrsOf = (list: any[] = []) =>
  Object.fromEntries(
    list.map((a: any) => [
      a.key,
      a.value.stringValue ??
        (a.value.intValue !== undefined
          ? Number(a.value.intValue)
          : undefined) ??
        a.value.boolValue ??
        a.value.arrayValue?.values.map((v: any) => v.stringValue),
    ])
  );

/**
 * The request's spans once `n` are exported: its one GenAI span (the call)
 * and its children, one per upstream attempt, in attempt order.
 */
async function callSpans(n: number) {
  await until(() => otelSpans().length >= n, `${n} OTel spans`);
  await new Promise((r) => setTimeout(r, 50));
  const spans = otelSpans();
  expect(spans).toHaveLength(n);
  const calls = spans.filter(
    (s) => attrsOf(s.attributes)['gen_ai.operation.name']
  );
  expect(calls).toHaveLength(1);
  const call = calls[0];
  const attempts = spans
    .filter((s) => s !== call)
    .sort(
      (a, b) =>
        attrsOf(a.attributes)['ps.attempt.number'] -
        attrsOf(b.attributes)['ps.attempt.number']
    );
  for (const a of attempts) {
    expect(a.traceId).toBe(call.traceId);
    expect(a.parentSpanId).toBe(call.spanId);
  }
  return { call, attempts };
}

beforeEach(() => {
  resetHarness();
  __resetCacheForTests();
});

afterEach(() => {
  jest.restoreAllMocks();
});

test('the guide has the three tested configs', () => {
  expect(Object.keys(configs).sort()).toEqual([
    'fallback.json',
    'loadbalance.json',
    'retry.json',
  ]);
});

describe('fallback.json', () => {
  test('A fails, B answers: two attempts on one trace, one billed usage row', async () => {
    upstream(OPENAI, () => failure(503));
    upstream(MISTRAL, (call) => chatOk(call.body.model));

    const res = await ask(config('fallback.json'));
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.choices[0].message.content).toBe('from mistral-small-latest');
    expect(res.headers.get('x-portkey-last-used-option-index')).toBe(
      'config.targets[1]'
    );

    // The targets in order, each with its own key and model.
    expect(upstreamCalls.map((c) => [c.url, c.body.model])).toEqual([
      ['https://api.openai.com/v1/chat/completions', 'gpt-4o-mini'],
      ['https://api.mistral.ai/v1/chat/completions', 'mistral-small-latest'],
    ]);
    expect(upstreamCalls[0].headers.authorization).toBe(
      'Bearer sk-openai-primary'
    );
    expect(upstreamCalls[1].headers.authorization).toBe(
      'Bearer mistral-backup'
    );

    // One usage event: the billed row, with both attempts.
    const ev = await telemetryEvent();
    expect(ev.served_provider).toBe('mistral-ai');
    expect(ev.tokens_in).toBe(10);
    expect(ev.tokens_out).toBe(5);
    expect(ev.trace_id).toBe('guide-trace');
    expect(ev.retry_count).toBe(1);
    expect(ev.fallback_chain).toEqual(['server_error']);
    expect(
      ev.attempts.map((a: any) => [a.attempt, a.provider, a.model, a.status])
    ).toEqual([
      [1, 'openai', 'gpt-4o-mini', 503],
      [2, 'mistral-ai', 'mistral-small-latest', 200],
    ]);
    expect(ev.attempts[0].error_class).toBe('server_error');
    expect(ev.attempts[1].error_class).toBeUndefined();
    await new Promise((r) => setTimeout(r, 50));
    expect(events).toHaveLength(1);

    // One GenAI span in the caller's trace, one child span per attempt.
    const { call, attempts } = await callSpans(3);
    expect(call.traceId).toBe(TRACE_ID);
    expect(call.parentSpanId).toBe(PARENT);
    expect(res.headers.get('traceparent')).toBe(
      `00-${TRACE_ID}-${call.spanId}-01`
    );
    const attrs = attrsOf(call.attributes);
    expect(attrs['ps.attempt.count']).toBe(2);
    expect(attrs['ps.fallback_chain']).toEqual(['server_error']);
    expect(attempts.map((a) => attrsOf(a.attributes))).toEqual([
      expect.objectContaining({
        'ps.attempt.number': 1,
        'ps.attempt.provider': 'openai',
        'ps.attempt.model': 'gpt-4o-mini',
        'http.response.status_code': 503,
        'error.type': 'server_error',
      }),
      expect.objectContaining({
        'ps.attempt.number': 2,
        'ps.attempt.provider': 'mistral_ai',
        'ps.attempt.model': 'mistral-small-latest',
        'http.request.resend_count': 1,
        'http.response.status_code': 200,
      }),
    ]);
    expect(attempts[0].status.code).toBe(2);
    expect(attempts[1].status.code).toBe(0);
    expect(attrsOf(attempts[1].attributes)['error.type']).toBeUndefined();
    // Attempts lie within the call span, in order.
    const ns = (v: string) => BigInt(v);
    expect(ns(attempts[0].endTimeUnixNano)).toBeLessThanOrEqual(
      ns(attempts[1].endTimeUnixNano)
    );
    expect(ns(attempts[0].startTimeUnixNano)).toBeGreaterThanOrEqual(
      ns(call.startTimeUnixNano)
    );
    expect(ns(attempts[1].endTimeUnixNano)).toBeLessThanOrEqual(
      ns(call.endTimeUnixNano)
    );
    // Metadata only: no content or error message on any span.
    expect(JSON.stringify(otelSpans())).not.toContain('upstream unavailable');
  });

  test('A answers: one attempt, B is never called', async () => {
    upstream(OPENAI, (call) => chatOk(call.body.model));
    upstream(MISTRAL, () => failure(500));

    const res = await ask(config('fallback.json'));
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(1);
    const ev = await telemetryEvent();
    expect(ev.retry_count).toBe(0);
    expect(ev.attempts).toHaveLength(1);
    const { call, attempts } = await callSpans(2);
    expect(attempts).toHaveLength(1);
    expect(attrsOf(call.attributes)['ps.fallback_chain']).toBeUndefined();
  });

  test('a status outside on_status_codes is returned as is', async () => {
    upstream(OPENAI, () => failure(400));
    upstream(MISTRAL, (call) => chatOk(call.body.model));

    const res = await ask(config('fallback.json'));
    expect(res.status).toBe(400);
    expect(upstreamCalls).toHaveLength(1);
  });

  test('with the cache on, an answer from B is cached under B: A is tried again', async () => {
    const cfg = { ...config('fallback.json'), cache: { mode: 'simple' } };
    upstream(OPENAI, () => failure(503));
    upstream(MISTRAL, (call) => chatOk(call.body.model));

    expect((await ask(cfg)).status).toBe(200);
    await until(() => events.length === 1, 'first event');
    expect((await ask(cfg)).status).toBe(200);
    await until(() => events.length === 2, 'second event');

    // Second request: A again (503), then B's cached answer, no B call.
    expect(upstreamCalls.map((c) => new URL(c.url).host)).toEqual([
      'api.openai.com',
      'api.mistral.ai',
      'api.openai.com',
    ]);
    const second = events[1];
    expect(second.cache_status).toBe('HIT');
    expect(second.cost).toBe(0);
    expect(second.attempts.map((a: any) => a.provider)).toEqual(['openai']);
  });
});

describe('loadbalance.json', () => {
  test('picks one target by weight and makes one attempt', async () => {
    upstream(OPENAI, (call) => chatOk(call.body.model));
    const random = jest.spyOn(Math, 'random');

    random.mockReturnValue(0.5); // 0.5 < 0.7: key A
    expect((await ask(config('loadbalance.json'))).status).toBe(200);
    random.mockReturnValue(0.9); // 0.9 >= 0.7: key B
    expect((await ask(config('loadbalance.json'))).status).toBe(200);

    expect(upstreamCalls.map((c) => c.headers.authorization)).toEqual([
      'Bearer sk-openai-key-a',
      'Bearer sk-openai-key-b',
    ]);
    await until(() => events.length === 2, 'two events');
    for (const ev of events) {
      expect(ev.retry_count).toBe(0);
      expect(ev.attempts).toHaveLength(1);
    }
  });

  test('does not fall back by itself', async () => {
    upstream(OPENAI, () => failure(503));
    jest.spyOn(Math, 'random').mockReturnValue(0.1);

    const res = await ask(config('loadbalance.json'));
    expect(res.status).toBe(503);
    expect(upstreamCalls).toHaveLength(1);
  });
});

describe('retry.json', () => {
  test('two 503s then a 200: three attempts, one response, one event', async () => {
    upstream(
      OPENAI,
      sequence(
        () => failure(503),
        () => failure(503),
        (call) => chatOk(call.body.model)
      )
    );

    const res = await ask(config('retry.json'));
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(3);
    expect(res.headers.get('x-portkey-retry-attempt-count')).toBe('2');

    const ev = await telemetryEvent();
    expect(ev.retry_count).toBe(2);
    expect(ev.attempts.map((a: any) => a.status)).toEqual([503, 503, 200]);
    expect(ev.fallback_chain).toEqual(['server_error', 'server_error']);

    const { call, attempts } = await callSpans(4);
    expect(attrsOf(call.attributes)['ps.attempt.count']).toBe(3);
    expect(
      attempts.map((a) => attrsOf(a.attributes)['http.request.resend_count'])
    ).toEqual([undefined, 1, 2]);
  });

  test('a status outside on_status_codes is not retried', async () => {
    upstream(OPENAI, () => failure(500));

    const res = await ask(config('retry.json'));
    expect(res.status).toBe(500);
    expect(upstreamCalls).toHaveLength(1);
  });
});
