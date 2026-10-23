/**
 * call_id and POST /v1/feedback (#43), through the whole gateway app and
 * offline: every reported LLM call gets a call_id (the caller's
 * X-PS-Call-Id, else a UUID) on its event and the X-PS-Call-Id response
 * header; feedback naming it is checked, rate-limited per user and
 * forwarded to the collector's /ingest/feedback as metadata only.
 */
jest.mock('jose', () => ({}));

import {
  events,
  feedbackCollector,
  feedbackRequests,
  json,
  post,
  resetHarness,
  sse,
  telemetryEvent,
  upstream,
  upstreamCalls,
} from './harness';
import app from '../../index';
import {
  _resetFeedbackRateLimitsForTests,
  FEEDBACK_REASONS,
  MAX_FEEDBACK_BYTES,
  parseFeedback,
} from '../../middlewares/psFeedback';

const OPENAI = {
  'x-portkey-provider': 'openai',
  authorization: 'Bearer sk-regression',
};
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

const chat = (headers: Record<string, string> = {}, stream = false) =>
  post(
    app,
    '/v1/chat/completions',
    {
      model: 'gpt-4o',
      messages: [{ role: 'user', content: 'hi' }],
      ...(stream ? { stream: true } : {}),
    },
    { ...OPENAI, ...headers }
  );

const feedback = (body: unknown, headers: Record<string, string> = {}) =>
  app.request('/v1/feedback', {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...OPENAI, ...headers },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });

beforeEach(() => {
  resetHarness();
  _resetFeedbackRateLimitsForTests();
  delete process.env.PS_FEEDBACK_RATE_LIMIT_PER_MINUTE;
  upstream(/api\.openai\.com\/v1\/chat\/completions$/, (call) =>
    call.body?.stream
      ? sse(
          'data: {"id":"c","object":"chat.completion.chunk","model":"gpt-4o","choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n' +
            'data: {"id":"c","object":"chat.completion.chunk","model":"gpt-4o","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\n' +
            'data: [DONE]\n\n'
        )
      : json({
          id: 'chatcmpl-1',
          object: 'chat.completion',
          model: 'gpt-4o-2024-08-06',
          choices: [
            {
              index: 0,
              message: { role: 'assistant', content: 'hi' },
              finish_reason: 'stop',
            },
          ],
          usage: { prompt_tokens: 3, completion_tokens: 1 },
        })
  );
});

describe('call_id', () => {
  test('a new UUID on the event and the response header', async () => {
    const res = await chat();
    expect(res.status).toBe(200);
    const id = res.headers.get('x-ps-call-id');
    expect(id).toMatch(UUID_RE);
    const event = await telemetryEvent();
    expect(event.call_id).toBe(id);

    const again = await chat();
    expect(again.headers.get('x-ps-call-id')).not.toBe(id);
  });

  test("the caller's X-PS-Call-Id is kept and never sent upstream", async () => {
    const res = await chat({ 'x-ps-call-id': 'sdk-call.42' });
    expect(res.headers.get('x-ps-call-id')).toBe('sdk-call.42');
    expect((await telemetryEvent()).call_id).toBe('sdk-call.42');
    expect(Object.keys(upstreamCalls[0].headers)).not.toContain('x-ps-call-id');
  });

  test('an unusable X-PS-Call-Id is replaced', async () => {
    const res = await chat({ 'x-ps-call-id': 'has spaces in it' });
    expect(res.headers.get('x-ps-call-id')).toMatch(UUID_RE);
  });

  test('streamed calls carry it too', async () => {
    const res = await chat({}, true);
    const id = res.headers.get('x-ps-call-id');
    expect(id).toMatch(UUID_RE);
    await res.text();
    expect((await telemetryEvent()).call_id).toBe(id);
  });
});

describe('POST /v1/feedback', () => {
  test('forwards metadata only to the collector', async () => {
    const res = await feedback({
      call_id: 'call-1',
      score: 0.25,
      reason: 'incomplete',
      user_id: 'u-1',
      comment: 'free text never leaves',
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      call_id: 'call-1',
      created: true,
      matched: true,
    });
    expect(feedbackRequests).toHaveLength(1);
    const [sent] = feedbackRequests;
    expect(sent.headers.authorization).toBe('Bearer ps-regression-key');
    expect(sent.body).toEqual({
      call_id: 'call-1',
      score: 0.25,
      reason: 'incomplete',
      user_id: 'u-1',
      source: 'gateway',
    });
    // Not an LLM call: no telemetry event, no upstream call.
    await new Promise((r) => setTimeout(r, 20));
    expect(events).toHaveLength(0);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('a boolean score is 1 or 0', async () => {
    await feedback({ call_id: 'c', score: true });
    await feedback({ call_id: 'c', score: false });
    expect(feedbackRequests.map((r) => r.body.score)).toEqual([1, 0]);
  });

  test.each([
    [{ score: 1 }],
    [{ call_id: 'a b', score: 1 }],
    [{ call_id: 'c', score: 1.5 }],
    [{ call_id: 'c', score: '1' }],
    [{ call_id: 'c' }],
    [{ call_id: 'c', score: 1, reason: 'it was rude' }],
    [{ call_id: 'c', score: 1, user_id: '' }],
    [{ call_id: 'c', score: 1, user_id: 'a\nb' }],
    [{ call_id: 'c\u0000', score: 1 }],
    [{ call_id: 'c', score: 1, other: 'x\u0000' }],
    [[{ call_id: 'c', score: 1 }]],
  ])('refuses %j with 422', async (body) => {
    const res = await feedback(body);
    expect(res.status).toBe(422);
    expect(feedbackRequests).toHaveLength(0);
  });

  test('refuses a lone surrogate, bad JSON and a large body', async () => {
    expect(
      (await feedback('{"call_id": "c", "score": 1, "user_id": "\\ud800"}'))
        .status
    ).toBe(422);
    expect((await feedback('{nope')).status).toBe(400);
    const big = JSON.stringify({
      call_id: 'c',
      score: 1,
      pad: 'x'.repeat(MAX_FEEDBACK_BYTES),
    });
    expect((await feedback(big)).status).toBe(413);
    expect(feedbackRequests).toHaveLength(0);
  });

  test('is rate-limited per user', async () => {
    process.env.PS_FEEDBACK_RATE_LIMIT_PER_MINUTE = '2';
    const alice = { authorization: 'Bearer sk-alice' };
    const bob = { authorization: 'Bearer sk-bob' };
    expect((await feedback({ call_id: 'a1', score: 1 }, alice)).status).toBe(
      200
    );
    expect((await feedback({ call_id: 'a2', score: 1 }, alice)).status).toBe(
      200
    );
    const limited = await feedback({ call_id: 'a3', score: 1 }, alice);
    expect(limited.status).toBe(429);
    expect(Number(limited.headers.get('retry-after'))).toBeGreaterThanOrEqual(
      1
    );
    // A body user_id does not escape the caller's own bucket.
    expect(
      (await feedback({ call_id: 'a4', score: 1, user_id: 'x' }, alice)).status
    ).toBe(429);
    expect((await feedback({ call_id: 'b1', score: 1 }, bob)).status).toBe(200);
    expect(feedbackRequests.map((r) => r.body.call_id)).toEqual([
      'a1',
      'a2',
      'b1',
    ]);
  });

  test('maps collector answers', async () => {
    feedbackCollector(
      () =>
        new Response(
          JSON.stringify({ detail: { status: 422, detail: 'bad' } }),
          { status: 422, headers: { 'content-type': 'application/json' } }
        )
    );
    expect((await feedback({ call_id: 'c', score: 1 })).status).toBe(422);

    feedbackCollector(
      () =>
        new Response('{}', {
          status: 429,
          headers: { 'retry-after': '7' },
        })
    );
    const limited = await feedback({ call_id: 'c', score: 1 });
    expect(limited.status).toBe(429);
    expect(limited.headers.get('retry-after')).toBe('7');

    feedbackCollector(() => new Response('{}', { status: 401 }));
    expect((await feedback({ call_id: 'c', score: 1 })).status).toBe(502);

    feedbackCollector(() => {
      throw new TypeError('fetch failed');
    });
    expect((await feedback({ call_id: 'c', score: 1 })).status).toBe(502);
  });
});

test('parseFeedback keeps only known fields', () => {
  expect(
    parseFeedback({ call_id: 'c', score: 0, reason: 'other', extra: 1 })
  ).toEqual({ call_id: 'c', score: 0, reason: 'other' });
  expect(FEEDBACK_REASONS).toContain('hallucination');
});
