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
import {
  estimateMessagesInputTokens,
  estimateResponsesInputTokens,
} from '../../adapters/messages/estimateTokens';
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

  test('a target with hooks is estimated on every count route', () => {
    for (const [provider, model] of [
      ['google', 'gemini-2.5-flash'],
      ['vertex-ai', 'gemini-2.5-pro'],
      ['openai', 'gpt-5'],
      ['anthropic', 'claude-sonnet-5-5'],
      ['bedrock', 'anthropic.claude-sonnet-5-5-v1:0'],
      ['vertex-ai', 'claude-sonnet-5-5'],
      ['claude-platform-aws', 'claude-sonnet-5-5'],
      ['groq', 'llama-3.3-70b-versatile'],
    ]) {
      const res = messagesCountTokensRoute(
        provider,
        body(model) as any,
        undefined,
        true
      );
      expect(res).toBeInstanceOf(Response);
      expect((res as Response).headers.get('x-ps-token-count')).toBe(
        'estimated'
      );
    }
    // A provider the Messages endpoint cannot reach still gets the 400.
    const jina = messagesCountTokensRoute(
      'jina',
      body('jina-embeddings-v3') as any,
      undefined,
      true
    ) as Response;
    expect(jina.status).toBe(400);
    expect(jina.headers.get('x-ps-token-count')).toBeNull();
  });

  test('guardrails keep content off Gemini countTokens', async () => {
    const res = await count(body('gemini-2.5-flash'), {
      'x-portkey-config': JSON.stringify({
        provider: 'google',
        api_key: 'gemini-key',
        input_guardrails: [
          {
            'default.contains': { operator: 'none', words: ['x'] },
            deny: true,
          },
        ],
      }),
    });
    expect(res.status).toBe(200);
    expect(res.headers.get('x-ps-token-count')).toBe('estimated');
    expect(upstreamCalls).toHaveLength(0);
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

describe('hooks keep the prompt off native count APIs', () => {
  // Hooks do not run on count requests, so a target with any hooks must
  // never send the prompt upstream to be counted: /v1/messages would have
  // redacted or denied it first. Every native count API is stubbed to answer,
  // so a leak would show up as an upstream call.
  const SECRET = 'call me at 555-1234';
  const secretBody = () => ({
    model: 'claude-sonnet-5-5',
    system: 'You are terse.',
    messages: [{ role: 'user', content: SECRET }],
  });

  const REDACT = {
    input_mutators: [
      {
        'default.regexReplace': {
          rule: '\\d{3}-\\d{4}',
          redactText: '[N]',
        },
      },
    ],
  };
  const DENY = {
    input_guardrails: [
      { 'default.contains': { operator: 'none', words: ['555'] }, deny: true },
    ],
  };

  const natives: [string, object][] = [
    ['anthropic', { provider: 'anthropic', api_key: 'sk-ant-test' }],
    [
      'bedrock',
      {
        provider: 'bedrock',
        aws_access_key_id: 'AKIAREGRESSIONTEST',
        aws_secret_access_key: 'regression-secret',
        aws_region: 'us-east-1',
        override_params: { model: 'anthropic.claude-sonnet-5-5-v1:0' },
      },
    ],
    [
      'vertex-ai (Claude)',
      {
        provider: 'vertex-ai',
        api_key: 'ya29.regression-token',
        vertex_project_id: 'ps-regression',
        vertex_region: 'us-east5',
        override_params: { model: 'anthropic.claude-sonnet-5-5' },
      },
    ],
    [
      'claude-platform-aws',
      {
        provider: 'claude-platform-aws',
        api_key: 'aws-claude-key',
        aws_region: 'us-west-2',
        anthropic_aws_workspace_id: 'wrkspc_regression',
      },
    ],
  ];

  // Bedrock's CountTokens answers `inputTokens`; the others `input_tokens`.
  const stubEveryUpstream = () =>
    upstream(/.*/, (call) =>
      json(
        /bedrock-runtime/.test(call.url)
          ? { inputTokens: 99 }
          : { input_tokens: 99 }
      )
    );

  test.each(natives)(
    '%s without hooks still uses its own count API',
    async (_name, target) => {
      stubEveryUpstream();
      const res = await count(secretBody(), {
        'x-portkey-config': JSON.stringify(target),
      });
      expect(res.status).toBe(200);
      expect(res.headers.get('x-ps-token-count')).toBeNull();
      expect(await res.json()).toEqual({ input_tokens: 99 });
      expect(upstreamCalls).toHaveLength(1);
    }
  );

  for (const [hookName, hooks] of [
    ['a redaction mutator', REDACT],
    ['a deny guardrail', DENY],
  ] as const) {
    test.each(natives)(
      `%s with ${hookName} is estimated and sends nothing upstream`,
      async (_name, target) => {
        stubEveryUpstream();
        const b = secretBody();
        const res = await count(b, {
          'x-portkey-config': JSON.stringify({ ...target, ...hooks }),
        });
        expect(res.status).toBe(200);
        expect(res.headers.get('x-ps-token-count')).toBe('estimated');
        expect(await res.json()).toEqual({
          input_tokens: estimateMessagesInputTokens(b),
        });
        expect(upstreamCalls).toHaveLength(0);
        expect(JSON.stringify(upstreamCalls)).not.toContain('555-1234');
      }
    );
  }

  test('hooks inherited from a strategy config apply to its targets', async () => {
    stubEveryUpstream();
    const res = await count(secretBody(), {
      'x-portkey-config': JSON.stringify({
        strategy: { mode: 'fallback' },
        ...REDACT,
        targets: [natives[0][1], natives[1][1]],
      }),
    });
    expect(res.status).toBe(200);
    expect(res.headers.get('x-ps-token-count')).toBe('estimated');
    expect(upstreamCalls).toHaveLength(0);
  });

  test('/v1/messages on the same target does redact (the contrast)', async () => {
    upstream(/api\.anthropic\.com\/v1\/messages$/, () =>
      json({
        id: 'msg_1',
        type: 'message',
        role: 'assistant',
        model: 'claude-sonnet-5-5',
        content: [{ type: 'text', text: 'ok' }],
        stop_reason: 'end_turn',
        usage: { input_tokens: 8, output_tokens: 1 },
      })
    );
    const res = await post(
      app,
      '/v1/messages',
      { ...secretBody(), max_tokens: 16 },
      {
        'anthropic-version': '2023-06-01',
        'x-portkey-config': JSON.stringify({ ...natives[0][1], ...REDACT }),
      }
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(1);
    expect(JSON.stringify(upstreamCalls[0].body)).not.toContain('555-1234');
  });

  describe('POST /v1/responses/input_tokens', () => {
    const responsesBody = {
      model: 'gpt-5.1',
      instructions: 'You are terse.',
      input: [{ role: 'user', content: SECRET }],
    };
    const openai = (extra: object = {}) =>
      JSON.stringify({ provider: 'openai', api_key: 'sk-test', ...extra });

    test('without hooks the OpenAI count API is called', async () => {
      upstream(/api\.openai\.com\/v1\/responses\/input_tokens$/, () =>
        json({ object: 'response.input_tokens', input_tokens: 12 })
      );
      const res = await post(app, '/v1/responses/input_tokens', responsesBody, {
        'x-portkey-config': openai(),
      });
      expect(res.status).toBe(200);
      expect(res.headers.get('x-ps-token-count')).toBeNull();
      expect(upstreamCalls).toHaveLength(1);
    });

    test.each([
      ['a redaction mutator', REDACT],
      ['a deny guardrail', DENY],
    ])('with %s it is estimated locally', async (_n, hooks) => {
      upstream(/.*/, () =>
        json({ object: 'response.input_tokens', input_tokens: 12 })
      );
      const res = await post(app, '/v1/responses/input_tokens', responsesBody, {
        'x-portkey-config': openai(hooks),
      });
      expect(res.status).toBe(200);
      expect(res.headers.get('x-ps-token-count')).toBe('estimated');
      expect(await res.json()).toEqual({
        object: 'response.input_tokens',
        input_tokens: estimateResponsesInputTokens(responsesBody),
      });
      expect(upstreamCalls).toHaveLength(0);
    });

    test('an unsupported provider with hooks still gets the 400', async () => {
      const res = await post(app, '/v1/responses/input_tokens', responsesBody, {
        'x-portkey-config': JSON.stringify({
          provider: 'groq',
          api_key: 'gsk',
          ...REDACT,
        }),
      });
      expect(res.status).toBe(400);
      expect(upstreamCalls).toHaveLength(0);
    });
  });
});

describe('estimateResponsesInputTokens', () => {
  test('counts instructions, items, tool calls and tools', () => {
    const tokens = estimateResponsesInputTokens({
      instructions: 'x'.repeat(40),
      input: [
        {
          role: 'user',
          content: [
            { type: 'input_text', text: 'y'.repeat(40) },
            { type: 'input_image', image_url: 'data:image/png;base64,AAAA' },
          ],
        },
        { type: 'function_call', name: 'calc', arguments: '{"a":1}' },
        { type: 'function_call_output', output: 'w'.repeat(40) },
      ],
      tools: [
        { type: 'function', name: 'calc', description: 'adds', parameters: {} },
      ],
    });
    // chars: 40*3 + 'calc' + '{"a":1}' + 'calc' + 'adds' + '{}' = 141
    expect(tokens).toBe(1600 + 3 * 3 + 3 + Math.ceil(141 / 4));
  });

  test('a string input is one item', () => {
    expect(estimateResponsesInputTokens({ input: 'abcdefgh' })).toBe(3 + 2);
  });

  test('is zero for an empty or invalid body', () => {
    expect(estimateResponsesInputTokens({})).toBe(0);
    expect(estimateResponsesInputTokens(null)).toBe(0);
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
