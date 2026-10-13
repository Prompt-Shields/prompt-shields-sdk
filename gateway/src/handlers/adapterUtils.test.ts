// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../utils/env', () => ({ Environment: () => ({}) }));

import {
  applyAdapterRequestTransform,
  normalizeTokenLimitForProvider,
} from './adapterUtils';

describe('normalizeTokenLimitForProvider', () => {
  test('keeps max_completion_tokens for providers that map it', () => {
    const out: any = normalizeTokenLimitForProvider(
      { model: 'm', max_completion_tokens: 64 },
      'mistral-ai'
    );
    expect(out.max_completion_tokens).toBe(64);
  });

  test('renames to max_tokens for providers that only map max_tokens', () => {
    const out: any = normalizeTokenLimitForProvider(
      { model: 'command-r', max_completion_tokens: 64 },
      'cohere'
    );
    expect(out.max_tokens).toBe(64);
    expect(out.max_completion_tokens).toBeUndefined();
  });

  test('leaves params alone without a limit or for unknown providers', () => {
    expect(normalizeTokenLimitForProvider({ model: 'm' }, 'cohere')).toEqual({
      model: 'm',
    });
    expect(
      normalizeTokenLimitForProvider(
        { model: 'm', max_completion_tokens: 1 },
        'no-such-provider'
      )
    ).toEqual({ model: 'm', max_completion_tokens: 1 });
  });
});

describe('applyAdapterRequestTransform', () => {
  const messagesBody: any = {
    model: 'command-r',
    max_tokens: 64,
    messages: [{ role: 'user', content: 'hi' }],
  };

  test('native Messages providers pass through', () => {
    expect(
      applyAdapterRequestTransform(
        'messages',
        'anthropic',
        messagesBody,
        messagesBody,
        false,
        'POST'
      )
    ).toBeNull();
  });

  test('chat-only providers get a chatComplete request with a usable limit', () => {
    const out: any = applyAdapterRequestTransform(
      'messages',
      'cohere',
      messagesBody,
      messagesBody,
      false,
      'POST'
    );
    expect(out.fn).toBe('chatComplete');
    expect(out.adapterCtx.isActive).toBe(true);
    expect(out.params.max_tokens).toBe(64);
  });

  test('Responses stays native on providers this fork supports natively', () => {
    for (const p of ['openai', 'azure-openai', 'groq', 'iointelligence']) {
      expect(
        applyAdapterRequestTransform(
          'createModelResponse',
          p,
          { model: 'm', input: 'hi' } as any,
          {} as any,
          false,
          'POST'
        )
      ).toBeNull();
    }
  });

  test('Responses GET on a non-native provider is rejected, not mistranslated', () => {
    const out = applyAdapterRequestTransform(
      'getModelResponse',
      'x-ai',
      {} as any,
      {} as any,
      false,
      'GET'
    );
    expect(out).toBeInstanceOf(Response);
    expect((out as Response).status).toBe(400);
  });
});
