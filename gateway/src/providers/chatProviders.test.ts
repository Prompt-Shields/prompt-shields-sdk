// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../utils/env', () => ({ Environment: () => ({}) }));

import Providers from '.';
import { VALID_PROVIDERS } from '../globals';
import { isSafeUpstreamURL } from '../middlewares/requestValidator';
import { transformUsingProviderConfig } from '../services/transformToProviderRequest';

// OpenAI-compatible chat providers added for #74.
const cases: [string, string, boolean][] = [
  ['minimax', 'https://api.minimax.io/v1', false],
  ['byteplus', 'https://ark.ap-southeast.bytepluses.com/api/v3', true],
  ['lightning-ai', 'https://lightning.ai/api/v1', false],
  ['berget', 'https://api.berget.ai/v1', true],
  ['scaleway', 'https://api.scaleway.ai/v1', true],
  ['regolo', 'https://api.regolo.ai/v1', true],
  ['ionos', 'https://openai.inference.de-txl.ionos.com/v1', true],
  [
    'stackit',
    'https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1',
    true,
  ],
  ['meta', 'https://api.llama.com/compat/v1', false],
  ['scx-ai', 'https://api.scx.ai/v1', false],
];

const c: any = { get: () => undefined, env: {} };

describe.each(cases)('%s', (provider, baseURL, hasEmbed) => {
  const config: any = Providers[provider];

  test('is registered as a valid provider', () => {
    expect(VALID_PROVIDERS).toContain(provider);
    expect(config).toBeDefined();
  });

  test('uses a fixed vendor base URL, ignoring caller-supplied hosts', async () => {
    const url = await config.api.getBaseURL({
      providerOptions: {
        apiKey: 'k',
        customHost: 'http://169.254.169.254',
        region: '169.254.169.254/latest#',
      },
      fn: 'chatComplete',
      c,
      gatewayRequestURL: '',
    });
    expect(url).toBe(baseURL);
    expect(isSafeUpstreamURL(url)).toBe(true);
    expect(config.api.getEndpoint({ fn: 'chatComplete' })).toBe(
      '/chat/completions'
    );
    expect(config.api.headers({ providerOptions: { apiKey: 'k' } })).toEqual({
      Authorization: 'Bearer k',
    });
  });

  test('chat request keeps the OpenAI shape and maps developer to system', () => {
    const body: any = transformUsingProviderConfig(config.chatComplete, {
      model: 'some-model',
      messages: [
        { role: 'developer', content: 'be brief' },
        { role: 'user', content: 'hi' },
      ],
      max_tokens: 16,
      stream: true,
    } as any);
    expect(body).toMatchObject({
      model: 'some-model',
      messages: [
        { role: 'system', content: 'be brief' },
        { role: 'user', content: 'hi' },
      ],
      max_tokens: 16,
      stream: true,
    });
  });

  test('chat response is tagged with the provider', () => {
    const out = config.responseTransforms.chatComplete(
      {
        id: 'x',
        object: 'chat.completion',
        created: 1,
        model: 'some-model',
        choices: [
          {
            index: 0,
            message: { role: 'assistant', content: 'ok' },
            finish_reason: 'stop',
          },
        ],
        usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
      },
      200
    );
    expect(out.provider).toBe(provider);
    expect(out.choices[0].message.content).toBe('ok');
  });

  test(`embeddings ${hasEmbed ? 'are' : 'are not'} routed`, () => {
    expect(config.api.getEndpoint({ fn: 'embed' })).toBe(
      hasEmbed ? '/embeddings' : ''
    );
    if (hasEmbed) {
      expect(
        transformUsingProviderConfig(config.embed, {
          model: 'e5',
          input: ['a'],
        } as any)
      ).toEqual({ model: 'e5', input: ['a'] });
    } else {
      expect(config.embed).toBeUndefined();
    }
  });
});
