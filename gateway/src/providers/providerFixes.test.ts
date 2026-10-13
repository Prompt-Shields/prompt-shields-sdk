// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../utils/env', () => ({ Environment: () => ({}) }));

import {
  OpenAIChatCompleteConfig,
  OpenAIChatCompleteResponseTransform,
} from './openai/chatComplete';
import { AzureOpenAIImageGenerateConfig } from './azure-openai/imageGenerate';
import { OpenAIImageGenerateConfig } from './openai/imageGenerate';
import { AzureImageEditRequestTransform } from './azure-openai/imageEdit';
import ZAIConfig from './z-ai';
import ZAIAPIConfig from './z-ai/api';

describe('provider fixes ported from Portkey 2.0.0 (#60)', () => {
  test('OpenAI passes prompt_cache_retention and returns service_tier', () => {
    expect((OpenAIChatCompleteConfig as any).prompt_cache_retention.param).toBe(
      'prompt_cache_retention'
    );
    const out: any = OpenAIChatCompleteResponseTransform(
      {
        id: 'x',
        object: 'chat.completion',
        created: 1,
        model: 'gpt-4o',
        service_tier: 'flex',
        choices: [
          {
            index: 0,
            message: { role: 'assistant', content: 'hi' },
            finish_reason: 'stop',
          },
        ],
        usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
      } as any,
      200
    );
    expect(out.service_tier).toBe('flex');
  });

  test.each([
    ['openai', OpenAIImageGenerateConfig],
    ['azure-openai', AzureOpenAIImageGenerateConfig],
  ])('%s image generation passes gpt-image-1 parameters', (_p, config: any) => {
    for (const k of [
      'moderation',
      'output_format',
      'output_compression',
      'background',
      'partial_images',
      'stream',
    ]) {
      expect(config[k]?.param).toBe(k);
    }
  });

  test('Azure image edits drop `model` unless the v1 API is used', () => {
    const form = new FormData();
    form.set('model', 'gpt-image-1');
    form.set('prompt', 'p');
    expect(AzureImageEditRequestTransform(form, {}).has('model')).toBe(false);

    const v1 = new FormData();
    v1.set('model', 'gpt-image-1');
    expect(
      AzureImageEditRequestTransform(v1, {
        'x-portkey-azure-api-version': 'v1',
      }).has('model')
    ).toBe(true);
  });

  test('Z.ai supports image generation', () => {
    expect((ZAIConfig as any).imageGenerate).toBeDefined();
    expect(ZAIAPIConfig.getEndpoint({ fn: 'imageGenerate' } as any)).toBe(
      '/images/generations'
    );
  });
});
