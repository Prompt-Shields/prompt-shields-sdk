/**
 * Stream assembly across chunk boundaries (#72), through the whole gateway
 * app and offline.
 *
 * Bedrock: an AWS event stream whose network reads split messages at any
 * byte offset (inside the 4-byte length prefix too) is reassembled, so an
 * extended-thinking stream's reasoning text, signature and answer all reach
 * the client and the stream is not cut off (re-implemented from Portkey
 * enterprise 2.25.1). Covers Converse chat completions and native Anthropic
 * /v1/messages (`invoke-with-response-stream`).
 *
 * Gemini / Vertex: a chunk with text followed by a function call keeps the
 * call (and a call followed by text keeps the text), and tool-call `index`
 * increases across the stream instead of restarting at 0 in every chunk.
 */
jest.mock('jose', () => ({}));

import {
  post,
  resetHarness,
  sse,
  telemetryEvent,
  upstream,
  upstreamCalls,
} from './harness';
import app from '../../index';
import { readAWSStream } from '../../handlers/streamHandler';
import { transformGeminiStreamParts } from '../../providers/google/chatComplete';

const enc = new TextEncoder();

beforeEach(() => resetHarness());

const dataLines = (text: string) =>
  text
    .split('\n')
    .filter((l) => l.startsWith('data: ') && l !== 'data: [DONE]')
    .map((l) => JSON.parse(l.slice(6)));

/** One AWS event-stream message (no headers, zero CRCs) per payload. */
const awsFrames = (payloads: object[]): Uint8Array => {
  const frames = payloads.map((p) => {
    const payload = enc.encode(JSON.stringify(p));
    const frame = new Uint8Array(12 + payload.length + 4);
    new DataView(frame.buffer).setUint32(0, frame.length);
    frame.set(payload, 12);
    return frame;
  });
  const out = new Uint8Array(frames.reduce((n, f) => n + f.length, 0));
  let at = 0;
  for (const f of frames) {
    out.set(f, at);
    at += f.length;
  }
  return out;
};

/** The bytes served as network reads of `readSize` bytes each. */
const splitStream = (bytes: Uint8Array, readSize: number): Response => {
  let at = 0;
  return new Response(
    new ReadableStream<Uint8Array>({
      pull(controller) {
        if (at >= bytes.length) return controller.close();
        controller.enqueue(bytes.slice(at, at + readSize));
        at += readSize;
      },
    }),
    {
      status: 200,
      headers: { 'content-type': 'application/vnd.amazon.eventstream' },
    }
  );
};

// Read sizes that land inside the length prefix (1-3), on odd offsets, and
// across several messages at once.
const READ_SIZES = [1, 2, 3, 5, 17, 64, 1000];

const THINKING = ['The user', ' wants a', ' greeting.', ' Keep it short.'];
// A realistic signature: long and base64, sent in a single delta.
const SIGNATURE = Buffer.from('s'.repeat(450)).toString('base64');

describe('Bedrock extended-thinking stream split across network reads (#72)', () => {
  const MODEL = 'anthropic.claude-3-7-sonnet-20250219-v1:0';
  const headers = {
    'x-portkey-config': JSON.stringify({
      provider: 'bedrock',
      aws_access_key_id: 'AKIAREGRESSIONTEST',
      aws_secret_access_key: 'regression-secret',
      aws_region: 'us-east-1',
    }),
    'x-portkey-strict-open-ai-compliance': 'false',
  };

  const converseEvents = [
    { role: 'assistant' },
    ...THINKING.map((text) => ({
      contentBlockIndex: 0,
      delta: { reasoningContent: { text } },
    })),
    {
      contentBlockIndex: 0,
      delta: { reasoningContent: { signature: SIGNATURE } },
    },
    { contentBlockIndex: 0 },
    { contentBlockIndex: 1, delta: { text: 'Hi' } },
    { contentBlockIndex: 1, delta: { text: ' there' } },
    { contentBlockIndex: 1 },
    { stopReason: 'end_turn' },
    { usage: { inputTokens: 11, outputTokens: 23, totalTokens: 34 } },
  ];

  test.each(READ_SIZES)('chat completions, %i-byte reads', async (readSize) => {
    upstream(/\/converse-stream$/, () =>
      splitStream(awsFrames(converseEvents), readSize)
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      {
        model: MODEL,
        stream: true,
        messages: [{ role: 'user', content: 'Say hi' }],
      },
      headers
    );
    const text = await res.text();
    const chunks = dataLines(text);
    const blocks = chunks.flatMap(
      (c) => c.choices?.[0]?.delta?.content_blocks ?? []
    );
    expect(
      blocks
        .filter((b: any) => b.index === 0)
        .map((b: any) => b.delta.thinking ?? '')
        .join('')
    ).toBe(THINKING.join(''));
    expect(
      blocks
        .filter((b: any) => b.delta.signature)
        .map((b: any) => b.delta.signature)
    ).toEqual([SIGNATURE]);
    expect(
      chunks.map((c) => c.choices?.[0]?.delta?.content ?? '').join('')
    ).toBe('Hi there');
    expect(
      chunks.map((c) => c.choices?.[0]?.finish_reason).filter(Boolean)
    ).toEqual(['end_turn']); // native reason: strict compliance is off
    expect(text.trim().endsWith('data: [DONE]')).toBe(true);
    const ev = await telemetryEvent();
    expect(ev.stream_truncated).toBeUndefined();
    expect(ev).toMatchObject({ tokens_in: 11, tokens_out: 23 });
  });

  // Native Anthropic events, base64 in each message's `bytes`.
  const anthropicEvents = [
    {
      type: 'message_start',
      message: {
        id: 'msg_1',
        type: 'message',
        role: 'assistant',
        model: 'claude-3-7-sonnet-20250219',
        content: [],
        stop_reason: null,
        usage: { input_tokens: 11, output_tokens: 1 },
      },
    },
    {
      type: 'content_block_start',
      index: 0,
      content_block: { type: 'thinking', thinking: '', signature: '' },
    },
    ...THINKING.map((thinking) => ({
      type: 'content_block_delta',
      index: 0,
      delta: { type: 'thinking_delta', thinking },
    })),
    {
      type: 'content_block_delta',
      index: 0,
      delta: { type: 'signature_delta', signature: SIGNATURE },
    },
    { type: 'content_block_stop', index: 0 },
    {
      type: 'content_block_start',
      index: 1,
      content_block: { type: 'text', text: '' },
    },
    {
      type: 'content_block_delta',
      index: 1,
      delta: { type: 'text_delta', text: 'Hi there' },
    },
    { type: 'content_block_stop', index: 1 },
    {
      type: 'message_delta',
      delta: { stop_reason: 'end_turn' },
      usage: { output_tokens: 23 },
    },
    { type: 'message_stop' },
  ].map((ev) => ({
    bytes: Buffer.from(JSON.stringify(ev)).toString('base64'),
  }));

  test.each(READ_SIZES)('/v1/messages, %i-byte reads', async (readSize) => {
    upstream(/\/invoke-with-response-stream$/, () =>
      splitStream(awsFrames(anthropicEvents), readSize)
    );
    const res = await post(
      app,
      '/v1/messages',
      {
        model: MODEL,
        stream: true,
        max_tokens: 2048,
        thinking: { type: 'enabled', budget_tokens: 1024 },
        messages: [{ role: 'user', content: 'Say hi' }],
      },
      headers
    );
    expect(upstreamCalls[0].url).toMatch(/invoke-with-response-stream$/);
    const out = dataLines(await res.text());
    const deltas = out.filter((e) => e.type === 'content_block_delta');
    expect(deltas.map((e) => e.delta.thinking ?? '').join('')).toBe(
      THINKING.join('')
    );
    expect(
      deltas.filter((e) => e.delta.signature).map((e) => e.delta.signature)
    ).toEqual([SIGNATURE]);
    expect(deltas.map((e) => e.delta.text ?? '').join('')).toBe('Hi there');
    expect(out[out.length - 1]).toEqual({ type: 'message_stop' });
    expect((await telemetryEvent()).stream_truncated).toBeUndefined();
  });
});

describe('Gemini / Vertex: mixed parts and tool-call indexes in streams (#72)', () => {
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
  const fnCall = (city: string) => ({
    functionCall: { name: 'get_weather', args: { city } },
  });
  const chunk = (parts: object[], finishReason?: string) =>
    `data: ${JSON.stringify({
      candidates: [
        {
          content: { role: 'model', parts },
          ...(finishReason && { finishReason }),
          index: 0,
        },
      ],
      modelVersion: 'gemini-2.5-flash',
      ...(finishReason && {
        usageMetadata: {
          promptTokenCount: 9,
          candidatesTokenCount: 4,
          totalTokenCount: 13,
        },
      }),
    })}\r\n\r\n`;

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
    const send = () =>
      post(
        app,
        '/v1/chat/completions',
        {
          model: 'gemini-2.5-flash',
          stream: true,
          tools,
          messages: [{ role: 'user', content: 'Weather?' }],
        },
        headers
      );
    const toolCallsOf = (chunks: any[]) =>
      chunks.flatMap((c) => c.choices?.[0]?.delta?.tool_calls ?? []);

    test('text before a function call in the same chunk keeps the call', async () => {
      upstream(/:streamGenerateContent/, () =>
        sse(
          chunk([{ text: 'Let me check.' }, fnCall('Paris')]) +
            chunk([{ text: '' }], 'STOP')
        )
      );
      const chunks = dataLines(await (await send()).text());
      expect(chunks[0].choices[0].delta.content).toBe('Let me check.');
      const calls = toolCallsOf(chunks);
      expect(calls).toHaveLength(1);
      expect(calls[0]).toMatchObject({
        index: 0,
        type: 'function',
        function: { name: 'get_weather', arguments: '{"city":"Paris"}' },
      });
      expect(
        chunks.map((c) => c.choices?.[0]?.finish_reason).filter(Boolean)
      ).toEqual(['tool_calls']);
    });

    test('text after a function call in the same chunk is kept', async () => {
      upstream(/:streamGenerateContent/, () =>
        sse(chunk([fnCall('Paris'), { text: 'Checking.' }], 'STOP'))
      );
      const chunks = dataLines(await (await send()).text());
      const delta = chunks[0].choices[0].delta;
      expect(delta.content).toBe('Checking.');
      expect(delta.tool_calls).toHaveLength(1);
      expect(delta.tool_calls[0].function.name).toBe('get_weather');
    });

    test('tool-call index increases across chunks', async () => {
      upstream(/:streamGenerateContent/, () =>
        sse(
          chunk([fnCall('Paris')]) +
            chunk([{ text: 'And ' }, fnCall('Berlin')]) +
            chunk([fnCall('Tokyo'), fnCall('Rome')], 'STOP')
        )
      );
      const calls = toolCallsOf(dataLines(await (await send()).text()));
      expect(calls.map((c: any) => c.index)).toEqual([0, 1, 2, 3]);
      expect(
        calls.map((c: any) => JSON.parse(c.function.arguments).city)
      ).toEqual(['Paris', 'Berlin', 'Tokyo', 'Rome']);
      expect(new Set(calls.map((c: any) => c.id)).size).toBe(4);
      expect((await telemetryEvent()).stream_truncated).toBeUndefined();
    });
  });
});

describe('readAWSStream framing edge cases (#72)', () => {
  const read = async (bytes: Uint8Array, readSize: number) => {
    const out: string[] = [];
    for await (const chunk of readAWSStream(
      splitStream(bytes, readSize).body!.getReader(),
      undefined,
      'fallback',
      false,
      {} as any
    )) {
      out.push(new TextDecoder().decode(chunk as Uint8Array));
    }
    return out;
  };
  const prelude = (length: number) => {
    const b = new Uint8Array(16);
    new DataView(b.buffer).setUint32(0, length);
    return b;
  };

  test('several messages in one read, each exactly framed', async () => {
    const bytes = awsFrames([{ a: 1 }, { b: 2 }, { c: 3 }]);
    expect(await read(bytes, bytes.length)).toHaveLength(3);
  });

  test('an incomplete trailing message is dropped, earlier ones kept', async () => {
    const whole = awsFrames([{ a: 1 }, { b: 2 }]);
    expect(await read(whole.slice(0, whole.length - 3), 7)).toHaveLength(1);
  });

  test('a length below the 16-byte minimum errors', async () => {
    await expect(read(prelude(8), 16)).rejects.toThrow(
      'Invalid AWS event stream message length: 8'
    );
  });

  test('a length above 16 MiB errors instead of buffering', async () => {
    // e.g. a non-event-stream body whose first bytes read as a huge length.
    await expect(read(enc.encode('<html><body>'), 4)).rejects.toThrow(
      /Invalid AWS event stream message length/
    );
  });
});

describe('transformGeminiStreamParts (#72)', () => {
  test('parallel calls, thought and text in one chunk', () => {
    const state: any = {};
    const msg = transformGeminiStreamParts(
      [
        { text: 'thinking...', thought: true },
        { functionCall: { name: 'a', args: {} } },
        { functionCall: { name: 'b', args: { x: 1 } } },
        { text: 'done' },
      ],
      state,
      0,
      true
    )!;
    expect(msg.content).toBe('done');
    expect(msg.content_blocks).toBeUndefined();
    expect(msg.tool_calls.map((t: any) => [t.index, t.function.name])).toEqual([
      [0, 'a'],
      [1, 'b'],
    ]);
  });

  test('tool-call indexes are per candidate', () => {
    const state: any = {};
    const call = [{ functionCall: { name: 'f', args: {} } }];
    transformGeminiStreamParts(call, state, 0, true);
    expect(
      transformGeminiStreamParts(call, state, 1, true)!.tool_calls[0].index
    ).toBe(0);
    expect(
      transformGeminiStreamParts(call, state, 0, true)!.tool_calls[0].index
    ).toBe(1);
  });
});
