/**
 * Hono adapter for gateway telemetry: reads the response body + c.get('psRoute')
 * and emits a fire-and-forget event to the collector.
 */
process.env.PS_API_KEY = 'ps-test';

import { Hono } from 'hono';
import { psTelemetry } from './ps-telemetry';

function makeApp(handler: (c: any) => any, seedRoute?: any) {
  const app = new Hono();
  app.use('/v1/chat/completions', psTelemetry());
  if (seedRoute) {
    app.use('/v1/chat/completions', async (c, next) => {
      c.set('psRoute' as never, seedRoute as never);
      await next();
    });
  }
  app.post('/v1/chat/completions', handler);
  return app;
}

const post = (app: Hono, body: any, headers: Record<string, string> = {}) =>
  app.request('/v1/chat/completions', {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: JSON.stringify(body),
  });

describe('psTelemetry (Hono adapter)', () => {
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

  test('emits tokens, model and routing fields from a JSON response', async () => {
    const app = makeApp(
      (c) =>
        c.json({
          model: 'gpt-4o-mini',
          usage: { prompt_tokens: 11, completion_tokens: 22 },
        }),
      {
        requestedModel: 'auto',
        servedModel: 'gpt-4o-mini',
        group: 'cheap',
        reason: 'heuristic:simple',
        estCost: 0.0004,
      }
    );
    await post(app, {
      model: 'auto',
      messages: [{ role: 'user', content: 'hi' }],
    });
    await flush();

    const ev = sent.events[0];
    expect(ev.source).toBe('gateway');
    expect(ev.tokens_in).toBe(11);
    expect(ev.tokens_out).toBe(22);
    expect(ev.requested_model).toBe('auto');
    expect(ev.served_model).toBe('gpt-4o-mini');
    expect(ev.route_group).toBe('cheap');
  });

  test('carries X-PS-* business headers into the event', async () => {
    const app = makeApp((c) => c.json({ model: 'gpt-4o' }));
    await post(
      app,
      { model: 'gpt-4o', messages: [{ role: 'user', content: 'hi' }] },
      { 'X-PS-Business-Unit': 'Legal', 'X-PS-Environment': 'production' }
    );
    await flush();

    expect(sent.events[0].business_unit).toBe('Legal');
    expect(sent.events[0].environment).toBe('production');
  });

  test('still emits (without usage) for a streaming response', async () => {
    const app = makeApp((c) => {
      c.header('content-type', 'text/event-stream');
      return c.body('data: {}\n\n');
    });
    const res = await post(app, {
      model: 'gpt-4o',
      messages: [{ role: 'user', content: 'hi' }],
    });
    // The event is sent once the client has read the stream (#72).
    await res.text();
    await flush();

    expect(sent.events[0].source).toBe('gateway');
    expect(sent.events[0].tokens_in).toBeUndefined();
    expect(sent.events[0].stream_truncated).toBe(true);
  });

  test('never throws when the collector fetch rejects (fail-open)', async () => {
    global.fetch = jest.fn(async () => {
      throw new Error('network down');
    }) as any;
    const app = makeApp((c) => c.json({ model: 'gpt-4o' }));
    const res = await post(app, {
      model: 'gpt-4o',
      messages: [{ role: 'user', content: 'hi' }],
    });
    expect(res.status).toBe(200);
  });
  test('never includes provider credentials in the event', async () => {
    const app = makeApp((c) => c.json({ model: 'gpt-4o' }));
    await post(
      app,
      { model: 'gpt-4o', messages: [{ role: 'user', content: 'hi' }] },
      {
        authorization: 'Bearer sk-live-provider-secret',
        'x-portkey-api-key': 'pk-secret',
        'x-portkey-config': JSON.stringify({ api_key: 'sk-config-secret' }),
        'x-ps-business-unit': 'HR',
      }
    );
    await flush();

    const serialized = JSON.stringify(sent);
    expect(sent.events[0].business_unit).toBe('HR');
    expect(serialized).not.toContain('sk-live-provider-secret');
    expect(serialized).not.toContain('pk-secret');
    expect(serialized).not.toContain('sk-config-secret');
  });
  test('reports the vendor from x-portkey-provider or the config', async () => {
    const app = makeApp((c) => c.json({ model: 'rerank-v3.5' }));
    await post(
      app,
      { model: 'rerank-v3.5', query: 'q', documents: ['a'] },
      { 'x-portkey-provider': 'cohere' }
    );
    await flush();
    expect(sent.events[0].vendor).toBe('cohere');

    await post(
      app,
      { model: 'gpt-4o', messages: [] },
      {
        'x-portkey-config': JSON.stringify({
          strategy: { mode: 'fallback' },
          targets: [{ provider: 'azure-openai' }, { provider: 'openai' }],
        }),
      }
    );
    await flush();
    expect(sent.events[0].vendor).toBe('azure-openai');
  });
  test('Messages/Responses usage and the API surface are recorded', async () => {
    const app = new Hono();
    app.use('*', psTelemetry());
    app.post('/v1/messages', (c) =>
      c.json({
        type: 'message',
        model: 'mistral-small',
        usage: { input_tokens: 5, output_tokens: 2 },
      })
    );
    await app.request('/v1/messages', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-portkey-provider': 'mistral-ai',
      },
      body: JSON.stringify({ model: 'mistral-small', messages: [] }),
    });
    await flush();
    expect(sent.events[0]).toMatchObject({
      vendor: 'mistral-ai',
      api_surface: 'messages',
      tokens_in: 5,
      tokens_out: 2,
    });
  });
});
