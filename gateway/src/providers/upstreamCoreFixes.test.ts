// utils/env uses top-level await, which ts-jest cannot compile here (#69).
jest.mock('../utils/env', () => ({ Environment: () => ({}) }));

import { BedrockConverseChatCompleteConfig } from './bedrock/chatComplete';
import { FireworksAIChatCompleteConfig } from './fireworks-ai/chatComplete';
import { transformUsingProviderConfig } from '../services/transformToProviderRequest';

const bedrockMessages = (messages: any[]) =>
  (
    transformUsingProviderConfig(BedrockConverseChatCompleteConfig, {
      model: 'anthropic.claude-3-5-sonnet',
      messages,
    } as any) as any
  ).messages;

describe('fixes ported for #108', () => {
  test('Bedrock: a tool call with empty arguments becomes {} instead of throwing', () => {
    const out = bedrockMessages([
      { role: 'user', content: 'hi' },
      {
        role: 'assistant',
        content: '',
        tool_calls: [
          {
            id: 't1',
            type: 'function',
            function: { name: 'now', arguments: '' },
          },
        ],
      },
      { role: 'tool', tool_call_id: 't1', content: '12:00' },
    ]);
    const toolUse = out[1].content.find((c: any) => c.toolUse).toolUse;
    expect(toolUse).toEqual({ name: 'now', input: {}, toolUseId: 't1' });
  });

  test('Bedrock: a document-only message gets the text block Converse requires', () => {
    const out = bedrockMessages([
      {
        role: 'user',
        content: [
          {
            type: 'image_url',
            image_url: { url: 'data:application/pdf;base64,JVBERi0=' },
          },
        ],
      },
    ]);
    expect(out[0].content[0]).toEqual({ text: ' ' });
    expect(out[0].content[1].document).toBeDefined();
  });

  test('Fireworks: prompt_cache_max_len is passed through', () => {
    expect(
      (FireworksAIChatCompleteConfig as any).prompt_cache_max_len.param
    ).toBe('prompt_cache_max_len');
  });
});
