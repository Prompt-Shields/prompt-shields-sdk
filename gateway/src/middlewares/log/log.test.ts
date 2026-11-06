import { Hono } from 'hono';
import { streamSSE } from 'hono/streaming';
import {
  buildLogRequestOptions,
  logHandler,
  redactPiiDeep,
  sanitizeRequestOption,
} from '.';

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

// PS fork (#337): PII in bodies never reaches /log/stream; categories do.
const EMAIL = 'jane.doe@example.com';
const CARD = '4111 1111 1111 1111';
const PROMPT = `My email is ${EMAIL} and my card is ${CARD}.`;

function chatOption(stream: boolean) {
  const body = {
    model: 'gpt-4o',
    stream,
    messages: [{ role: 'user', content: PROMPT }],
  };
  return {
    providerOptions: { provider: 'openai', apiKey: 'sk-live-secret' },
    transformedRequest: { body, headers: { authorization: 'Bearer x' } },
    requestParams: body,
    finalUntransformedRequest: { body },
    originalResponse: {
      body: { choices: [{ message: { content: `Noted ${EMAIL}` } }] },
    },
    createdAt: new Date('2026-11-06T12:00:00Z'),
  };
}

function expectRedacted(serialized: string) {
  expect(serialized).not.toContain(EMAIL);
  expect(serialized).not.toContain(CARD);
  expect(serialized).not.toContain('4111111111111111');
  expect(serialized).toContain('[EMAIL]');
  expect(serialized).toContain('[CREDIT_CARD]');
}

describe('log stream PII redaction', () => {
  test('JSON request and response bodies are redacted', () => {
    const option = chatOption(false);
    const [sanitized] = buildLogRequestOptions([option], {
      choices: [{ message: { content: `Your card ${CARD} is on file` } }],
    });
    expectRedacted(JSON.stringify(sanitized));
    expect(sanitized.requestParams.messages[0].content).toBe(
      'My email is [EMAIL] and my card is [CREDIT_CARD].'
    );
    expect(sanitized.response.choices[0].message.content).toBe(
      'Your card [CREDIT_CARD] is on file'
    );
    expect(sanitized.requestParams.model).toBe('gpt-4o');
    // Dates and other non-plain objects are kept; the shared options are not
    // modified.
    expect(sanitized.createdAt).toBeInstanceOf(Date);
    expect(option.requestParams.messages[0].content).toBe(PROMPT);
    expect((option as any).response).toBeUndefined();
  });

  test('a card number sent as a JSON number is redacted', () => {
    const redacted = redactPiiDeep({
      card: 4111111111111111,
      tokens: 128,
      created: 1730894400,
      createdMs: 1730894400000,
    }) as Record<string, unknown>;
    expect(redacted).toEqual({
      card: '[CREDIT_CARD]',
      tokens: 128,
      created: 1730894400,
      createdMs: 1730894400000,
    });
  });

  test('the response is redacted before the 100 KB cut', () => {
    // The card straddles the cut: cutting first would leak its first digits.
    const filler = 'x'.repeat(100000 - 20);
    const [sanitized] = buildLogRequestOptions([chatOption(false)], {
      text: `${filler} ${CARD} tail`,
    });
    expect(typeof sanitized.response).toBe('string');
    expect(sanitized.response.length).toBe(100000 + 3);
    expect(sanitized.response).not.toContain('4111');
    expect(sanitized.response).toMatch(/ \[CREDIT_[A-Z]*\.\.\.$/);
  });

  test('only the first entry gets the final response', () => {
    const entries = buildLogRequestOptions(
      [chatOption(false), chatOption(false)],
      { ok: true }
    );
    expect(entries[0].response).toEqual({ ok: true });
    expect(entries[1].response).toBeUndefined();
    expectRedacted(JSON.stringify(entries));
  });

  async function streamedLog(
    stream: boolean,
    path = '/v1/chat/completions'
  ): Promise<string> {
    const app = new Hono<any>();
    app.use('*', logHandler());
    app.get('/log/stream', (c) =>
      streamSSE(c, async (sse) => {
        const add: any = c.get('addLogClient');
        const remove: any = c.get('removeLogClient');
        add('test-client', {
          sendLog: (message: any) => sse.writeSSE(message),
        });
        await sse.writeSSE({ event: 'connected', data: 'test-client' });
        await new Promise((resolve) => setTimeout(resolve, 500));
        remove('test-client');
      })
    );
    app.post('/v1/*', (c) => {
      c.set('requestOptions', [chatOption(stream)]);
      if (stream)
        return c.body(
          `data: {"choices":[{"delta":{"content":"${EMAIL}"}}]}\n\n`,
          200,
          { 'content-type': 'text/event-stream' }
        );
      return c.json({
        choices: [{ message: { content: `Saved ${EMAIL} and ${CARD}` } }],
      });
    });

    const res = await app.request('http://localhost:8787/log/stream');
    const reader = res.body!.getReader();
    const decoder = new TextDecoder();
    let text = '';
    // Wait for the client to be registered before making the call.
    while (!text.includes('event: connected')) {
      const { value, done } = await reader.read();
      if (done) break;
      text += decoder.decode(value);
    }
    await app.request(`http://localhost:8787${path}`, { method: 'POST' });
    while (!text.includes('event: log')) {
      const { value, done } = await reader.read();
      if (done) break;
      text += decoder.decode(value);
    }
    await reader.cancel();
    return text;
  }

  test('the SSE stream carries categories, not values (JSON body)', async () => {
    const text = await streamedLog(false);
    expect(text).toContain('event: log');
    expectRedacted(text);
    expect(text).toContain('Saved [EMAIL] and [CREDIT_CARD]');
    expect(text).not.toContain('sk-live-secret');
  });

  test('the SSE stream carries categories, not values (streamed call)', async () => {
    const text = await streamedLog(true, `/v1/chat/completions?u=${EMAIL}`);
    expect(text).toContain('event: log');
    expectRedacted(text);
    expect(text).toContain('The response was a stream.');
    expect(text).toContain('/v1/chat/completions?u=[EMAIL]');
  });
});
