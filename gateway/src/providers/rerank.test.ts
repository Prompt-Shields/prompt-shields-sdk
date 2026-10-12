import {
  BedrockRerankConfig,
  BedrockRerankResponseTransform,
} from './bedrock/rerank';
import {
  CohereRerankConfig,
  CohereRerankResponseTransform,
} from './cohere/rerank';
import { JinaRerankConfig, JinaRerankResponseTransform } from './jina/rerank';
import {
  VoyageRerankConfig,
  VoyageRerankResponseTransform,
} from './voyage/rerank';

const request: any = {
  model: 'rerank-model',
  query: 'capital of france',
  documents: ['Paris is the capital', { text: 'Berlin is in Germany' }],
  top_n: 1,
};

const headers = new Headers();

describe.each([
  ['cohere', CohereRerankConfig],
  ['jina', JinaRerankConfig],
  ['voyage', VoyageRerankConfig],
])('%s rerank request config', (_name, config: any) => {
  test('flattens object documents to strings', () => {
    const docs = config.documents.transform(request);
    expect(docs).toEqual(['Paris is the capital', 'Berlin is in Germany']);
  });

  test('requires query and documents', () => {
    expect(config.query.required).toBe(true);
    expect(config.documents.required).toBe(true);
  });
});

describe('rerank response transforms map to the unified shape', () => {
  test('cohere', () => {
    const out: any = CohereRerankResponseTransform(
      {
        id: 'abc',
        results: [{ index: 0, relevance_score: 0.9 }],
        meta: { billed_units: { search_units: 1 } },
      } as any,
      200,
      headers,
      false,
      '',
      request
    );
    expect(out).toMatchObject({
      object: 'list',
      provider: 'cohere',
      model: 'rerank-model',
      results: [{ index: 0, relevance_score: 0.9 }],
      usage: { search_units: 1 },
    });
  });

  test('jina', () => {
    const out: any = JinaRerankResponseTransform(
      {
        model: 'jina-reranker-v2',
        results: [{ index: 1, relevance_score: 0.4 }],
        usage: { total_tokens: 12 },
      } as any,
      200,
      headers,
      false,
      '',
      request
    );
    expect(out.object).toBe('list');
    expect(out.provider).toBe('jina');
    expect(out.results[0]).toMatchObject({ index: 1, relevance_score: 0.4 });
  });

  test('voyage', () => {
    const out: any = VoyageRerankResponseTransform(
      {
        model: 'rerank-2',
        data: [{ index: 0, relevance_score: 0.8 }],
        usage: { total_tokens: 30 },
      } as any,
      200,
      headers,
      false,
      '',
      request
    );
    expect(out).toMatchObject({
      object: 'list',
      provider: 'voyage',
      model: 'rerank-2',
      usage: { search_units: 30 },
    });
  });

  test('provider errors become gateway error responses', () => {
    const out: any = CohereRerankResponseTransform(
      { message: 'invalid model' } as any,
      400,
      headers,
      false,
      '',
      request
    );
    expect(out.error.message).toContain('invalid model');
  });

  test('bedrock request uses the Agents runtime shape', () => {
    const cfg: any = BedrockRerankConfig;
    expect(
      cfg.model.transform({
        ...request,
        model: 'arn:aws:bedrock:us-west-2::foundation-model/amazon.rerank-v1:0',
      })
    ).toEqual({
      type: 'BEDROCK_RERANKING_MODEL',
      bedrockRerankingConfiguration: {
        modelConfiguration: {
          modelArn:
            'arn:aws:bedrock:us-west-2::foundation-model/amazon.rerank-v1:0',
        },
        numberOfResults: 1,
      },
    });
    expect(cfg.query.transform(request)[0].textQuery.text).toBe(
      'capital of france'
    );
    expect(
      cfg.documents.transform(request)[1].inlineDocumentSource.textDocument.text
    ).toBe('Berlin is in Germany');
  });

  test('bedrock response', () => {
    const out: any = BedrockRerankResponseTransform(
      { results: [{ index: 0, relevanceScore: 0.7 }] } as any,
      200,
      headers,
      false,
      '',
      request
    );
    expect(out.object).toBe('list');
    expect(out.provider).toBe('bedrock');
    expect(out.results[0]).toMatchObject({ index: 0, relevance_score: 0.7 });
  });
});
