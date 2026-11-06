import FireworksAIAPIConfig from '../api';

// PS fork (#208): Fireworks upstream URL parts are checked before use
// (Portkey enterprise changelog 2.31, re-implemented).
const baseURL = (fn: string, fireworksAccountId?: unknown) =>
  FireworksAIAPIConfig.getBaseURL({
    fn,
    providerOptions: { provider: 'fireworks-ai', fireworksAccountId },
  } as any);

const endpoint = (fn: string, body: Record<string, unknown>, url: string) =>
  FireworksAIAPIConfig.getEndpoint({
    fn,
    providerOptions: { provider: 'fireworks-ai' },
    gatewayRequestBodyJSON: body,
    gatewayRequestURL: url,
  } as any);

describe('Fireworks AI upstream URL hardening (#208)', () => {
  it('builds account URLs for a valid account id', () => {
    expect(baseURL('chatComplete')).toBe(
      'https://api.fireworks.ai/inference/v1'
    );
    expect(baseURL('listFiles', 'my-account_1')).toBe(
      'https://api.fireworks.ai/v1/accounts/my-account_1'
    );
  });

  it('rejects an account id that would change the upstream path', () => {
    for (const id of ['../other', 'a/b', 'a?x=1', 'a#b', '', undefined]) {
      expect(() => baseURL('listFiles', id)).toThrow(
        'Invalid fireworksAccountId'
      );
    }
  });

  it('allows model paths but not traversal in image generation', () => {
    const url = 'https://gateway.local/v1/images/generations';
    expect(
      endpoint(
        'imageGenerate',
        { model: 'accounts/fireworks/models/stable-diffusion-xl-1024-v1-0' },
        url
      )
    ).toBe(
      '/image_generation/accounts/fireworks/models/stable-diffusion-xl-1024-v1-0'
    );
    for (const model of [
      'accounts/fireworks/models/../../other/x',
      'accounts//models/x',
      'x?y=1',
      'x#y',
      '/x',
    ]) {
      expect(() => endpoint('imageGenerate', { model }, url)).toThrow(
        'Invalid model for fireworks-ai'
      );
    }
  });

  it('encodes the list query parameters', () => {
    expect(
      endpoint(
        'listFiles',
        {},
        'https://gateway.local/v1/files?limit=5&after=2%26pageSize%3D1000'
      )
    ).toBe('/datasets?pageToken=2%26pageSize%3D1000&pageSize=5');
  });
});
