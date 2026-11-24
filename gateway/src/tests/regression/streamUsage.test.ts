/**
 * Stream usage (#389), through the whole gateway app and offline: the
 * gateway asks allowlisted OpenAI-style providers for usage
 * (`stream_options.include_usage`) and strips what that adds from the
 * client stream, so a client that did not ask gets the same bytes; streams
 * from OpenAI, Azure OpenAI, Anthropic, Vertex Claude and an
 * OpenAI-compatible host all report tokens; and counts the provider never
 * sent are estimated and marked `usage_source: "estimated"`.
 */
jest.mock('jose', () => ({}));

import {
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

const OPENAI = {
  'x-portkey-provider': 'openai',
  authorization: 'Bearer sk-regression',
};
const AZURE = {
  'x-portkey-provider': 'azure-openai',
  'x-portkey-azure-resource-name': 'regres',
  'x-portkey-azure-deployment-id': 'gpt4o-prod',
  'x-portkey-azure-api-version': '2024-10-21',
  authorization: 'Bearer az-key',
};

const chatBody = (extra: object = {}) => ({
  model: 'gpt-4o',
  messages: [{ role: 'user', content: 'Say hello to the regression suite' }],
  stream: true,
  ...extra,
});

/** OpenAI chunks, as OpenAI sends them with and without include_usage. */
function openaiStream(withUsage: boolean): string {
  const chunk = (extra: object) =>
    `data: ${JSON.stringify({
      id: 'chatcmpl-1',
      object: 'chat.completion.chunk',
      created: 1,
      model: 'gpt-4o-2024-08-06',
      system_fingerprint: 'fp_1',
      ...extra,
      ...(withUsage && { usage: null }),
    })}\n\n`;
  let out =
    chunk({
      choices: [
        {
          index: 0,
          delta: { role: 'assistant', content: '' },
          finish_reason: null,
        },
      ],
    }) +
    chunk({
      choices: [
        {
          index: 0,
          delta: { content: 'Hello, regression suite!' },
          finish_reason: null,
        },
      ],
    }) +
    chunk({ choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] });
  if (withUsage) {
    out += `data: ${JSON.stringify({
      id: 'chatcmpl-1',
      object: 'chat.completion.chunk',
      created: 1,
      model: 'gpt-4o-2024-08-06',
      system_fingerprint: 'fp_1',
      choices: [],
      usage: { prompt_tokens: 14, completion_tokens: 6, total_tokens: 20 },
    })}\n\n`;
  }
  return out + 'data: [DONE]\n\n';
}

/** Serves the stream that matches what the request asked for. */
function openaiUpstream(match: RegExp) {
  upstream(match, (call) =>
    sse(openaiStream(call.body?.stream_options?.include_usage === true))
  );
}

/** A response body delivered in small pieces, splitting events. */
function chunked(text: string, size = 7): Response {
  const bytes = new TextEncoder().encode(text);
  let i = 0;
  return new Response(
    new ReadableStream<Uint8Array>({
      pull(controller) {
        if (i >= bytes.length) return controller.close();
        controller.enqueue(bytes.slice(i, i + size));
        i += size;
      },
    }),
    { status: 200, headers: { 'content-type': 'text/event-stream' } }
  );
}

beforeEach(() => {
  resetHarness();
  delete process.env.PS_STREAM_INCLUDE_USAGE;
  delete process.env.PS_STREAM_INCLUDE_USAGE_HOSTS;
});

describe('include_usage injection (OpenAI)', () => {
  test('injected, and the client gets the stream it would have got without it', async () => {
    openaiUpstream(/api\.openai\.com\/v1\/chat\/completions$/);
    const res = await post(app, '/v1/chat/completions', chatBody(), OPENAI);
    const text = await res.text();
    expect(upstreamCalls[0].body.stream_options).toEqual({
      include_usage: true,
    });
    expect(text).toBe(openaiStream(false));
    const ev = await telemetryEvent();
    expect(ev).toMatchObject({
      stream: true,
      tokens_in: 14,
      tokens_out: 6,
      usage_source: 'provider',
      cost_source: 'pricing_table',
    });
  });

  test('byte-identical when events are split across reads', async () => {
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
      chunked(openaiStream(true), 5)
    );
    const res = await post(app, '/v1/chat/completions', chatBody(), OPENAI);
    expect(await res.text()).toBe(openaiStream(false));
    expect(await telemetryEvent()).toMatchObject({
      tokens_in: 14,
      tokens_out: 6,
      usage_source: 'provider',
    });
  });

  test('other stream_options are kept', async () => {
    openaiUpstream(/api\.openai\.com\/v1\/chat\/completions$/);
    await (
      await post(
        app,
        '/v1/chat/completions',
        chatBody({ stream_options: { include_obfuscation: false } }),
        OPENAI
      )
    ).text();
    expect(upstreamCalls[0].body.stream_options).toEqual({
      include_obfuscation: false,
      include_usage: true,
    });
    await telemetryEvent();
  });

  test('a client that asked for usage gets every chunk', async () => {
    openaiUpstream(/api\.openai\.com\/v1\/chat\/completions$/);
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody({ stream_options: { include_usage: true } }),
      OPENAI
    );
    expect(await res.text()).toBe(openaiStream(true));
    expect(await telemetryEvent()).toMatchObject({
      tokens_in: 14,
      tokens_out: 6,
      usage_source: 'provider',
    });
  });

  test('include_usage: false is respected; tokens are estimated', async () => {
    openaiUpstream(/api\.openai\.com\/v1\/chat\/completions$/);
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody({ stream_options: { include_usage: false } }),
      OPENAI
    );
    expect(await res.text()).toBe(openaiStream(false));
    expect(upstreamCalls[0].body.stream_options).toEqual({
      include_usage: false,
    });
    const ev = await telemetryEvent();
    expect(ev.usage_source).toBe('estimated');
    expect(ev.tokens_in).toBeGreaterThan(5);
    expect(ev.tokens_out).toBeGreaterThan(3);
    expect(ev.cost_source).toBe('pricing_table_estimated_tokens');
    const span = await (async () => {
      await until(() => otelSpans().length > 0, 'span');
      return otelSpans()[0];
    })();
    expect(spanAttrs(span)['ps.usage_source']).toBe('estimated');
  });

  test('PS_STREAM_INCLUDE_USAGE=false turns injection off', async () => {
    process.env.PS_STREAM_INCLUDE_USAGE = 'false';
    openaiUpstream(/api\.openai\.com\/v1\/chat\/completions$/);
    const res = await post(app, '/v1/chat/completions', chatBody(), OPENAI);
    expect(await res.text()).toBe(openaiStream(false));
    expect(upstreamCalls[0].body.stream_options).toBeUndefined();
    expect((await telemetryEvent()).usage_source).toBe('estimated');
  });

  test('non-streamed requests are left alone', async () => {
    upstream(
      /api\.openai\.com\/v1\/chat\/completions$/,
      () =>
        new Response(
          JSON.stringify({
            id: 'c',
            model: 'gpt-4o',
            choices: [{ message: { role: 'assistant', content: 'Hi' } }],
            usage: { prompt_tokens: 3, completion_tokens: 1 },
          }),
          { status: 200, headers: { 'content-type': 'application/json' } }
        )
    );
    await (
      await post(
        app,
        '/v1/chat/completions',
        chatBody({ stream: false }),
        OPENAI
      )
    ).text();
    expect(upstreamCalls[0].body.stream_options).toBeUndefined();
    const ev = await telemetryEvent();
    expect(ev.usage_source).toBeUndefined();
  });
});

describe('Azure OpenAI', () => {
  test('an API version older than 2024-07-01 is not sent stream_options', async () => {
    openaiUpstream(
      /regres\.openai\.azure\.com\/openai\/deployments\/gpt4o-prod\/chat\/completions/
    );
    const res = await post(app, '/v1/chat/completions', chatBody(), {
      ...AZURE,
      'x-portkey-azure-api-version': '2024-02-01',
    });
    expect(await res.text()).toBe(openaiStream(false));
    expect(upstreamCalls[0].body.stream_options).toBeUndefined();
    expect((await telemetryEvent()).usage_source).toBe('estimated');
  });

  test('injected and stripped; provider usage', async () => {
    openaiUpstream(
      /regres\.openai\.azure\.com\/openai\/deployments\/gpt4o-prod\/chat\/completions/
    );
    const res = await post(app, '/v1/chat/completions', chatBody(), AZURE);
    expect(await res.text()).toBe(openaiStream(false));
    expect(upstreamCalls[0].body.stream_options).toEqual({
      include_usage: true,
    });
    expect(await telemetryEvent()).toMatchObject({
      tokens_in: 14,
      tokens_out: 6,
      usage_source: 'provider',
    });
  });
});

const anthropicEvent = (type: string, data: object) =>
  `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`;
const ANTHROPIC_STREAM =
  anthropicEvent('message_start', {
    message: {
      id: 'msg_1',
      type: 'message',
      role: 'assistant',
      model: 'claude-sonnet-4-20250514',
      content: [],
      stop_reason: null,
      usage: { input_tokens: 21, output_tokens: 1 },
    },
  }) +
  anthropicEvent('content_block_start', {
    index: 0,
    content_block: { type: 'text', text: '' },
  }) +
  anthropicEvent('content_block_delta', {
    index: 0,
    delta: { type: 'text_delta', text: 'Hello there, regression suite.' },
  }) +
  anthropicEvent('content_block_stop', { index: 0 }) +
  anthropicEvent('message_delta', {
    delta: { stop_reason: 'end_turn', stop_sequence: null },
    usage: { output_tokens: 9 },
  }) +
  anthropicEvent('message_stop', {});

describe('providers that always send usage', () => {
  test('Anthropic Messages', async () => {
    upstream(/api\.anthropic\.com\/v1\/messages$/, () => sse(ANTHROPIC_STREAM));
    const res = await post(
      app,
      '/v1/messages',
      {
        model: 'claude-sonnet-4-20250514',
        max_tokens: 64,
        stream: true,
        messages: [{ role: 'user', content: 'Hi' }],
      },
      { 'x-portkey-provider': 'anthropic', 'x-api-key': 'sk-ant-regression' }
    );
    expect(await res.text()).toBe(ANTHROPIC_STREAM);
    expect(await telemetryEvent()).toMatchObject({
      tokens_in: 21,
      tokens_out: 9,
      usage_source: 'provider',
    });
  });

  test('Vertex AI Claude', async () => {
    upstream(/publishers\/anthropic\/models\/.*:streamRawPredict/, () =>
      sse(ANTHROPIC_STREAM)
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      {
        model: 'anthropic.claude-sonnet-4@20250514',
        max_tokens: 64,
        stream: true,
        messages: [{ role: 'user', content: 'Hi' }],
      },
      {
        'x-portkey-config': JSON.stringify({
          provider: 'vertex-ai',
          api_key: 'ya29.regression-token',
          vertex_project_id: 'ps-regression',
          vertex_region: 'us-east5',
        }),
      }
    );
    expect(res.status).toBe(200);
    await res.text();
    expect(await telemetryEvent()).toMatchObject({
      tokens_in: 21,
      tokens_out: 9,
      usage_source: 'provider',
    });
  });

  test('OpenAI-compatible host (Together) sends usage on its last chunk', async () => {
    const together =
      `data: ${JSON.stringify({
        id: 't1',
        object: 'chat.completion.chunk',
        model: 'meta-llama/Llama-3.3-70B-Instruct-Turbo',
        choices: [
          { index: 0, delta: { content: 'Hello!' }, finish_reason: null },
        ],
      })}\n\n` +
      `data: ${JSON.stringify({
        id: 't1',
        object: 'chat.completion.chunk',
        model: 'meta-llama/Llama-3.3-70B-Instruct-Turbo',
        choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
        usage: { prompt_tokens: 11, completion_tokens: 3, total_tokens: 14 },
      })}\n\n` +
      'data: [DONE]\n\n';
    upstream(/api\.together\.xyz\/v1\/chat\/completions$/, () => sse(together));
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody({ model: 'meta-llama/Llama-3.3-70B-Instruct-Turbo' }),
      { 'x-portkey-provider': 'together-ai', authorization: 'Bearer tg-key' }
    );
    await res.text();
    expect(upstreamCalls[0].body.stream_options).toBeUndefined();
    expect(await telemetryEvent()).toMatchObject({
      tokens_in: 11,
      tokens_out: 3,
      usage_source: 'provider',
    });
  });
});

describe('estimates', () => {
  test('a custom host is not sent stream_options unless listed', async () => {
    upstream(/vllm\.internal\.example\/v1\/chat\/completions$/, (call) =>
      sse(openaiStream(call.body?.stream_options?.include_usage === true))
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody({ model: 'llama-3' }),
      { ...OPENAI, 'x-portkey-custom-host': 'https://vllm.internal.example/v1' }
    );
    expect(res.status).toBe(200);
    expect(await res.text()).toBe(openaiStream(false));
    expect(upstreamCalls[0].body.stream_options).toBeUndefined();
    expect((await telemetryEvent()).usage_source).toBe('estimated');
  });

  test('a listed custom host (vLLM) gets include_usage', async () => {
    process.env.PS_STREAM_INCLUDE_USAGE_HOSTS = 'vllm.internal.example';
    upstream(/vllm\.internal\.example\/v1\/chat\/completions$/, (call) =>
      sse(openaiStream(call.body?.stream_options?.include_usage === true))
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody({ model: 'llama-3' }),
      { ...OPENAI, 'x-portkey-custom-host': 'https://vllm.internal.example/v1' }
    );
    expect(res.status).toBe(200);
    expect(await res.text()).toBe(openaiStream(false));
    expect(upstreamCalls[0].body.stream_options).toEqual({
      include_usage: true,
    });
    expect(await telemetryEvent()).toMatchObject({
      tokens_in: 14,
      tokens_out: 6,
      usage_source: 'provider',
    });
  });

  test('a cancelled stream reports estimated usage up to the cut', async () => {
    const long = 'word '.repeat(400);
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
      sse(
        `data: ${JSON.stringify({
          id: 'c',
          model: 'gpt-4o',
          choices: [
            { index: 0, delta: { content: long }, finish_reason: null },
          ],
          usage: null,
        })}\n\n` +
          `data: ${JSON.stringify({
            id: 'c',
            model: 'gpt-4o',
            choices: [
              { index: 0, delta: { content: long }, finish_reason: null },
            ],
            usage: null,
          })}\n\n`
      )
    );
    const res = await post(app, '/v1/chat/completions', chatBody(), OPENAI);
    const reader = res.body!.getReader();
    await reader.read();
    await reader.cancel();
    const ev = await telemetryEvent();
    expect(ev.usage_source).toBe('estimated');
    expect(ev.tokens_in).toBeGreaterThan(5);
    expect(ev.tokens_out).toBeGreaterThan(0);
    expect(ev.stream_cancelled || ev.stream_truncated).toBe(true);
  });

  test('a truncated Anthropic stream keeps input and estimates output', async () => {
    const cut = ANTHROPIC_STREAM.split('event: content_block_stop')[0];
    upstream(/api\.anthropic\.com\/v1\/messages$/, () => sse(cut));
    const res = await post(
      app,
      '/v1/messages',
      {
        model: 'claude-sonnet-4-20250514',
        max_tokens: 64,
        stream: true,
        messages: [{ role: 'user', content: 'Hi' }],
      },
      { 'x-portkey-provider': 'anthropic', 'x-api-key': 'sk-ant-regression' }
    );
    await res.text();
    const ev = await telemetryEvent();
    expect(ev).toMatchObject({
      tokens_in: 21,
      stream_truncated: true,
      usage_source: 'estimated',
    });
    // "Hello there, regression suite." is about 7 tokens, more than the 1
    // message_start reported.
    expect(ev.tokens_out).toBeGreaterThanOrEqual(5);
  });
});
