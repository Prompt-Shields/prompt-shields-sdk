// utils/env uses top-level await, which ts-jest cannot compile here (#69).
jest.mock('../../../utils/env', () => ({ Environment: () => ({}) }));

import { GoogleApiConfig, assertVertexUrlParts } from '../api';

// PS fork (#208): Vertex upstream URL parts are checked before use
// (Portkey enterprise changelog 2.31, re-implemented).
const providerOptions = (extra: Record<string, unknown> = {}) =>
  ({
    provider: 'vertex-ai',
    vertexRegion: 'us-central1',
    vertexProjectId: 'my-project',
    ...extra,
  }) as any;

const baseURL = (options: any, fn = 'chatComplete') =>
  GoogleApiConfig.getBaseURL({ providerOptions: options, fn } as any);

const endpoint = (
  options: any,
  body: Record<string, unknown>,
  fn = 'chatComplete',
  url = 'https://gateway.local/v1/chat/completions'
) =>
  GoogleApiConfig.getEndpoint({
    fn,
    providerOptions: options,
    gatewayRequestBodyJSON: body,
    gatewayRequestURL: url,
  } as any);

describe('Vertex AI upstream URL hardening (#208)', () => {
  it('builds the usual URLs for valid parts', () => {
    expect(baseURL(providerOptions())).toBe(
      'https://us-central1-aiplatform.googleapis.com'
    );
    expect(baseURL(providerOptions({ vertexRegion: 'global' }))).toBe(
      'https://aiplatform.googleapis.com'
    );
    expect(endpoint(providerOptions(), { model: 'gemini-2.5-pro' })).toBe(
      '/v1/projects/my-project/locations/us-central1/publishers/google/models/gemini-2.5-pro:generateContent'
    );
    expect(
      endpoint(providerOptions({ vertexProjectId: 'example.com:my-project' }), {
        model: 'anthropic.claude-sonnet-4@20250514',
      })
    ).toBe(
      '/v1/projects/example.com:my-project/locations/us-central1/publishers/anthropic/models/claude-sonnet-4@20250514:rawPredict'
    );
  });

  it('rejects a region that would rewrite the upstream host', () => {
    for (const vertexRegion of [
      'evil.example/#',
      'evil.example?',
      'us-central1.evil.example',
      'US Central',
      '',
    ]) {
      expect(() => baseURL(providerOptions({ vertexRegion }))).toThrow(
        'Invalid vertexRegion'
      );
      expect(() =>
        endpoint(providerOptions({ vertexRegion }), { model: 'gemini-2.5-pro' })
      ).toThrow('Invalid vertexRegion');
    }
  });

  it('rejects a project id or model that would change the upstream path', () => {
    for (const vertexProjectId of ['a/../../b', 'p?x=1', 'p#frag', 'p%2F']) {
      expect(() =>
        endpoint(providerOptions({ vertexProjectId }), {
          model: 'gemini-2.5-pro',
        })
      ).toThrow('Invalid vertexProjectId');
    }
    for (const model of [
      'gemini/../../../endpoints/x',
      'gemini:streamGenerateContent?alt=sse#',
      'anthropic.claude/../x',
      'gemini%2F..',
    ]) {
      expect(() => endpoint(providerOptions(), { model })).toThrow(
        'Invalid model for vertex-ai'
      );
    }
  });

  it('checks the project of batch routes and encodes their query', () => {
    expect(() =>
      endpoint(
        providerOptions({ vertexProjectId: 'x/../y' }),
        {},
        'listBatches',
        'https://gateway.local/v1/batches'
      )
    ).toThrow('Invalid vertexProjectId');
    expect(
      endpoint(
        providerOptions(),
        {},
        'listBatches',
        'https://gateway.local/v1/batches?limit=5&after=a%26pageSize%3D1000'
      )
    ).toBe(
      '/v1/projects/my-project/locations/us-central1/batchPredictionJobs?pageSize=5&pageToken=a%26pageSize%3D1000'
    );
  });

  it('leaves unset parts to fail upstream as before', () => {
    expect(() => assertVertexUrlParts({})).not.toThrow();
  });
});
