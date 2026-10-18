/**
 * Cached, cache-write, reasoning and service-tier usage in ps-telemetry
 * (#9), through the whole gateway app and offline: Chat Completions,
 * Responses and Messages, streamed and not, for OpenAI, Anthropic, Bedrock
 * and Vertex AI (Gemini). Field names and semantics match the SDK's
 * provider adapters: `tokens_in` is the whole prompt, cache reads and writes
 * included, and `cost_source` is sent only with a provider-reported cost.
 */
jest.mock('jose', () => ({}));

import {
  json,
  post,
  resetHarness,
  sse,
  telemetryEvent,
  upstream,
} from './harness';
import app from '../../index';

const DETAIL_FIELDS = [
  'tokens_cached',
  'tokens_cache_write',
  'tokens_cache_write_1h',
  'tokens_reasoning',
  'service_tier',
  'cost',
  'cost_source',
];

/** The event's usage detail fields only, to compare them exactly. */
function details(ev: any) {
  const out: Record<string, unknown> = {};
  for (const k of DETAIL_FIELDS) if (ev[k] !== undefined) out[k] = ev[k];
  return out;
}

const chatBody = (extra: object = {}) => ({
  model: 'gpt-4o',
  messages: [{ role: 'user', content: 'Say hi' }],
  max_tokens: 16,
  ...extra,
});

const OPENAI = {
  'x-portkey-provider': 'openai',
  authorization: 'Bearer sk-regression',
};

const sseEvent = (type: string, data: object) =>
  `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`;

beforeEach(() => resetHarness());

describe('OpenAI', () => {
  test('chat: cached and reasoning tokens, served tier', async () => {
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
      json({
        id: 'chatcmpl-1',
        object: 'chat.completion',
        created: 1,
        model: 'o3-mini-2025-01-31',
        service_tier: 'flex',
        choices: [
          {
            index: 0,
            message: { role: 'assistant', content: 'Hi' },
            finish_reason: 'stop',
          },
        ],
        usage: {
          prompt_tokens: 2000,
          completion_tokens: 300,
          total_tokens: 2300,
          prompt_tokens_details: { cached_tokens: 1536, audio_tokens: 0 },
          completion_tokens_details: { reasoning_tokens: 256 },
        },
      })
    );
    await post(app, '/v1/chat/completions', chatBody(), OPENAI);
    const ev = await telemetryEvent();
    // prompt_tokens already includes the cached tokens.
    expect(ev).toMatchObject({ tokens_in: 2000, tokens_out: 300 });
    expect(details(ev)).toEqual({
      tokens_cached: 1536,
      tokens_reasoning: 256,
      service_tier: 'flex',
    });
  });

  test('streaming chat: usage chunk and per-chunk tier', async () => {
    const chunk = (extra: object) =>
      `data: ${JSON.stringify({
        id: 'c1',
        object: 'chat.completion.chunk',
        created: 1,
        model: 'o3-mini-2025-01-31',
        service_tier: 'default',
        ...extra,
      })}\n\n`;
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
      sse(
        chunk({
          choices: [
            { index: 0, delta: { content: 'Hi' }, finish_reason: null },
          ],
          usage: null,
        }) +
          chunk({
            choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
            usage: null,
          }) +
          chunk({
            choices: [],
            usage: {
              prompt_tokens: 1200,
              completion_tokens: 80,
              total_tokens: 1280,
              prompt_tokens_details: { cached_tokens: 1024 },
              completion_tokens_details: { reasoning_tokens: 64 },
            },
          }) +
          'data: [DONE]\n\n'
      )
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody({ stream: true, stream_options: { include_usage: true } }),
      OPENAI
    );
    await res.text();
    const ev = await telemetryEvent();
    expect(ev).toMatchObject({ stream: true, tokens_in: 1200, tokens_out: 80 });
    expect(ev.stream_truncated).toBeUndefined();
    expect(details(ev)).toEqual({
      tokens_cached: 1024,
      tokens_reasoning: 64,
      service_tier: 'default',
    });
  });

  test('Responses (native): input/output token details', async () => {
    upstream(/api\.openai\.com\/v1\/responses$/, () =>
      json({
        id: 'resp_1',
        object: 'response',
        status: 'completed',
        model: 'o4-mini-2025-04-16',
        service_tier: 'flex',
        output: [],
        usage: {
          input_tokens: 5000,
          input_tokens_details: { cached_tokens: 4096 },
          output_tokens: 900,
          output_tokens_details: { reasoning_tokens: 768 },
          total_tokens: 5900,
        },
      })
    );
    await post(
      app,
      '/v1/responses',
      { model: 'o4-mini', input: 'Hi', service_tier: 'flex' },
      OPENAI
    );
    const ev = await telemetryEvent();
    expect(ev).toMatchObject({
      api_surface: 'responses',
      tokens_in: 5000,
      tokens_out: 900,
    });
    expect(details(ev)).toEqual({
      tokens_cached: 4096,
      tokens_reasoning: 768,
      service_tier: 'flex',
    });
  });

  test('streaming Responses (native): read from response.completed', async () => {
    const response = (status: string, usage: object | null) => ({
      id: 'resp_2',
      object: 'response',
      status,
      model: 'o4-mini-2025-04-16',
      service_tier: status === 'completed' ? 'default' : 'auto',
      output: [],
      usage,
    });
    upstream(/api\.openai\.com\/v1\/responses$/, () =>
      sse(
        sseEvent('response.created', {
          response: response('in_progress', null),
        }) +
          sseEvent('response.output_text.delta', { delta: 'Hi' }) +
          sseEvent('response.completed', {
            response: response('completed', {
              input_tokens: 3000,
              input_tokens_details: { cached_tokens: 2048 },
              output_tokens: 500,
              output_tokens_details: { reasoning_tokens: 384 },
              total_tokens: 3500,
            }),
          })
      )
    );
    const res = await post(
      app,
      '/v1/responses',
      { model: 'o4-mini', input: 'Hi', stream: true },
      OPENAI
    );
    await res.text();
    const ev = await telemetryEvent();
    expect(ev).toMatchObject({
      stream: true,
      tokens_in: 3000,
      tokens_out: 500,
    });
    expect(ev.stream_truncated).toBeUndefined();
    // The served tier from response.completed, not the requested "auto".
    expect(details(ev)).toEqual({
      tokens_cached: 2048,
      tokens_reasoning: 384,
      service_tier: 'default',
    });
  });

  test('no details reported: no detail fields and no cost_source', async () => {
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
      json({
        id: 'chatcmpl-3',
        object: 'chat.completion',
        created: 1,
        model: 'gpt-4o-2024-08-06',
        choices: [
          {
            index: 0,
            message: { role: 'assistant', content: 'Hi' },
            finish_reason: 'stop',
          },
        ],
        usage: { prompt_tokens: 9, completion_tokens: 1, total_tokens: 10 },
      })
    );
    await post(app, '/v1/chat/completions', chatBody(), OPENAI);
    const ev = await telemetryEvent();
    expect(ev).toMatchObject({ tokens_in: 9, tokens_out: 1 });
    // The gateway prices nothing itself: without a provider-reported cost
    // there is no cost and so no cost_source (SDK add_usage_detail_fields).
    expect(details(ev)).toEqual({});
  });
});

describe('OpenRouter', () => {
  test('a provider-reported cost is sent with cost_source provider_reported', async () => {
    upstream(/openrouter\.ai\/api\/v1\/chat\/completions$/, () =>
      json({
        id: 'gen-1',
        object: 'chat.completion',
        created: 1,
        model: 'openai/gpt-4o',
        choices: [
          {
            index: 0,
            message: { role: 'assistant', content: 'Hi' },
            finish_reason: 'stop',
          },
        ],
        usage: {
          prompt_tokens: 100,
          completion_tokens: 10,
          total_tokens: 110,
          prompt_tokens_details: { cached_tokens: 64 },
          cost: 0.00042,
        },
      })
    );
    await post(
      app,
      '/v1/chat/completions',
      chatBody({ model: 'openai/gpt-4o' }),
      {
        'x-portkey-provider': 'openrouter',
        authorization: 'Bearer sk-or-regression',
      }
    );
    const ev = await telemetryEvent();
    expect(ev).toMatchObject({ vendor: 'openrouter', tokens_in: 100 });
    expect(details(ev)).toEqual({
      tokens_cached: 64,
      cost: 0.00042,
      cost_source: 'provider_reported',
    });
  });
});

describe('Anthropic', () => {
  const MODEL = 'claude-sonnet-4-20250514';
  const headers = {
    'x-portkey-config': JSON.stringify({
      provider: 'anthropic',
      api_key: 'sk-ant-regression',
    }),
  };
  const usage = {
    input_tokens: 50,
    output_tokens: 20,
    cache_read_input_tokens: 1000,
    cache_creation_input_tokens: 600,
    cache_creation: {
      ephemeral_5m_input_tokens: 200,
      ephemeral_1h_input_tokens: 400,
    },
    service_tier: 'standard',
  };
  const message = (u: object) => ({
    id: 'msg_1',
    type: 'message',
    role: 'assistant',
    model: MODEL,
    content: [{ type: 'text', text: 'Hi' }],
    stop_reason: 'end_turn',
    usage: u,
  });
  /** message_start carries the prompt usage, message_delta the output. */
  const stream = (deltaUsage: object) =>
    sse(
      sseEvent('message_start', {
        message: {
          ...message({ ...usage, output_tokens: 1 }),
          content: [],
          stop_reason: null,
        },
      }) +
        sseEvent('content_block_start', {
          index: 0,
          content_block: { type: 'text', text: '' },
        }) +
        sseEvent('content_block_delta', {
          index: 0,
          delta: { type: 'text_delta', text: 'Hi' },
        }) +
        sseEvent('content_block_stop', { index: 0 }) +
        sseEvent('message_delta', {
          delta: { stop_reason: 'end_turn' },
          usage: deltaUsage,
        }) +
        sseEvent('message_stop', {})
    );

  // tokens_in is the whole prompt: input + cache reads + cache writes.
  const TOKENS_IN = 50 + 1000 + 600;

  test('chat: cache reads, writes and the 1-hour write subset', async () => {
    upstream(/api\.anthropic\.com\/v1\/messages$/, () => json(message(usage)));
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody({ model: MODEL }),
      headers
    );
    const body: any = await res.json();
    expect(body.usage).toMatchObject({
      prompt_tokens: TOKENS_IN,
      cache_creation: { ephemeral_1h_input_tokens: 400 },
    });
    const ev = await telemetryEvent();
    expect(ev).toMatchObject({ tokens_in: TOKENS_IN, tokens_out: 20 });
    expect(details(ev)).toEqual({
      tokens_cached: 1000,
      tokens_cache_write: 600,
      tokens_cache_write_1h: 400,
    });
  });

  test('streaming chat: message_start usage kept to the final chunk', async () => {
    upstream(/api\.anthropic\.com\/v1\/messages$/, () =>
      stream({ output_tokens: 20 })
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody({ model: MODEL, stream: true }),
      headers
    );
    await res.text();
    const ev = await telemetryEvent();
    expect(ev).toMatchObject({
      stream: true,
      tokens_in: TOKENS_IN,
      tokens_out: 20,
    });
    expect(details(ev)).toEqual({
      tokens_cached: 1000,
      tokens_cache_write: 600,
      tokens_cache_write_1h: 400,
    });
  });

  test('Messages (native): usage and service tier as Anthropic reports them', async () => {
    upstream(/api\.anthropic\.com\/v1\/messages$/, () => json(message(usage)));
    await post(
      app,
      '/v1/messages',
      {
        model: MODEL,
        max_tokens: 16,
        messages: [{ role: 'user', content: 'Say hi' }],
      },
      headers
    );
    const ev = await telemetryEvent();
    expect(ev).toMatchObject({
      api_surface: 'messages',
      tokens_in: TOKENS_IN,
      tokens_out: 20,
    });
    expect(details(ev)).toEqual({
      tokens_cached: 1000,
      tokens_cache_write: 600,
      tokens_cache_write_1h: 400,
      service_tier: 'standard',
    });
  });

  test('streaming Messages (native): message_delta usage accumulated', async () => {
    // message_delta repeats the cumulative counts; output grows to 20.
    upstream(/api\.anthropic\.com\/v1\/messages$/, () =>
      stream({
        input_tokens: 50,
        output_tokens: 20,
        cache_read_input_tokens: 1000,
        cache_creation_input_tokens: 600,
      })
    );
    const res = await post(
      app,
      '/v1/messages',
      {
        model: MODEL,
        max_tokens: 16,
        stream: true,
        messages: [{ role: 'user', content: 'Say hi' }],
      },
      headers
    );
    await res.text();
    const ev = await telemetryEvent();
    expect(ev).toMatchObject({
      stream: true,
      api_surface: 'messages',
      tokens_in: TOKENS_IN,
      tokens_out: 20,
    });
    expect(ev.stream_truncated).toBeUndefined();
    expect(details(ev)).toEqual({
      tokens_cached: 1000,
      tokens_cache_write: 600,
      tokens_cache_write_1h: 400,
      service_tier: 'standard',
    });
  });

  test('Responses adapter: cached tokens kept, echoed tier not reported', async () => {
    upstream(/api\.anthropic\.com\/v1\/messages$/, () => json(message(usage)));
    const res = await post(
      app,
      '/v1/responses',
      { model: MODEL, input: 'Hi', service_tier: 'flex' },
      { 'x-portkey-provider': 'anthropic', 'x-api-key': 'sk-ant-regression' }
    );
    const out: any = await res.json();
    // The adapter echoes the requested tier; Anthropic never served "flex".
    expect(out.service_tier).toBe('flex');
    const ev = await telemetryEvent();
    expect(ev).toMatchObject({
      api_surface: 'responses',
      tokens_in: TOKENS_IN,
      tokens_out: 20,
    });
    expect(details(ev)).toEqual({ tokens_cached: 1000, tokens_reasoning: 0 });
  });
});

describe('Bedrock', () => {
  const MODEL = 'anthropic.claude-3-5-haiku-20241022-v1:0';
  const headers = {
    'x-portkey-config': JSON.stringify({
      provider: 'bedrock',
      aws_access_key_id: 'AKIAREGRESSIONTEST',
      aws_secret_access_key: 'regression-secret',
      aws_region: 'us-east-1',
      override_params: { model: MODEL },
    }),
  };

  test('chat (Converse): cache read and write tokens', async () => {
    upstream(/\/converse$/, () =>
      json({
        output: { message: { role: 'assistant', content: [{ text: 'Hi' }] } },
        stopReason: 'end_turn',
        usage: {
          inputTokens: 30,
          outputTokens: 5,
          totalTokens: 835,
          cacheReadInputTokens: 500,
          cacheWriteInputTokens: 300,
        },
      })
    );
    await post(app, '/v1/chat/completions', chatBody(), headers);
    const ev = await telemetryEvent();
    expect(ev).toMatchObject({ tokens_in: 830, tokens_out: 5 });
    expect(details(ev)).toEqual({
      tokens_cached: 500,
      tokens_cache_write: 300,
    });
  });
});

describe('Vertex AI (Gemini)', () => {
  const MODEL = 'gemini-2.5-flash';
  const headers = {
    'x-portkey-config': JSON.stringify({
      provider: 'vertex-ai',
      api_key: 'ya29.regression-token',
      vertex_project_id: 'ps-regression',
      vertex_region: 'us-central1',
      override_params: { model: MODEL },
    }),
  };
  const gemini = (text: string, usage = true) => ({
    candidates: [
      {
        content: { role: 'model', parts: [{ text }] },
        finishReason: 'STOP',
        index: 0,
      },
    ],
    ...(usage && {
      usageMetadata: {
        promptTokenCount: 900,
        cachedContentTokenCount: 512,
        candidatesTokenCount: 40,
        thoughtsTokenCount: 120,
        totalTokenCount: 1060,
      },
    }),
    modelVersion: MODEL,
  });

  // As in the SDK: thinking tokens are billed as output, so tokens_out
  // includes them, and promptTokenCount already includes the cached tokens.
  const expected = { tokens_in: 900, tokens_out: 160 };
  const expectedDetails = { tokens_cached: 512, tokens_reasoning: 120 };

  test('chat: cachedContentTokenCount and thoughtsTokenCount', async () => {
    upstream(/:generateContent$/, () => json(gemini('Hi')));
    await post(app, '/v1/chat/completions', chatBody(), headers);
    const ev = await telemetryEvent();
    expect(ev).toMatchObject(expected);
    expect(details(ev)).toEqual(expectedDetails);
  });

  test('streaming chat', async () => {
    upstream(/:streamGenerateContent\?alt=sse$/, () =>
      sse(
        `data: ${JSON.stringify(gemini('Hi', false))}\r\n\r\n` +
          `data: ${JSON.stringify(gemini(''))}\r\n\r\n`
      )
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody({ stream: true }),
      headers
    );
    await res.text();
    const ev = await telemetryEvent();
    expect(ev).toMatchObject({ stream: true, ...expected });
    expect(details(ev)).toEqual(expectedDetails);
  });
});
