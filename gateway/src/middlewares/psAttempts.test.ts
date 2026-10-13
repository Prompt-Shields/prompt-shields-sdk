/**
 * Per-attempt retry telemetry (#78): retryRequest reports every attempt, the
 * attempts are recorded on the context, and ps-telemetry folds them into the
 * request's event without any request or response content.
 */
process.env.PS_API_KEY = 'ps-test';

import { Hono } from 'hono';
import { retryRequest } from '../handlers/retryHandler';
import { attemptsOf, errorClassOf, recordAttempt } from './psAttempts';
import { psTelemetry } from './ps-telemetry';

const SECRET = 'sk-secret-should-never-leak';

function responses(...statuses: number[]) {
  const queue = [...statuses];
  return jest.fn(
    async () =>
      new Response(
        JSON.stringify({ error: { message: `upstream said ${SECRET}` } }),
        { status: queue.shift() ?? 200 }
      )
  );
}

function fakeContext() {
  const store = new Map<string, any>();
  return {
    get: (k: string) => store.get(k),
    set: (k: string, v: any) => store.set(k, v),
  };
}

describe('errorClassOf', () => {
  test('maps statuses to coarse classes', () => {
    expect(errorClassOf(200)).toBeUndefined();
    expect(errorClassOf(429)).toBe('rate_limited');
    expect(errorClassOf(500)).toBe('server_error');
    expect(errorClassOf(408)).toBe('timeout');
    expect(errorClassOf(400)).toBe('client_error');
    expect(errorClassOf(undefined)).toBe('network_error');
    expect(errorClassOf(undefined, 'AbortError')).toBe('timeout');
  });
});

describe('retryRequest onAttempt', () => {
  test('a 500 -> 429 -> 200 chain reports three attempts', async () => {
    const seen: any[] = [];
    const { response } = await retryRequest(
      'https://upstream.invalid',
      {},
      3,
      [429, 500],
      null,
      responses(500, 429, 200),
      false,
      (a) => seen.push(a)
    );
    expect(response.status).toBe(200);
    expect(seen.map((a) => a.status)).toEqual([500, 429, 200]);
    expect(seen.map((a) => a.error_class)).toEqual([
      'server_error',
      'rate_limited',
      undefined,
    ]);
    seen.forEach((a) => expect(typeof a.latency_ms).toBe('number'));
  });

  test('a non-retried error and a thrown network error are reported', async () => {
    const seen: any[] = [];
    await retryRequest(
      'https://upstream.invalid',
      {},
      2,
      [429],
      null,
      responses(400),
      false,
      (a) => seen.push(a)
    );
    expect(seen).toHaveLength(1);
    expect(seen[0]).toMatchObject({ status: 400, error_class: 'client_error' });

    const net: any[] = [];
    const spy = jest.spyOn(console, 'error').mockImplementation(() => {});
    await retryRequest(
      'https://upstream.invalid',
      {},
      0,
      [],
      null,
      async () => {
        throw new TypeError('fetch failed');
      },
      false,
      (a) => net.push(a)
    );
    spy.mockRestore();
    expect(net).toEqual([
      expect.objectContaining({ error_class: 'network_error' }),
    ]);
    expect(net[0].status).toBeUndefined();
  });

  test('a throwing callback does not affect the request', async () => {
    const { response } = await retryRequest(
      'https://upstream.invalid',
      {},
      0,
      [],
      null,
      responses(200),
      false,
      () => {
        throw new Error('boom');
      }
    );
    expect(response.status).toBe(200);
  });
});

describe('recordAttempt', () => {
  test('numbers attempts across targets and keeps only metadata', () => {
    const c = fakeContext();
    recordAttempt(
      c,
      { provider: 'openai', model: 'gpt-4o' },
      { status: 500, latency_ms: 12, error_class: 'server_error' }
    );
    recordAttempt(
      c,
      { provider: 'anthropic', model: 'claude-x' },
      { status: 200, latency_ms: 30 }
    );
    expect(attemptsOf(c)).toEqual([
      {
        attempt: 1,
        provider: 'openai',
        model: 'gpt-4o',
        status: 500,
        latency_ms: 12,
        error_class: 'server_error',
      },
      {
        attempt: 2,
        provider: 'anthropic',
        model: 'claude-x',
        status: 200,
        latency_ms: 30,
      },
    ]);
  });

  test('attemptsOf is empty when nothing was recorded', () => {
    expect(attemptsOf(fakeContext())).toEqual([]);
  });
});

describe('psTelemetry with attempts', () => {
  const origFetch = global.fetch;
  let sent: any;

  beforeEach(() => {
    sent = undefined;
    global.fetch = jest.fn(async (_url: any, init: any) => {
      sent = JSON.parse(init.body);
      return { ok: true } as any;
    }) as any;
  });
  afterAll(() => {
    global.fetch = origFetch;
  });

  const flush = () => new Promise((r) => setTimeout(r, 0));

  function app(record: boolean) {
    const a = new Hono();
    a.use('/v1/chat/completions', psTelemetry());
    a.post('/v1/chat/completions', async (c) => {
      if (record) {
        // Same path as handlerUtils: retryRequest -> recordAttempt.
        await retryRequest(
          'https://upstream.invalid',
          {},
          3,
          [429, 500],
          null,
          responses(500, 429, 200),
          false,
          (at) => recordAttempt(c, { provider: 'openai', model: 'gpt-4o' }, at)
        );
      }
      return c.json({
        model: 'gpt-4o',
        usage: { prompt_tokens: 3, completion_tokens: 4 },
      });
    });
    return a;
  }

  const post = (a: Hono, headers: Record<string, string> = {}) =>
    a.request('/v1/chat/completions', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-portkey-provider': 'openai',
        authorization: `Bearer ${SECRET}`,
        ...headers,
      },
      body: JSON.stringify({
        model: 'gpt-4o',
        messages: [{ role: 'user', content: `hello ${SECRET}` }],
      }),
    });

  test('folds a 500 -> 429 -> 200 chain into the final event', async () => {
    await post(app(true), { 'x-portkey-trace-id': 'trace-123' });
    await flush();
    const ev = sent.events[0];
    // Existing fields are unchanged.
    expect(ev).toMatchObject({
      vendor: 'openai',
      model: 'gpt-4o',
      source: 'gateway',
      tokens_in: 3,
      tokens_out: 4,
    });
    expect(ev.trace_id).toBe('trace-123');
    expect(ev.retry_count).toBe(2);
    expect(ev.attempts.map((a: any) => [a.attempt, a.status])).toEqual([
      [1, 500],
      [2, 429],
      [3, 200],
    ]);
    expect(ev.attempts[0]).toMatchObject({
      provider: 'openai',
      model: 'gpt-4o',
      error_class: 'server_error',
    });
    expect(JSON.stringify(sent)).not.toContain(SECRET);
  });

  test('generates a trace id when the caller sent none', async () => {
    await post(app(true));
    await flush();
    expect(typeof sent.events[0].trace_id).toBe('string');
    expect(sent.events[0].trace_id.length).toBeGreaterThan(0);
  });

  test('leaves the event unchanged when no attempt was recorded', async () => {
    await post(app(false));
    await flush();
    const ev = sent.events[0];
    expect(ev.attempts).toBeUndefined();
    expect(ev.retry_count).toBeUndefined();
    expect(ev.trace_id).toBeUndefined();
  });
});
