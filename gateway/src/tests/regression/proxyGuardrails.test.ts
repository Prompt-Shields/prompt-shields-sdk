/**
 * Regression tests: guardrails on the generic proxy routes (security).
 *
 * Upstream skips every hook on `/v1/proxy/*` and the `/v1/*` catch-all, so a
 * target's guardrails could be bypassed by calling the same model API through
 * the proxy. Recognised Chat Completions / Completions / Messages / Responses
 * / Embeddings requests now run the hooks; anything else is a 400 before any
 * upstream call. Targets without hooks are proxied unchanged.
 */
jest.mock('jose', () => ({}));

import { json, post, resetHarness, upstream, upstreamCalls } from './harness';
import app from '../../index';

beforeEach(() => resetHarness());

const CHAT_URL = /api\.openai\.com\/v1\/chat\/completions$/;

const chatOk = (text = 'ok') =>
  json({
    id: 'chatcmpl-p',
    object: 'chat.completion',
    model: 'gpt-4o-mini',
    choices: [
      {
        index: 0,
        message: { role: 'assistant', content: text },
        finish_reason: 'stop',
      },
    ],
    usage: { prompt_tokens: 3, completion_tokens: 1, total_tokens: 4 },
  });

const config = (cfg: Record<string, any>) => ({
  'x-portkey-config': JSON.stringify(cfg),
});

const openai = (extra: Record<string, any> = {}) =>
  config({ provider: 'openai', api_key: 'sk-regression', ...extra });

const blockWord = (word: string) => [
  { 'default.contains': { words: [word], operator: 'none' }, deny: true },
];

const INPUT_GUARD = openai({ input_guardrails: blockWord('SECRET') });
const OUTPUT_GUARD = openai({ output_guardrails: blockWord('LEAK') });

const chatBody = (text: string) => ({
  model: 'gpt-4o-mini',
  messages: [{ role: 'user', content: text }],
});

describe('input guardrails on proxied model calls', () => {
  test('a blocked chat completion never reaches the provider', async () => {
    upstream(CHAT_URL, () => chatOk());
    const res = await post(
      app,
      '/v1/proxy/chat/completions',
      chatBody('tell me the SECRET'),
      INPUT_GUARD
    );
    expect(res.status).toBe(446);
    expect(upstreamCalls).toHaveLength(0);
    const body: any = await res.json();
    expect(body.error.type).toBe('hooks_failed');
    expect(body.hook_results.before_request_hooks[0].verdict).toBe(false);
  });

  test('a passing chat completion is forwarded unchanged', async () => {
    upstream(CHAT_URL, () => chatOk());
    const res = await post(
      app,
      '/v1/proxy/chat/completions',
      chatBody('hi'),
      INPUT_GUARD
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].url).toBe(
      'https://api.openai.com/v1/chat/completions'
    );
    expect(upstreamCalls[0].body).toEqual(chatBody('hi'));
    expect(((await res.json()) as any).choices[0].message.content).toBe('ok');
  });

  test.each([
    [
      '/v1/proxy/completions',
      /api\.openai\.com\/v1\/completions$/,
      { model: 'gpt-3.5-turbo-instruct', prompt: 'the SECRET' },
    ],
    [
      '/v1/proxy/embeddings',
      /api\.openai\.com\/v1\/embeddings$/,
      { model: 'text-embedding-3-small', input: 'the SECRET' },
    ],
    [
      '/v1/proxy/responses',
      /api\.openai\.com\/v1\/responses$/,
      { model: 'gpt-5', input: 'the SECRET' },
    ],
  ])('%s is blocked too', async (path, url, body) => {
    upstream(url, () => json({}));
    const res = await post(app, path, body, INPUT_GUARD);
    expect(res.status).toBe(446);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('a proxied Anthropic Messages request is blocked', async () => {
    upstream(/api\.anthropic\.com/, () => json({}));
    const res = await post(
      app,
      '/v1/proxy/messages',
      {
        model: 'claude-sonnet-4-5',
        max_tokens: 16,
        messages: [{ role: 'user', content: 'the SECRET' }],
      },
      config({
        provider: 'anthropic',
        api_key: 'sk-ant-regression',
        input_guardrails: blockWord('SECRET'),
      })
    );
    expect(res.status).toBe(446);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('a proxied Mantle /openai/v1/responses request is blocked', async () => {
    upstream(/.*/, () => json({}));
    const res = await post(
      app,
      '/v1/proxy/openai/v1/responses',
      { model: 'openai.gpt-5', input: 'tell me the SECRET' },
      config({
        provider: 'bedrock-mantle',
        aws_access_key_id: 'AKIAREGRESSIONTEST',
        aws_secret_access_key: 'regression-secret',
        aws_region: 'us-east-1',
        input_guardrails: blockWord('SECRET'),
      })
    );
    expect(res.status).toBe(446);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('default input guardrails (header) apply on the proxy', async () => {
    upstream(CHAT_URL, () => chatOk());
    const res = await post(
      app,
      '/v1/proxy/chat/completions',
      chatBody('SECRET'),
      {
        ...openai(),
        'x-portkey-default-input-guardrails': JSON.stringify(
          blockWord('SECRET')
        ),
      }
    );
    expect(res.status).toBe(446);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('guardrails on a fallback child apply to it', async () => {
    upstream(CHAT_URL, () => chatOk());
    const res = await post(
      app,
      '/v1/proxy/chat/completions',
      chatBody('the SECRET'),
      config({
        strategy: { mode: 'fallback' },
        targets: [
          {
            provider: 'openai',
            api_key: 'sk-regression',
            input_guardrails: blockWord('SECRET'),
          },
        ],
      })
    );
    expect(res.status).toBe(446);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('an input mutator rewrites the proxied body', async () => {
    upstream(CHAT_URL, () => chatOk());
    const res = await post(
      app,
      '/v1/proxy/chat/completions',
      chatBody('call 555-1234'),
      openai({
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
    expect(res.status).toBe(200);
    expect(JSON.stringify(upstreamCalls[0].body)).toContain('call [N]');
    expect(JSON.stringify(upstreamCalls[0].body)).not.toContain('555-1234');
  });
});

describe('output guardrails on proxied model calls', () => {
  test('a blocked proxied response is replaced by a 446', async () => {
    upstream(CHAT_URL, () => chatOk('a LEAK'));
    const res = await post(
      app,
      '/v1/proxy/chat/completions',
      chatBody('hi'),
      OUTPUT_GUARD
    );
    expect(res.status).toBe(446);
    expect(upstreamCalls).toHaveLength(1);
    expect(JSON.stringify(await res.json())).not.toContain('a LEAK');
  });

  test('a passing proxied response is returned', async () => {
    upstream(CHAT_URL, () => chatOk('fine'));
    const res = await post(
      app,
      '/v1/proxy/chat/completions',
      chatBody('hi'),
      OUTPUT_GUARD
    );
    expect(res.status).toBe(200);
    expect(((await res.json()) as any).choices[0].message.content).toBe('fine');
  });
});

describe('proxied requests the guardrails cannot inspect fail closed', () => {
  const expect400 = async (res: Response) => {
    expect(res.status).toBe(400);
    expect(upstreamCalls).toHaveLength(0);
    const body: any = await res.json();
    expect(body.error.type).toBe('invalid_request_error');
    expect(body.error.message).toContain('Guardrails');
  };

  test.each([
    ['/v1/proxy/assistants'],
    ['/v1/some/unknown/endpoint'],
    ['/v1/proxy/threads/t1/messages'], // path matches, body is not Messages
  ])('unknown proxied POST %s is a 400', async (path) => {
    upstream(/.*/, () => json({}));
    await expect400(
      await post(app, path, { role: 'user', content: 'SECRET' }, INPUT_GUARD)
    );
  });

  test('a chat-completions path with a non-chat body is a 400', async () => {
    upstream(/.*/, () => json({}));
    await expect400(
      await post(
        app,
        '/v1/proxy/chat/completions',
        { model: 'gpt-4o-mini', prompt: 'SECRET' },
        INPUT_GUARD
      )
    );
  });

  test('a multipart proxied request is a 400', async () => {
    upstream(/.*/, () => json({}));
    const form = new FormData();
    form.append('prompt', 'SECRET');
    await expect400(
      await app.request('/v1/proxy/chat/completions', {
        method: 'POST',
        body: form,
        headers: INPUT_GUARD,
      })
    );
  });

  test('a binary proxied request is a 400', async () => {
    upstream(/.*/, () => json({}));
    await expect400(
      await app.request('/v1/proxy/audio/x', {
        method: 'POST',
        body: new Uint8Array([1, 2, 3]),
        headers: { ...INPUT_GUARD, 'content-type': 'audio/wav' },
      })
    );
  });

  test('a proxied GET of stored output is a 400', async () => {
    upstream(/.*/, () => json({}));
    await expect400(
      await app.request('/v1/proxy/responses/resp_1', {
        method: 'GET',
        headers: OUTPUT_GUARD,
      })
    );
  });

  test('a fallback cannot reach an unguarded target first', async () => {
    upstream(/.*/, () => json({}));
    await expect400(
      await post(
        app,
        '/v1/proxy/assistants',
        { name: 'x' },
        config({
          strategy: { mode: 'fallback' },
          targets: [
            { provider: 'openai', api_key: 'sk-a' },
            {
              provider: 'openai',
              api_key: 'sk-b',
              input_guardrails: blockWord('SECRET'),
            },
          ],
        })
      )
    );
  });

  test('a Responses check that cannot read Responses is a 400', async () => {
    upstream(/.*/, () => json({}));
    const res = await post(
      app,
      '/v1/proxy/responses',
      { model: 'gpt-5', input: 'hi' },
      openai({
        before_request_hooks: [
          {
            type: 'guardrail',
            id: 'g',
            deny: true,
            checks: [
              {
                id: 'default.webhook',
                parameters: { webhookURL: 'https://hooks.example/x' },
              },
            ],
          },
        ],
      })
    );
    expect(res.status).toBe(400);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('a proxied model listing still works', async () => {
    upstream(/api\.openai\.com\/v1\/models$/, () =>
      json({ object: 'list', data: [] })
    );
    const res = await app.request('/v1/proxy/models', {
      method: 'GET',
      headers: INPUT_GUARD,
    });
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(1);
  });
});

describe('targets without guardrails keep pure passthrough', () => {
  test('a chat completion is forwarded as sent', async () => {
    upstream(CHAT_URL, () => chatOk());
    const res = await post(
      app,
      '/v1/proxy/chat/completions',
      chatBody('the SECRET'),
      openai()
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].body).toEqual(chatBody('the SECRET'));
  });

  test('an unknown JSON path is proxied', async () => {
    upstream(/api\.openai\.com\/v1\/assistants$/, () => json({ id: 'a' }));
    const res = await post(
      app,
      '/v1/proxy/assistants',
      { name: 'x' },
      openai()
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].body).toEqual({ name: 'x' });
  });

  test('multipart bodies keep their fields and bytes', async () => {
    upstream(/api\.openai\.com\/v1\/uploads\/u1\/parts$/, () =>
      json({ id: 'part' })
    );
    const bytes = new Uint8Array([0, 255, 1, 128, 10, 13]);
    const form = new FormData();
    form.append('purpose', 'x');
    form.append('data', new Blob([bytes]), 'a.bin');
    const res = await app.request('/v1/proxy/uploads/u1/parts', {
      method: 'POST',
      body: form,
      headers: openai(),
    });
    expect(res.status).toBe(200);
    const sent = upstreamCalls[0].body as FormData;
    expect(sent).toBeInstanceOf(FormData);
    expect(sent.get('purpose')).toBe('x');
    const file = sent.get('data') as unknown as Blob;
    expect(new Uint8Array(await file.arrayBuffer())).toEqual(bytes);
  });
});
