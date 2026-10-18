/**
 * Regression tests for the endpoints ported from Portkey 2.0 (#58, #59, #61,
 * #62), through the whole gateway app and offline: /v1/rerank, the Responses
 * and Messages adapters, /v1/health, /metrics and the JSON payload limit.
 * Each LLM call also checks the ps-telemetry event it emits (#69).
 */
jest.mock('jose', () => ({}));

import {
  json,
  post,
  resetHarness,
  telemetryEvent,
  upstream,
  upstreamCalls,
} from './harness';
import app from '../../index';

beforeEach(() => resetHarness());

describe('/v1/rerank (#58)', () => {
  const body = {
    model: 'rerank-v3.5',
    query: 'capital of france',
    documents: ['Paris is the capital', { text: 'Berlin is in Germany' }],
    top_n: 1,
  };

  test('cohere: request, unified response and telemetry with search units', async () => {
    upstream(/api\.cohere\.ai\/v2\/rerank$/, () =>
      json({
        id: 'rr-1',
        results: [{ index: 0, relevance_score: 0.93 }],
        meta: { billed_units: { search_units: 1 } },
      })
    );
    const res = await post(app, '/v1/rerank', body, {
      'x-portkey-provider': 'cohere',
      authorization: 'Bearer co-regression',
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({
      object: 'list',
      provider: 'cohere',
      model: 'rerank-v3.5',
      results: [{ index: 0, relevance_score: 0.93 }],
      usage: { search_units: 1 },
    });
    expect(upstreamCalls[0].body).toMatchObject({
      model: 'rerank-v3.5',
      query: 'capital of france',
      documents: ['Paris is the capital', 'Berlin is in Germany'],
      top_n: 1,
    });
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'cohere',
      api_surface: 'rerank',
      model: 'rerank-v3.5',
      usage_units: 1,
      usage_unit_type: 'search_units',
    });
  });

  test('pinecone (new provider, #57)', async () => {
    upstream(/api\.pinecone\.io\/rerank$/, () =>
      json({
        model: 'bge-reranker-v2-m3',
        data: [{ index: 0, score: 0.8, document: { text: 'Paris' } }],
        usage: { rerank_units: 1 },
      })
    );
    const res = await post(
      app,
      '/v1/rerank',
      { ...body, model: 'bge-reranker-v2-m3' },
      { 'x-portkey-provider': 'pinecone', authorization: 'Bearer pc-key' }
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].headers['api-key']).toBe('pc-key');
    expect(await res.json()).toMatchObject({
      provider: 'pinecone',
      results: [{ index: 0, relevance_score: 0.8 }],
    });
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'pinecone',
      api_surface: 'rerank',
      usage_units: 1,
      usage_unit_type: 'search_units',
    });
  });

  test('bedrock goes to bedrock-agent-runtime, signed', async () => {
    const ARN =
      'arn:aws:bedrock:us-west-2::foundation-model/amazon.rerank-v1:0';
    upstream(/bedrock-agent-runtime\.us-west-2\.amazonaws\.com\/rerank$/, () =>
      json({ results: [{ index: 0, relevanceScore: 0.7 }] })
    );
    const res = await post(
      app,
      '/v1/rerank',
      { ...body, model: ARN },
      {
        'x-portkey-provider': 'bedrock',
        'x-portkey-aws-access-key-id': 'AKIAREGRESSIONTEST',
        'x-portkey-aws-secret-access-key': 'regression-secret',
        'x-portkey-aws-region': 'us-west-2',
      }
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].headers.authorization).toMatch(
      /^AWS4-HMAC-SHA256 Credential=AKIAREGRESSIONTEST\/\d{8}\/us-west-2\/bedrock\//
    );
    expect(
      upstreamCalls[0].body.rerankingConfiguration.bedrockRerankingConfiguration
        .modelConfiguration.modelArn
    ).toBe(ARN);
    expect(await res.json()).toMatchObject({
      provider: 'bedrock',
      results: [{ index: 0, relevance_score: 0.7 }],
    });
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'bedrock',
      api_surface: 'rerank',
      model: ARN,
    });
  });

  test('a provider without rerank is refused before any upstream call', async () => {
    const res = await post(app, '/v1/rerank', body, {
      'x-portkey-provider': 'anthropic',
      'x-api-key': 'sk-ant',
    });
    expect(res.status).toBeGreaterThanOrEqual(400);
    expect(upstreamCalls).toHaveLength(0);
  });
});

describe('Responses adapter (#59)', () => {
  test('/v1/responses on Anthropic is served via Messages and mapped back', async () => {
    upstream(/api\.anthropic\.com\/v1\/messages$/, () =>
      json({
        id: 'msg_1',
        type: 'message',
        role: 'assistant',
        model: 'claude-sonnet-4-20250514',
        content: [{ type: 'text', text: 'Bonjour' }],
        stop_reason: 'end_turn',
        usage: { input_tokens: 10, output_tokens: 3 },
      })
    );
    const res = await post(
      app,
      '/v1/responses',
      {
        model: 'claude-sonnet-4-20250514',
        instructions: 'Answer in French.',
        input: 'Hello',
        max_output_tokens: 64,
      },
      { 'x-portkey-provider': 'anthropic', 'x-api-key': 'sk-ant-regression' }
    );
    expect(res.status).toBe(200);
    const out: any = await res.json();
    expect(out.object).toBe('response');
    expect(out.status).toBe('completed');
    expect(out.output[0].content[0]).toMatchObject({
      type: 'output_text',
      text: 'Bonjour',
    });
    expect(out.usage).toMatchObject({ input_tokens: 10, output_tokens: 3 });

    expect(upstreamCalls[0].body).toMatchObject({
      model: 'claude-sonnet-4-20250514',
      max_tokens: 64,
      messages: [{ role: 'user', content: 'Hello' }],
    });
    expect(JSON.stringify(upstreamCalls[0].body.system)).toContain(
      'Answer in French.'
    );

    expect(await telemetryEvent()).toMatchObject({
      vendor: 'anthropic',
      api_surface: 'responses',
      model: 'claude-sonnet-4-20250514',
      tokens_in: 10,
      tokens_out: 3,
    });
  });

  test('/v1/responses on OpenAI is passed through natively', async () => {
    upstream(/api\.openai\.com\/v1\/responses$/, () =>
      json({
        id: 'resp_1',
        object: 'response',
        status: 'completed',
        model: 'gpt-4o-mini-2024-07-18',
        output: [],
        usage: { input_tokens: 4, output_tokens: 1, total_tokens: 5 },
      })
    );
    const res = await post(
      app,
      '/v1/responses',
      { model: 'gpt-4o-mini', input: 'Hi' },
      { 'x-portkey-provider': 'openai', authorization: 'Bearer sk-regression' }
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].body).toEqual({
      model: 'gpt-4o-mini',
      input: 'Hi',
    });
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'openai',
      api_surface: 'responses',
      tokens_in: 4,
      tokens_out: 1,
    });
  });
});

describe('Messages adapter (#59)', () => {
  test('/v1/messages on OpenAI is served via chat completions, routed', async () => {
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
      json({
        id: 'chatcmpl-1',
        object: 'chat.completion',
        created: 1,
        model: 'gpt-4o-mini-2024-07-18',
        choices: [
          {
            index: 0,
            message: { role: 'assistant', content: 'Hi there' },
            finish_reason: 'stop',
          },
        ],
        usage: { prompt_tokens: 7, completion_tokens: 2, total_tokens: 9 },
      })
    );
    const res = await post(
      app,
      '/v1/messages',
      {
        model: 'auto',
        max_tokens: 32,
        system: 'Be brief.',
        messages: [{ role: 'user', content: 'Hello' }],
      },
      {
        'x-portkey-provider': 'openai',
        authorization: 'Bearer sk-regression',
        'x-ps-route': 'cheap',
      }
    );
    expect(res.status).toBe(200);
    const out: any = await res.json();
    expect(out).toMatchObject({
      type: 'message',
      role: 'assistant',
      content: [{ type: 'text', text: 'Hi there' }],
      stop_reason: 'end_turn',
      usage: { input_tokens: 7, output_tokens: 2 },
    });

    const sent = upstreamCalls[0];
    expect(sent.url).toBe('https://api.openai.com/v1/chat/completions');
    expect(sent.body.model).toBe('gpt-4o-mini');
    expect(sent.body.messages[0]).toMatchObject({ role: 'system' });

    expect(await telemetryEvent()).toMatchObject({
      vendor: 'openai',
      api_surface: 'messages',
      requested_model: 'auto',
      served_model: 'gpt-4o-mini-2024-07-18',
      route_group: 'cheap',
      tokens_in: 7,
      tokens_out: 2,
    });
  });
});

describe('/v1/health and /metrics (#61)', () => {
  test('health answers without auth or upstream calls', async () => {
    const res = await app.request('/v1/health');
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body).toMatchObject({ status: 'success' });
    expect(typeof body.version).toBe('string');
    expect(upstreamCalls).toHaveLength(0);
  });

  test('metrics count a proxied chat call by provider and model', async () => {
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
      json({
        id: 'chatcmpl-1',
        object: 'chat.completion',
        created: 1,
        model: 'gpt-4o-metrics-probe',
        choices: [
          {
            index: 0,
            message: { role: 'assistant', content: 'ok' },
            finish_reason: 'stop',
          },
        ],
        usage: { prompt_tokens: 5, completion_tokens: 1, total_tokens: 6 },
      })
    );
    await post(
      app,
      '/v1/chat/completions',
      {
        model: 'gpt-4o-metrics-probe',
        messages: [{ role: 'user', content: 'secret prompt text' }],
      },
      { 'x-portkey-provider': 'openai', authorization: 'Bearer sk-regression' }
    );
    const res = await app.request('/metrics');
    expect(res.status).toBe(200);
    const text = await res.text();
    expect(text).toMatch(
      /ps_gateway_requests_total\{route="\/v1\/chat\/completions",method="POST",status="200",provider="openai"[^}]*\} \d+/
    );
    expect(text).toMatch(
      /ps_gateway_tokens_total\{provider="openai",model="gpt-4o-metrics-probe",direction="input"[^}]*\} 5/
    );
    expect(text).not.toContain('secret prompt text');
    expect(text).not.toContain('sk-regression');
  });
});

describe('JSON payload size limit (#62)', () => {
  afterEach(() => {
    delete process.env.MAX_JSON_PAYLOAD_SIZE_IN_MB;
  });

  const big = JSON.stringify({
    model: 'gpt-4o-mini',
    messages: [{ role: 'user', content: 'x'.repeat(2048) }],
  });

  test('an oversized body is a 413 and never reaches the provider', async () => {
    process.env.MAX_JSON_PAYLOAD_SIZE_IN_MB = '0.001';
    const res = await post(app, '/v1/chat/completions', big, {
      'x-portkey-provider': 'openai',
      authorization: 'Bearer sk-regression',
      'content-length': String(big.length),
    });
    expect(res.status).toBe(413);
    expect(((await res.json()) as any).message).toContain('0.001MB');
    expect(upstreamCalls).toHaveLength(0);
  });

  test('without a limit the same body is forwarded', async () => {
    upstream(/api\.openai\.com/, () =>
      json({
        id: 'c',
        object: 'chat.completion',
        created: 1,
        model: 'gpt-4o-mini',
        choices: [],
      })
    );
    const res = await post(app, '/v1/chat/completions', big, {
      'x-portkey-provider': 'openai',
      authorization: 'Bearer sk-regression',
      'content-length': String(big.length),
    });
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(1);
  });
});

describe('multipart uploads report their model (#75)', () => {
  const upload = (path: string, form: FormData, provider: string) =>
    app.request(path, {
      method: 'POST',
      headers: { 'x-portkey-provider': provider, authorization: 'Bearer k' },
      body: form,
    });

  test('a transcription is registered with its model and audio modality', async () => {
    upstream(/api\.deepgram\.com\/v1\/listen/, () =>
      json({
        metadata: { duration: 2 },
        results: { channels: [{ alternatives: [{ transcript: 'hi' }] }] },
      })
    );
    const form = new FormData();
    form.append('file', new Blob(['abc'], { type: 'audio/wav' }), 'a.wav');
    form.append('model', 'nova-3');
    form.append('prompt', 'secret context');
    const res = await upload('/v1/audio/transcriptions', form, 'deepgram');
    expect(res.status).toBe(200);
    const event = await telemetryEvent();
    expect(event).toMatchObject({
      vendor: 'deepgram',
      model: 'nova-3',
      api_surface: 'audio',
      modality: 'audio',
      usage_units: 2,
      usage_unit_type: 'seconds',
    });
    expect(JSON.stringify(event)).not.toContain('secret context');
  });

  test('an image edit is registered with its model, not unknown', async () => {
    upstream(/api\.openai\.com\/v1\/images\/edits$/, () =>
      json({ created: 1, data: [{ b64_json: 'AAAA' }] })
    );
    const form = new FormData();
    form.append('image', new Blob(['png'], { type: 'image/png' }), 'a.png');
    form.append('model', 'gpt-image-1');
    form.append('prompt', 'secret prompt');
    const res = await upload('/v1/images/edits', form, 'openai');
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(1);
    const event = await telemetryEvent();
    expect(event).toMatchObject({
      vendor: 'openai',
      model: 'gpt-image-1',
      api_surface: 'images',
      modality: 'image',
    });
    expect(JSON.stringify(event)).not.toContain('secret prompt');
  });
});
