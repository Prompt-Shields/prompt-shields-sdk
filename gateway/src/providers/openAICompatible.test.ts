// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../utils/env', () => ({ Environment: () => ({}) }));

import * as fs from 'fs';
import * as path from 'path';
import Providers from '.';
import { VALID_PROVIDERS } from '../globals';
import { isSafeUpstreamURL } from '../middlewares/requestValidator';
import {
  policyProviderId,
  servedJurisdiction,
  tierServesTarget,
} from '../middlewares/psServedTarget';
import { ModelTier } from '../middlewares/router/types';

const catalog = JSON.parse(
  fs.readFileSync(
    path.resolve(__dirname, '../../../schemas/catalog/source.v1.json'),
    'utf8'
  )
);

// Gateway provider id -> catalog provider id (#20).
const SOVEREIGN: [string, string, boolean][] = [
  ['berget', 'berget', true],
  ['scaleway', 'scaleway', true],
  ['regolo', 'regolo', true],
  ['ionos', 'ionos', true],
  ['stackit', 'stackit', true],
  ['infercom', 'infercom', false],
  ['ovhcloud', 'ovhcloud', false],
  ['mistral-ai', 'mistral', true],
];

const c: any = { get: () => undefined, env: {} };

describe.each(SOVEREIGN)('%s', (provider, catalogId, hasEmbed) => {
  const config: any = Providers[provider];

  test('base URL matches the model catalog and is a safe upstream', async () => {
    expect(VALID_PROVIDERS).toContain(provider);
    const url = await config.api.getBaseURL({
      providerOptions: { apiKey: 'k', customHost: 'http://169.254.169.254' },
      fn: 'chatComplete',
      c,
      gatewayRequestURL: '',
    });
    expect(url).toBe(catalog.providers[catalogId].api_base);
    expect(catalog.providers[catalogId].openai_compatible).toBe(true);
    expect(isSafeUpstreamURL(url)).toBe(true);
    expect(policyProviderId(provider)).toBe(catalogId);
  });

  test('embeddings only where the vendor offers them', () => {
    expect(!!config.embed).toBe(hasEmbed);
  });
});

test('infercom sends bearer auth to chat completions', () => {
  const api: any = Providers.infercom.api;
  expect(api.headers({ providerOptions: { apiKey: 'k' } })).toEqual({
    Authorization: 'Bearer k',
  });
  expect(api.getEndpoint({ fn: 'chatComplete' })).toBe('/chat/completions');
  expect(api.getEndpoint({ fn: 'embed' })).toBe('');
});

const tier = (extra: Partial<ModelTier>): ModelTier => ({
  model: 'm',
  group: 'g',
  inputCostPer1k: 0,
  outputCostPer1k: 0,
  ...extra,
});

describe('tierServesTarget', () => {
  test('a tier without provider states facts about the model alone', () => {
    expect(tierServesTarget(tier({}), { provider: 'openai' })).toBe(true);
  });

  test('a provider tier applies only to that provider', () => {
    const t = tier({ provider: 'scaleway' });
    expect(tierServesTarget(t, { provider: 'scaleway' })).toBe(true);
    expect(tierServesTarget(t, { provider: 'openai' })).toBe(false);
    expect(
      tierServesTarget(tier({ provider: 'mistral' }), {
        provider: 'mistral-ai',
      })
    ).toBe(true);
  });

  test("a custom host keeps the facts only at the provider's own origin", () => {
    const t = tier({ provider: 'scaleway' });
    expect(
      tierServesTarget(t, {
        provider: 'scaleway',
        customHost: 'https://API.scaleway.ai/v1/',
      })
    ).toBe(true);
    expect(
      tierServesTarget(t, {
        provider: 'scaleway',
        customHost: 'https://api.scaleway.ai.evil.example/v1',
      })
    ).toBe(false);
    expect(
      tierServesTarget(tier({ provider: 'eu-vllm' }), {
        provider: 'eu-vllm',
        customHost: 'https://vllm.example.eu/v1',
      })
    ).toBe(false);
  });
});

test('servedJurisdiction needs the applicable tiers to agree', () => {
  const policy: any = {
    groups: {
      a: [tier({ provider: 'berget', jurisdiction: 'SE' })],
      b: [
        tier({ provider: 'openai', jurisdiction: 'US' }),
        tier({ model: 'x', jurisdiction: 'EEA' }),
        tier({ model: 'x', jurisdiction: 'US' }),
      ],
    },
  };
  expect(servedJurisdiction(policy, 'm', { provider: 'berget' })).toBe('SE');
  expect(servedJurisdiction(policy, 'm', { provider: 'openai' })).toBe('US');
  expect(servedJurisdiction(policy, 'm', { provider: 'groq' })).toBeUndefined();
  expect(servedJurisdiction(policy, 'x', { provider: 'groq' })).toBeUndefined();
  expect(servedJurisdiction(undefined, 'm', {})).toBeUndefined();
});
