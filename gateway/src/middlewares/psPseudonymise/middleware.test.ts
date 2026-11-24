/**
 * psPseudonymise (#350) in a Hono app: what reaches the layers below it
 * (telemetry, hooks, cache, provider) and what the client gets back.
 */
import { Hono } from 'hono';
import { PseudonymisationSettings } from './config';
import { CLOSE, OPEN } from './core';
import { PSEUDONYMISED_HEADER, psPseudonymise } from './index';

const EMAIL = 'kari@example.no';
const PHONE = '555-123-4567';
const CARD = '4111 1111 1111 1111';
const ORIGINALS = [EMAIL, PHONE, CARD];
const E1 = `${OPEN}EMAIL_1${CLOSE}`;
const P1 = `${OPEN}PHONE_1${CLOSE}`;

const ON: PseudonymisationSettings = { enabled: true, onUnsupported: 'refuse' };

interface Seen {
  json?: any;
  text?: string;
  raw?: string;
  responses: string[];
}

/**
 * An app with the middleware (unless `settings` is null), a spy standing
 * for every layer below it (it records the body and, after the handler,
 * the response those layers see), and `handler` standing for the provider.
 */
function makeApp(
  settings: PseudonymisationSettings | undefined | null,
  handler: (c: any, body: any) => Response | Promise<Response>
) {
  const seen: Seen = { responses: [] };
  const app = new Hono();
  if (settings !== null)
    app.use(
      '*',
      psPseudonymise(() => settings ?? undefined, {})
    );
  app.use('*', async (c, next) => {
    if (c.req.method === 'POST') {
      // The raw request, unless the middleware already read it (and so
      // left it as it was): then the body cache is all there is.
      seen.raw = c.req.raw.bodyUsed
        ? undefined
        : await c.req.raw.clone().text();
      seen.json = await c.req.json().catch(() => undefined);
      seen.text = await c.req.text().catch(() => undefined);
      seen.raw ??= seen.text;
    }
    await next();
    seen.responses.push(await c.res.clone().text());
  });
  app.all('*', async (c) =>
    handler(
      c,
      c.req.method === 'POST'
        ? await c.req.json().catch(() => undefined)
        : undefined
    )
  );
  return { app, seen };
}

const post = (
  app: Hono,
  path: string,
  body: unknown,
  type = 'application/json'
) =>
  app.request(path, {
    method: 'POST',
    headers: { 'content-type': type },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });

const noOriginals = (s: string | undefined) => {
  for (const v of ORIGINALS) expect(s ?? '').not.toContain(v);
};

const chatBody = {
  model: 'gpt-4o',
  messages: [
    { role: 'system', content: `Account ${EMAIL}` },
    { role: 'user', content: `Call ${PHONE}, card ${CARD}, mail ${EMAIL}` },
  ],
};

const sse = (events: string[], split = 0) => {
  const bytes = new TextEncoder().encode(events.join(''));
  return new ReadableStream<Uint8Array>({
    start(controller) {
      if (!split) controller.enqueue(bytes);
      else
        for (let i = 0; i < bytes.length; i += split)
          controller.enqueue(bytes.slice(i, i + split));
      controller.close();
    },
  });
};
const sseResponse = (events: string[], split = 0) =>
  new Response(sse(events, split), {
    headers: { 'content-type': 'text/event-stream' },
  });

describe('psPseudonymise: off', () => {
  test.each([undefined, { enabled: false, onUnsupported: 'refuse' } as const])(
    'requests and responses are byte-identical (%p)',
    async (settings) => {
      const answer = `{"choices":[{"message":{"content":"${E1} ok"}}]}`;
      const run = async (s: any) => {
        const { app, seen } = makeApp(
          s,
          () =>
            new Response(answer, {
              headers: { 'content-type': 'application/json' },
            })
        );
        const res = await post(app, '/v1/chat/completions', chatBody);
        return { seen, text: await res.text(), headers: [...res.headers] };
      };
      const without = await run(null);
      const withMw = await run(settings);
      expect(withMw).toEqual(without);
      expect(withMw.seen.raw).toBe(JSON.stringify(chatBody));
    }
  );

  test('unsupported endpoints pass', async () => {
    const { app } = makeApp(undefined, () => new Response('ok'));
    expect((await post(app, '/v1/embeddings', { input: EMAIL })).status).toBe(
      200
    );
  });
});

describe('psPseudonymise: on', () => {
  test('chat JSON: no original value below, answer restored', async () => {
    const { app, seen } = makeApp(ON, (c, body) =>
      c.json({
        choices: [
          {
            message: {
              content: `I will call ${P1} and mail ${E1}`,
              tool_calls: [
                { function: { name: 'f', arguments: `{"to": "${E1}"}` } },
              ],
            },
          },
        ],
        echo: body.messages[1].content,
      })
    );
    const res = await post(app, '/v1/chat/completions', chatBody);
    for (const s of [
      JSON.stringify(seen.json),
      seen.text,
      seen.raw,
      ...seen.responses,
    ])
      noOriginals(s);
    expect(seen.json.messages[0].content).toBe(`Account ${E1}`);
    expect(seen.json.model).toBe('gpt-4o');
    const out: any = await res.json();
    expect(out.choices[0].message.content).toBe(
      `I will call ${PHONE} and mail ${EMAIL}`
    );
    expect(out.choices[0].message.tool_calls[0].function.arguments).toBe(
      `{"to": "${EMAIL}"}`
    );
    // Only fields that carry the answer are restored.
    expect(out.echo).toContain(P1);
    expect(res.headers.get(PSEUDONYMISED_HEADER)).toBe('3');
    expect(res.headers.get('content-length')).toBeNull();
  });

  test('nothing detected: passed through untouched', async () => {
    const body = { model: 'm', messages: [{ role: 'user', content: 'hello' }] };
    const { app, seen } = makeApp(ON, (c) => c.json({ ok: 1 }));
    const res = await post(app, '/v1/chat/completions', body);
    expect(seen.raw).toBe(JSON.stringify(body));
    expect(res.headers.get(PSEUDONYMISED_HEADER)).toBeNull();
  });

  test('error responses are left as they are (placeholders, never values)', async () => {
    const { app } = makeApp(ON, (c) =>
      c.json({ error: { message: `bad ${E1}` } }, 400)
    );
    const res = await post(app, '/v1/chat/completions', chatBody);
    expect(((await res.json()) as any).error.message).toBe(`bad ${E1}`);
  });

  const chatStream = [
    `data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"Mail "}}]}\n\n`,
    `data: {"choices":[{"index":0,"delta":{"content":"${OPEN}EMA"}}]}\n\n`,
    `data: {"choices":[{"index":0,"delta":{"content":"IL_1${CLOSE}, call ${OPEN}PHONE_1"}}]}\n\n`,
    `data: {"choices":[{"index":0,"delta":{"content":"${CLOSE}."},"finish_reason":"stop"}]}\n\n`,
    `data: [DONE]\n\n`,
  ];

  test.each([0, 1, 3, 7, 64])(
    'chat stream split every %i bytes is restored',
    async (split) => {
      const { app, seen } = makeApp(ON, () => sseResponse(chatStream, split));
      const res = await post(app, '/v1/chat/completions', chatBody);
      expect(res.headers.get(PSEUDONYMISED_HEADER)).toBe('3');
      const text = await res.text();
      const content = text
        .split('\n\n')
        .filter((b) => b.startsWith('data: {'))
        .map((b) => JSON.parse(b.slice(6)).choices[0].delta.content ?? '')
        .join('');
      expect(content).toBe(`Mail ${EMAIL}, call ${PHONE}.`);
      expect(text.trimEnd().endsWith('data: [DONE]')).toBe(true);
      // Events with nothing to restore are forwarded byte for byte.
      expect(text.startsWith(chatStream[0])).toBe(true);
      for (const s of seen.responses) noOriginals(s);
    }
  );

  test('Anthropic Messages stream with tool input JSON', async () => {
    const events = [
      ['message_start', { type: 'message_start', message: { content: [] } }],
      [
        'content_block_start',
        {
          type: 'content_block_start',
          index: 0,
          content_block: { type: 'text', text: '' },
        },
      ],
      [
        'content_block_delta',
        {
          type: 'content_block_delta',
          index: 0,
          delta: { type: 'text_delta', text: `Hi ${OPEN}EMAIL` },
        },
      ],
      [
        'content_block_delta',
        {
          type: 'content_block_delta',
          index: 0,
          delta: { type: 'text_delta', text: `_1${CLOSE}` },
        },
      ],
      ['content_block_stop', { type: 'content_block_stop', index: 0 }],
      [
        'content_block_delta',
        {
          type: 'content_block_delta',
          index: 1,
          delta: {
            type: 'input_json_delta',
            partial_json: '{"to": "\\u27e6PHO',
          },
        },
      ],
      [
        'content_block_delta',
        {
          type: 'content_block_delta',
          index: 1,
          delta: { type: 'input_json_delta', partial_json: 'NE_1\\u27e7"}' },
        },
      ],
      ['content_block_stop', { type: 'content_block_stop', index: 1 }],
      ['message_stop', { type: 'message_stop' }],
    ].map(
      ([name, data]) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`
    );
    const { app } = makeApp(ON, () => sseResponse(events, 5));
    const res = await post(app, '/v1/messages', {
      model: 'claude',
      system: `owner ${EMAIL}`,
      messages: [{ role: 'user', content: `call ${PHONE}` }],
    });
    const blocks = (await res.text()).split('\n\n').filter(Boolean);
    const datas = blocks.map((b) =>
      JSON.parse(
        b
          .split('\n')
          .find((l) => l.startsWith('data: '))!
          .slice(6)
      )
    );
    const text = datas
      .filter((d) => d.index === 0 && d.delta)
      .map((d) => d.delta.text)
      .join('');
    const json = datas
      .filter((d) => d.index === 1 && d.delta)
      .map((d) => d.delta.partial_json)
      .join('');
    expect(text).toBe(`Hi ${EMAIL}`);
    expect(JSON.parse(json)).toEqual({ to: PHONE });
    for (const b of blocks) expect(b.startsWith('event: ')).toBe(true);
  });

  test('Responses stream: deltas, done events and the completed response', async () => {
    const ev = (data: any) =>
      `event: ${data.type}\ndata: ${JSON.stringify(data)}\n\n`;
    const events = [
      ev({
        type: 'response.output_text.delta',
        output_index: 0,
        content_index: 0,
        delta: `to ${OPEN}EM`,
      }),
      ev({
        type: 'response.output_text.delta',
        output_index: 0,
        content_index: 0,
        delta: `AIL_1${CLOSE} ${OPEN}PHONE_`,
      }),
      ev({
        type: 'response.output_text.done',
        output_index: 0,
        content_index: 0,
        text: `to ${E1} ${OPEN}PHONE_`,
      }),
      ev({
        type: 'response.completed',
        response: {
          output: [
            {
              type: 'message',
              content: [{ type: 'output_text', text: `to ${E1}` }],
            },
          ],
        },
      }),
    ];
    const { app, seen } = makeApp(ON, () => sseResponse(events, 11));
    const res = await post(app, '/v1/responses', {
      model: 'm',
      input: `mail ${EMAIL} or ${PHONE}`,
    });
    noOriginals(JSON.stringify(seen.json));
    const datas = (await res.text())
      .split('\n\n')
      .filter(Boolean)
      .map((b) => JSON.parse(b.split('\n')[1].slice(6)));
    const deltas = datas
      .filter((d) => d.type.endsWith('.delta'))
      .map((d) => d.delta)
      .join('');
    expect(deltas).toBe(`to ${EMAIL} ${OPEN}PHONE_`);
    expect(datas.find((d) => d.type === 'response.output_text.done').text).toBe(
      `to ${EMAIL} ${OPEN}PHONE_`
    );
    expect(
      datas.find((d) => d.type === 'response.completed').response.output[0]
        .content[0].text
    ).toBe(`to ${EMAIL}`);
  });

  test.each([
    ['/v1/embeddings', { input: EMAIL }],
    ['/v1/completions', { prompt: EMAIL }],
    ['/v1/some/proxied/path', { x: EMAIL }],
    ['/v1/responses', { input: EMAIL, previous_response_id: 'resp_1' }],
    ['/v1/responses', { input: EMAIL, conversation: 'conv_1' }],
  ])(
    'refuses %s before any upstream call (on_unsupported: refuse)',
    async (path, body) => {
      const handler = jest.fn(() => new Response('ok'));
      const { app } = makeApp(ON, handler);
      const res = await post(app, path, body);
      expect(res.status).toBe(400);
      expect(((await res.json()) as any).error.code).toBe(
        'pseudonymisation_unsupported'
      );
      expect(handler).not.toHaveBeenCalled();
    }
  );

  test('refuses a non-JSON body and a realtime upgrade', async () => {
    const { app } = makeApp(ON, () => new Response('ok'));
    expect(
      (await post(app, '/v1/chat/completions', EMAIL, 'text/plain')).status
    ).toBe(400);
    expect((await app.request('/v1/realtime?model=x')).status).toBe(400);
  });

  test('lets through calls that carry no prompt', async () => {
    const { app } = makeApp(ON, () => new Response('ok'));
    expect((await app.request('/v1/models')).status).toBe(200);
    expect(
      (await app.request('/v1/responses/r1', { method: 'DELETE' })).status
    ).toBe(200);
    expect((await post(app, '/v1/responses/r1/cancel', {})).status).toBe(200);
    expect((await post(app, '/v1/feedback', { call_id: 'x' })).status).toBe(
      200
    );
    expect((await post(app, '/v1/mcp/srv', { jsonrpc: '2.0' })).status).toBe(
      200
    );
  });

  test('passthrough mode forwards unsupported calls unchanged', async () => {
    const { app, seen } = makeApp(
      { enabled: true, onUnsupported: 'passthrough' },
      () => new Response('ok')
    );
    expect((await post(app, '/v1/embeddings', { input: EMAIL })).status).toBe(
      200
    );
    expect(seen.raw).toContain(EMAIL);
  });

  test('categories limit what is replaced', async () => {
    const { app, seen } = makeApp({ ...ON, categories: ['phone'] }, (c) =>
      c.json({})
    );
    await post(app, '/v1/chat/completions', chatBody);
    expect(seen.raw).toContain(EMAIL);
    expect(seen.raw).not.toContain(PHONE);
  });
});
