// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../utils/env', () => ({ Environment: () => ({}) }));

import { GatewayError } from '../errors/GatewayError';
import { Options } from '../types/requestBody';
import { AzureOpenAIGetBatchOutputRequestHandler } from './azure-openai/getBatchOutput';
import { AzureAIInferenceGetBatchOutputRequestHandler } from './azure-ai-inference/getBatchOutput';
import { BatchOutputRequestHandler as VertexBatchOutputRequestHandler } from './google-vertex-ai/getBatchOutput';
import { GoogleFileUploadRequestHandler } from './google-vertex-ai/uploadFile';
import { BedrockUploadFileRequestHandler } from './bedrock/uploadFile';
import { BedrockGetBatchOutputRequestHandler } from './bedrock/getBatchOutput';

// Provider request handlers build and fetch their own URLs, so the upstream
// URL check in tryPostToProvider does not cover them. An injected resource
// name, region or bucket must be rejected before any fetch happens.

const fetchSpy = jest.spyOn(globalThis, 'fetch');

beforeEach(() => {
  fetchSpy.mockReset();
  fetchSpy.mockRejectedValue(new Error('no network in tests'));
});

afterAll(() => fetchSpy.mockRestore());

const makeContext = () =>
  ({
    env: {},
    get: () => undefined,
    set: () => undefined,
    req: { header: () => undefined, param: () => ({}) },
  }) as any;

const expectRejected = async (promise: Promise<unknown>) => {
  const err: any = await Promise.resolve(promise).catch((e) => e);
  expect(err).toBeInstanceOf(GatewayError);
  expect(err.status).toBe(400);
  expect(fetchSpy).not.toHaveBeenCalled();
};

const INTERNAL_PREFIXES = [
  '169.254.169.254/latest/meta-data#',
  '10.0.0.5/admin?',
  'x@169.254.169.254/#',
];

const batchOutputURL = 'https://gateway.local/v1/batches/batch_123/output';

describe('Azure OpenAI getBatchOutput', () => {
  test.each(INTERNAL_PREFIXES)('rejects resourceName %s', async (name) => {
    await expectRejected(
      AzureOpenAIGetBatchOutputRequestHandler({
        c: makeContext(),
        providerOptions: {
          provider: 'azure-openai',
          resourceName: name,
          apiKey: 'k',
          apiVersion: '2024-07-01-preview',
        } as Options,
        requestURL: batchOutputURL,
      })
    );
  });

  test('fetches a normal resource', async () => {
    await AzureOpenAIGetBatchOutputRequestHandler({
      c: makeContext(),
      providerOptions: {
        provider: 'azure-openai',
        resourceName: 'myresource',
        apiKey: 'k',
        apiVersion: '2024-07-01-preview',
      } as Options,
      requestURL: batchOutputURL,
    }).catch(() => {});
    expect(fetchSpy).toHaveBeenCalled();
    expect(String(fetchSpy.mock.calls[0][0])).toMatch(
      /^https:\/\/myresource\.openai\.azure\.com\/openai\/batches\/batch_123/
    );
  });
});

describe('Azure AI Inference getBatchOutput', () => {
  test.each([
    'http://169.254.169.254/latest/meta-data',
    'https://10.1.2.3/models',
    'http://192.168.1.10:8080/models',
  ])('rejects azureFoundryUrl %s', async (url) => {
    await expectRejected(
      AzureAIInferenceGetBatchOutputRequestHandler({
        c: makeContext(),
        providerOptions: {
          provider: 'azure-ai',
          azureFoundryUrl: url,
          apiKey: 'k',
        } as Options,
        requestURL: batchOutputURL,
      })
    );
  });
});

describe('Vertex getBatchOutput', () => {
  test.each(INTERNAL_PREFIXES)('rejects vertexRegion %s', async (region) => {
    await expectRejected(
      VertexBatchOutputRequestHandler({
        c: makeContext(),
        providerOptions: {
          provider: 'vertex-ai',
          vertexRegion: region,
          vertexProjectId: 'my-project',
          apiKey: 'k',
        } as Options,
        requestURL: batchOutputURL,
        requestBody: {},
        requestHeaders: {},
      } as any)
    );
  });

  test('fetches a normal region', async () => {
    await VertexBatchOutputRequestHandler({
      c: makeContext(),
      providerOptions: {
        provider: 'vertex-ai',
        vertexRegion: 'us-central1',
        vertexProjectId: 'my-project',
        apiKey: 'k',
      } as Options,
      requestURL: batchOutputURL,
      requestBody: {},
      requestHeaders: {},
    } as any).catch(() => {});
    expect(fetchSpy).toHaveBeenCalled();
    expect(String(fetchSpy.mock.calls[0][0])).toBe(
      'https://us-central1-aiplatform.googleapis.com/v1/projects/my-project/locations/us-central1/batchPredictionJobs/batch_123'
    );
  });
});

describe('Vertex uploadFile', () => {
  test.each(INTERNAL_PREFIXES)(
    'rejects vertexStorageBucketName %s',
    async (bucket) => {
      await expectRejected(
        GoogleFileUploadRequestHandler({
          c: makeContext(),
          providerOptions: {
            provider: 'vertex-ai',
            vertexStorageBucketName: bucket,
            vertexModelName: 'gemini-1.5-pro',
            apiKey: 'k',
          } as Options,
          requestURL: 'https://gateway.local/v1/files',
          requestBody: new ReadableStream(),
          requestHeaders: {},
        } as any)
      );
    }
  );
});

describe('Bedrock with apiKey auth (no SigV4 host check)', () => {
  test.each(INTERNAL_PREFIXES)(
    'uploadFile rejects awsS3Bucket %s',
    async (b) => {
      await expectRejected(
        BedrockUploadFileRequestHandler({
          c: makeContext(),
          providerOptions: {
            provider: 'bedrock',
            awsAuthType: 'apiKey',
            apiKey: 'k',
            awsRegion: 'us-east-1',
            awsS3Bucket: b,
            awsBedrockModel: 'anthropic.claude-3-haiku-20240307-v1:0',
          } as Options,
          requestURL: 'https://gateway.local/v1/files',
          requestBody: new ReadableStream(),
          requestHeaders: {},
        } as any)
      );
    }
  );

  test('getBatchOutput rejects an output bucket that rewrites the host', async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          jobArn: 'arn:aws:bedrock:us-east-1:123:model-invocation-job/job1',
          modelId: 'anthropic.claude-3-haiku-20240307-v1:0',
          outputDataConfig: {
            s3OutputDataConfig: { s3Uri: 's3://169.254.169.254#/out/' },
          },
          inputDataConfig: {
            s3InputDataConfig: { s3Uri: 's3://bucket/in/input.jsonl' },
          },
        }),
        { status: 200, headers: { 'content-type': 'application/json' } }
      )
    );
    const err = await BedrockGetBatchOutputRequestHandler({
      c: makeContext(),
      providerOptions: {
        provider: 'bedrock',
        awsAuthType: 'apiKey',
        apiKey: 'k',
        awsRegion: 'us-east-1',
      } as Options,
      requestURL: batchOutputURL,
    }).catch((e) => e);
    expect(err).toBeInstanceOf(GatewayError);
    expect(err.status).toBe(400);
    // Only the batch lookup against the Bedrock control plane went out.
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(String(fetchSpy.mock.calls[0][0])).toMatch(
      /^https:\/\/bedrock\.us-east-1\.amazonaws\.com\//
    );
  });
});
