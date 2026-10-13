// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../utils/env', () => ({ Environment: () => ({}) }));

import DatabricksAPIConfig from './databricks/api';
import PineconeAPIConfig from './pinecone/api';
import { PineconeRerankConfig } from './pinecone/rerank';
import { LatitudeChatCompleteResponseTransform } from './latitude/chatComplete';
import { isSafeUpstreamURL } from '../middlewares/requestValidator';

const c: any = { get: () => undefined, env: {} };
const base = (api: any, providerOptions: any, fn = 'chatComplete') =>
  api.getBaseURL({ providerOptions, fn, c, gatewayRequestURL: '' });

describe('databricks', () => {
  test('builds the workspace serving-endpoints URL', async () => {
    const url = await base(DatabricksAPIConfig, {
      databricksWorkspace: 'dbc-a1b2c3d4-e5f6',
    });
    expect(url).toBe(
      'https://dbc-a1b2c3d4-e5f6.cloud.databricks.com/serving-endpoints'
    );
    expect(isSafeUpstreamURL(url)).toBe(true);
    expect(
      DatabricksAPIConfig.getEndpoint({
        fn: 'chatComplete',
        gatewayRequestBodyJSON: { model: 'my-endpoint' },
      } as any)
    ).toBe('/my-endpoint/invocations');
  });

  test('requires a workspace', async () => {
    await expect(async () => base(DatabricksAPIConfig, {})).rejects.toThrow(
      'workspace'
    );
  });

  test('a workspace value cannot point the gateway at internal hosts', async () => {
    const url = await base(DatabricksAPIConfig, {
      databricksWorkspace: '169.254.169.254/latest/meta-data#',
    });
    expect(isSafeUpstreamURL(url)).toBe(false);
  });
});

describe('pinecone', () => {
  test('rerank goes to api.pinecone.io/rerank with an Api-Key header', async () => {
    expect(
      (await base(PineconeAPIConfig, {}, 'rerank')) +
        PineconeAPIConfig.getEndpoint({ fn: 'rerank' } as any)
    ).toBe('https://api.pinecone.io/rerank');
    expect(
      (PineconeAPIConfig.headers as any)({ providerOptions: { apiKey: 'pc' } })
    ).toMatchObject({ 'Api-Key': 'pc' });
  });

  test('documents become {id, text} objects', () => {
    const docs = (PineconeRerankConfig as any).documents.transform({
      model: 'bge-reranker-v2-m3',
      query: 'q',
      documents: ['a', { text: 'b' }],
    });
    expect(docs).toEqual([
      { id: 'doc_0', text: 'a' },
      { id: 'doc_1', text: 'b' },
    ]);
  });
});

describe('latitude', () => {
  test('chat response is tagged with the provider', () => {
    const out: any = LatitudeChatCompleteResponseTransform(
      {
        id: 'x',
        object: 'chat.completion',
        created: 1,
        model: 'qwen',
        choices: [
          {
            index: 0,
            message: { role: 'assistant', content: 'hi' },
            finish_reason: 'stop',
          },
        ],
        usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
      } as any,
      200,
      new Headers(),
      false
    );
    expect(out.provider).toBe('latitude');
    expect(out.choices[0].finish_reason).toBe('stop');
  });
});
