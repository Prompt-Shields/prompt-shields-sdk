// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../../utils/env', () => ({ Environment: () => ({}) }));

import {
  CohereGetBatchOutputHandler,
  cohereDatasetPartRequest,
} from './getBatchOutput';
import avro from 'avsc';

const c: any = { req: { param: () => ({}) }, get: () => undefined };
const originalFetch = global.fetch;
let fetchMock: jest.Mock;

beforeEach(() => {
  fetchMock = jest.fn();
  global.fetch = fetchMock as any;
});

afterEach(() => {
  global.fetch = originalFetch;
});

const COHERE = 'https://api.cohere.ai';
const headers = { Authorization: 'Bearer co-secret' };

describe('cohereDatasetPartRequest (security)', () => {
  test('sends the key to the Cohere API origin', () => {
    expect(
      cohereDatasetPartRequest(`${COHERE}/v1/parts/1`, COHERE, headers, c)
    ).toEqual({ method: 'GET', headers });
  });

  test('fetches a presigned storage URL without the key', () => {
    const req = cohereDatasetPartRequest(
      'https://storage.googleapis.com/cohere-datasets/part-0.avro?X-Goog-Signature=abc',
      COHERE,
      headers,
      c
    );
    expect(req).toEqual({ method: 'GET' });
  });

  test.each([
    'http://storage.googleapis.com/part.avro',
    'https://169.254.169.254/latest/meta-data/',
    'https://metadata.google.internal/computeMetadata/v1/',
    'not a url',
  ])('refuses %s', (url) => {
    expect(cohereDatasetPartRequest(url, COHERE, headers, c)).toBeNull();
  });
});

describe('CohereGetBatchOutputHandler (security)', () => {
  test('does not send the Cohere key to a dataset part on another host', async () => {
    fetchMock
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ output_dataset_id: 'ds-1' }))
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            dataset: {
              dataset_parts: [
                { url: 'https://storage.googleapis.com/b/part-0.avro?sig=1' },
              ],
            },
          })
        )
      )
      .mockResolvedValueOnce(new Response(new Uint8Array()));
    const res = await CohereGetBatchOutputHandler({
      c,
      providerOptions: { provider: 'cohere', apiKey: 'co-secret' } as any,
      requestURL: 'https://gw.example/v1/batches/batch-1/output',
    });
    // Drain the stream so the part fetch runs; the empty avro body may error.
    await res.text().catch(() => {});
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const [, cohereOpts] = fetchMock.mock.calls[1];
    expect(JSON.stringify(cohereOpts.headers)).toContain('co-secret');
    const [partUrl, partOpts] = fetchMock.mock.calls[2];
    expect(partUrl).toBe('https://storage.googleapis.com/b/part-0.avro?sig=1');
    expect(partOpts.headers).toBeUndefined();
    expect(partOpts.redirect).toBe('manual');
  });
});

// An avro object container file whose records are JSON strings, as the
// handler expects of a Cohere embed-batch output part.
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

const embedRecord = (id: string, custom_id: string) => ({
  id,
  custom_id,
  embeddings: { float: { array: [0.1, 0.2] } },
});

const batchAndDataset = (parts: string[]) => {
  fetchMock
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ output_dataset_id: 'ds-1' }))
    )
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          dataset: { dataset_parts: parts.map((url) => ({ url })) },
        })
      )
    );
};

const callHandler = () =>
  CohereGetBatchOutputHandler({
    c,
    providerOptions: { provider: 'cohere', apiKey: 'co-secret' } as any,
    requestURL: 'https://gw.example/v1/batches/batch-1/output',
  });

describe('CohereGetBatchOutputHandler (output)', () => {
  test('streams every record of a multi-part output, in order', async () => {
    batchAndDataset([
      'https://storage.googleapis.com/b/part-0.avro?sig=1',
      'https://storage.googleapis.com/b/part-1.avro?sig=2',
      `${COHERE}/v1/datasets/ds-1/part-2.avro`,
    ]);
    fetchMock
      .mockResolvedValueOnce(
        new Response(
          await avroPart([embedRecord('e0', 'r0'), embedRecord('e1', 'r1')])
        )
      )
      .mockResolvedValueOnce(
        new Response(await avroPart([embedRecord('e2', 'r2')]))
      )
      .mockResolvedValueOnce(
        new Response(await avroPart([embedRecord('e3', 'r3')]))
      );

    const res = await callHandler();
    expect(res.status).toBe(200);
    const lines = (await res.text())
      .trim()
      .split('\n')
      .map((l) => JSON.parse(l));
    expect(lines.map((l) => l.custom_id)).toEqual(['r0', 'r1', 'r2', 'r3']);
    expect(lines[0].response.body.data[0].embedding).toEqual([0.1, 0.2]);

    // The Cohere key goes to the Cohere origin only (#209).
    expect(fetchMock).toHaveBeenCalledTimes(5);
    expect(fetchMock.mock.calls[2][1].headers).toBeUndefined();
    expect(fetchMock.mock.calls[3][1].headers).toBeUndefined();
    expect(JSON.stringify(fetchMock.mock.calls[4][1].headers)).toContain(
      'co-secret'
    );
  });

  test('a part on a refused host errors the stream and is not fetched', async () => {
    batchAndDataset([
      'https://storage.googleapis.com/b/part-0.avro?sig=1',
      'http://storage.googleapis.com/b/part-1.avro',
    ]);
    fetchMock.mockResolvedValueOnce(
      new Response(await avroPart([embedRecord('e0', 'r0')]))
    );
    const res = await callHandler();
    await expect(res.text()).rejects.toThrow(
      'Unexpected dataset part location'
    );
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  test('a failed part fetch errors the stream', async () => {
    batchAndDataset(['https://storage.googleapis.com/b/part-0.avro?sig=1']);
    fetchMock.mockResolvedValueOnce(new Response('denied', { status: 403 }));
    const res = await callHandler();
    await expect(res.text()).rejects.toThrow('403');
  });

  test('a dataset lookup error returns the provider message', async () => {
    fetchMock
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ output_dataset_id: 'ds-1' }))
      )
      .mockResolvedValueOnce(
        new Response('{"message":"dataset not found"}', { status: 404 })
      );
    const res = await callHandler();
    expect(res.status).toBe(500);
    const body: any = await res.json();
    expect(body).toMatchObject({ type: 'provider_error', code: 404 });
    expect(body.message).toContain('dataset not found');
  });
});
