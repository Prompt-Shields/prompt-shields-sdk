// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../../utils/env', () => ({ Environment: () => ({}) }));

import { OpenAIChatCompleteConfig } from './chatComplete';
import { OpenAICreateModelResponseConfig } from '../open-ai-base/createModelResponse';
import { transformUsingProviderConfig } from '../../services/transformToProviderRequest';

// PS fork (#208): parameters the Portkey enterprise changelog 2.32/2.33 says
// are now forwarded; before, the provider config dropped them silently.
describe('OpenAI parameter pass-through (#208)', () => {
  test('chat completions forward moderation', () => {
    const out = transformUsingProviderConfig(OpenAIChatCompleteConfig, {
      model: 'gpt-5',
      messages: [{ role: 'user', content: 'hi' }],
      moderation: { mode: 'block' },
    } as any);
    expect(out.moderation).toEqual({ mode: 'block' });
  });

  test('responses forward prompt_cache_options', () => {
    const out = transformUsingProviderConfig(OpenAICreateModelResponseConfig, {
      model: 'gpt-5',
      input: 'hi',
      prompt_cache_options: { retention: '24h' },
    } as any);
    expect(out.prompt_cache_options).toEqual({ retention: '24h' });
  });
});
