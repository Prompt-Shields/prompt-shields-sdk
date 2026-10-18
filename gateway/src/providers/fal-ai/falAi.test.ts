// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../../utils/env', () => ({ Environment: () => ({}) }));

import Providers from '..';
import { FAL_AI, VALID_PROVIDERS } from '../../globals';
import { transformUsingProviderConfig } from '../../services/transformToProviderRequest';

const config: any = Providers[FAL_AI];
const transform = (params: object) =>
  transformUsingProviderConfig(config.imageGenerate, params as any);
const respond = config.responseTransforms.imageGenerate;

test('is registered as a valid provider', () => {
  expect(VALID_PROVIDERS).toContain('fal-ai');
  expect(config.api.getBaseURL({})).toBe('https://fal.run');
});

describe('api', () => {
  test('the model id is the path', () => {
    expect(
      config.api.getEndpoint({
        fn: 'imageGenerate',
        gatewayRequestBodyJSON: { model: 'fal-ai/flux-pro/v1.1-ultra' },
      })
    ).toBe('/fal-ai/flux-pro/v1.1-ultra');
  });

  test.each([
    undefined,
    42,
    'flux',
    'fal-ai/../x',
    'fal-ai//x',
    '/fal-ai/x',
    'fal-ai/x?y',
    'fal-ai/x#y',
    'a@b/c',
    'fal-ai/x/',
  ])('rejects model %p', (model) => {
    expect(() =>
      config.api.getEndpoint({
        fn: 'imageGenerate',
        gatewayRequestBodyJSON: { model },
      })
    ).toThrow(/fal model id/);
  });

  test('auth is `Key <key>`, whichever scheme the caller used', () => {
    expect(config.api.headers({ providerOptions: { apiKey: 'k1' } })).toEqual({
      Authorization: 'Key k1',
    });
    expect(
      config.api.headers({ providerOptions: { apiKey: 'Key k2' } })
    ).toEqual({ Authorization: 'Key k2' });
    expect(() => config.api.headers({ providerOptions: {} })).toThrow(
      /API key/
    );
  });
});

describe('request transform', () => {
  test('maps the OpenAI image parameters to fal inputs', () => {
    expect(
      transform({
        model: 'fal-ai/flux/dev',
        prompt: 'a cat',
        n: 2,
        size: '512x768',
        output_format: 'png',
        response_format: 'b64_json',
        seed: 7,
        guidance_scale: 3.5,
      })
    ).toEqual({
      prompt: 'a cat',
      num_images: 2,
      image_size: { width: 512, height: 768 },
      output_format: 'png',
      sync_mode: true,
      seed: 7,
      guidance_scale: 3.5,
    });
  });

  test('passes fal size presets through and leaves auto to the model', () => {
    expect(transform({ prompt: 'x', size: 'landscape_16_9' })).toEqual({
      prompt: 'x',
      image_size: 'landscape_16_9',
    });
    expect(
      transform({ prompt: 'x', size: 'auto', response_format: 'url' })
    ).toEqual({
      prompt: 'x',
    });
  });
});

describe('response transform', () => {
  test('returns OpenAI image data', () => {
    const out = respond(
      {
        images: [
          { url: 'https://v3.fal.media/a.png', width: 1, height: 1 },
          { url: 'data:image/jpeg;base64,/9j/4AAQ' },
        ],
        seed: 1,
      },
      200
    );
    expect(out.provider).toBe('fal-ai');
    expect(out.data).toEqual([
      { url: 'https://v3.fal.media/a.png' },
      { b64_json: '/9j/4AAQ' },
    ]);
  });

  test('maps fal errors', () => {
    expect(respond({ detail: 'Unauthorized' }, 401)).toMatchObject({
      error: { message: 'fal-ai error: Unauthorized' },
      provider: 'fal-ai',
    });
    expect(
      respond(
        { detail: [{ loc: ['body', 'image_size'], msg: 'bad size' }] },
        422
      ).error.message
    ).toBe('fal-ai error: body.image_size: bad size');
    expect(respond({ foo: 1 }, 200).error).toBeDefined();
  });
});
