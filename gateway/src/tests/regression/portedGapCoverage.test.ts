/**
 * Whole-app regression tests (#69) for the ported 2.0 items that so far had
 * only handler- or transform-level tests: Gemini (AI Studio) and Claude on
 * Vertex chat (#108), the Vertex batch flow (upload, create, output) and the
 * Cohere batch output (#108), AI21 and Ollama chat (#60), and the
 * requested/served model fields for Latitude (#57). Offline, stub backends
 * only; each LLM call also checks its ps-telemetry event.
 */
jest.mock('jose', () => ({}));

import avro from 'avsc';
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

const ROUTE = { 'x-ps-route': 'cheap' };

const chat = (model: string, extra: object = {}) => ({
  model,
  messages: [{ role: 'user', content: 'Say hi' }],
  max_tokens: 16,
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
  usage: { prompt_tokens: 6, completion_tokens: 1, total_tokens: 7 },
});

async function readSSE(res: Response) {
  const text = await res.text();
  return text
    .split('\n')
    .filter((l) => l.startsWith('data: ') && l !== 'data: [DONE]')
    .map((l) => JSON.parse(l.slice(6)));
}

const config = (c: object) => JSON.stringify(c);

const VERTEX = {
  provider: 'vertex-ai',
  api_key: 'ya29.regression-token',
  vertex_project_id: 'ps-regression',
  vertex_region: 'us-central1',
};
const VERTEX_BASE =
  'https://us-central1-aiplatform.googleapis.com/v1/projects/ps-regression/locations/us-central1';

beforeEach(() => resetHarness());

describe('Gemini on Google AI Studio (#108)', () => {
  const MODEL = 'gemini-2.5-flash';
  const headers = {
    'x-portkey-config': config({
      provider: 'google',
      api_key: 'goog-regression',
      override_params: { model: MODEL },
    }),
    ...ROUTE,
  };

  test('chat: request mapping, unified response and telemetry', async () => {
    upstream(/generativelanguage\.googleapis\.com/, () =>
      json({
        candidates: [
          {
            content: {
              role: 'model',
              parts: [{ text: 'Hi', thoughtSignature: 'sig-1' }],
            },
            finishReason: 'STOP',
            index: 0,
          },
        ],
        usageMetadata: {
          promptTokenCount: 6,
          candidatesTokenCount: 2,
          thoughtsTokenCount: 3,
          totalTokenCount: 11,
        },
        modelVersion: MODEL,
      })
    );
    const res = await post(app, '/v1/chat/completions', chat('auto'), headers);
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.choices[0].message.content).toBe('Hi');

    const call = upstreamCalls[0];
    expect(call.url).toBe(
      `https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent?key=goog-regression`
    );
    expect(call.body.contents[0]).toMatchObject({
      role: 'user',
      parts: [{ text: 'Say hi' }],
    });
    expect(call.body.generationConfig.maxOutputTokens).toBe(16);

    expect(await telemetryEvent()).toMatchObject({
      vendor: 'google',
      requested_model: 'auto',
      served_model: MODEL,
      tokens_in: 6,
    });
  });

  test('streaming chat: deltas and usage from the last chunk', async () => {
    const chunk = (obj: object) => `data: ${JSON.stringify(obj)}\r\n\r\n`;
    upstream(/:streamGenerateContent\?key=goog-regression/, () =>
      sse(
        chunk({
          candidates: [
            { content: { role: 'model', parts: [{ text: 'H' }] }, index: 0 },
          ],
          modelVersion: MODEL,
        }) +
          chunk({
            candidates: [
              {
                content: { role: 'model', parts: [{ text: 'i' }] },
                finishReason: 'STOP',
                index: 0,
              },
            ],
            usageMetadata: {
              promptTokenCount: 6,
              candidatesTokenCount: 2,
              totalTokenCount: 8,
            },
            modelVersion: MODEL,
          })
      )
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chat('auto', { stream: true }),
      headers
    );
    expect(res.status).toBe(200);
    const chunks = await readSSE(res);
    expect(
      chunks.map((c) => c.choices?.[0]?.delta?.content ?? '').join('')
    ).toBe('Hi');
    const ev = await telemetryEvent();
    expect(ev).toMatchObject({
      stream: true,
      requested_model: 'auto',
      served_model: MODEL,
      tokens_in: 6,
      tokens_out: 2,
    });
    expect(ev.stream_truncated).toBeUndefined();
  });
});

describe('Claude on Vertex AI (#108)', () => {
  const MODEL = 'anthropic.claude-sonnet-4@20250514';
  const headers = {
    'x-portkey-config': config({
      ...VERTEX,
      override_params: { model: MODEL },
    }),
    ...ROUTE,
  };

  test('chat: Anthropic transforms on rawPredict, cached tokens counted', async () => {
    upstream(/publishers\/anthropic\/models\/.*:rawPredict$/, () =>
      json({
        id: 'msg_1',
        type: 'message',
        role: 'assistant',
        model: 'claude-sonnet-4-20250514',
        content: [{ type: 'text', text: 'Hi' }],
        stop_reason: 'end_turn',
        usage: {
          input_tokens: 4,
          cache_read_input_tokens: 2,
          cache_creation_input_tokens: 0,
          output_tokens: 1,
        },
      })
    );
    const res = await post(app, '/v1/chat/completions', chat('auto'), headers);
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.choices[0].message.content).toBe('Hi');
    // Ported (#108): cached input tokens are part of prompt_tokens.
    expect(body.usage.prompt_tokens).toBe(6);

    const call = upstreamCalls[0];
    expect(call.url).toBe(
      `${VERTEX_BASE}/publishers/anthropic/models/claude-sonnet-4@20250514:rawPredict`
    );
    expect(call.headers.authorization).toBe('Bearer ya29.regression-token');
    expect(call.body.anthropic_version).toBe('vertex-2023-10-16');
    expect(call.body.model).toBeUndefined();
    expect(call.body.messages[0]).toMatchObject({ role: 'user' });

    expect(await telemetryEvent()).toMatchObject({
      vendor: 'vertex-ai',
      requested_model: 'auto',
      served_model: 'claude-sonnet-4-20250514',
      tokens_out: 1,
    });
  });
});

describe('Vertex batch flow (#108)', () => {
  const batchHeaders = (extra: Record<string, string> = {}) => ({
    'x-portkey-config': config(VERTEX),
    ...extra,
  });

  test('upload: each JSONL line is transformed and PUT to the bucket', async () => {
    let uploaded = '';
    upstream(/^https:\/\/storage\.googleapis\.com\/ps-bucket\//, async (c) => {
      uploaded = await new Response(c.body).text();
      return json({ name: 'in.jsonl', bucket: 'ps-bucket' });
    });
    const line = (id: string, text: string) =>
      JSON.stringify({
        custom_id: id,
        method: 'POST',
        url: '/v1/chat/completions',
        body: {
          model: 'gemini-2.5-flash',
          messages: [{ role: 'user', content: text }],
        },
      });
    // An OpenAI SDK upload: multipart, purpose as a form field.
    const form = new FormData();
    form.append('purpose', 'batch');
    form.append(
      'file',
      new Blob([`${line('a', 'héllo')}\n${line('b', 'hi')}\n`]),
      'in.jsonl'
    );
    const req = new Request('http://local/', { method: 'POST', body: form });
    const bytes = await req.arrayBuffer();
    // Upload options are read from headers (a config carries none of them).
    const res = await app.request('/v1/files', {
      method: 'POST',
      headers: {
        'x-portkey-provider': 'vertex-ai',
        authorization: 'Bearer ya29.regression-token',
        'x-portkey-vertex-project-id': 'ps-regression',
        'x-portkey-vertex-region': 'us-central1',
        'content-type': req.headers.get('content-type') as string,
        'content-length': String(bytes.byteLength),
        'x-portkey-vertex-storage-bucket-name': 'ps-bucket',
        'x-portkey-provider-model': 'gemini-2.5-flash',
        'x-portkey-provider-file-name': 'in.jsonl',
      },
      body: bytes,
    });
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].url).toBe(
      'https://storage.googleapis.com/ps-bucket/in.jsonl'
    );
    expect(upstreamCalls[0].method).toBe('PUT');
    expect(upstreamCalls[0].headers.authorization).toBe(
      'Bearer ya29.regression-token'
    );
    const rows = uploaded
      .trim()
      .split('\n')
      .map((l) => JSON.parse(l));
    expect(rows).toHaveLength(2);
    expect(rows[0].request.contents[0].parts[0].text).toBe('héllo');
    expect(JSON.stringify(rows[1])).toContain('"b"');
    const out: any = await res.json();
    expect(out.id).toContain(encodeURIComponent('gs://ps-bucket/in.jsonl'));
  });

  test('create: model and GCS locations mapped to a batchPredictionJob', async () => {
    upstream(/batchPredictionJobs$/, () =>
      json({
        name: `projects/1/locations/us-central1/batchPredictionJobs/42`,
        displayName: 'job',
        model: 'publishers/google/models/gemini-2.5-flash',
        state: 'JOB_STATE_PENDING',
        createTime: '2026-11-06T00:00:00Z',
        inputConfig: {
          gcsSource: { uris: ['gs://ps-bucket/in.jsonl'] },
        },
        outputConfig: {
          gcsDestination: { outputUriPrefix: 'gs://ps-bucket/' },
        },
      })
    );
    const res = await post(
      app,
      '/v1/batches',
      {
        model: 'gemini-2.5-flash',
        input_file_id: encodeURIComponent('gs://ps-bucket/in.jsonl'),
        endpoint: '/v1/chat/completions',
        completion_window: '24h',
      },
      batchHeaders()
    );
    expect(res.status).toBe(200);
    const call = upstreamCalls[0];
    expect(call.url).toBe(`${VERTEX_BASE}/batchPredictionJobs`);
    expect(call.body).toMatchObject({
      model: 'publishers/google/models/gemini-2.5-flash',
      inputConfig: {
        instancesFormat: 'jsonl',
        gcsSource: { uris: 'gs://ps-bucket/in.jsonl' },
      },
      outputConfig: {
        predictionsFormat: 'jsonl',
        gcsDestination: { outputUriPrefix: 'gs://ps-bucket/' },
      },
    });
    expect(((await res.json()) as any).id).toBe('42');
  });

  test('output: streams transformed rows from the GCS output directory', async () => {
    upstream(/batchPredictionJobs\/42$/, () =>
      json({
        model: 'publishers/google/models/gemini-2.5-flash',
        outputInfo: { gcsOutputDirectory: 'gs://ps-bucket/out' },
      })
    );
    upstream(
      /^https:\/\/storage\.googleapis\.com\/ps-bucket\/out\/predictions\.jsonl$/,
      (c) =>
        c.method === 'HEAD'
          ? new Response(null, { status: 200 })
          : new Response(
              JSON.stringify({
                requestId: 'a',
                status: '',
                response: {
                  responseId: 'r1',
                  candidates: [
                    {
                      content: { role: 'model', parts: [{ text: 'Hi' }] },
                      finishReason: 'STOP',
                    },
                  ],
                  usageMetadata: {
                    promptTokenCount: 3,
                    candidatesTokenCount: 1,
                    totalTokenCount: 4,
                  },
                },
              }) + '\n',
              { status: 200 }
            )
    );
    const res = await app.request('/v1/batches/42/output', {
      headers: batchHeaders(),
    });
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toContain(
      'application/octet-stream'
    );
    const rows = (await res.text())
      .trim()
      .split('\n')
      .map((l) => JSON.parse(l));
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      id: 'batch-42-r1',
      custom_id: 'a',
      response: { status_code: 200 },
    });
    expect(rows[0].response.body.choices[0].message.content).toBe('Hi');
    for (const c of upstreamCalls) {
      expect(c.headers.authorization).toBe('Bearer ya29.regression-token');
    }
  });

  test('output: a location outside GCS is refused with no fetch', async () => {
    upstream(/batchPredictionJobs\/42$/, () =>
      json({
        model: 'publishers/google/models/gemini-2.5-flash',
        outputInfo: { gcsOutputDirectory: 'https://attacker.example/out' },
      })
    );
    const res = await app.request('/v1/batches/42/output', {
      headers: batchHeaders(),
    });
    expect(res.status).toBe(500);
    expect(upstreamCalls).toHaveLength(1);
  });
});

describe('Cohere batch output (#108)', () => {
  const avroPart = (records: object[]): Promise<Uint8Array> =>
    new Promise((resolve, reject) => {
      const chunks: Buffer[] = [];
      const encoder = new avro.streams.BlockEncoder(
        avro.Type.forSchema('string')
      );
      encoder.on('data', (chunk: Buffer) => chunks.push(chunk));
      encoder.on('end', () => resolve(new Uint8Array(Buffer.concat(chunks))));
      encoder.on('error', reject);
      for (const r of records) encoder.write(JSON.stringify(r));
      encoder.end();
    });

  test('decodes dataset parts; the API key never reaches the storage host', async () => {
    const part = await avroPart([
      {
        id: 'e1',
        custom_id: 'a',
        embeddings: { float: { array: [0.1, 0.2] } },
      },
    ]);
    upstream(/api\.cohere\.ai\/v1\/embed-jobs\/job-1$/, () =>
      json({ output_dataset_id: 'ds-1' })
    );
    upstream(/api\.cohere\.ai\/v1\/datasets\/ds-1$/, () =>
      json({
        dataset: {
          dataset_parts: [
            { url: 'https://storage.googleapis.com/co/part-0.avro?sig=1' },
          ],
        },
      })
    );
    upstream(
      /storage\.googleapis\.com\/co\/part-0\.avro/,
      () => new Response(part, { status: 200 })
    );
    const res = await app.request('/v1/batches/job-1/output', {
      headers: {
        'x-portkey-provider': 'cohere',
        authorization: 'Bearer co-secret',
      },
    });
    expect(res.status).toBe(200);
    const rows = (await res.text())
      .trim()
      .split('\n')
      .map((l) => JSON.parse(l));
    expect(rows).toEqual([
      expect.objectContaining({
        id: 'e1',
        custom_id: 'a',
        response: expect.objectContaining({ status_code: 200 }),
      }),
    ]);
    expect(rows[0].response.body.data[0].embedding).toEqual([0.1, 0.2]);
    expect(upstreamCalls).toHaveLength(3);
    expect(upstreamCalls[0].headers.authorization).toBe('Bearer co-secret');
    expect(upstreamCalls[1].headers.authorization).toBe('Bearer co-secret');
    expect(upstreamCalls[2].headers.authorization).toBeUndefined();
  });
});

describe('AI21 and Ollama chat (#60)', () => {
  test('ai21: model in the path, text messages, telemetry', async () => {
    upstream(/^https:\/\/api\.ai21\.com\/studio\/v1\/jamba-mini\/chat$/, () =>
      json({
        id: 'ai21-1',
        outputs: [
          {
            text: 'Hi',
            role: 'assistant',
            finishReason: { reason: 'endoftext', length: null, sequence: null },
          },
        ],
      })
    );
    const res = await post(app, '/v1/chat/completions', chat('auto'), {
      'x-portkey-config': config({
        provider: 'ai21',
        api_key: 'ai21-key',
        override_params: { model: 'jamba-mini' },
      }),
      ...ROUTE,
    });
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.choices[0].message.content).toBe('Hi');
    expect(upstreamCalls[0].headers.authorization).toBe('Bearer ai21-key');
    expect(upstreamCalls[0].body).toMatchObject({
      messages: [{ role: 'user', text: 'Say hi' }],
      maxTokens: 16,
    });
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'ai21',
      requested_model: 'auto',
      served_model: 'jamba-mini',
    });
  });

  test('ollama: custom host, developer role becomes system, telemetry', async () => {
    upstream(/^https:\/\/ollama\.example\.com\/v1\/chat\/completions$/, () =>
      json(completion('llama3.2'))
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      {
        model: 'auto',
        messages: [
          { role: 'developer', content: 'Be brief' },
          { role: 'user', content: 'Say hi' },
        ],
      },
      {
        'x-portkey-config': config({
          provider: 'ollama',
          custom_host: 'https://ollama.example.com',
          override_params: { model: 'llama3.2' },
        }),
        ...ROUTE,
      }
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].body.messages[0]).toEqual({
      role: 'system',
      content: 'Be brief',
    });
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'ollama',
      requested_model: 'auto',
      served_model: 'llama3.2',
      tokens_in: 6,
      tokens_out: 1,
    });
  });
});

describe('Latitude routed chat (#57)', () => {
  test('requested and served model on the event', async () => {
    upstream(/^https:\/\/api\.lsh\.ai\/v1\/chat\/completions$/, () =>
      json(completion('qwen-2.5-7b'))
    );
    const res = await post(app, '/v1/chat/completions', chat('auto'), {
      'x-portkey-config': config({
        provider: 'latitude',
        api_key: 'lat-regression',
        override_params: { model: 'qwen-2.5-7b' },
      }),
      ...ROUTE,
    });
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].headers.authorization).toBe(
      'Bearer lat-regression'
    );
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'latitude',
      requested_model: 'auto',
      served_model: 'qwen-2.5-7b',
      tokens_in: 6,
      tokens_out: 1,
    });
  });
});
