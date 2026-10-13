import { sanitizeRequestOption } from '.';

describe('log stream redaction', () => {
  test('provider credentials and header values never reach log clients', () => {
    const option = {
      providerOptions: {
        provider: 'openai',
        apiKey: 'sk-live-secret',
        awsSecretAccessKey: 'aws-secret',
        requestURL: 'https://api.openai.com/v1/chat/completions',
        rubeusURL:
          'https://generativelanguage.googleapis.com/v1/models?key=AIzaSyA1234567890abcdefghijklmnopqrstuv',
      },
      transformedRequest: {
        body: { model: 'gpt-4o' },
        headers: { authorization: 'Bearer sk-live-secret' },
      },
      responseHeaders: { 'set-cookie': 'session=abc' },
      requestParams: { model: 'gpt-4o' },
    };

    const sanitized = sanitizeRequestOption(option);
    const serialized = JSON.stringify(sanitized);

    expect(serialized).not.toContain('sk-live-secret');
    expect(serialized).not.toContain('aws-secret');
    expect(serialized).not.toContain('session=abc');
    expect(serialized).not.toContain('AIzaSyA1234567890abcdefghijklmnopqrstuv');
    expect(sanitized.providerOptions.provider).toBe('openai');
    expect(sanitized.providerOptions.requestURL).toBe(
      'https://api.openai.com/v1/chat/completions'
    );
    // The original object is not mutated.
    expect(option.providerOptions.apiKey).toBe('sk-live-secret');
  });
});
