/**
 * Implicit outcome signals in gateway telemetry (#44): the classes the
 * gateway can see (fallback, max_tokens, refusal, stream_error, abandoned),
 * named and versioned as the SDK names them.
 */
process.env.PS_API_KEY = 'ps-test';

import { Hono } from 'hono';
import { psTelemetry } from './ps-telemetry';
import {
  hasRefusal,
  MAX_STOP_REASONS,
  OUTCOME_SIGNALS_VERSION,
  outcomeSignals,
  stopReasonsOf,
} from './psOutcomeSignals';
import { SSEUsageTracker } from './psStreamObserver';
import { RESPONSE_HEADER_KEYS } from '../globals';

const enc = new TextEncoder();

describe('outcome signal helpers', () => {
  test('stop reasons from every response shape', () => {
    expect(
      stopReasonsOf({
        choices: [{ finish_reason: 'length' }, { finish_reason: 'stop' }],
      })
    ).toEqual(['length', 'stop']);
    expect(stopReasonsOf({ stop_reason: 'max_tokens' })).toEqual([
      'max_tokens',
    ]);
    expect(
      stopReasonsOf({
        type: 'message_delta',
        delta: { stop_reason: 'refusal' },
      })
    ).toEqual(['refusal']);
    expect(
      stopReasonsOf({ incomplete_details: { reason: 'max_output_tokens' } })
    ).toEqual(['max_output_tokens']);
    expect(
      stopReasonsOf({
        type: 'response.incomplete',
        response: { incomplete_details: { reason: 'content_filter' } },
      })
    ).toEqual(['content_filter']);
    expect(stopReasonsOf({ stop_reason: 'FinishReason.SAFETY' })).toEqual([
      'safety',
    ]);
    expect(stopReasonsOf(undefined)).toEqual([]);
    expect(stopReasonsOf({ stop_reason: 'x'.repeat(65) })).toEqual([]);
  });

  test('explicit refusals', () => {
    expect(hasRefusal({ choices: [{ message: { refusal: 'No.' } }] })).toBe(
      true
    );
    expect(hasRefusal({ choices: [{ message: { refusal: null } }] })).toBe(
      false
    );
    expect(
      hasRefusal({
        output: [{ content: [{ type: 'refusal', refusal: 'No.' }] }],
      })
    ).toBe(true);
    expect(hasRefusal({ type: 'response.refusal.delta', delta: 'N' })).toBe(
      true
    );
    expect(hasRefusal({ choices: [{ message: { content: 'ok' } }] })).toBe(
      false
    );
  });

  test('classes, sorted; abandoned wins over stream_error', () => {
    expect(outcomeSignals({})).toEqual([]);
    expect(
      outcomeSignals({
        stopReasons: ['length', 'content_filter'],
        fallbackChain: ['rate_limited'],
      })
    ).toEqual(['fallback', 'max_tokens', 'refusal']);
    expect(outcomeSignals({ fallbackChain: [] })).toEqual([]);
    expect(outcomeSignals({ refusal: true })).toEqual(['refusal']);
    expect(outcomeSignals({ streamTruncated: true })).toEqual(['stream_error']);
    expect(
      outcomeSignals({ streamTruncated: true, streamCancelled: true })
    ).toEqual(['abandoned']);
  });

  test('the stream tracker keeps stop reasons and refusals, bounded', () => {
    const t = new SSEUsageTracker();
    const reasons = Array.from(
      { length: MAX_STOP_REASONS + 4 },
      (_, i) => `r${i}`
    );
    for (const r of reasons) {
      t.push(
        enc.encode(
          `data: {"choices":[{"delta":{},"finish_reason":"${r}"}]}\n\n`
        )
      );
    }
    t.push(enc.encode('data: {"choices":[{"delta":{"refusal":"No"}}]}\n\n'));
    const s = t.end();
    expect(s.stopReasons).toEqual(reasons.slice(0, MAX_STOP_REASONS));
    expect(s.refusal).toBe(true);
  });
});

describe('psTelemetry emits outcome signals', () => {
  const origFetch = global.fetch;
  let sent: any[];

  beforeEach(() => {
    sent = [];
    global.fetch = jest.fn(async (_url: any, init: any) => {
      sent.push(JSON.parse(init.body).events[0]);
      return { ok: true } as any;
    }) as any;
  });
  afterAll(() => {
    global.fetch = origFetch;
  });

  const flush = () => new Promise((r) => setTimeout(r, 0));

  function makeApp(path: string, respond: () => Response) {
    const app = new Hono();
    app.use('*', psTelemetry());
    app.post(path, respond);
    return app;
  }

  const post = (app: Hono, path: string) =>
    app.request(path, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-portkey-provider': 'openai',
      },
      body: JSON.stringify({ model: 'gpt-4o' }),
    });

  const json = (body: any) => () =>
    new Response(JSON.stringify(body), {
      headers: { 'content-type': 'application/json' },
    });

  test('JSON chat answer cut off at max tokens', async () => {
    const app = makeApp(
      '/v1/chat/completions',
      json({
        model: 'gpt-4o',
        choices: [{ message: { content: 'Hi' }, finish_reason: 'length' }],
        usage: { prompt_tokens: 3, completion_tokens: 5 },
      })
    );
    await post(app, '/v1/chat/completions');
    await flush();
    expect(sent[0]).toMatchObject({
      outcome_signals: ['max_tokens'],
      outcome_signals_version: OUTCOME_SIGNALS_VERSION,
    });
  });

  test('a clean answer reports an empty list (measured, nothing happened)', async () => {
    const app = makeApp(
      '/v1/messages',
      json({ model: 'claude-x', stop_reason: 'end_turn', content: [] })
    );
    await post(app, '/v1/messages');
    await flush();
    expect(sent[0].outcome_signals).toEqual([]);
  });

  test('a cache hit carries no outcome signals (not a model call)', async () => {
    const app = makeApp(
      '/v1/chat/completions',
      () =>
        new Response(
          JSON.stringify({
            model: 'gpt-4o',
            choices: [{ message: { content: 'Hi' }, finish_reason: 'length' }],
            usage: { prompt_tokens: 3, completion_tokens: 5 },
          }),
          {
            headers: {
              'content-type': 'application/json',
              [RESPONSE_HEADER_KEYS.CACHE_STATUS]: 'SEMANTIC HIT',
            },
          }
        )
    );
    await post(app, '/v1/chat/completions');
    await flush();
    expect(sent[0].cost_source).toBe('cache');
    expect(sent[0].outcome_signals).toBeUndefined();
    expect(sent[0].outcome_signals_version).toBeUndefined();
  });

  test('non-generation surfaces carry no outcome signals', async () => {
    const app = makeApp(
      '/v1/embeddings',
      json({ data: [], usage: { prompt_tokens: 3 } })
    );
    await post(app, '/v1/embeddings');
    await flush();
    expect(sent[0].outcome_signals).toBeUndefined();
    expect(sent[0].outcome_signals_version).toBeUndefined();
  });

  test('a stream cut off upstream is a stream_error; refusal from its events', async () => {
    const chunks = [
      'data: {"model":"gpt-4o","choices":[{"delta":{"refusal":"I can"}}]}\n\n',
    ];
    const app = makeApp(
      '/v1/chat/completions',
      () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              chunks.forEach((c) => controller.enqueue(enc.encode(c)));
              controller.close();
            },
          }),
          { headers: { 'content-type': 'text/event-stream' } }
        )
    );
    const res = await post(app, '/v1/chat/completions');
    await res.text();
    await flush();
    expect(sent[0]).toMatchObject({
      stream: true,
      stream_truncated: true,
      outcome_signals: ['refusal', 'stream_error'],
      outcome_signals_version: OUTCOME_SIGNALS_VERSION,
    });
  });
});
