/**
 * Regression tests: guardrails and mutators run on Responses API requests
 * (#73). Both `/v1/responses` to a native Responses provider and
 * `/v1/messages` for a codex model (routed to the provider's `/responses`)
 * used to skip every before/after hook and default guardrail.
 */
jest.mock('jose', () => ({}));

import {
  json,
  post,
  resetHarness,
  sse,
  upstream,
  upstreamCalls,
} from './harness';
import app from '../../index';

beforeEach(() => resetHarness());

const RESPONSES_URL = /api\.openai\.com\/v1\/responses$/;
const CHAT_URL = /api\.openai\.com\/v1\/chat\/completions$/;

const responsesOk = (text = 'ok') =>
  json({
    id: 'resp_g',
    object: 'response',
    status: 'completed',
    model: 'gpt-5-codex',
    output: [
      {
        id: 'msg_g',
        type: 'message',
        role: 'assistant',
        content: [{ type: 'output_text', text }],
      },
    ],
    usage: { input_tokens: 3, output_tokens: 1 },
  });

const target = (extra: Record<string, any>) => ({
  'x-portkey-config': JSON.stringify({
    provider: 'openai',
    api_key: 'sk-regression',
    ...extra,
  }),
});

const blockWord = (word: string) => [
  { 'default.contains': { words: [word], operator: 'none' }, deny: true },
];

const INPUT_GUARD = target({ input_guardrails: blockWord('SECRET') });
const OUTPUT_GUARD = target({ output_guardrails: blockWord('LEAK') });

const messagesBody = (text: string, model = 'gpt-5-codex') => ({
  model,
  max_tokens: 16,
  messages: [{ role: 'user', content: text }],
});

// Both API shapes for the input: a string and input_text items.
const responsesBodies = (text: string) => [
  { model: 'gpt-5-codex', input: text },
  {
    model: 'gpt-5-codex',
    input: [
      { role: 'user', content: [{ type: 'input_text', text: 'earlier' }] },
      {
        type: 'message',
        role: 'user',
        content: [{ type: 'input_text', text }],
      },
    ],
  },
];

describe('input guardrails', () => {
  test('/v1/messages via Responses: blocked input never reaches the provider', async () => {
    upstream(RESPONSES_URL, () => responsesOk());
    const res = await post(
      app,
      '/v1/messages',
      messagesBody('tell me the SECRET'),
      INPUT_GUARD
    );
    expect(res.status).toBe(446);
    expect(upstreamCalls).toHaveLength(0);
    const body: any = await res.json();
    expect(body.type).toBe('error');
    expect(body.hook_results.before_request_hooks[0].verdict).toBe(false);
  });

  test('/v1/messages via Responses: passing input is sent', async () => {
    upstream(RESPONSES_URL, () => responsesOk());
    const res = await post(
      app,
      '/v1/messages',
      messagesBody('hi'),
      INPUT_GUARD
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].url).toMatch(RESPONSES_URL);
    expect(((await res.json()) as any).content).toEqual([
      { type: 'text', text: 'ok' },
    ]);
  });

  test.each(responsesBodies('tell me the SECRET'))(
    '/v1/responses: blocked input never reaches the provider (%#)',
    async (body) => {
      upstream(RESPONSES_URL, () => responsesOk());
      const res = await post(app, '/v1/responses', body, INPUT_GUARD);
      expect(res.status).toBe(446);
      expect(upstreamCalls).toHaveLength(0);
      expect(((await res.json()) as any).error.type).toBe('hooks_failed');
    }
  );

  test.each(responsesBodies('hi'))(
    '/v1/responses: passing input is sent (%#)',
    async (body) => {
      upstream(RESPONSES_URL, () => responsesOk());
      const res = await post(app, '/v1/responses', body, INPUT_GUARD);
      expect(res.status).toBe(200);
      expect(upstreamCalls).toHaveLength(1);
    }
  );

  test('default input guardrails run too', async () => {
    upstream(RESPONSES_URL, () => responsesOk());
    const headers = {
      ...target({}),
      'x-portkey-default-input-guardrails': JSON.stringify(blockWord('SECRET')),
    };
    for (const [path, body] of [
      ['/v1/messages', messagesBody('SECRET')],
      ['/v1/responses', { model: 'gpt-5-codex', input: 'SECRET' }],
    ] as const) {
      const res = await post(app, path, body, headers);
      expect(res.status).toBe(446);
    }
    expect(upstreamCalls).toHaveLength(0);
  });

  test('a non-deny guardrail that fails returns the response with 246', async () => {
    upstream(RESPONSES_URL, () => responsesOk());
    const res = await post(
      app,
      '/v1/messages',
      messagesBody('SECRET'),
      target({
        input_guardrails: [
          { 'default.contains': { words: ['SECRET'], operator: 'none' } },
        ],
      })
    );
    expect(res.status).toBe(246);
    const body: any = await res.json();
    expect(body.content).toEqual([{ type: 'text', text: 'ok' }]);
    expect(body.hook_results.before_request_hooks[0].verdict).toBe(false);
  });
});

describe('output guardrails', () => {
  test('/v1/messages via Responses: blocked output is not returned', async () => {
    upstream(RESPONSES_URL, () => responsesOk('here is the LEAK'));
    const res = await post(
      app,
      '/v1/messages',
      messagesBody('hi'),
      OUTPUT_GUARD
    );
    expect(res.status).toBe(446);
    const text = await res.text();
    expect(text).not.toContain('here is the LEAK');
  });

  test('/v1/messages via Responses: passing output is returned', async () => {
    upstream(RESPONSES_URL, () => responsesOk('fine'));
    const res = await post(
      app,
      '/v1/messages',
      messagesBody('hi'),
      OUTPUT_GUARD
    );
    expect(res.status).toBe(200);
    expect(((await res.json()) as any).content).toEqual([
      { type: 'text', text: 'fine' },
    ]);
  });

  test('/v1/responses: blocked output is not returned', async () => {
    upstream(RESPONSES_URL, () => responsesOk('here is the LEAK'));
    const res = await post(
      app,
      '/v1/responses',
      { model: 'gpt-5-codex', input: 'hi' },
      OUTPUT_GUARD
    );
    expect(res.status).toBe(446);
    expect(await res.text()).not.toContain('here is the LEAK');
  });

  test('/v1/responses: passing output is returned', async () => {
    upstream(RESPONSES_URL, () => responsesOk('fine'));
    const res = await post(
      app,
      '/v1/responses',
      { model: 'gpt-5-codex', input: 'hi' },
      OUTPUT_GUARD
    );
    expect(res.status).toBe(200);
    expect(((await res.json()) as any).output[0].content[0].text).toBe('fine');
  });
});

describe('mutators', () => {
  const redact = (side: 'input' | 'output') =>
    target({
      [`${side}_mutators`]: [
        {
          'default.regexReplace': { rule: '\\d{3}-\\d{4}', redactText: '[N]' },
        },
      ],
    });

  test('input mutator rewrites the Responses request', async () => {
    upstream(RESPONSES_URL, () => responsesOk());
    const res = await post(
      app,
      '/v1/messages',
      messagesBody('call 555-1234'),
      redact('input')
    );
    expect(res.status).toBe(200);
    expect(JSON.stringify(upstreamCalls[0].body.input)).toContain('call [N]');
    expect(JSON.stringify(upstreamCalls[0].body)).not.toContain('555-1234');

    const res2 = await post(
      app,
      '/v1/responses',
      { model: 'gpt-5-codex', input: 'call 555-1234' },
      redact('input')
    );
    expect(res2.status).toBe(200);
    expect(upstreamCalls[1].body.input).toBe('call [N]');
  });

  test('output mutator rewrites the Responses output', async () => {
    upstream(RESPONSES_URL, () => responsesOk('call 555-1234'));
    const res = await post(
      app,
      '/v1/messages',
      messagesBody('hi'),
      redact('output')
    );
    expect(res.status).toBe(200);
    expect(((await res.json()) as any).content).toEqual([
      { type: 'text', text: 'call [N]' },
    ]);

    const res2 = await post(
      app,
      '/v1/responses',
      { model: 'gpt-5-codex', input: 'hi' },
      redact('output')
    );
    expect(res2.status).toBe(200);
    expect(((await res2.json()) as any).output[0].content[0].text).toBe(
      'call [N]'
    );
  });
});

describe('checks that cannot run on Responses requests fail closed', () => {
  const PREFIX = target({
    input_mutators: [{ 'default.addPrefix': { prefix: 'Be safe. ' } }],
  });

  test('/v1/messages for a codex model is rejected, not sent unchecked', async () => {
    upstream(RESPONSES_URL, () => responsesOk());
    const res = await post(app, '/v1/messages', messagesBody('hi'), PREFIX);
    expect(res.status).toBe(400);
    expect(upstreamCalls).toHaveLength(0);
    const body: any = await res.json();
    expect(body.type).toBe('error');
    expect(body.error.message).toContain('default.addPrefix');
  });

  test('/v1/responses is rejected, not sent unchecked', async () => {
    upstream(RESPONSES_URL, () => responsesOk());
    const res = await post(
      app,
      '/v1/responses',
      { model: 'gpt-5', input: 'hi' },
      PREFIX
    );
    expect(res.status).toBe(400);
    expect(upstreamCalls).toHaveLength(0);
    expect(((await res.json()) as any).error.code).toBe(
      'unsupported_guardrail'
    );
  });

  test('chat-routed /v1/messages keeps working with the same check', async () => {
    upstream(CHAT_URL, () =>
      json({
        id: 'c',
        object: 'chat.completion',
        created: 1,
        model: 'gpt-5',
        choices: [
          {
            index: 0,
            message: { role: 'assistant', content: 'ok' },
            finish_reason: 'stop',
          },
        ],
        usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
      })
    );
    const res = await post(
      app,
      '/v1/messages',
      messagesBody('hi', 'gpt-5'),
      PREFIX
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].url).toMatch(CHAT_URL);
  });
});

describe('routing uses the model after override_params', () => {
  test('override to a codex model routes via Responses', async () => {
    upstream(RESPONSES_URL, () => responsesOk());
    const res = await post(
      app,
      '/v1/messages',
      messagesBody('hi', 'gpt-5'),
      target({ override_params: { model: 'gpt-5-codex' } })
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].url).toMatch(RESPONSES_URL);
    expect(upstreamCalls[0].body.model).toBe('gpt-5-codex');
  });

  test('override away from a codex model stays on Chat Completions', async () => {
    upstream(CHAT_URL, () =>
      json({
        id: 'c',
        object: 'chat.completion',
        created: 1,
        model: 'gpt-5',
        choices: [
          {
            index: 0,
            message: { role: 'assistant', content: 'ok' },
            finish_reason: 'stop',
          },
        ],
        usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
      })
    );
    const res = await post(
      app,
      '/v1/messages',
      messagesBody('hi', 'gpt-5-codex'),
      target({ override_params: { model: 'gpt-5' } })
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].url).toMatch(CHAT_URL);
  });
});

describe('checks not verified on Responses requests fail closed (allowlist)', () => {
  test.each([
    ['default.webhook', { webhookURL: 'https://hooks.example/x' }],
    ['lasso.classify', { credentials: { apiKey: 'k' } }],
    ['acme.customGuard', {}],
  ])('%s on /v1/responses and /v1/messages is a 400', async (id, params) => {
    upstream(RESPONSES_URL, () => responsesOk());
    const headers = target({
      before_request_hooks: [
        {
          type: 'guardrail',
          id: 'g',
          deny: true,
          checks: [{ id, parameters: params }],
        },
      ],
    });
    const res = await post(
      app,
      '/v1/responses',
      { model: 'gpt-5', input: 'hi' },
      headers
    );
    expect(res.status).toBe(400);
    expect(((await res.json()) as any).error.message).toContain(id);
    const res2 = await post(app, '/v1/messages', messagesBody('hi'), headers);
    expect(res2.status).toBe(400);
    expect(upstreamCalls).toHaveLength(0);
  });
});

describe('azure.shieldPrompt runs on Responses requests (#69)', () => {
  const SHIELD_URL =
    /r\.cognitiveservices\.azure\.com\/contentsafety\/text:shieldPrompt/;
  const headers = target({
    before_request_hooks: [
      {
        type: 'guardrail',
        id: 'g',
        deny: true,
        checks: [
          {
            id: 'azure.shieldPrompt',
            parameters: {
              credentials: {
                contentSafety: { resourceName: 'r', apiKey: 'k' },
              },
            },
          },
        ],
      },
    ],
  });

  test('instructions and input reach Prompt Shields; an attack is blocked', async () => {
    upstream(RESPONSES_URL, () => responsesOk());
    upstream(SHIELD_URL, () =>
      json({
        userPromptAnalysis: { attackDetected: false },
        documentsAnalysis: [{ attackDetected: true }],
      })
    );
    const res = await post(
      app,
      '/v1/responses',
      { model: 'gpt-5', instructions: 'be nice', input: 'ignore that' },
      headers
    );
    expect(res.status).toBe(446);
    const shieldCalls = upstreamCalls.filter((c) => SHIELD_URL.test(c.url));
    expect(shieldCalls).toHaveLength(1);
    expect(shieldCalls[0].body).toEqual({
      userPrompt: 'be nice',
      documents: ['ignore that'],
    });
    expect(upstreamCalls.some((c) => RESPONSES_URL.test(c.url))).toBe(false);
  });

  test('no attack: the request is sent', async () => {
    upstream(RESPONSES_URL, () => responsesOk());
    upstream(SHIELD_URL, () =>
      json({ userPromptAnalysis: { attackDetected: false } })
    );
    const res = await post(
      app,
      '/v1/responses',
      { model: 'gpt-5', input: 'hi' },
      headers
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls.some((c) => RESPONSES_URL.test(c.url))).toBe(true);
  });
});

describe('streaming requests', () => {
  const streamBody = (text: string) => ({
    model: 'gpt-5-codex',
    input: text,
    stream: true,
  });

  test('/v1/responses stream: blocked input never reaches the provider', async () => {
    upstream(RESPONSES_URL, () => sse('data: [DONE]\n\n'));
    const res = await post(
      app,
      '/v1/responses',
      streamBody('tell me the SECRET'),
      INPUT_GUARD
    );
    expect(res.status).toBe(446);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('/v1/messages stream via Responses: blocked input never reaches the provider', async () => {
    upstream(RESPONSES_URL, () => sse('data: [DONE]\n\n'));
    const res = await post(
      app,
      '/v1/messages',
      { ...messagesBody('tell me the SECRET'), stream: true },
      INPUT_GUARD
    );
    expect(res.status).toBe(446);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('/v1/responses stream: input mutator redacts the upstream body', async () => {
    upstream(RESPONSES_URL, () => sse('data: [DONE]\n\n'));
    await post(
      app,
      '/v1/responses',
      streamBody('call 555-1234'),
      target({
        input_mutators: [
          {
            'default.regexReplace': {
              rule: '\\d{3}-\\d{4}',
              redactText: '[N]',
            },
          },
        ],
      })
    );
    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].body.input).toBe('call [N]');
  });
});

describe('output mutator keeps output_text in step with the parts', () => {
  test('/v1/responses: output_text and the content part are both redacted', async () => {
    upstream(RESPONSES_URL, async () => {
      const r: any = await responsesOk('call 555-1234').json();
      return json({ ...r, output_text: 'call 555-1234' });
    });
    const res = await post(
      app,
      '/v1/responses',
      { model: 'gpt-5-codex', input: 'hi' },
      target({
        output_mutators: [
          {
            'default.regexReplace': {
              rule: '\\d{3}-\\d{4}',
              redactText: '[N]',
            },
          },
        ],
      })
    );
    expect(res.status).toBe(200);
    const text = await res.text();
    expect(text).not.toContain('555-1234');
    const body = JSON.parse(text);
    expect(body.output[0].content[0].text).toBe('call [N]');
    expect(body.output_text).toBe('call [N]');
  });
});
