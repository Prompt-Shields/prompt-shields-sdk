// utils/env uses top-level await, which ts-jest cannot compile here (#69).
jest.mock('../../../utils/env', () => ({ Environment: () => ({}) }));

import {
  AnthropicChatCompleteConfig,
  getAnthropicChatCompleteResponseTransform,
} from '../chatComplete';
import { transformUsingProviderConfig } from '../../../services/transformToProviderRequest';

const base = {
  model: 'claude-sonnet-4-5',
  max_tokens: 100,
  messages: [{ role: 'user', content: 'hi' }],
} as any;

describe('Anthropic chat (ported from Portkey 2.0.0, #108)', () => {
  test('prompt_tokens includes cached tokens, as OpenAI counts them', () => {
    const out: any = getAnthropicChatCompleteResponseTransform('anthropic')(
      {
        id: 'msg_1',
        type: 'message',
        role: 'assistant',
        model: 'claude-sonnet-4-5',
        content: [{ type: 'text', text: 'hello' }],
        stop_reason: 'end_turn',
        usage: {
          input_tokens: 10,
          output_tokens: 5,
          cache_read_input_tokens: 100,
          cache_creation_input_tokens: 20,
        },
      } as any,
      200,
      new Headers(),
      true
    );
    expect(out.usage.prompt_tokens).toBe(130);
    expect(out.usage.total_tokens).toBe(135);
  });

  test('response_format json_schema maps to output_config.format', () => {
    const req: any = transformUsingProviderConfig(AnthropicChatCompleteConfig, {
      ...base,
      response_format: {
        type: 'json_schema',
        json_schema: { name: 'x', schema: { type: 'object' } },
      },
    });
    expect(req.output_config).toEqual({
      format: { type: 'json_schema', schema: { type: 'object' } },
    });
  });

  test('reasoning_effort maps to output_config.effort and adaptive thinking', () => {
    const req: any = transformUsingProviderConfig(AnthropicChatCompleteConfig, {
      ...base,
      reasoning_effort: 'high',
    });
    expect(req.output_config).toEqual({ effort: 'high' });
    expect(req.thinking).toEqual({ type: 'adaptive' });
  });

  test('tool `strict` is not forwarded (reverted upstream in 2.4.4)', () => {
    const req: any = transformUsingProviderConfig(AnthropicChatCompleteConfig, {
      ...base,
      tools: [
        {
          type: 'function',
          function: {
            name: 'f',
            strict: true,
            parameters: { type: 'object', properties: {} },
          },
        },
      ],
    } as any);
    expect(req.tools[0].name).toBe('f');
    expect(req.tools[0]).not.toHaveProperty('strict');
  });

  test('default model is a current one', () => {
    const req: any = transformUsingProviderConfig(AnthropicChatCompleteConfig, {
      messages: base.messages,
    } as any);
    expect(req.model).toBe('claude-sonnet-4-5');
  });
});
