// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../../utils/env', () => ({ Environment: () => ({}) }));

import {
  CohereGetBatchOutputHandler,
  cohereDatasetPartRequest,
} from './getBatchOutput';

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
