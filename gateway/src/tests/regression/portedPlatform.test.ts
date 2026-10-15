/**
 * Regression tests, through the whole gateway app and offline, for the
 * ported/hardened request-pipeline features that routing and telemetry sit
 * on (#69): new providers (#57), keyless upstream auth (#76), the host
 * credential gates of the Bedrock/Vertex ports (#108, #76), the response
 * cache scope, and client stream cancellation.
 */
jest.mock('jose', () => ({}));

import {
  events,
  json,
  post,
  resetHarness,
  telemetryEvent,
  until,
  upstream,
  upstreamCalls,
} from './harness';
import app from '../../index';

const chat = (model: string, extra: object = {}) => ({
  model,
  messages: [{ role: 'user', content: 'Say hi' }],
  ...extra,
});

const completion = (model: string) => ({
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
  usage: { prompt_tokens: 4, completion_tokens: 1, total_tokens: 5 },
});

beforeEach(() => resetHarness());

describe('new providers (#57)', () => {
  test('databricks: workspace serving endpoint, routed, with telemetry', async () => {
    upstream(
      /^https:\/\/dbc-a1b2c3d4-e5f6\.cloud\.databricks\.com\/serving-endpoints\/gpt-4o-mini\/invocations$/,
      () => json(completion('databricks-served'))
    );
    const res = await post(app, '/v1/chat/completions', chat('auto'), {
      'x-portkey-provider': 'databricks',
      'x-portkey-databricks-workspace': 'dbc-a1b2c3d4-e5f6',
      authorization: 'Bearer dapi-regression',
      'x-ps-route': 'cheap',
    });
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].headers.authorization).toBe(
      'Bearer dapi-regression'
    );
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'databricks',
      requested_model: 'auto',
      served_model: 'databricks-served',
      tokens_in: 4,
      tokens_out: 1,
    });
  });

  test('databricks: a workspace pointing at another host is refused', async () => {
    const res = await post(app, '/v1/chat/completions', chat('m'), {
      'x-portkey-provider': 'databricks',
      'x-portkey-databricks-workspace': '169.254.169.254/latest#',
      authorization: 'Bearer dapi-regression',
    });
    expect(res.status).toBeGreaterThanOrEqual(400);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('latitude: fixed vendor host, response tagged with the provider', async () => {
    upstream(/^https:\/\/api\.lsh\.ai\/v1\/chat\/completions$/, () =>
      json(completion('qwen-2.5-7b'))
    );
    const res = await post(app, '/v1/chat/completions', chat('qwen-2.5-7b'), {
      'x-portkey-provider': 'latitude',
      authorization: 'Bearer lat-regression',
    });
    expect(res.status).toBe(200);
    expect(((await res.json()) as any).provider).toBe('latitude');
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'latitude',
      model: 'qwen-2.5-7b',
      tokens_in: 4,
    });
  });
});

describe('keyless upstream auth (#76)', () => {
  test('OAuth client credentials: the token, not the static key, goes upstream', async () => {
    upstream(/^https:\/\/idp\.example\.com\/oauth2\/token$/, () =>
      json({ access_token: 'oauth-access-1', expires_in: 3600 })
    );
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
      json(completion('gpt-4o-mini'))
    );
    const res = await post(app, '/v1/chat/completions', chat('gpt-4o-mini'), {
      'x-portkey-provider': 'openai',
      authorization: 'Bearer static-key-ignored',
      'x-portkey-oauth-token-url': 'https://idp.example.com/oauth2/token',
      'x-portkey-oauth-client-id': 'client-regression',
      'x-portkey-oauth-client-secret': 'oauth-secret-regression',
    });
    expect(res.status).toBe(200);
    const [tokenCall, llmCall] = upstreamCalls;
    expect(tokenCall.url).toBe('https://idp.example.com/oauth2/token');
    expect(String(tokenCall.body)).toContain('grant_type=client_credentials');
    expect(llmCall.headers.authorization).toBe('Bearer oauth-access-1');

    // The client secret never reaches telemetry.
    const ev = await telemetryEvent();
    expect(JSON.stringify(ev)).not.toContain('oauth-secret-regression');
  });

  test('a partial OAuth set is a 400 with no token request', async () => {
    const res = await post(app, '/v1/chat/completions', chat('gpt-4o-mini'), {
      'x-portkey-provider': 'openai',
      'x-portkey-oauth-token-url': 'https://idp.example.com/oauth2/token',
    });
    expect(res.status).toBe(400);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('Vertex workload identity is 403 unless the host enables it', async () => {
    const res = await post(
      app,
      '/v1/chat/completions',
      chat('gemini-2.0-flash'),
      {
        'x-portkey-provider': 'vertex-ai',
        'x-portkey-vertex-auth-type': 'workload',
        'x-portkey-vertex-project-id': 'p',
        'x-portkey-vertex-region': 'us-central1',
      }
    );
    expect(res.status).toBe(403);
    // No metadata-server request, no Vertex request.
    expect(upstreamCalls).toHaveLength(0);
  });

  test('Bedrock serviceRole is 403 unless the host enables it (#108)', async () => {
    const res = await post(
      app,
      '/v1/chat/completions',
      chat('anthropic.claude-3-5-haiku-20241022-v1:0'),
      {
        'x-portkey-provider': 'bedrock',
        'x-portkey-aws-auth-type': 'serviceRole',
        'x-portkey-aws-region': 'us-east-1',
      }
    );
    expect(res.status).toBe(403);
    expect(upstreamCalls).toHaveLength(0);
  });
});

describe('response cache scope', () => {
  const cached = (auth: string) =>
    post(app, '/v1/chat/completions', chat('gpt-4o-mini'), {
      'x-portkey-provider': 'openai',
      authorization: auth,
      'x-ps-cache': 'on',
    });

  test('the same caller hits; a caller with another key does not', async () => {
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, (call) =>
      json(completion(`served-for-${call.headers.authorization.slice(-1)}`))
    );
    const first = await cached('Bearer sk-scope-A');
    expect(first.headers.get('x-portkey-cache-status')).toBe('MISS');
    await first.text();

    const again = await cached('Bearer sk-scope-A');
    expect(again.headers.get('x-portkey-cache-status')).toBe('HIT');
    expect(((await again.json()) as any).model).toBe('served-for-A');

    const other = await cached('Bearer sk-scope-B');
    expect(other.headers.get('x-portkey-cache-status')).toBe('MISS');
    expect(((await other.json()) as any).model).toBe('served-for-B');

    // Two upstream calls: the HIT was served from cache.
    expect(upstreamCalls).toHaveLength(2);
  });
});

describe('client stream cancellation', () => {
  test('cancelling the client stream cancels the upstream body; telemetry marks it', async () => {
    let upstreamCancelled = false;
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () => {
      const enc = new TextEncoder();
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(
            enc.encode(
              'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"gpt-4o-mini","choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}\n\n'
            )
          );
          // Never closes: a long generation the client abandons.
        },
        cancel() {
          upstreamCancelled = true;
        },
      });
      return new Response(body, {
        headers: { 'content-type': 'text/event-stream' },
      });
    });
    const res = await post(
      app,
      '/v1/chat/completions',
      chat('auto', { stream: true }),
      {
        'x-portkey-provider': 'openai',
        authorization: 'Bearer sk-regression',
        'x-ps-route': 'cheap',
      }
    );
    const reader = res.body!.getReader();
    const first = await reader.read();
    expect(new TextDecoder().decode(first.value)).toContain('Hi');
    await reader.cancel();

    await until(() => upstreamCancelled, 'upstream cancel');
    const ev = await telemetryEvent();
    expect(ev).toMatchObject({
      stream: true,
      stream_cancelled: true,
      requested_model: 'auto',
    });
    expect(ev.stream_truncated).toBeUndefined();
    expect(events).toHaveLength(1);
  });
});
