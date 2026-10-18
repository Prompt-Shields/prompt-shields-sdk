/**
 * Offline regression tests for the #74 providers `bedrock-mantle` (Amazon
 * Bedrock's OpenAI-compatible endpoint, SigV4 or Bedrock API key) and
 * `fal-ai` (image generation, proxy for video/audio) through the whole
 * gateway app: the request the provider receives, the unified response and
 * the ps-telemetry event (vendor, model, tokens).
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

const get = (path: string, headers: Record<string, string>) =>
  app.request(path, { method: 'GET', headers });

describe('Bedrock Mantle', () => {
  const HOST = 'https://bedrock-mantle.us-east-1.api.aws';
  const OSS = 'openai.gpt-oss-120b';

  const sigv4Config = (extra: object = {}) =>
    JSON.stringify({
      provider: 'bedrock-mantle',
      aws_access_key_id: 'AKIAREGRESSIONTEST',
      aws_secret_access_key: 'regression-secret',
      aws_session_token: 'regression-session',
      aws_region: 'us-east-1',
      ...extra,
    });

  const apiKeyConfig = (extra: object = {}) =>
    JSON.stringify({
      provider: 'bedrock-mantle',
      api_key: 'bedrock-api-key',
      aws_region: 'us-east-1',
      ...extra,
    });

  const chatBody = (extra: object = {}) => ({
    model: OSS,
    messages: [{ role: 'user', content: 'Say hi' }],
    max_tokens: 16,
    ...extra,
  });

  const completion = (model = OSS) => ({
    id: 'chatcmpl-1',
    object: 'chat.completion',
    created: 1,
    model,
    choices: [
      {
        index: 0,
        message: { role: 'assistant', content: 'Hi' },
        finish_reason: 'stop',
      },
    ],
    usage: { prompt_tokens: 9, completion_tokens: 1, total_tokens: 10 },
  });

  function expectSigned(call: any, method: string) {
    expect(call.method).toBe(method);
    expect(call.headers.authorization).toMatch(
      /^AWS4-HMAC-SHA256 Credential=AKIAREGRESSIONTEST\/\d{8}\/us-east-1\/bedrock-mantle\/aws4_request, SignedHeaders=/
    );
    expect(call.headers['x-amz-date']).toMatch(/^\d{8}T\d{6}Z$/);
    expect(call.headers['x-amz-security-token']).toBe('regression-session');
  }

  test('chat, SigV4-signed, on /v1', async () => {
    upstream(
      /bedrock-mantle\.us-east-1\.api\.aws\/v1\/chat\/completions$/,
      () => json(completion())
    );
    const res = await post(app, '/v1/chat/completions', chatBody(), {
      'x-portkey-config': sigv4Config(),
    });
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.choices[0].message.content).toBe('Hi');
    expect(body.provider).toBe('bedrock-mantle');

    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].url).toBe(`${HOST}/v1/chat/completions`);
    expectSigned(upstreamCalls[0], 'POST');
    expect(upstreamCalls[0].body).toMatchObject({
      model: OSS,
      messages: [{ role: 'user', content: 'Say hi' }],
    });

    expect(await telemetryEvent()).toMatchObject({
      vendor: 'bedrock-mantle',
      api_surface: 'chat',
      model: OSS,
      tokens_in: 9,
      tokens_out: 1,
    });
  });

  test('chat for an OpenAI GPT model uses /openai/v1 and a Bedrock API key', async () => {
    upstream(/\/openai\/v1\/chat\/completions$/, () =>
      json(completion('openai.gpt-5.5'))
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody({ model: 'openai.gpt-5.5' }),
      { 'x-portkey-config': apiKeyConfig() }
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].url).toBe(`${HOST}/openai/v1/chat/completions`);
    expect(upstreamCalls[0].headers.authorization).toBe(
      'Bearer bedrock-api-key'
    );
    expect(upstreamCalls[0].headers['x-amz-date']).toBeUndefined();
  });

  test('bedrock_mantle_api_path overrides the base path', async () => {
    upstream(/\/openai\/v1\/chat\/completions$/, () => json(completion()));
    const res = await post(app, '/v1/chat/completions', chatBody(), {
      'x-portkey-config': apiKeyConfig({
        bedrock_mantle_api_path: 'openai/v1',
      }),
    });
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].url).toBe(`${HOST}/openai/v1/chat/completions`);
  });

  test('the base path and Anthropic headers also come from request headers', async () => {
    upstream(/\/openai\/v1\/chat\/completions$/, () => json(completion()));
    const res = await post(app, '/v1/chat/completions', chatBody(), {
      'x-portkey-provider': 'bedrock-mantle',
      authorization: 'Bearer bedrock-api-key',
      'x-portkey-aws-region': 'us-east-1',
      'x-portkey-bedrock-mantle-api-path': '/openai/v1',
    });
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].url).toBe(`${HOST}/openai/v1/chat/completions`);
    expect(upstreamCalls[0].headers.authorization).toBe(
      'Bearer bedrock-api-key'
    );
  });

  test('streaming chat with x-portkey-* headers', async () => {
    const chunk = (delta: object, extra: object = {}) =>
      `data: ${JSON.stringify({
        id: 'c1',
        object: 'chat.completion.chunk',
        created: 1,
        model: OSS,
        choices: [{ index: 0, delta, finish_reason: null }],
        ...extra,
      })}\n\n`;
    upstream(
      /bedrock-mantle\.us-east-1\.api\.aws\/v1\/chat\/completions$/,
      () =>
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
      chatBody({ stream: true, stream_options: { include_usage: true } }),
      {
        'x-portkey-provider': 'bedrock-mantle',
        'x-portkey-aws-access-key-id': 'AKIAREGRESSIONTEST',
        'x-portkey-aws-secret-access-key': 'regression-secret',
        'x-portkey-aws-session-token': 'regression-session',
        'x-portkey-aws-region': 'us-east-1',
      }
    );
    const text = await res.text();
    expect(text).toContain('"content":"Hi"');
    expectSigned(upstreamCalls[0], 'POST');
    const ev = await telemetryEvent();
    expect(ev.stream).toBe(true);
    expect(ev).toMatchObject({
      vendor: 'bedrock-mantle',
      tokens_in: 9,
      tokens_out: 1,
    });
  });

  test('/v1/responses is native', async () => {
    upstream(/bedrock-mantle\.us-east-1\.api\.aws\/v1\/responses$/, () =>
      json({
        id: 'resp_1',
        object: 'response',
        created_at: 1,
        status: 'completed',
        model: OSS,
        output: [
          {
            type: 'message',
            id: 'msg_1',
            status: 'completed',
            role: 'assistant',
            content: [{ type: 'output_text', text: 'Hi', annotations: [] }],
          },
        ],
        usage: { input_tokens: 7, output_tokens: 2, total_tokens: 9 },
      })
    );
    const res = await post(
      app,
      '/v1/responses',
      { model: OSS, input: 'Say hi', reasoning: { effort: 'low' } },
      { 'x-portkey-config': sigv4Config() }
    );
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.output[0].content[0].text).toBe('Hi');
    expect(upstreamCalls[0].url).toBe(`${HOST}/v1/responses`);
    expect(upstreamCalls[0].body).toMatchObject({
      model: OSS,
      input: 'Say hi',
      reasoning: { effort: 'low' },
    });
    expectSigned(upstreamCalls[0], 'POST');
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'bedrock-mantle',
      api_surface: 'responses',
      model: OSS,
      tokens_in: 7,
      tokens_out: 2,
    });
  });

  test('/v1/messages for Claude goes to /anthropic/v1/messages natively', async () => {
    upstream(/\/anthropic\/v1\/messages$/, () =>
      json({
        id: 'msg_1',
        type: 'message',
        role: 'assistant',
        model: 'anthropic.claude-opus-4-7',
        content: [{ type: 'text', text: 'Hi' }],
        stop_reason: 'end_turn',
        usage: { input_tokens: 8, output_tokens: 2 },
      })
    );
    const res = await post(
      app,
      '/v1/messages',
      {
        model: 'anthropic.claude-opus-4-7',
        max_tokens: 64,
        thinking: { type: 'enabled', budget_tokens: 32 },
        safeguards: { level: 'standard' },
        messages: [{ role: 'user', content: 'Say hi' }],
      },
      {
        'x-portkey-config': sigv4Config({
          anthropic_beta:
            'interleaved-thinking-2025-05-14,extended-cache-ttl-2025-04-11,redact-thinking-2026-01-01',
        }),
      }
    );
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body).toMatchObject({ type: 'message', content: [{ text: 'Hi' }] });
    const call = upstreamCalls[0];
    expect(call.url).toBe(`${HOST}/anthropic/v1/messages`);
    expectSigned(call, 'POST');
    expect(call.headers['anthropic-version']).toBe('2023-06-01');
    expect(call.headers['anthropic-beta']).toBe(
      'interleaved-thinking-2025-05-14'
    );
    expect(call.body).toMatchObject({
      thinking: { type: 'enabled', budget_tokens: 32 },
      safeguards: { level: 'standard' },
    });
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'bedrock-mantle',
      api_surface: 'messages',
      model: 'anthropic.claude-opus-4-7',
      tokens_in: 8,
      tokens_out: 2,
    });
  });

  test('/v1/messages for a non-Claude model goes through Chat Completions', async () => {
    upstream(/\/v1\/chat\/completions$/, () => json(completion()));
    const res = await post(
      app,
      '/v1/messages',
      {
        model: OSS,
        max_tokens: 16,
        messages: [{ role: 'user', content: 'Say hi' }],
      },
      { 'x-portkey-config': apiKeyConfig() }
    );
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.content[0].text).toBe('Hi');
    expect(upstreamCalls[0].url).toBe(`${HOST}/v1/chat/completions`);
  });

  test('model listing is a signed GET to /v1/models', async () => {
    upstream(/bedrock-mantle\.us-east-1\.api\.aws\/v1\/models$/, () =>
      json({
        object: 'list',
        data: [{ id: OSS, object: 'model', created: 1, owned_by: 'openai' }],
      })
    );
    const res = await get('/v1/models', { 'x-portkey-config': sigv4Config() });
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.data.map((m: any) => m.id)).toEqual([OSS]);
    expect(upstreamCalls[0].url).toBe(`${HOST}/v1/models`);
    expectSigned(upstreamCalls[0], 'GET');
    expect(upstreamCalls[0].headers['content-type']).toBeUndefined();
  });

  test.each([
    ['no region', { aws_region: undefined }],
    ['an invalid region', { aws_region: 'x@evil.example/' }],
    ['a host-changing region', { aws_region: 'us-east-1.evil.example' }],
    ['a custom host', { custom_host: 'https://mantle.example.com' }],
    [
      'a look-alike custom host',
      { custom_host: 'https://bedrock-mantle.us-east-1.api.aws.evil.example' },
    ],
    [
      'a userinfo custom host',
      { custom_host: 'https://bedrock-mantle.us-east-1.api.aws@evil.example' },
    ],
    [
      'a custom port on the real host',
      { custom_host: 'https://bedrock-mantle.us-east-1.api.aws:8443' },
    ],
    [
      'plain http on the real host',
      { custom_host: 'http://bedrock-mantle.us-east-1.api.aws' },
    ],
    [
      'another region’s host',
      { custom_host: 'https://bedrock-mantle.us-west-2.api.aws' },
    ],
    ['an unknown api path', { bedrock_mantle_api_path: '../evil' }],
  ])('rejects a SigV4 request with %s before calling AWS', async (_, extra) => {
    upstream(/./, () => json(completion()));
    const res = await post(app, '/v1/chat/completions', chatBody(), {
      'x-portkey-config': sigv4Config(extra),
    });
    expect(res.status).toBeGreaterThanOrEqual(400);
    expect(res.status).toBeLessThan(500);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('serviceRole is refused without AWS_AMBIENT_CREDENTIALS_ENABLED', async () => {
    const prev = process.env.AWS_AMBIENT_CREDENTIALS_ENABLED;
    delete process.env.AWS_AMBIENT_CREDENTIALS_ENABLED;
    try {
      upstream(/./, () => json(completion()));
      const res = await post(app, '/v1/chat/completions', chatBody(), {
        'x-portkey-provider': 'bedrock-mantle',
        'x-portkey-aws-auth-type': 'serviceRole',
        'x-portkey-aws-region': 'us-east-1',
      });
      expect(res.status).toBeGreaterThanOrEqual(400);
      expect(res.status).toBeLessThan(500);
      expect(await res.text()).toMatch(/serviceRole is disabled/);
      expect(upstreamCalls).toHaveLength(0);
    } finally {
      if (prev !== undefined)
        process.env.AWS_AMBIENT_CREDENTIALS_ENABLED = prev;
    }
  });
});

describe('fal.ai', () => {
  const MODEL = 'fal-ai/flux/schnell';
  const headers = {
    'x-portkey-provider': 'fal-ai',
    authorization: 'Bearer fal-regression-key',
  };

  test('/v1/images/generations maps to fal.run/<model>', async () => {
    upstream(/fal\.run\/fal-ai\/flux\/schnell$/, () =>
      json({
        images: [
          {
            url: 'https://v3.fal.media/files/a.jpeg',
            width: 1024,
            height: 768,
            content_type: 'image/jpeg',
          },
        ],
        seed: 42,
        has_nsfw_concepts: [false],
        prompt: 'a lighthouse',
        timings: { inference: 0.4 },
      })
    );
    const res = await post(
      app,
      '/v1/images/generations',
      {
        model: MODEL,
        prompt: 'a lighthouse',
        n: 1,
        size: '1024x768',
        seed: 42,
      },
      headers
    );
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body).toMatchObject({
      provider: 'fal-ai',
      data: [{ url: 'https://v3.fal.media/files/a.jpeg' }],
    });
    expect(typeof body.created).toBe('number');

    const call = upstreamCalls[0];
    expect(call.url).toBe(`https://fal.run/${MODEL}`);
    expect(call.headers.authorization).toBe('Key fal-regression-key');
    expect(call.body).toEqual({
      prompt: 'a lighthouse',
      num_images: 1,
      image_size: { width: 1024, height: 768 },
      seed: 42,
    });
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'fal-ai',
      api_surface: 'images',
      modality: 'image',
      model: MODEL,
    });
  });

  test('b64_json asks for inline images and returns their base64', async () => {
    upstream(/fal\.run\//, () =>
      json({ images: [{ url: 'data:image/png;base64,iVBORw0KGgo=' }] })
    );
    const res = await post(
      app,
      '/v1/images/generations',
      { model: MODEL, prompt: 'x', response_format: 'b64_json' },
      { ...headers, authorization: 'Key fal-regression-key' }
    );
    const body: any = await res.json();
    expect(body.data).toEqual([{ b64_json: 'iVBORw0KGgo=' }]);
    expect(upstreamCalls[0].body.sync_mode).toBe(true);
    expect(upstreamCalls[0].headers.authorization).toBe(
      'Key fal-regression-key'
    );
  });

  test('a fal validation error becomes a gateway error', async () => {
    upstream(/fal\.run\//, () =>
      json(
        {
          detail: [
            { loc: ['body', 'prompt'], msg: 'field required', type: 'missing' },
          ],
        },
        422
      )
    );
    const res = await post(
      app,
      '/v1/images/generations',
      { model: MODEL, prompt: 'x' },
      headers
    );
    expect(res.status).toBe(422);
    const body: any = await res.json();
    expect(body.error.message).toContain('body.prompt: field required');
    expect(body.provider).toBe('fal-ai');
  });

  test.each([
    ['no model', undefined],
    ['a single-segment model', 'flux'],
    ['a traversal', 'fal-ai/../../evil'],
    ['a host in the model', '@evil.example/x'],
    ['an absolute URL', 'https://evil.example/x'],
    ['a query string', 'fal-ai/flux?x=1'],
  ])('rejects %s before calling fal', async (_, model) => {
    upstream(/./, () => json({ images: [] }));
    const res = await post(
      app,
      '/v1/images/generations',
      { model, prompt: 'x' },
      headers
    );
    expect(res.status).toBe(400);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('video models are reached through the proxy with Key auth', async () => {
    upstream(/fal\.run\/fal-ai\/kling-video\/v2\/master\/text-to-video$/, () =>
      json({ video: { url: 'https://v3.fal.media/files/v.mp4' } })
    );
    const res = await post(
      app,
      '/v1/fal-ai/kling-video/v2/master/text-to-video',
      { prompt: 'a lighthouse at dusk' },
      headers
    );
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      video: { url: 'https://v3.fal.media/files/v.mp4' },
    });
    expect(upstreamCalls[0].url).toBe(
      'https://fal.run/fal-ai/kling-video/v2/master/text-to-video'
    );
    expect(upstreamCalls[0].headers.authorization).toBe(
      'Key fal-regression-key'
    );
  });
});
