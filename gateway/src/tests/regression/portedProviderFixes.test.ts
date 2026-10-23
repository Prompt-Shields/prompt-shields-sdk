/**
 * Regression tests for the remaining ported 2.0 items (#69) that had only
 * transform-level tests: Jina and Voyage rerank (#58), the upload size limit
 * (#62 / #75), the stand-alone provider fixes (#60: `prompt_cache_retention`,
 * `service_tier`, gpt-image-1 parameters, Azure image edit, Z.ai images,
 * Cohere v2 stream deltas), Fireworks chat and OpenAI batch output (#108).
 * Whole app, offline; each LLM call also checks its ps-telemetry event.
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

const completion = (model: string, extra: object = {}) => ({
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
  usage: { prompt_tokens: 6, completion_tokens: 1, total_tokens: 7 },
  ...extra,
});

const chat = (model: string, extra: object = {}) => ({
  model,
  messages: [{ role: 'user', content: 'Say hi' }],
  ...extra,
});

async function readSSE(res: Response) {
  const text = await res.text();
  return text
    .split('\n')
    .filter((l) => l.startsWith('data: ') && l !== 'data: [DONE]')
    .map((l) => JSON.parse(l.slice(6)));
}

beforeEach(() => resetHarness());

describe('/v1/rerank: Jina and Voyage (#58)', () => {
  const body = {
    model: 'm',
    query: 'capital of france',
    documents: ['Paris is the capital', { text: 'Berlin is in Germany' }],
    top_n: 1,
  };

  test('jina: request, unified response, telemetry with token units', async () => {
    upstream(/^https:\/\/api\.jina\.ai\/v1\/rerank$/, () =>
      json({
        model: 'jina-reranker-v2-base-multilingual',
        results: [{ index: 0, relevance_score: 0.91 }],
        usage: { total_tokens: 12 },
      })
    );
    const res = await post(
      app,
      '/v1/rerank',
      { ...body, model: 'jina-reranker-v2-base-multilingual' },
      { 'x-portkey-provider': 'jina', authorization: 'Bearer jina-key' }
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].headers.authorization).toBe('Bearer jina-key');
    expect(upstreamCalls[0].body).toMatchObject({
      model: 'jina-reranker-v2-base-multilingual',
      documents: ['Paris is the capital', 'Berlin is in Germany'],
      top_n: 1,
    });
    expect(await res.json()).toMatchObject({
      object: 'list',
      provider: 'jina',
      results: [{ index: 0, relevance_score: 0.91 }],
      usage: { search_units: 12 },
    });
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'jina',
      api_surface: 'rerank',
      model: 'jina-reranker-v2-base-multilingual',
      usage_units: 12,
      usage_unit_type: 'search_units',
    });
  });

  test('voyage: top_n becomes top_k; response mapped from data[]', async () => {
    upstream(/^https:\/\/api\.voyageai\.com\/v1\/rerank$/, () =>
      json({
        object: 'list',
        model: 'rerank-2',
        data: [{ index: 0, relevance_score: 0.87 }],
        usage: { total_tokens: 9 },
      })
    );
    const res = await post(
      app,
      '/v1/rerank',
      { ...body, model: 'rerank-2' },
      { 'x-portkey-provider': 'voyage', authorization: 'Bearer voy-key' }
    );
    expect(res.status).toBe(200);
    const sent = upstreamCalls[0].body;
    expect(sent).toMatchObject({ model: 'rerank-2', top_k: 1 });
    expect(sent.top_n).toBeUndefined();
    expect(await res.json()).toMatchObject({
      provider: 'voyage',
      model: 'rerank-2',
      results: [{ index: 0, relevance_score: 0.87 }],
    });
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'voyage',
      api_surface: 'rerank',
      usage_units: 9,
      usage_unit_type: 'search_units',
    });
  });
});

describe('upload size limit (#62, #75)', () => {
  afterEach(() => {
    delete process.env.MAX_FILE_PAYLOAD_SIZE_IN_MB;
  });

  /** A multipart body with its real content-type and length. */
  async function multipart(size: number) {
    const form = new FormData();
    form.append(
      'file',
      new Blob(['a'.repeat(size)], { type: 'audio/wav' }),
      'a.wav'
    );
    form.append('model', 'whisper-1');
    const req = new Request('http://local/', { method: 'POST', body: form });
    const bytes = await req.arrayBuffer();
    return {
      bytes,
      contentType: req.headers.get('content-type') as string,
    };
  }

  const transcribe = (headers: Record<string, string>, body: ArrayBuffer) =>
    app.request('/v1/audio/transcriptions', {
      method: 'POST',
      headers: {
        'x-portkey-provider': 'openai',
        authorization: 'Bearer sk-regression',
        ...headers,
      },
      body,
    });

  test('an upload over MAX_FILE_PAYLOAD_SIZE_IN_MB is a 413, not forwarded', async () => {
    process.env.MAX_FILE_PAYLOAD_SIZE_IN_MB = '0.001';
    const { bytes, contentType } = await multipart(4096);
    const res = await transcribe(
      {
        'content-type': contentType,
        'content-length': String(bytes.byteLength),
      },
      bytes
    );
    expect(res.status).toBe(413);
    expect(((await res.json()) as any).message).toContain('0.001MB');
    expect(upstreamCalls).toHaveLength(0);
  });

  test('a multipart upload without content-length is a 411 while the limit is set', async () => {
    process.env.MAX_FILE_PAYLOAD_SIZE_IN_MB = '1';
    const { bytes, contentType } = await multipart(16);
    const res = await transcribe({ 'content-type': contentType }, bytes);
    expect(res.status).toBe(411);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('an upload under the limit is forwarded and registered', async () => {
    process.env.MAX_FILE_PAYLOAD_SIZE_IN_MB = '1';
    upstream(/api\.openai\.com\/v1\/audio\/transcriptions$/, () =>
      json({ text: 'hi' })
    );
    const { bytes, contentType } = await multipart(16);
    const res = await transcribe(
      {
        'content-type': contentType,
        'content-length': String(bytes.byteLength),
      },
      bytes
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(1);
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'openai',
      model: 'whisper-1',
      api_surface: 'audio',
    });
  });
});

describe('provider fixes (#60)', () => {
  test('openai: prompt_cache_retention is forwarded, service_tier returned', async () => {
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
      json(completion('gpt-4o-mini-2024-07-18', { service_tier: 'default' }))
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chat('auto', { prompt_cache_retention: '24h', service_tier: 'auto' }),
      {
        'x-portkey-provider': 'openai',
        authorization: 'Bearer sk-regression',
        'x-ps-route': 'cheap',
      }
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].body).toMatchObject({
      model: 'gpt-4o-mini',
      prompt_cache_retention: '24h',
      service_tier: 'auto',
    });
    expect(((await res.json()) as any).service_tier).toBe('default');
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'openai',
      requested_model: 'auto',
      served_model: 'gpt-4o-mini-2024-07-18',
      route_group: 'cheap',
      tokens_in: 6,
      tokens_out: 1,
    });
  });

  test('azure-openai: deployment URL; prompt_cache_retention forwarded', async () => {
    upstream(
      /^https:\/\/regres\.openai\.azure\.com\/openai\/deployments\/gpt4o\/chat\/completions\?api-version=/,
      () => json(completion('gpt-4o-2024-11-20'))
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chat('auto', { prompt_cache_retention: 'in_memory' }),
      {
        'x-ps-route': 'cheap',
        'x-portkey-provider': 'azure-openai',
        'x-portkey-azure-resource-name': 'regres',
        'x-portkey-azure-deployment-id': 'gpt4o',
        'x-portkey-azure-api-version': '2024-10-21',
        authorization: 'Bearer az-key',
      }
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].headers['api-key']).toBe('az-key');
    expect(upstreamCalls[0].body.prompt_cache_retention).toBe('in_memory');
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'azure-openai',
      requested_model: 'auto',
      served_model: 'gpt-4o-2024-11-20',
      tokens_in: 6,
      tokens_out: 1,
    });
  });

  test('azure-ai (Azure AI Inference): Foundry URL; prompt_cache_retention forwarded', async () => {
    upstream(
      /^https:\/\/regres\.services\.ai\.azure\.com\/models\/chat\/completions\?api-version=2024-05-01-preview$/,
      () => json(completion('Phi-4'))
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chat('auto', { prompt_cache_retention: '24h' }),
      {
        'x-ps-route': 'cheap',
        'x-portkey-provider': 'azure-ai',
        'x-portkey-azure-foundry-url':
          'https://regres.services.ai.azure.com/models',
        'x-portkey-azure-api-version': '2024-05-01-preview',
        authorization: 'Bearer foundry-key',
      }
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].body.prompt_cache_retention).toBe('24h');
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'azure-ai',
      requested_model: 'auto',
      served_model: 'Phi-4',
      tokens_in: 6,
    });
  });

  test('openai: gpt-image-1 parameters reach the provider', async () => {
    upstream(/api\.openai\.com\/v1\/images\/generations$/, () =>
      json({ created: 1, data: [{ b64_json: 'AAAA' }] })
    );
    const res = await post(
      app,
      '/v1/images/generations',
      {
        model: 'gpt-image-1',
        prompt: 'a lighthouse',
        background: 'transparent',
        moderation: 'low',
        output_format: 'webp',
        output_compression: 80,
      },
      { 'x-portkey-provider': 'openai', authorization: 'Bearer sk-regression' }
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].body).toMatchObject({
      model: 'gpt-image-1',
      background: 'transparent',
      moderation: 'low',
      output_format: 'webp',
      output_compression: 80,
    });
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'openai',
      model: 'gpt-image-1',
      api_surface: 'images',
      modality: 'image',
    });
  });

  test.each([
    ['2025-04-01-preview', false],
    ['v1', true],
  ])(
    'azure-openai image edit, api-version %s: model forwarded = %s',
    async (apiVersion, keepsModel) => {
      upstream(/regres\.openai\.azure\.com\/openai\/.*images\/edits/, () =>
        json({ created: 1, data: [{ b64_json: 'AAAA' }] })
      );
      const form = new FormData();
      form.append('image', new Blob(['png'], { type: 'image/png' }), 'a.png');
      form.append('model', 'gpt-image-1');
      form.append('prompt', 'add a hat');
      const res = await app.request('/v1/images/edits', {
        method: 'POST',
        headers: {
          'x-portkey-provider': 'azure-openai',
          'x-portkey-azure-resource-name': 'regres',
          'x-portkey-azure-deployment-id': 'img1',
          'x-portkey-azure-api-version': apiVersion,
          authorization: 'Bearer az-key',
        },
        body: form,
      });
      expect(res.status).toBe(200);
      const sent = upstreamCalls[0].body as FormData;
      expect(sent).toBeInstanceOf(FormData);
      expect(sent.has('model')).toBe(keepsModel);
      expect(sent.get('prompt')).toBe('add a hat');
    }
  );

  test('z-ai: image generation, user mapped to user_id', async () => {
    upstream(/^https:\/\/api\.z\.ai\/api\/paas\/v4\/images\/generations$/, () =>
      json({ created: 1, data: [{ url: 'https://cdn.z.ai/i.png' }] })
    );
    const res = await post(
      app,
      '/v1/images/generations',
      { model: 'cogview-4-250304', prompt: 'a lighthouse', user: 'u-1' },
      { 'x-portkey-provider': 'z-ai', authorization: 'Bearer zai-key' }
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].body).toMatchObject({
      model: 'cogview-4-250304',
      prompt: 'a lighthouse',
      user_id: 'u-1',
    });
    expect(((await res.json()) as any).data[0].url).toBe(
      'https://cdn.z.ai/i.png'
    );
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'z-ai',
      model: 'cogview-4-250304',
      api_surface: 'images',
    });
  });

  test('cohere: v2 streaming chat deltas and usage', async () => {
    upstream(/^https:\/\/api\.cohere\.ai\/v2\/chat$/, () =>
      sse(
        [
          'event: message-start\ndata: {"type":"message-start","id":"gen-1","delta":{"message":{"role":"assistant"}}}',
          'event: content-delta\ndata: {"type":"content-delta","index":0,"delta":{"message":{"content":{"text":"Hel"}}}}',
          'event: content-delta\ndata: {"type":"content-delta","index":0,"delta":{"message":{"content":{"text":"lo"}}}}',
          'event: message-end\ndata: {"type":"message-end","delta":{"finish_reason":"COMPLETE","usage":{"tokens":{"input_tokens":8,"output_tokens":2}}}}',
          '',
        ].join('\n\n')
      )
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chat('auto', { stream: true }),
      {
        'x-ps-route': 'cheap',
        'x-portkey-config': JSON.stringify({
          provider: 'cohere',
          api_key: 'co-key',
          override_params: { model: 'command-r-plus' },
        }),
      }
    );
    expect(res.status).toBe(200);
    const chunks = await readSSE(res);
    expect(chunks.map((c) => c.choices[0]?.delta?.content ?? '').join('')).toBe(
      'Hello'
    );
    expect(chunks.at(-1).usage).toMatchObject({
      prompt_tokens: 8,
      completion_tokens: 2,
    });
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'cohere',
      stream: true,
      requested_model: 'auto',
      served_model: 'command-r-plus',
      tokens_in: 8,
      tokens_out: 2,
    });
  });
});

describe('Fireworks and batch output (#108)', () => {
  test('fireworks: chat on the inference host, with telemetry', async () => {
    const MODEL = 'accounts/fireworks/models/llama-v3p1-8b-instruct';
    upstream(
      /^https:\/\/api\.fireworks\.ai\/inference\/v1\/chat\/completions$/,
      () => json(completion(MODEL))
    );
    const res = await post(app, '/v1/chat/completions', chat('auto'), {
      'x-ps-route': 'cheap',
      'x-portkey-config': JSON.stringify({
        provider: 'fireworks-ai',
        api_key: 'fw-key',
        override_params: { model: MODEL },
      }),
    });
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].headers.authorization).toBe('Bearer fw-key');
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'fireworks-ai',
      requested_model: 'auto',
      served_model: MODEL,
      tokens_in: 6,
      tokens_out: 1,
    });
  });

  const output = (id: string) =>
    app.request(`/v1/batches/${id}/output`, {
      headers: {
        'x-portkey-provider': 'openai',
        authorization: 'Bearer sk-regression',
      },
    });

  test('openai batch output streams the output file as octet-stream', async () => {
    const lines = '{"custom_id":"a"}\n{"custom_id":"b"}\n';
    upstream(/api\.openai\.com\/v1\/batches\/batch_1$/, () =>
      json({ id: 'batch_1', output_file_id: 'file-out' })
    );
    upstream(/api\.openai\.com\/v1\/files\/file-out\/content$/, () => {
      return new Response(lines, {
        status: 200,
        headers: { 'content-type': 'application/jsonl' },
      });
    });
    const res = await output('batch_1');
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toContain(
      'application/octet-stream'
    );
    expect(await res.text()).toBe(lines);
    expect(upstreamCalls.map((c) => c.url)).toEqual([
      'https://api.openai.com/v1/batches/batch_1',
      'https://api.openai.com/v1/files/file-out/content',
    ]);
  });

  test('openai batch output falls back to the error file', async () => {
    upstream(/api\.openai\.com\/v1\/batches\/batch_2$/, () =>
      json({ id: 'batch_2', output_file_id: null, error_file_id: 'file-err' })
    );
    upstream(
      /api\.openai\.com\/v1\/files\/file-err\/content$/,
      () => new Response('{"error":"x"}\n', { status: 200 })
    );
    const res = await output('batch_2');
    expect(res.status).toBe(200);
    expect(await res.text()).toBe('{"error":"x"}\n');
  });

  test('openai batch output with no file never requests files/undefined', async () => {
    upstream(/api\.openai\.com\/v1\/batches\/batch_3$/, () =>
      json({ id: 'batch_3', status: 'in_progress' })
    );
    const res = await output('batch_3');
    expect(res.status).toBe(400);
    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls.some((c) => c.url.includes('undefined'))).toBe(false);
  });
});
