import { Hono } from 'hono';
import {
  formatTraceparent,
  newTraceContext,
  parseTraceparentHeader,
  propagatesUpstream,
  psTraceContext,
  traceContextOf,
  upstreamTraceHeaders,
} from './psTraceContext';
import { otelConfig, psOtel } from './psOtel';
import { constructRequest } from '../handlers/handlerUtils';
import { RequestContext } from '../handlers/services/requestContext';

const INBOUND = '00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01';
const TRACEPARENT = /^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$/;

describe('parseTraceparentHeader', () => {
  test('reads a valid header', () => {
    expect(parseTraceparentHeader(INBOUND)).toEqual({
      traceId: '0af7651916cd43dd8448eb211c80319c',
      parentSpanId: 'b7ad6b7169203331',
      flags: '01',
    });
  });

  test.each([
    undefined,
    '',
    'garbage',
    '00-00000000000000000000000000000000-b7ad6b7169203331-01',
    '00-0af7651916cd43dd8448eb211c80319c-0000000000000000-01',
    'ff-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01',
    '00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01-extra',
    '00-0AF7651916CD43DD8448EB211C80319C-b7ad6b7169203331-01',
  ])('rejects %p', (header) => {
    expect(parseTraceparentHeader(header)).toBeUndefined();
  });

  test('accepts trailing fields from a future version', () => {
    expect(
      parseTraceparentHeader(
        '01-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01-future'
      )?.traceId
    ).toBe('0af7651916cd43dd8448eb211c80319c');
  });
});

describe('newTraceContext', () => {
  test('joins the inbound trace with a new gateway span', () => {
    const ctx = newTraceContext(INBOUND, 'vendor=abc');
    expect(ctx.traceId).toBe('0af7651916cd43dd8448eb211c80319c');
    expect(ctx.parentSpanId).toBe('b7ad6b7169203331');
    expect(ctx.spanId).toMatch(/^[0-9a-f]{16}$/);
    expect(ctx.spanId).not.toBe('b7ad6b7169203331');
    expect(ctx.tracestate).toBe('vendor=abc');
    expect(formatTraceparent(ctx)).toBe(
      `00-0af7651916cd43dd8448eb211c80319c-${ctx.spanId}-01`
    );
  });

  test('starts a sampled trace without one', () => {
    const ctx = newTraceContext(undefined, 'vendor=abc');
    expect(ctx.parentSpanId).toBeUndefined();
    expect(ctx.tracestate).toBeUndefined();
    expect(formatTraceparent(ctx)).toMatch(TRACEPARENT);
    expect(ctx.flags).toBe('01');
  });

  test('keeps the caller sampling flag and drops a bad tracestate', () => {
    const ctx = newTraceContext(
      '00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00',
      'x'.repeat(600)
    );
    expect(ctx.flags).toBe('00');
    expect(ctx.tracestate).toBeUndefined();
  });
});

describe('psTraceContext middleware', () => {
  const makeApp = (handler: (c: any) => any) => {
    const app = new Hono();
    app.use('*', psTraceContext());
    app.post('/v1/chat/completions', handler);
    return app;
  };

  test('returns traceparent on every response, joined to the caller', async () => {
    const app = makeApp((c) => c.json({ ok: true }));
    const res = await app.request('/v1/chat/completions', {
      method: 'POST',
      headers: { traceparent: INBOUND },
    });
    const m = res.headers.get('traceparent')!.match(TRACEPARENT)!;
    expect(m[1]).toBe('0af7651916cd43dd8448eb211c80319c');
    expect(m[2]).not.toBe('b7ad6b7169203331');
    const notFound = await app.request('/nope');
    expect(notFound.status).toBe(404);
    expect(notFound.headers.get('traceparent')).toMatch(TRACEPARENT);
  });

  test('sets the header on a fetched (immutable) response', async () => {
    const app = makeApp(() => {
      const res = new Response('{}', {
        headers: { 'content-type': 'application/json' },
      });
      Object.freeze(res.headers);
      return res;
    });
    const res = await app.request('/v1/chat/completions', { method: 'POST' });
    expect(res.headers.get('traceparent')).toMatch(TRACEPARENT);
  });

  test('the gateway span has the returned span id', async () => {
    const sent: any[] = [];
    const exporter = { enqueue: (s: any) => sent.push(s) } as any;
    const config = otelConfig({ PS_OTEL_ENDPOINT: 'https://c.example/x' })!;
    const app = new Hono();
    app.use('*', psTraceContext());
    app.use('*', psOtel(config, exporter));
    app.post('/v1/chat/completions', (c) => c.json({ model: 'gpt-4o' }));
    const res = await app.request('/v1/chat/completions', {
      method: 'POST',
      headers: {
        traceparent: INBOUND,
        'content-type': 'application/json',
        'x-portkey-provider': 'openai',
      },
      body: JSON.stringify({ model: 'gpt-4o', messages: [] }),
    });
    const [, traceId, spanId] = res.headers
      .get('traceparent')!
      .match(TRACEPARENT)!;
    expect(sent).toHaveLength(1);
    expect(sent[0].span.traceId).toBe(traceId);
    expect(sent[0].span.spanId).toBe(spanId);
    expect(sent[0].span.parentSpanId).toBe('b7ad6b7169203331');
  });
});

describe('upstream propagation', () => {
  const ON = { PS_OTEL_PROPAGATE_UPSTREAM: 'true' };

  test('is off by default', () => {
    expect(propagatesUpstream({})).toBe(false);
    expect(propagatesUpstream(ON)).toBe(true);
    expect(upstreamTraceHeaders(undefined, ON)).toEqual({});
  });

  const headersFor = async (
    env: Record<string, string>,
    clientHeaders: Record<string, string>,
    forwardHeaders: string[] = []
  ) => {
    let out: Record<string, string> = {};
    let ctxSpan = '';
    const app = new Hono();
    app.post('/v1/chat/completions', async (c) => {
      const saved = { ...process.env };
      Object.assign(process.env, env);
      try {
        ctxSpan = traceContextOf(c).spanId;
        const rc = new RequestContext(
          c,
          { provider: 'openai', forwardHeaders } as any,
          'chatComplete',
          clientHeaders,
          { model: 'gpt-4o', messages: [] } as any,
          'POST',
          0
        );
        const providerContext = {
          getHeaders: async () => ({ authorization: 'Bearer sk-test' }),
        } as any;
        const init = await constructRequest(providerContext, rc);
        out = init.headers as Record<string, string>;
      } finally {
        process.env = saved;
      }
      return c.json({});
    });
    await app.request('/v1/chat/completions', {
      method: 'POST',
      headers: clientHeaders,
    });
    return { out, ctxSpan };
  };

  test('adds nothing when off', async () => {
    const { out } = await headersFor(
      {},
      { traceparent: INBOUND, tracestate: 'v=1' }
    );
    expect(out.traceparent).toBeUndefined();
    expect(out.tracestate).toBeUndefined();
    expect(out.authorization).toBe('Bearer sk-test');
  });

  test('an explicitly forwarded header still passes when off', async () => {
    const { out } = await headersFor({}, { traceparent: INBOUND }, [
      'traceparent',
    ]);
    expect(out.traceparent).toBe(INBOUND);
  });

  test('sends the gateway span as parent when on', async () => {
    const { out, ctxSpan } = await headersFor(ON, {
      traceparent: INBOUND,
      tracestate: 'v=1',
    });
    expect(out.traceparent).toBe(
      `00-0af7651916cd43dd8448eb211c80319c-${ctxSpan}-01`
    );
    expect(out.tracestate).toBe('v=1');
    expect(out.authorization).toBe('Bearer sk-test');
  });
});
