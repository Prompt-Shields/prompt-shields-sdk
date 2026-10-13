// utils/env uses top-level await, which ts-jest cannot compile here (#69).
jest.mock('../utils/env', () => ({ Environment: () => ({}) }));

import {
  BedrockChatCompleteResponseTransform,
  BedrockConverseAnthropicChatCompleteConfig,
  BedrockConverseChatCompleteConfig,
} from './bedrock/chatComplete';
import BedrockConfig from './bedrock';
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

  test('Bedrock: anthropic_beta accepts the header string or the body array', () => {
    for (const anthropic_beta of [
      'context-1m-2025-08-07, interleaved-thinking-2025-05-14',
      ['context-1m-2025-08-07', 'interleaved-thinking-2025-05-14'],
    ]) {
      const out = transformUsingProviderConfig(
        BedrockConverseAnthropicChatCompleteConfig,
        {
          model: 'anthropic.claude-sonnet-4',
          messages: [{ role: 'user', content: 'hi' }],
          anthropic_beta,
        } as any
      ) as any;
      expect(out.additionalModelRequestFields.anthropic_beta).toEqual([
        'context-1m-2025-08-07',
        'interleaved-thinking-2025-05-14',
      ]);
    }
  });

  test('Bedrock: cache reads and writes count towards prompt_tokens', () => {
    const res = BedrockChatCompleteResponseTransform(
      {
        output: { message: { role: 'assistant', content: [{ text: 'ok' }] } },
        stopReason: 'end_turn',
        usage: {
          inputTokens: 10,
          outputTokens: 5,
          totalTokens: 1215,
          cacheReadInputTokens: 1000,
          cacheWriteInputTokens: 200,
        },
      } as any,
      200,
      new Headers(),
      false,
      '',
      { model: 'anthropic.claude-sonnet-4' } as any
    ) as any;
    expect(res.usage.prompt_tokens).toBe(1210);
    expect(res.usage.completion_tokens).toBe(5);
    expect(res.usage.prompt_tokens_details.cached_tokens).toBe(1000);
    expect(res.usage.cache_creation_input_tokens).toBe(200);
  });

  test('Bedrock: an inference-profile ARN uses the resolved base model config', () => {
    const arn =
      'arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/abc';
    const config = BedrockConfig.getConfig!({
      params: { model: arn },
      providerOptions: {
        provider: 'bedrock',
        foundationModel: 'anthropic.claude-sonnet-4-20250514-v1:0',
      },
    });
    expect(config.chatComplete).toBe(
      BedrockConverseAnthropicChatCompleteConfig
    );
  });
});
