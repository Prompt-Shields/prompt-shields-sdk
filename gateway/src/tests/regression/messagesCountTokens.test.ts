/**
 * `POST /v1/messages/count_tokens` for every provider (#73 follow-up),
 * through the whole gateway app with stub upstreams: provider token-count
 * APIs where the gateway has one (Anthropic, Gemini countTokens on AI Studio
 * and Vertex AI, OpenAI Responses input_tokens), a local estimate marked
 * `x-ps-token-count: estimated` elsewhere, and a 400 (never a 500) for a
 * provider the Messages endpoint cannot reach.
 */
jest.mock('jose', () => ({}));

import { json, post, resetHarness, upstream, upstreamCalls } from './harness';
import app from '../../index';
import { estimateMessagesInputTokens } from '../../adapters/messages/estimateTokens';
import { messagesCountTokensRoute } from '../../handlers/adapterUtils';

beforeEach(() => resetHarness());

const body = (model: string, extra: object = {}) => ({
  model,
  system: 'You are terse.',
  messages: [{ role: 'user', content: 'Count these words please' }],
  tools: [
    {
      name: 'get_weather',
      description: 'Weather for a city',
      input_schema: {
        type: 'object',
        properties: { city: { type: 'string' } },
      },
    },
  ],
  ...extra,
});

const count = (b: unknown, headers: Record<string, string>) =>
  post(app, '/v1/messages/count_tokens', b, {
    'anthropic-version': '2023-06-01',
    ...headers,
  });

describe('provider token-count APIs', () => {
  test('anthropic is passed through unchanged', async () => {
    upstream(/api\.anthropic\.com\/v1\/messages\/count_tokens$/, () =>
      json({ input_tokens: 42 })
    );
    const res = await count(body('claude-sonnet-5-5'), {
      'x-portkey-provider': 'anthropic',
      'x-api-key': 'sk-ant-test',
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ input_tokens: 42 });
    expect(res.headers.get('x-ps-token-count')).toBeNull();
    expect(upstreamCalls).toHaveLength(1);
  });

  test('Gemini on Google AI Studio uses models:countTokens', async () => {
    upstream(/generativelanguage\.googleapis\.com/, () =>
      json({ totalTokens: 37 })
    );
    const res = await count(body('gemini-2.5-flash'), {
      'x-portkey-provider': 'google',
      authorization: 'Bearer gemini-key',
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ input_tokens: 37 });
    expect(res.headers.get('x-ps-token-count')).toBeNull();
    const call = upstreamCalls[0];
    expect(call.url).toBe(
      'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:countTokens?key=gemini-key'
    );
    const req = call.body.generateContentRequest;
    expect(req.model).toBe('models/gemini-2.5-flash');
    expect(req.contents).toEqual([
      { role: 'user', parts: [{ text: 'Count these words please' }] },
    ]);
    expect(JSON.stringify(req.systemInstruction)).toContain('You are terse.');
    expect(JSON.stringify(req.tools)).toContain('get_weather');
    // Nothing generation-only reaches the count.
    expect(req.generationConfig).toBeUndefined();
  });

  const vertexConfig = (model: string) =>
    JSON.stringify({
      provider: 'vertex-ai',
      api_key: 'ya29.regression-token',
      vertex_project_id: 'ps-regression',
      vertex_region: 'us-central1',
      override_params: { model },
    });
  const VERTEX =
    'https://us-central1-aiplatform.googleapis.com/v1/projects/ps-regression/locations/us-central1/publishers/google/models/';

  test('Gemini on Vertex AI uses :countTokens', async () => {
    upstream(/aiplatform\.googleapis\.com/, () => json({ totalTokens: 29 }));
    const res = await count(body('claude-sonnet-5-5'), {
      'x-portkey-config': vertexConfig('gemini-2.5-pro'),
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ input_tokens: 29 });
    const call = upstreamCalls[0];
    expect(call.url).toBe(`${VERTEX}gemini-2.5-pro:countTokens`);
    expect(call.headers.authorization).toBe('Bearer ya29.regression-token');
    expect(call.body.contents).toEqual([
      { role: 'user', parts: [{ text: 'Count these words please' }] },
    ]);
    expect(JSON.stringify(call.body.systemInstruction)).toContain(
      'You are terse.'
    );
    expect(JSON.stringify(call.body.tools)).toContain('get_weather');
    expect(call.body.generationConfig).toBeUndefined();
  });

  test('a Vertex AI error is returned as a 4xx, not a 500', async () => {
    upstream(/aiplatform\.googleapis\.com/, () =>
      json(
        {
          error: {
            code: 400,
            message: 'bad contents',
            status: 'INVALID_ARGUMENT',
          },
        },
        400
      )
    );
    const res = await count(body('x'), {
      'x-portkey-config': vertexConfig('gemini-2.5-pro'),
    });
    expect(res.status).toBe(400);
    expect(JSON.stringify(await res.json())).toContain('bad contents');
  });

  test('OpenAI goes through Responses input_tokens', async () => {
    upstream(/api\.openai\.com\/v1\/responses\/input_tokens$/, () =>
      json({ object: 'response.input_tokens', input_tokens: 31 })
    );
    const res = await count(body('gpt-5.1', { max_tokens: 1024 }), {
      'x-portkey-provider': 'openai',
      authorization: 'Bearer sk-test',
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ input_tokens: 31 });
    expect(res.headers.get('x-ps-token-count')).toBeNull();
    const call = upstreamCalls[0];
    expect(call.method).toBe('POST');
    expect(call.body.model).toBe('gpt-5.1');
    expect(call.body.instructions).toBe('You are terse.');
    expect(JSON.stringify(call.body.input)).toContain(
      'Count these words please'
    );
    expect(call.body.tools[0]).toMatchObject({
      type: 'function',
      name: 'get_weather',
    });
    for (const k of ['store', 'stream', 'max_output_tokens']) {
      expect(call.body[k]).toBeUndefined();
    }
  });

  test('an OpenAI error keeps its status in the Anthropic error shape', async () => {
    upstream(/api\.openai\.com\/v1\/responses\/input_tokens$/, () =>
      json(
        {
          error: {
            message: 'Incorrect API key',
            type: 'invalid_request_error',
          },
        },
        401
      )
    );
    const res = await count(body('gpt-5.1'), {
      'x-portkey-provider': 'openai',
      authorization: 'Bearer sk-bad',
    });
    expect(res.status).toBe(401);
    const out: any = await res.json();
    expect(out.type).toBe('error');
    expect(out.error.message).toContain('Incorrect API key');
  });

  test('an OpenAI success without a count is a 502', async () => {
    upstream(/api\.openai\.com\/v1\/responses\/input_tokens$/, () =>
      json({ object: 'response.input_tokens' })
    );
    const res = await count(body('gpt-5.1'), {
      'x-portkey-provider': 'openai',
      authorization: 'Bearer sk-test',
    });
    expect(res.status).toBe(502);
    expect(((await res.json()) as any).type).toBe('error');
  });
});

describe('local estimate', () => {
  test('a provider without a count API gets a marked estimate', async () => {
    const b = body('llama-3.3-70b-versatile');
    const res = await count(b, {
      'x-portkey-provider': 'groq',
      authorization: 'Bearer gsk-test',
    });
    expect(res.status).toBe(200);
    expect(res.headers.get('x-ps-token-count')).toBe('estimated');
    expect(await res.json()).toEqual({
      input_tokens: estimateMessagesInputTokens(b),
    });
    expect(upstreamCalls).toHaveLength(0);
  });

  test('a provider the Messages endpoint cannot reach gets a 400', async () => {
    const res = await count(body('jina-embeddings-v3'), {
      'x-portkey-provider': 'jina',
      authorization: 'Bearer jina-test',
    });
    expect(res.status).toBe(400);
    const out: any = await res.json();
    expect(out).toMatchObject({
      type: 'error',
      error: { type: 'invalid_request_error' },
    });
    expect(out.error.message).toContain('jina');
    expect(upstreamCalls).toHaveLength(0);
  });

  test('OpenAI behind a custom host is estimated', () => {
    const res = messagesCountTokensRoute(
      'openai',
      body('local-model') as any,
      'https://vllm.internal.example/v1'
    );
    expect(res).toBeInstanceOf(Response);
    expect((res as Response).headers.get('x-ps-token-count')).toBe('estimated');
  });

  test('native count configs are used as is', () => {
    expect(messagesCountTokensRoute('anthropic', body('c') as any)).toBeNull();
    expect(
      messagesCountTokensRoute('bedrock', body('amazon.nova-pro') as any)
    ).toBeNull();
    expect(
      messagesCountTokensRoute('vertex-ai', body('claude-sonnet-5-5') as any)
    ).toBeNull();
    expect(
      messagesCountTokensRoute('vertex-ai', body('gemini-2.5-pro') as any)
    ).toBeNull();
  });
});

describe('estimateMessagesInputTokens', () => {
  test('counts text at four characters a token, plus framing', () => {
    expect(
      estimateMessagesInputTokens({
        messages: [{ role: 'user', content: 'abcdefgh' }],
      })
    ).toBe(3 + 2);
  });

  test('covers system, blocks, tool use, results and tool definitions', () => {
    const tokens = estimateMessagesInputTokens({
      system: [{ type: 'text', text: 'x'.repeat(40) }],
      messages: [
        {
          role: 'user',
          content: [
            { type: 'text', text: 'y'.repeat(40) },
            { type: 'image', source: { type: 'base64', data: 'AAAA' } },
          ],
        },
        {
          role: 'assistant',
          content: [
            { type: 'thinking', thinking: 'z'.repeat(40), signature: 's' },
            { type: 'tool_use', id: 't1', name: 'calc', input: { a: 1 } },
          ],
        },
        {
          role: 'user',
          content: [
            {
              type: 'tool_result',
              tool_use_id: 't1',
              content: [{ type: 'text', text: 'w'.repeat(40) }],
            },
          ],
        },
      ],
      tools: [{ name: 'calc', description: 'adds', input_schema: {} }],
    });
    // chars: 40*4 text + 'calc' + '{"a":1}' + 'calc' + 'adds' + '{}' = 181
    expect(tokens).toBe(1600 + 3 * 3 + 3 + Math.ceil(181 / 4));
  });

  test('is zero for an empty or invalid body', () => {
    expect(estimateMessagesInputTokens({})).toBe(0);
    expect(estimateMessagesInputTokens(null)).toBe(0);
  });
});

test('a body that is not JSON is a 400, not a 500', async () => {
  const res = await count('{not json', {
    'x-portkey-provider': 'groq',
    authorization: 'Bearer gsk-test',
  });
  expect(res.status).toBe(400);
  expect(((await res.json()) as any).error.type).toBe('invalid_request_error');
});
