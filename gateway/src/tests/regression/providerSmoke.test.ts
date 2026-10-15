/**
 * Regression smoke test for the ported 2.0 provider code (#69): chat,
 * streaming chat and embeddings through the whole gateway app for OpenAI,
 * Anthropic, Bedrock and Vertex AI, offline. Each case checks the request the
 * provider receives, the unified response, and the ps-telemetry event
 * (requested_model, served_model, usage).
 */
jest.mock('jose', () => ({}));

import {
  awsEventStream,
  events,
  json,
  otelSpans,
  post,
  resetHarness,
  spanAttrs,
  sse,
  telemetryEvent,
  until,
  upstream,
  upstreamCalls,
} from './harness';
import app from '../../index';

const chatBody = (extra: object = {}) => ({
  model: 'auto',
  messages: [{ role: 'user', content: 'Say hi' }],
  max_tokens: 16,
  ...extra,
});

// The router picks a tier ("cheap": gpt-4o-mini); a config target maps it to
// the provider's own model id. served_model must be what the provider served.
const ROUTE = { 'x-ps-route': 'cheap' };

const bedrockConfig = (model: string) =>
  JSON.stringify({
    provider: 'bedrock',
    aws_access_key_id: 'AKIAREGRESSIONTEST',
    aws_secret_access_key: 'regression-secret',
    aws_region: 'us-east-1',
    override_params: { model },
  });

const vertexConfig = (model: string) =>
  JSON.stringify({
    provider: 'vertex-ai',
    api_key: 'ya29.regression-token',
    vertex_project_id: 'ps-regression',
    vertex_region: 'us-central1',
    override_params: { model },
  });

/** A complete stream: usage read from the stream, not marked truncated. */
function expectCompleteStream(ev: any) {
  expect(ev.stream).toBe(true);
  expect(ev.stream_truncated).toBeUndefined();
  expect(ev.stream_cancelled).toBeUndefined();
}

async function readSSE(res: Response) {
  const text = await res.text();
  return text
    .split('\n')
    .filter((l) => l.startsWith('data: ') && l !== 'data: [DONE]')
    .map((l) => JSON.parse(l.slice(6)));
}

beforeEach(() => resetHarness());

describe('OpenAI', () => {
  const headers = {
    'x-portkey-provider': 'openai',
    authorization: 'Bearer sk-regression',
  };

  test('chat', async () => {
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
      json({
        id: 'chatcmpl-1',
        object: 'chat.completion',
        created: 1,
        model: 'gpt-4o-mini-2024-07-18',
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
    const res = await post(app, '/v1/chat/completions', chatBody(), {
      ...headers,
      ...ROUTE,
    });
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.choices[0].message.content).toBe('Hi');

    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].body.model).toBe('gpt-4o-mini');
    expect(upstreamCalls[0].headers.authorization).toBe('Bearer sk-regression');

    expect(await telemetryEvent()).toMatchObject({
      vendor: 'openai',
      api_surface: 'chat',
      requested_model: 'auto',
      served_model: 'gpt-4o-mini-2024-07-18',
      route_group: 'cheap',
      tokens_in: 9,
      tokens_out: 1,
    });
  });

  test('streaming chat', async () => {
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
      sse(
        [
          'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"gpt-4o-mini-2024-07-18","choices":[{"index":0,"delta":{"role":"assistant","content":"Hi"},"finish_reason":null}]}',
          'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"gpt-4o-mini-2024-07-18","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":9,"completion_tokens":1,"total_tokens":10}}',
          'data: [DONE]',
          '',
        ].join('\n\n')
      )
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody({ stream: true, stream_options: { include_usage: true } }),
      { ...headers, ...ROUTE }
    );
    expect(res.headers.get('content-type')).toContain('text/event-stream');
    const chunks = await readSSE(res);
    expect(chunks.map((c) => c.choices[0]?.delta?.content ?? '').join('')).toBe(
      'Hi'
    );
    const ev = await telemetryEvent();
    expectCompleteStream(ev);
    expect(ev).toMatchObject({
      vendor: 'openai',
      requested_model: 'auto',
      served_model: 'gpt-4o-mini-2024-07-18',
      tokens_in: 9,
      tokens_out: 1,
    });
  });

  test('embeddings', async () => {
    upstream(/api\.openai\.com\/v1\/embeddings$/, () =>
      json({
        object: 'list',
        data: [{ object: 'embedding', embedding: [0.1, 0.2], index: 0 }],
        model: 'text-embedding-3-small',
        usage: { prompt_tokens: 2, total_tokens: 2 },
      })
    );
    const res = await post(
      app,
      '/v1/embeddings',
      { model: 'text-embedding-3-small', input: 'hello' },
      headers
    );
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.data[0].embedding).toEqual([0.1, 0.2]);
    // Only chat-shaped bodies are routed: no requested/served pair here.
    const ev = await telemetryEvent();
    expect(ev).toMatchObject({
      vendor: 'openai',
      api_surface: 'embeddings',
      model: 'text-embedding-3-small',
      tokens_in: 2,
    });
    expect(ev.requested_model).toBeUndefined();
  });
});

describe('Anthropic', () => {
  const MODEL = 'claude-sonnet-4-20250514';
  const headers = {
    'x-portkey-config': JSON.stringify({
      provider: 'anthropic',
      api_key: 'sk-ant-regression',
      override_params: { model: MODEL },
    }),
  };

  test('chat', async () => {
    upstream(/api\.anthropic\.com\/v1\/messages$/, () =>
      json({
        id: 'msg_1',
        type: 'message',
        role: 'assistant',
        model: 'claude-sonnet-4-20250514',
        content: [{ type: 'text', text: 'Hi' }],
        stop_reason: 'end_turn',
        usage: {
          input_tokens: 8,
          output_tokens: 2,
          cache_read_input_tokens: 4,
        },
      })
    );
    const res = await post(app, '/v1/chat/completions', chatBody(), {
      ...headers,
      ...ROUTE,
    });
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.choices[0].message.content).toBe('Hi');
    expect(body.choices[0].finish_reason).toBe('stop');
    // Cached tokens count in prompt_tokens (#108).
    expect(body.usage.prompt_tokens).toBe(12);

    expect(upstreamCalls[0].headers['x-api-key']).toBe('sk-ant-regression');
    expect(upstreamCalls[0].body).toMatchObject({
      model: MODEL,
      max_tokens: 16,
      messages: [{ role: 'user', content: 'Say hi' }],
    });

    expect(await telemetryEvent()).toMatchObject({
      vendor: 'anthropic',
      requested_model: 'auto',
      served_model: 'claude-sonnet-4-20250514',
      tokens_in: 12,
      tokens_out: 2,
    });
  });

  test('streaming chat', async () => {
    const sseEvent = (type: string, data: object) =>
      `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`;
    upstream(/api\.anthropic\.com\/v1\/messages$/, () =>
      sse(
        sseEvent('message_start', {
          message: {
            id: 'msg_1',
            type: 'message',
            role: 'assistant',
            model: 'claude-sonnet-4-20250514',
            content: [],
            usage: { input_tokens: 8, output_tokens: 1 },
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
            usage: { output_tokens: 2 },
          }) +
          sseEvent('message_stop', {})
      )
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody({ stream: true }),
      { ...headers, ...ROUTE }
    );
    const chunks = await readSSE(res);
    expect(
      chunks.map((c) => c.choices?.[0]?.delta?.content ?? '').join('')
    ).toBe('Hi');
    expect(chunks.some((c) => c.choices?.[0]?.finish_reason === 'stop')).toBe(
      true
    );
    const ev = await telemetryEvent();
    expectCompleteStream(ev);
    expect(ev).toMatchObject({
      vendor: 'anthropic',
      requested_model: 'auto',
      served_model: 'claude-sonnet-4-20250514',
      tokens_in: 8,
      tokens_out: 2,
    });
  });

  test('embeddings are not offered and never reach the provider', async () => {
    const res = await post(
      app,
      '/v1/embeddings',
      { model: MODEL, input: 'hello' },
      headers
    );
    expect(res.status).toBeGreaterThanOrEqual(400);
    expect(upstreamCalls).toHaveLength(0);
  });
});

describe('Bedrock', () => {
  const MODEL = 'anthropic.claude-3-5-haiku-20241022-v1:0';
  const headers = { 'x-portkey-config': bedrockConfig(MODEL) };

  test('chat (Converse), SigV4-signed', async () => {
    upstream(
      /bedrock-runtime\.us-east-1\.amazonaws\.com\/model\/.+\/converse$/,
      () =>
        json({
          output: {
            message: { role: 'assistant', content: [{ text: 'Hi' }] },
          },
          stopReason: 'end_turn',
          usage: { inputTokens: 7, outputTokens: 2, totalTokens: 9 },
        })
    );
    const res = await post(app, '/v1/chat/completions', chatBody(), {
      ...headers,
      ...ROUTE,
    });
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.choices[0].message.content).toBe('Hi');
    expect(body.model).toBe(MODEL);

    const call = upstreamCalls[0];
    expect(call.url).toBe(
      `https://bedrock-runtime.us-east-1.amazonaws.com/model/${encodeURIComponent(MODEL)}/converse`
    );
    expect(call.headers.authorization).toMatch(
      /^AWS4-HMAC-SHA256 Credential=AKIAREGRESSIONTEST\/\d{8}\/us-east-1\/bedrock\/aws4_request/
    );
    expect(call.body.messages[0]).toEqual({
      role: 'user',
      content: [{ text: 'Say hi' }],
    });

    expect(await telemetryEvent()).toMatchObject({
      vendor: 'bedrock',
      requested_model: 'auto',
      served_model: MODEL,
      tokens_in: 7,
      tokens_out: 2,
    });
  });

  test('streaming chat (AWS event stream)', async () => {
    upstream(/\/converse-stream$/, () =>
      awsEventStream([
        { role: 'assistant' },
        { contentBlockIndex: 0, delta: { text: 'Hi' } },
        { contentBlockIndex: 0 },
        { stopReason: 'end_turn' },
        {
          usage: { inputTokens: 7, outputTokens: 2, totalTokens: 9 },
          metrics: { latencyMs: 5 },
        },
      ])
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody({ stream: true }),
      { ...headers, ...ROUTE }
    );
    const chunks = await readSSE(res);
    expect(
      chunks.map((c) => c.choices?.[0]?.delta?.content ?? '').join('')
    ).toBe('Hi');
    expect(upstreamCalls[0].url).toContain('/converse-stream');
    const ev = await telemetryEvent();
    expectCompleteStream(ev);
    expect(ev).toMatchObject({
      vendor: 'bedrock',
      requested_model: 'auto',
      served_model: MODEL,
      tokens_in: 7,
      tokens_out: 2,
    });
  });

  test('embeddings (Titan)', async () => {
    const EMBED = 'amazon.titan-embed-text-v2:0';
    upstream(
      /bedrock-runtime\.us-east-1\.amazonaws\.com\/model\/.+\/invoke$/,
      () => json({ embedding: [0.3, 0.4], inputTextTokenCount: 3 })
    );
    const res = await post(
      app,
      '/v1/embeddings',
      { model: EMBED, input: 'hello' },
      { 'x-portkey-config': bedrockConfig(EMBED) }
    );
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.data[0].embedding).toEqual([0.3, 0.4]);
    expect(upstreamCalls[0].body).toMatchObject({ inputText: 'hello' });
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'bedrock',
      api_surface: 'embeddings',
      model: EMBED,
      tokens_in: 3,
    });
  });
});

describe('Vertex AI', () => {
  const MODEL = 'gemini-2.0-flash-001';
  const headers = { 'x-portkey-config': vertexConfig(MODEL) };
  const base =
    'https://us-central1-aiplatform.googleapis.com/v1/projects/ps-regression/locations/us-central1/publishers/google/models/';

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
        promptTokenCount: 6,
        candidatesTokenCount: 2,
        totalTokenCount: 8,
      },
    }),
    modelVersion: MODEL,
  });

  test('chat (Gemini generateContent)', async () => {
    upstream(/:generateContent$/, () => json(gemini('Hi')));
    const res = await post(app, '/v1/chat/completions', chatBody(), {
      ...headers,
      ...ROUTE,
    });
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.choices[0].message.content).toBe('Hi');

    const call = upstreamCalls[0];
    expect(call.url).toBe(`${base}${MODEL}:generateContent`);
    expect(call.headers.authorization).toBe('Bearer ya29.regression-token');
    expect(call.body.contents[0]).toMatchObject({
      role: 'user',
      parts: [{ text: 'Say hi' }],
    });

    expect(await telemetryEvent()).toMatchObject({
      vendor: 'vertex-ai',
      requested_model: 'auto',
      served_model: MODEL,
      tokens_in: 6,
      tokens_out: 2,
    });
  });

  test('streaming chat (Gemini SSE)', async () => {
    upstream(/:streamGenerateContent\?alt=sse$/, () =>
      sse(
        `data: ${JSON.stringify(gemini('Hi', false))}\r\n\r\n` +
          `data: ${JSON.stringify({
            ...gemini('', true),
            candidates: [
              {
                content: { role: 'model', parts: [{ text: '' }] },
                finishReason: 'STOP',
                index: 0,
              },
            ],
          })}\r\n\r\n`
      )
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody({ stream: true }),
      { ...headers, ...ROUTE }
    );
    const chunks = await readSSE(res);
    expect(
      chunks.map((c) => c.choices?.[0]?.delta?.content ?? '').join('')
    ).toBe('Hi');
    const ev = await telemetryEvent();
    expectCompleteStream(ev);
    expect(ev).toMatchObject({
      vendor: 'vertex-ai',
      requested_model: 'auto',
      served_model: MODEL,
      tokens_in: 6,
      tokens_out: 2,
    });
  });

  test('embeddings', async () => {
    const EMBED = 'text-embedding-004';
    upstream(/text-embedding-004:predict$/, () =>
      json({
        predictions: [
          {
            embeddings: {
              values: [0.5, 0.6],
              statistics: { token_count: 4, truncated: false },
            },
          },
        ],
      })
    );
    const res = await post(
      app,
      '/v1/embeddings',
      { model: EMBED, input: 'hello' },
      { 'x-portkey-config': vertexConfig(EMBED) }
    );
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.data[0].embedding).toEqual([0.5, 0.6]);
    expect(upstreamCalls[0].url).toBe(`${base}${EMBED}:predict`);
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'vertex-ai',
      api_surface: 'embeddings',
      model: EMBED,
      tokens_in: 4,
    });
  });
});

describe('OTel GenAI spans', () => {
  test('a routed chat call exports one span with model and usage', async () => {
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
      json({
        id: 'chatcmpl-2',
        object: 'chat.completion',
        created: 1,
        model: 'gpt-4o-mini-2024-07-18',
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
    await post(app, '/v1/chat/completions', chatBody(), {
      'x-portkey-provider': 'openai',
      authorization: 'Bearer sk-regression',
      ...ROUTE,
    });
    await until(() => otelSpans().length > 0, 'otel span');
    const spans = otelSpans();
    expect(spans).toHaveLength(1);
    const attrs = spanAttrs(spans[0]);
    expect(attrs).toMatchObject({
      'gen_ai.operation.name': 'chat',
      'gen_ai.provider.name': 'openai',
      'gen_ai.response.model': 'gpt-4o-mini-2024-07-18',
      'gen_ai.usage.input_tokens': 9,
      'gen_ai.usage.output_tokens': 1,
    });
    // Prompt content is off by default.
    expect(JSON.stringify(spans)).not.toContain('Say hi');
    expect(events).toHaveLength(1);
  });
});
