/**
 * Truncated-stream detection and streaming fixes (#72), through the whole
 * gateway app and offline, with fault injection: for each terminal-event
 * style (`[DONE]`, a bare `finish_reason`, Anthropic `message_stop`,
 * Responses `response.completed`, Bedrock `messageStop`) a complete stream is
 * not marked truncated, and a stream cut off before its terminal event, or
 * failing mid-stream, is. Also: only complete, successful responses are
 * written to the response cache; Gemini/Vertex tool calls finish with
 * `tool_calls`; Anthropic keepalive pings reach native /v1/messages clients.
 */
jest.mock('jose', () => ({}));

import {
  awsEventStream,
  events,
  json,
  post,
  resetHarness,
  sse,
  telemetryEvent,
  until,
  upstream,
  upstreamCalls,
} from './harness';
import app from '../../index';

const enc = new TextEncoder();

/** SSE that sends `text`, then fails (connection reset mid-stream). */
const failingSSE = (text: string) => {
  let sent = false;
  return new Response(
    new ReadableStream<Uint8Array>({
      pull(controller) {
        if (sent) return controller.error(new Error('ECONNRESET'));
        sent = true;
        controller.enqueue(enc.encode(text));
      },
    }),
    { status: 200, headers: { 'content-type': 'text/event-stream' } }
  );
};

const chat = (model: string, extra: object = {}) => ({
  model,
  messages: [{ role: 'user', content: 'Say hi' }],
  ...extra,
});

const OPENAI = {
  'x-portkey-provider': 'openai',
  authorization: 'Bearer sk-regression',
};

const openaiChunk = (delta: object, finish: string | null = null) =>
  `data: ${JSON.stringify({
    id: 'c1',
    object: 'chat.completion.chunk',
    created: 1,
    model: 'gpt-4o-mini',
    choices: [{ index: 0, delta, finish_reason: finish }],
  })}\n\n`;

const expectComplete = (ev: any) => {
  expect(ev.stream).toBe(true);
  expect(ev.stream_truncated).toBeUndefined();
  expect(ev.stream_cancelled).toBeUndefined();
};

const expectTruncated = (ev: any) => {
  expect(ev).toMatchObject({ stream: true, stream_truncated: true });
};

const dataLines = (text: string) =>
  text
    .split('\n')
    .filter((l) => l.startsWith('data: ') && l !== 'data: [DONE]')
    .map((l) => JSON.parse(l.slice(6)));

beforeEach(() => resetHarness());

describe('terminal-event styles: complete vs truncated (fault injection)', () => {
  describe('[DONE] (OpenAI chat)', () => {
    const send = () =>
      post(app, '/v1/chat/completions', chat('gpt-4o-mini', { stream: true }), {
        ...OPENAI,
      });

    test('complete', async () => {
      upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
        sse(
          openaiChunk({ content: 'Hi' }) +
            openaiChunk({}, 'stop') +
            'data: [DONE]\n\n'
        )
      );
      await (await send()).text();
      expectComplete(await telemetryEvent());
    });

    test('cut off before finish_reason and [DONE]', async () => {
      upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
        sse(openaiChunk({ content: 'Hi' }))
      );
      const res = await send();
      expect(await res.text()).toContain('Hi');
      expectTruncated(await telemetryEvent());
    });

    test('upstream error mid-stream', async () => {
      upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
        failingSSE(openaiChunk({ content: 'Hi' }))
      );
      const res = await send();
      expect(await res.text()).toContain('Hi');
      expectTruncated(await telemetryEvent());
    });
  });

  describe('finish_reason only, no [DONE] (Gemini via Google AI Studio)', () => {
    const MODEL = 'gemini-2.0-flash';
    const headers = {
      'x-portkey-config': JSON.stringify({
        provider: 'google',
        api_key: 'g-regression',
      }),
    };
    const part = (text: string, finishReason?: string) =>
      `data: ${JSON.stringify({
        candidates: [
          {
            content: { role: 'model', parts: [{ text }] },
            ...(finishReason && { finishReason }),
            index: 0,
          },
        ],
        modelVersion: MODEL,
      })}\r\n\r\n`;
    const send = () =>
      post(app, '/v1/chat/completions', chat(MODEL, { stream: true }), headers);

    test('complete', async () => {
      upstream(/:streamGenerateContent/, () =>
        sse(part('Hi') + part('', 'STOP'))
      );
      await (await send()).text();
      expectComplete(await telemetryEvent());
    });

    test('cut off before the finishReason chunk', async () => {
      upstream(/:streamGenerateContent/, () => sse(part('Hi')));
      expect(await (await send()).text()).toContain('Hi');
      expectTruncated(await telemetryEvent());
    });
  });

  describe('message_stop (native /v1/messages, Anthropic)', () => {
    const headers = {
      'x-portkey-provider': 'anthropic',
      'x-api-key': 'sk-ant-regression',
    };
    const ev = (type: string, data: object = {}) =>
      `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`;
    const head =
      ev('message_start', {
        message: {
          id: 'msg_1',
          type: 'message',
          role: 'assistant',
          model: 'claude-sonnet-4-20250514',
          content: [],
          usage: { input_tokens: 8, output_tokens: 1 },
        },
      }) +
      ev('ping') +
      ev('content_block_start', {
        index: 0,
        content_block: { type: 'text', text: '' },
      }) +
      ev('content_block_delta', {
        index: 0,
        delta: { type: 'text_delta', text: 'Hi' },
      });
    const tail =
      ev('content_block_stop', { index: 0 }) +
      ev('message_delta', {
        delta: { stop_reason: 'end_turn' },
        usage: { output_tokens: 2 },
      }) +
      ev('message_stop');
    const send = () =>
      post(
        app,
        '/v1/messages',
        {
          model: 'claude-sonnet-4-20250514',
          max_tokens: 16,
          stream: true,
          messages: [{ role: 'user', content: 'Say hi' }],
        },
        headers
      );

    test('complete, with keepalive pings passed through', async () => {
      upstream(/api\.anthropic\.com\/v1\/messages$/, () =>
        sse(head + ev('ping') + tail)
      );
      const text = await (await send()).text();
      // Anthropic sends `ping` during long prefills; clients that use it as
      // a keepalive must still receive it (Portkey enterprise 2.20).
      expect(text.match(/^event: ping$/gm)).toHaveLength(2);
      expect(text).toContain('event: message_stop');
      const e = await telemetryEvent();
      expectComplete(e);
      expect(e).toMatchObject({ tokens_in: 8, tokens_out: 2 });
    });

    test('cut off before message_stop', async () => {
      upstream(/api\.anthropic\.com\/v1\/messages$/, () => sse(head));
      expect(await (await send()).text()).toContain('text_delta');
      expectTruncated(await telemetryEvent());
    });

    test('upstream error mid-stream', async () => {
      upstream(/api\.anthropic\.com\/v1\/messages$/, () => failingSSE(head));
      await (await send()).text();
      expectTruncated(await telemetryEvent());
    });
  });

  describe('response.completed (native /v1/responses, OpenAI)', () => {
    const ev = (data: { type: string; [k: string]: unknown }) =>
      `event: ${data.type}\ndata: ${JSON.stringify(data)}\n\n`;
    const head =
      ev({
        type: 'response.created',
        response: { id: 'resp_1', model: 'gpt-4o', status: 'in_progress' },
      }) +
      ev({ type: 'response.output_text.delta', item_id: 'm1', delta: 'Hi' });
    const send = () =>
      post(
        app,
        '/v1/responses',
        { model: 'gpt-4o', input: 'Say hi', stream: true },
        OPENAI
      );

    test('complete', async () => {
      upstream(/api\.openai\.com\/v1\/responses$/, () =>
        sse(
          head +
            ev({
              type: 'response.completed',
              response: {
                id: 'resp_1',
                model: 'gpt-4o',
                status: 'completed',
                usage: { input_tokens: 5, output_tokens: 1, total_tokens: 6 },
              },
            })
        )
      );
      await (await send()).text();
      const e = await telemetryEvent();
      expectComplete(e);
      expect(e).toMatchObject({ tokens_in: 5, tokens_out: 1 });
    });

    test('cut off before response.completed', async () => {
      upstream(/api\.openai\.com\/v1\/responses$/, () => sse(head));
      expect(await (await send()).text()).toContain('Hi');
      expectTruncated(await telemetryEvent());
    });
  });

  describe('messageStop (Bedrock Converse event stream)', () => {
    const MODEL = 'anthropic.claude-3-5-haiku-20241022-v1:0';
    const headers = {
      'x-portkey-config': JSON.stringify({
        provider: 'bedrock',
        aws_access_key_id: 'AKIAREGRESSIONTEST',
        aws_secret_access_key: 'regression-secret',
        aws_region: 'us-east-1',
      }),
    };
    const head = [
      { role: 'assistant' },
      { contentBlockIndex: 0, delta: { text: 'Hi' } },
    ];
    const send = () =>
      post(app, '/v1/chat/completions', chat(MODEL, { stream: true }), headers);

    test('complete', async () => {
      upstream(/\/converse-stream$/, () =>
        awsEventStream([
          ...head,
          { contentBlockIndex: 0 },
          { stopReason: 'end_turn' },
          { usage: { inputTokens: 7, outputTokens: 2, totalTokens: 9 } },
        ])
      );
      await (await send()).text();
      expectComplete(await telemetryEvent());
    });

    test('cut off before messageStop', async () => {
      upstream(/\/converse-stream$/, () => awsEventStream(head));
      expect(await (await send()).text()).toContain('Hi');
      expectTruncated(await telemetryEvent());
    });
  });
});

describe('response cache: only complete responses are written (#72)', () => {
  const cached = (body: object, path = '/v1/chat/completions') =>
    post(app, path, body, { ...OPENAI, 'x-ps-cache': 'on' });

  test('a truncated stream is never cached; the retry goes upstream', async () => {
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
      sse(openaiChunk({ content: 'Hal' }))
    );
    const body = chat('gpt-4o-mini', { stream: true });
    const first = await cached(body);
    expect(await first.text()).toContain('Hal');
    // With the cache on, a cache-status event is sent as well.
    await until(() => events.some((e) => e.stream), 'stream event');
    expectTruncated(events.find((e) => e.stream));

    const again = await cached(body);
    expect(again.headers.get('x-portkey-cache-status')).not.toBe('HIT');
    await again.text();
    expect(upstreamCalls).toHaveLength(2);
  });

  test('an upstream error is not cached (and never served as a 200 HIT)', async () => {
    let calls = 0;
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
      ++calls === 1
        ? json({ error: { message: 'overloaded', type: 'server_error' } }, 500)
        : json({
            id: 'chatcmpl-1',
            object: 'chat.completion',
            created: 1,
            model: 'gpt-4o-mini',
            choices: [
              {
                index: 0,
                message: { role: 'assistant', content: 'Hi' },
                finish_reason: 'stop',
              },
            ],
          })
    );
    const body = chat('gpt-4o-mini', { stream: false });
    const first = await cached(body);
    expect(first.status).toBe(500);
    await first.text();

    const second = await cached(body);
    expect(second.status).toBe(200);
    expect(second.headers.get('x-portkey-cache-status')).toBe('MISS');
    expect(((await second.json()) as any).choices[0].message.content).toBe(
      'Hi'
    );

    const third = await cached(body);
    expect(third.headers.get('x-portkey-cache-status')).toBe('HIT');
    await third.text();
    expect(upstreamCalls).toHaveLength(2);
  });

  test('a 200 with an incomplete Responses status is not cached', async () => {
    upstream(/api\.openai\.com\/v1\/responses$/, () =>
      json({
        id: 'resp_inc',
        object: 'response',
        model: 'gpt-4o',
        status: 'incomplete',
        incomplete_details: { reason: 'max_output_tokens' },
        output: [],
        usage: { input_tokens: 5, output_tokens: 16, total_tokens: 21 },
      })
    );
    const body = { model: 'gpt-4o', input: 'Say hi', stream: false };
    await (await cached(body, '/v1/responses')).text();
    const again = await cached(body, '/v1/responses');
    expect(again.headers.get('x-portkey-cache-status')).toBe('MISS');
    await again.text();
    expect(upstreamCalls).toHaveLength(2);
  });
});

describe('Gemini / Vertex tool calls finish with tool_calls (#72)', () => {
  const tools = [
    {
      type: 'function',
      function: {
        name: 'get_weather',
        parameters: {
          type: 'object',
          properties: { city: { type: 'string' } },
        },
      },
    },
  ];
  const fnCall = {
    functionCall: { name: 'get_weather', args: { city: 'Paris' } },
  };
  const candidate = (parts: object[], finishReason?: string) => ({
    candidates: [
      {
        content: { role: 'model', parts },
        ...(finishReason && { finishReason }),
        index: 0,
      },
    ],
    modelVersion: 'gemini-2.0-flash',
  });
  const usage = {
    usageMetadata: {
      promptTokenCount: 9,
      candidatesTokenCount: 4,
      totalTokenCount: 13,
    },
  };

  const providers: [string, Record<string, string>][] = [
    [
      'google',
      {
        'x-portkey-config': JSON.stringify({
          provider: 'google',
          api_key: 'g-regression',
        }),
      },
    ],
    [
      'vertex-ai',
      {
        'x-portkey-config': JSON.stringify({
          provider: 'vertex-ai',
          api_key: 'ya29.regression-token',
          vertex_project_id: 'ps-regression',
          vertex_region: 'us-central1',
        }),
      },
    ],
  ];

  describe.each(providers)('%s', (_name, headers) => {
    const body = (stream: boolean) =>
      chat('gemini-2.0-flash', { stream, tools });

    test('JSON response', async () => {
      upstream(/:generateContent/, () =>
        json({ ...candidate([fnCall], 'STOP'), ...usage })
      );
      const res = await post(app, '/v1/chat/completions', body(false), headers);
      const out: any = await res.json();
      expect(out.choices[0].message.tool_calls[0].function).toMatchObject({
        name: 'get_weather',
        arguments: '{"city":"Paris"}',
      });
      expect(out.choices[0].finish_reason).toBe('tool_calls');
    });

    test('stream: STOP in the same chunk as the function call', async () => {
      upstream(/:streamGenerateContent/, () =>
        sse(
          `data: ${JSON.stringify({ ...candidate([fnCall], 'STOP'), ...usage })}\r\n\r\n`
        )
      );
      const res = await post(app, '/v1/chat/completions', body(true), headers);
      const chunks = dataLines(await res.text());
      const finishes = chunks
        .map((c) => c.choices?.[0]?.finish_reason)
        .filter(Boolean);
      expect(finishes).toEqual(['tool_calls']);
      expectComplete(await telemetryEvent());
    });

    test('stream: STOP in a later chunk than the function call', async () => {
      upstream(/:streamGenerateContent/, () =>
        sse(
          `data: ${JSON.stringify(candidate([fnCall]))}\r\n\r\n` +
            `data: ${JSON.stringify({
              ...candidate([{ text: '' }], 'STOP'),
              ...usage,
            })}\r\n\r\n`
        )
      );
      const res = await post(app, '/v1/chat/completions', body(true), headers);
      const chunks = dataLines(await res.text());
      expect(chunks[0].choices[0].delta.tool_calls[0].function.name).toBe(
        'get_weather'
      );
      const finishes = chunks
        .map((c) => c.choices?.[0]?.finish_reason)
        .filter(Boolean);
      expect(finishes).toEqual(['tool_calls']);
    });

    test('stream: a text-only answer still finishes with stop', async () => {
      upstream(/:streamGenerateContent/, () =>
        sse(
          `data: ${JSON.stringify({ ...candidate([{ text: 'Hi' }], 'STOP'), ...usage })}\r\n\r\n`
        )
      );
      const res = await post(app, '/v1/chat/completions', body(true), headers);
      const finishes = dataLines(await res.text())
        .map((c) => c.choices?.[0]?.finish_reason)
        .filter(Boolean);
      expect(finishes).toEqual(['stop']);
    });

    test('non-strict mode keeps the native finish reason', async () => {
      upstream(/:generateContent/, () =>
        json({ ...candidate([fnCall], 'STOP'), ...usage })
      );
      const res = await post(app, '/v1/chat/completions', body(false), {
        ...headers,
        'x-portkey-strict-open-ai-compliance': 'false',
      });
      const out: any = await res.json();
      expect(out.choices[0].finish_reason).toBe('STOP');
    });
  });
});
