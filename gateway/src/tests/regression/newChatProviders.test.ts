/**
 * Offline regression tests for the #74 slice-2 providers through the whole
 * gateway app: Claude Platform on AWS (`claude-platform-aws`, SigV4 or API
 * key) and the Meta Llama API (`meta`). Each case checks the request the
 * provider receives, the unified response, and the ps-telemetry event
 * (vendor, served model, tokens).
 */
jest.mock('jose', () => ({}));

import {
  json,
  post,
  resetHarness,
  sse,
  telemetryEvent,
  upstream,
  upstreamCalls,
} from './harness';
import app from '../../index';

beforeEach(() => resetHarness());

const chatBody = (extra: object = {}) => ({
  model: 'claude-sonnet-5-5',
  messages: [{ role: 'user', content: 'Say hi' }],
  max_tokens: 16,
  ...extra,
});

async function readSSE(res: Response) {
  const text = await res.text();
  return text
    .split('\n')
    .filter((l) => l.startsWith('data: ') && l !== 'data: [DONE]')
    .map((l) => JSON.parse(l.slice(6)));
}

const get = (path: string, headers: Record<string, string>) =>
  app.request(path, { method: 'GET', headers });

describe('Claude Platform on AWS', () => {
  const HOST = 'https://aws-external-anthropic.us-west-2.api.aws';
  const WORKSPACE = 'wrkspc_regression';

  const sigv4Config = (extra: object = {}) =>
    JSON.stringify({
      provider: 'claude-platform-aws',
      aws_access_key_id: 'AKIAREGRESSIONTEST',
      aws_secret_access_key: 'regression-secret',
      aws_session_token: 'regression-session',
      aws_region: 'us-west-2',
      anthropic_aws_workspace_id: WORKSPACE,
      ...extra,
    });

  const apiKeyConfig = JSON.stringify({
    provider: 'claude-platform-aws',
    api_key: 'aws-claude-key',
    aws_region: 'us-west-2',
    anthropic_aws_workspace_id: WORKSPACE,
  });

  const message = {
    id: 'msg_1',
    type: 'message',
    role: 'assistant',
    model: 'claude-sonnet-5-5',
    content: [{ type: 'text', text: 'Hi' }],
    stop_reason: 'end_turn',
    usage: { input_tokens: 8, output_tokens: 2 },
  };

  function expectSigned(call: any, method: string) {
    expect(call.method).toBe(method);
    expect(call.headers.authorization).toMatch(
      /^AWS4-HMAC-SHA256 Credential=AKIAREGRESSIONTEST\/\d{8}\/us-west-2\/aws-external-anthropic\/aws4_request, SignedHeaders=[^,]*anthropic-workspace-id/
    );
    expect(call.headers['x-amz-date']).toMatch(/^\d{8}T\d{6}Z$/);
    expect(call.headers['x-amz-security-token']).toBe('regression-session');
    expect(call.headers['x-api-key']).toBeUndefined();
    expect(call.headers['anthropic-workspace-id']).toBe(WORKSPACE);
    expect(call.headers['anthropic-version']).toBe('2023-06-01');
  }

  test('chat, SigV4-signed', async () => {
    upstream(/aws-external-anthropic\.us-west-2\.api\.aws\/v1\/messages$/, () =>
      json(message)
    );
    const res = await post(app, '/v1/chat/completions', chatBody(), {
      'x-portkey-config': sigv4Config(),
    });
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.choices[0].message.content).toBe('Hi');
    expect(body.choices[0].finish_reason).toBe('stop');

    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].url).toBe(`${HOST}/v1/messages`);
    expectSigned(upstreamCalls[0], 'POST');
    expect(upstreamCalls[0].body).toMatchObject({
      model: 'claude-sonnet-5-5',
      max_tokens: 16,
      messages: [{ role: 'user', content: 'Say hi' }],
    });

    expect(await telemetryEvent()).toMatchObject({
      vendor: 'claude-platform-aws',
      api_surface: 'chat',
      model: 'claude-sonnet-5-5',
      tokens_in: 8,
      tokens_out: 2,
    });
  });

  test('streaming chat with an API key', async () => {
    const ev = (type: string, data: object) =>
      `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`;
    upstream(/aws-external-anthropic\.us-west-2\.api\.aws\/v1\/messages$/, () =>
      sse(
        ev('message_start', {
          message: { ...message, content: [], usage: { input_tokens: 8 } },
        }) +
          ev('content_block_start', {
            index: 0,
            content_block: { type: 'text', text: '' },
          }) +
          ev('content_block_delta', {
            index: 0,
            delta: { type: 'text_delta', text: 'Hi' },
          }) +
          ev('content_block_stop', { index: 0 }) +
          ev('message_delta', {
            delta: { stop_reason: 'end_turn' },
            usage: { output_tokens: 2 },
          }) +
          ev('message_stop', {})
      )
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody({ stream: true }),
      { 'x-portkey-config': apiKeyConfig }
    );
    const chunks = await readSSE(res);
    expect(
      chunks.map((c) => c.choices?.[0]?.delta?.content ?? '').join('')
    ).toBe('Hi');

    const call = upstreamCalls[0];
    expect(call.headers['x-api-key']).toBe('aws-claude-key');
    expect(call.headers.authorization).toBeUndefined();
    expect(call.headers['anthropic-workspace-id']).toBe(WORKSPACE);

    const event = await telemetryEvent();
    expect(event.stream).toBe(true);
    expect(event.stream_truncated).toBeUndefined();
    expect(event).toMatchObject({
      vendor: 'claude-platform-aws',
      tokens_in: 8,
      tokens_out: 2,
    });
  });

  test('/v1/messages with x-portkey-* headers', async () => {
    upstream(/aws-external-anthropic\.us-west-2\.api\.aws\/v1\/messages$/, () =>
      json(message)
    );
    const res = await post(app, '/v1/messages', chatBody(), {
      'x-portkey-provider': 'claude-platform-aws',
      'x-portkey-aws-access-key-id': 'AKIAREGRESSIONTEST',
      'x-portkey-aws-secret-access-key': 'regression-secret',
      'x-portkey-aws-session-token': 'regression-session',
      'x-portkey-aws-region': 'us-west-2',
      'anthropic-workspace-id': WORKSPACE,
      'anthropic-version': '2023-06-01',
    });
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.content[0].text).toBe('Hi');
    expectSigned(upstreamCalls[0], 'POST');

    expect(await telemetryEvent()).toMatchObject({
      vendor: 'claude-platform-aws',
      tokens_in: 8,
      tokens_out: 2,
    });
  });

  test('/v1/messages is native: Messages-only fields reach the provider', async () => {
    upstream(/aws-external-anthropic\.us-west-2\.api\.aws\/v1\/messages$/, () =>
      json(message)
    );
    const res = await post(
      app,
      '/v1/messages',
      {
        model: 'claude-sonnet-5-5',
        max_tokens: 1024,
        thinking: { type: 'enabled', budget_tokens: 512 },
        messages: [
          {
            role: 'user',
            content: [
              {
                type: 'text',
                text: 'Say hi',
                cache_control: { type: 'ephemeral' },
              },
            ],
          },
        ],
      },
      { 'x-portkey-config': sigv4Config() }
    );
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body).toMatchObject({ type: 'message', content: [{ text: 'Hi' }] });
    expect(upstreamCalls[0].body).toMatchObject({
      thinking: { type: 'enabled', budget_tokens: 512 },
      messages: [{ content: [{ cache_control: { type: 'ephemeral' } }] }],
    });
  });

  test('streaming /v1/messages passes events through and reports usage', async () => {
    const ev = (type: string, data: object) =>
      `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`;
    upstream(/aws-external-anthropic\.us-west-2\.api\.aws\/v1\/messages$/, () =>
      sse(
        ev('message_start', {
          message: { ...message, content: [], usage: { input_tokens: 8 } },
        }) +
          ev('content_block_start', {
            index: 0,
            content_block: { type: 'text', text: '' },
          }) +
          ev('content_block_delta', {
            index: 0,
            delta: { type: 'text_delta', text: 'Hi' },
          }) +
          ev('content_block_stop', { index: 0 }) +
          ev('message_delta', {
            delta: { stop_reason: 'end_turn' },
            usage: { output_tokens: 2 },
          }) +
          ev('message_stop', {})
      )
    );
    const res = await post(app, '/v1/messages', chatBody({ stream: true }), {
      'x-portkey-config': apiKeyConfig,
    });
    const text = await res.text();
    expect(text).toContain('event: message_start');
    expect(text).toContain('"text":"Hi"');
    expect(upstreamCalls[0].headers['x-api-key']).toBe('aws-claude-key');
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'claude-platform-aws',
      stream: true,
      tokens_in: 8,
      tokens_out: 2,
    });
  });

  test('count_tokens is a signed POST to /v1/messages/count_tokens', async () => {
    upstream(
      /aws-external-anthropic\.us-west-2\.api\.aws\/v1\/messages\/count_tokens$/,
      () => json({ input_tokens: 11 })
    );
    const res = await post(app, '/v1/messages/count_tokens', chatBody(), {
      'x-portkey-config': sigv4Config(),
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ input_tokens: 11 });
    expect(upstreamCalls[0].url).toBe(`${HOST}/v1/messages/count_tokens`);
    expectSigned(upstreamCalls[0], 'POST');
  });

  test('model listing is a signed GET to /v1/models', async () => {
    upstream(/aws-external-anthropic\.us-west-2\.api\.aws\/v1\/models$/, () =>
      json({
        data: [
          {
            type: 'model',
            id: 'claude-sonnet-5-5',
            display_name: 'Claude Sonnet 5.5',
            created_at: '2026-05-01T00:00:00Z',
          },
        ],
        has_more: false,
        first_id: 'claude-sonnet-5-5',
        last_id: 'claude-sonnet-5-5',
      })
    );
    const res = await get('/v1/models', { 'x-portkey-config': sigv4Config() });
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body).toMatchObject({
      object: 'list',
      data: [{ id: 'claude-sonnet-5-5', object: 'model' }],
    });
    expect(upstreamCalls[0].url).toBe(`${HOST}/v1/models`);
    expectSigned(upstreamCalls[0], 'GET');
    expect(upstreamCalls[0].headers['content-type']).toBeUndefined();
  });

  test.each([
    ['no region', { aws_region: undefined }],
    ['an invalid region', { aws_region: 'x@evil.example/' }],
    ['no workspace id', { anthropic_aws_workspace_id: undefined }],
    ['a custom host', { custom_host: 'https://claude.example.com/v1' }],
  ])('rejects a request with %s before calling AWS', async (_, extra) => {
    const res = await post(app, '/v1/chat/completions', chatBody(), {
      'x-portkey-config': sigv4Config(extra),
    });
    expect(res.status).toBe(400);
    expect(upstreamCalls).toHaveLength(0);
  });
});

describe('Meta Llama API', () => {
  const headers = {
    'x-portkey-provider': 'meta',
    authorization: 'Bearer llama-regression',
  };
  const MODEL = 'Llama-4-Maverick-17B-128E-Instruct-FP8';

  test('chat', async () => {
    upstream(/api\.llama\.com\/compat\/v1\/chat\/completions$/, () =>
      json({
        id: 'chatcmpl-1',
        object: 'chat.completion',
        created: 1,
        model: MODEL,
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
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody({ model: MODEL }),
      headers
    );
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.choices[0].message.content).toBe('Hi');
    expect(body.provider).toBe('meta');
    expect(upstreamCalls[0].headers.authorization).toBe(
      'Bearer llama-regression'
    );
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'meta',
      api_surface: 'chat',
      model: MODEL,
      tokens_in: 9,
      tokens_out: 1,
    });
  });

  test('streaming chat', async () => {
    const chunk = (delta: object, extra: object = {}) =>
      `data: ${JSON.stringify({
        id: 'c1',
        object: 'chat.completion.chunk',
        created: 1,
        model: MODEL,
        choices: [{ index: 0, delta, finish_reason: null }],
        ...extra,
      })}\n\n`;
    upstream(/api\.llama\.com\/compat\/v1\/chat\/completions$/, () =>
      sse(
        chunk({ role: 'assistant', content: 'Hi' }) +
          chunk(
            {},
            {
              usage: {
                prompt_tokens: 9,
                completion_tokens: 1,
                total_tokens: 10,
              },
            }
          ) +
          'data: [DONE]\n\n'
      )
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody({
        model: MODEL,
        stream: true,
        stream_options: { include_usage: true },
      }),
      headers
    );
    expect(res.headers.get('content-type')).toContain('text/event-stream');
    const chunks = await readSSE(res);
    expect(chunks.map((c) => c.choices[0]?.delta?.content ?? '').join('')).toBe(
      'Hi'
    );
    expect(upstreamCalls[0].body.stream).toBe(true);
    const ev = await telemetryEvent();
    expect(ev.stream).toBe(true);
    expect(ev).toMatchObject({ vendor: 'meta', tokens_in: 9, tokens_out: 1 });
  });

  test('model listing', async () => {
    upstream(/api\.llama\.com\/compat\/v1\/models$/, () =>
      json({
        object: 'list',
        data: [{ id: MODEL, object: 'model', created: 1, owned_by: 'Meta' }],
      })
    );
    const res = await get('/v1/models', headers);
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.data.map((m: any) => m.id)).toEqual([MODEL]);
    expect(upstreamCalls[0].url).toBe('https://api.llama.com/compat/v1/models');
    expect(upstreamCalls[0].headers.authorization).toBe(
      'Bearer llama-regression'
    );
  });
});
