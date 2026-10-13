// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../utils/env', () => ({ Environment: () => ({}) }));

import { createLineSplitter } from '../handlers/streamHandlerUtils';
import { OpenAIGetBatchOutputRequestHandler } from './openai/getBatchOutput';
import { AzureOpenAIGetBatchOutputRequestHandler } from './azure-openai/getBatchOutput';
import { BatchOutputRequestHandler as VertexBatchOutputRequestHandler } from './google-vertex-ai/getBatchOutput';
import { GoogleFileUploadRequestHandler } from './google-vertex-ai/uploadFile';
import { GoogleBatchCreateConfig } from './google-vertex-ai/createBatch';

const encoder = new TextEncoder();
const decoder = new TextDecoder();

const streamOf = (chunks: (string | Uint8Array)[]) =>
  new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(
          typeof chunk === 'string' ? encoder.encode(chunk) : chunk
        );
      }
      controller.close();
    },
  });

const readAll = async (stream: ReadableStream<any>) => {
  const out: any[] = [];
  const reader = stream.getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return out;
    out.push(value);
  }
};

const c: any = { req: { param: () => ({}) } };
const originalFetch = global.fetch;
let fetchMock: jest.Mock;

beforeEach(() => {
  fetchMock = jest.fn();
  global.fetch = fetchMock as any;
});

afterEach(() => {
  global.fetch = originalFetch;
});

describe('createLineSplitter (#108)', () => {
  test('emits trimmed lines as bytes and keeps split multi-byte characters', async () => {
    const bytes = encoder.encode('{"a":"é"}\n\n  {"b":2}  \n{"c":3}');
    // Split inside the two-byte "é".
    const cut = bytes.indexOf(0xc3) + 1;
    const lines = await readAll(
      streamOf([bytes.slice(0, cut), bytes.slice(cut)]).pipeThrough(
        createLineSplitter()
      )
    );
    expect(lines.every((l) => l instanceof Uint8Array)).toBe(true);
    expect(lines.map((l) => decoder.decode(l))).toEqual([
      '{"a":"é"}',
      '{"b":2}',
      '{"c":3}',
    ]);
  });
});

describe('OpenAI/Azure batch output streaming (#108)', () => {
  test('OpenAI streams the output file as octet-stream', async () => {
    fetchMock
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ output_file_id: 'file-1' }))
      )
      .mockResolvedValueOnce(
        new Response(streamOf(['{"id":1}\n', '{"id":2}\n']), {
          headers: { 'Content-Type': 'application/jsonl' },
        })
      );
    const res = await OpenAIGetBatchOutputRequestHandler({
      c,
      providerOptions: { provider: 'openai', apiKey: 'k' },
      requestURL: 'https://gw/v1/batches/batch_1/output',
    });
    expect(fetchMock.mock.calls[1][0]).toBe(
      'https://api.openai.com/v1/files/file-1/content'
    );
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toBe('application/octet-stream');
    expect(await res.text()).toBe('{"id":1}\n{"id":2}\n');
  });

  test('OpenAI passes a provider error through unchanged', async () => {
    fetchMock
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ output_file_id: 'file-1' }))
      )
      .mockResolvedValueOnce(
        new Response('{"error":"nope"}', {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        })
      );
    const res = await OpenAIGetBatchOutputRequestHandler({
      c,
      providerOptions: { provider: 'openai', apiKey: 'k' },
      requestURL: 'https://gw/v1/batches/batch_1/output',
    });
    expect(res.status).toBe(404);
    expect(res.headers.get('content-type')).toBe('application/json');
  });

  const azureOptions: any = {
    provider: 'azure-openai',
    apiKey: 'k',
    resourceName: 'res',
    apiVersion: '2024-10-21',
  };

  test('Azure streams the output file as octet-stream', async () => {
    fetchMock
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ output_file_id: 'file-1' }))
      )
      .mockResolvedValueOnce(new Response(streamOf(['{"id":1}\n'])));
    const res = await AzureOpenAIGetBatchOutputRequestHandler({
      c,
      providerOptions: azureOptions,
      requestURL: 'https://gw/v1/batches/batch_1/output',
    });
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toBe('application/octet-stream');
    expect(await res.text()).toBe('{"id":1}\n');
  });

  test('Azure maps a failed fetch to a 500 error response', async () => {
    fetchMock
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ output_file_id: 'file-1' }))
      )
      .mockRejectedValueOnce(new Error('socket hang up'));
    const res = await AzureOpenAIGetBatchOutputRequestHandler({
      c,
      providerOptions: azureOptions,
      requestURL: 'https://gw/v1/batches/batch_1/output',
    });
    expect(res.status).toBe(500);
    const body: any = await res.json();
    expect(body.error.message).toContain('Unable to get batch output');
  });
});

describe('Vertex batch output and upload (#108)', () => {
  const vertexOptions: any = {
    provider: 'vertex-ai',
    apiKey: 'tok',
    vertexProjectId: 'proj',
    vertexRegion: 'us-central1',
  };

  test('probes for the embeddings output file and streams transformed rows', async () => {
    fetchMock.mockImplementation(async (url: string, init: any) => {
      if (url.includes('batchPredictionJobs')) {
        return new Response(
          JSON.stringify({
            model: 'publishers/google/models/text-embedding-004',
            outputInfo: { gcsOutputDirectory: 'gs://bucket/out' },
          })
        );
      }
      if (init?.method === 'HEAD') {
        return new Response(null, {
          status: url.endsWith('/000000000000.jsonl') ? 200 : 404,
        });
      }
      expect(url).toBe(
        'https://storage.googleapis.com/bucket/out/000000000000.jsonl'
      );
      return new Response(
        streamOf([
          '{"instance":{"requestId":"r1"},"predictions":[{"embeddings":{"values":[0.1]}}]}\n',
          'not json\n',
        ])
      );
    });
    const res = await VertexBatchOutputRequestHandler({
      c,
      providerOptions: vertexOptions,
      requestURL: 'https://gw/v1/batches/123/output',
      requestBody: {},
      requestHeaders: {},
    } as any);
    expect(res.headers.get('content-type')).toBe('application/octet-stream');
    const lines = (await res.text()).trim().split('\n');
    expect(lines).toHaveLength(1);
    const row = JSON.parse(lines[0]);
    expect(row.id).toBe('batch-123');
    expect(row.custom_id).toBe('r1');
  });

  test('returns an error response when the batch lookup fails', async () => {
    fetchMock.mockResolvedValueOnce(new Response('denied', { status: 403 }));
    const res = await VertexBatchOutputRequestHandler({
      c,
      providerOptions: vertexOptions,
      requestURL: 'https://gw/v1/batches/123/output',
      requestBody: {},
      requestHeaders: {},
    } as any);
    expect(res.status).toBe(403);
    const body: any = await res.json();
    expect(body.error.message).toContain('denied');
  });

  test('upload transforms each JSONL line, including split multi-byte text', async () => {
    let uploaded = '';
    fetchMock.mockImplementation(async (_url: string, init: any) => {
      for (const chunk of await readAll(init.body)) {
        uploaded += decoder.decode(chunk, { stream: true });
      }
      return new Response('{}');
    });
    const line = (id: string, text: string) =>
      JSON.stringify({
        custom_id: id,
        method: 'POST',
        url: '/v1/chat/completions',
        body: { model: 'gemini', messages: [{ role: 'user', content: text }] },
      });
    const bytes = encoder.encode(`${line('a', 'héllo')}\n${line('b', 'hi')}\n`);
    const cut = bytes.indexOf(0xc3) + 1;
    const res = await GoogleFileUploadRequestHandler({
      c,
      providerOptions: {
        ...vertexOptions,
        vertexStorageBucketName: 'bucket',
        vertexModelName: 'publishers/google/models/gemini-2.5-flash',
        filename: 'in.jsonl',
      },
      requestBody: streamOf([bytes.slice(0, cut), bytes.slice(cut)]),
      requestHeaders: { 'x-portkey-file-purpose': 'batch' },
      requestURL: 'https://gw/v1/files',
    } as any);
    expect(res.status).toBe(200);
    expect(fetchMock.mock.calls[0][0]).toBe(
      'https://storage.googleapis.com/bucket/in.jsonl'
    );
    const rows = uploaded
      .trim()
      .split('\n')
      .map((l) => JSON.parse(l));
    expect(rows).toHaveLength(2);
    expect(JSON.stringify(rows[0])).toContain('héllo');
    expect(JSON.stringify(rows[1])).toContain('"b"');
  });

  test('createBatch passes resource-name models through', () => {
    const transform = (GoogleBatchCreateConfig.model as any).transform;
    expect(transform({ model: 'projects/p/locations/l/models/123' })).toBe(
      'projects/p/locations/l/models/123'
    );
    expect(transform({ model: 'anthropic.claude-x' })).toBe(
      'publishers/anthropic/models/claude-x'
    );
  });
});
