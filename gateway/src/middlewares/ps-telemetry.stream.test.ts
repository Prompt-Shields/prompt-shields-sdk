/**
 * Streamed (SSE) responses in gateway telemetry (#72): usage is read from the
 * stream as it passes through, and a stream that ends without its terminal
 * event is marked truncated. Fault injection per terminal-event style.
 */
process.env.PS_API_KEY = 'ps-test';

import { Hono } from 'hono';
import { psTelemetry } from './ps-telemetry';
import { SSEUsageTracker } from './psStreamObserver';

const enc = new TextEncoder();

/** A stream that emits `chunks`, then closes (or errors if `fail`). */
function sseBody(chunks: string[], fail = false) {
  let i = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < chunks.length) controller.enqueue(enc.encode(chunks[i++]));
      else if (fail) controller.error(new Error('upstream reset'));
      else controller.close();
    },
  });
}

const OPENAI_COMPLETE = [
  'data: {"id":"1","model":"gpt-4o-2024","choices":[{"delta":{"content":"Hi"},"finish_reason":null}]}\n\n',
  'data: {"id":"1","model":"gpt-4o-2024","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
  'data: {"id":"1","model":"gpt-4o-2024","choices":[],"usage":{"prompt_tokens":12,"completion_tokens":3}}\n\n',
  'data: [DONE]\n\n',
];
const OPENAI_TRUNCATED = OPENAI_COMPLETE.slice(0, 1);

const ANTHROPIC_COMPLETE = [
  'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-x","usage":{"input_tokens":25,"output_tokens":1}}}\n\n',
  'event: ping\ndata: {"type":"ping"}\n\n',
  'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}\n\n',
  'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":15}}\n\n',
  'event: message_stop\ndata: {"type":"message_stop"}\n\n',
];
const ANTHROPIC_TRUNCATED = ANTHROPIC_COMPLETE.slice(0, 3);

const RESPONSES_COMPLETE = [
  'event: response.created\ndata: {"type":"response.created","response":{"model":"gpt-4.1","usage":null}}\n\n',
  'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"Hi"}\n\n',
  'event: response.completed\ndata: {"type":"response.completed","response":{"model":"gpt-4.1","usage":{"input_tokens":30,"output_tokens":4}}}\n\n',
];
const RESPONSES_TRUNCATED = RESPONSES_COMPLETE.slice(0, 2);

describe('SSEUsageTracker', () => {
  const run = (chunks: string[]) => {
    const t = new SSEUsageTracker();
    chunks.forEach((ch) => t.push(enc.encode(ch)));
    return t.end();
  };

  test.each([
    ['OpenAI chat [DONE]', OPENAI_COMPLETE, 12, 3, 'gpt-4o-2024'],
    ['Anthropic message_stop', ANTHROPIC_COMPLETE, 25, 15, 'claude-x'],
    ['Responses response.completed', RESPONSES_COMPLETE, 30, 4, 'gpt-4.1'],
  ])('%s: usage, model and terminal event', (_n, chunks, tin, tout, model) => {
    expect(run(chunks)).toMatchObject({
      tokens_in: tin,
      tokens_out: tout,
      model,
      terminal: true,
    });
  });

  test.each([
    ['OpenAI chat', OPENAI_TRUNCATED],
    ['Anthropic', ANTHROPIC_TRUNCATED],
    ['Responses', RESPONSES_TRUNCATED],
  ])('%s: no terminal event when cut off', (_n, chunks) => {
    expect(run(chunks).terminal).toBe(false);
  });

  test('parses events split across chunk boundaries (even mid-character)', () => {
    const whole = enc.encode(ANTHROPIC_COMPLETE.join('').replace('Hi', 'Hé'));
    const t = new SSEUsageTracker();
    for (let i = 0; i < whole.length; i += 7) t.push(whole.slice(i, i + 7));
    expect(t.end()).toMatchObject({
      tokens_in: 25,
      tokens_out: 15,
      terminal: true,
    });
  });

  test('a finish_reason counts as terminal when [DONE] never comes', () => {
    expect(run(OPENAI_COMPLETE.slice(0, 2)).terminal).toBe(true);
  });

  test('ignores malformed data lines', () => {
    const s = run(['data: {not json\n\n', 'data: [DONE]\n\n']);
    expect(s.terminal).toBe(true);
    expect(s.tokens_in).toBeUndefined();
  });
});

describe('psTelemetry (Hono adapter) with streamed responses', () => {
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

  function makeApp(
    path: string,
    body: () => ReadableStream<Uint8Array>,
    route?: any
  ) {
    const app = new Hono();
    app.use('*', psTelemetry());
    if (route) {
      app.use('*', async (c, next) => {
        c.set('psRoute' as never, route as never);
        await next();
      });
    }
    app.post(
      path,
      () =>
        new Response(body(), {
          headers: { 'content-type': 'text/event-stream' },
        })
    );
    return app;
  }

  const post = (app: Hono, path: string) =>
    app.request(path, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-portkey-provider': 'openai',
      },
      body: JSON.stringify({ model: 'requested-model', stream: true }),
    });

  test.each([
    ['/v1/chat/completions', OPENAI_COMPLETE, 12, 3],
    ['/v1/messages', ANTHROPIC_COMPLETE, 25, 15],
    ['/v1/responses', RESPONSES_COMPLETE, 30, 4],
  ])(
    '%s: forwards the stream unchanged and emits usage after it ends',
    async (path, chunks, tin, tout) => {
      const app = makeApp(path, () => sseBody(chunks));
      const res = await post(app, path);
      await flush();
      expect(sent).toHaveLength(0); // nothing until the client has read it

      expect(await res.text()).toBe(chunks.join(''));
      await flush();
      expect(sent).toHaveLength(1);
      expect(sent[0]).toMatchObject({
        source: 'gateway',
        model: 'requested-model',
        tokens_in: tin,
        tokens_out: tout,
        stream: true,
      });
      expect(sent[0].stream_truncated).toBeUndefined();
    }
  );

  test.each([
    ['/v1/chat/completions', OPENAI_TRUNCATED],
    ['/v1/messages', ANTHROPIC_TRUNCATED],
    ['/v1/responses', RESPONSES_TRUNCATED],
  ])(
    '%s: upstream closes without a terminal event -> stream_truncated',
    async (path, chunks) => {
      const app = makeApp(path, () => sseBody(chunks));
      const res = await post(app, path);
      expect(await res.text()).toBe(chunks.join(''));
      await flush();
      expect(sent[0]).toMatchObject({ stream: true, stream_truncated: true });
    }
  );

  test('an upstream error mid-stream is marked truncated and still reaches the client', async () => {
    const app = makeApp('/v1/messages', () =>
      sseBody(ANTHROPIC_TRUNCATED, true)
    );
    const res = await post(app, '/v1/messages');
    await expect(res.text()).rejects.toThrow();
    await flush();
    expect(sent[0]).toMatchObject({
      stream_truncated: true,
      tokens_in: 25,
    });
  });

  test('chunks reach the client before the upstream finishes', async () => {
    let push!: (s: string | null) => void;
    const body = () =>
      new ReadableStream<Uint8Array>({
        start(controller) {
          push = (s) =>
            s === null ? controller.close() : controller.enqueue(enc.encode(s));
        },
      });
    const app = makeApp('/v1/chat/completions', body);
    const res = await post(app, '/v1/chat/completions');
    const reader = res.body!.getReader();

    push(OPENAI_COMPLETE[0]);
    const first = await reader.read();
    expect(new TextDecoder().decode(first.value)).toBe(OPENAI_COMPLETE[0]);
    expect(sent).toHaveLength(0);

    push(OPENAI_COMPLETE[3]);
    push(null);
    while (!(await reader.read()).done);
    await flush();
    expect(sent[0]).toMatchObject({ stream: true });
    expect(sent[0].stream_truncated).toBeUndefined();
  });

  test('client cancel emits once, marked cancelled rather than truncated', async () => {
    const app = makeApp('/v1/chat/completions', () =>
      sseBody([...OPENAI_TRUNCATED, ...OPENAI_TRUNCATED, ...OPENAI_TRUNCATED])
    );
    const res = await post(app, '/v1/chat/completions');
    const reader = res.body!.getReader();
    await reader.read();
    await reader.cancel();
    await flush();
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ stream_cancelled: true });
    expect(sent[0].stream_truncated).toBeUndefined();
  });

  test('a routed stream reports the model the provider served', async () => {
    const app = makeApp(
      '/v1/chat/completions',
      () => sseBody(OPENAI_COMPLETE),
      {
        requestedModel: 'auto',
        servedModel: 'gpt-4o',
        group: 'cheap',
        reason: 'heuristic:simple',
        estCost: 0.001,
      }
    );
    const res = await post(app, '/v1/chat/completions');
    await res.text();
    await flush();
    expect(sent[0]).toMatchObject({
      requested_model: 'auto',
      served_model: 'gpt-4o-2024',
    });
  });
});
