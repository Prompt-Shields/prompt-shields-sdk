/**
 * Offline regression tests for the #74 provider `meta-model-api` (Meta Model
 * API, `https://api.meta.ai/v1`) through the whole gateway app with a stub
 * upstream: Chat Completions, native Responses and Messages (no adapter),
 * Messages count_tokens, and the Files API. Checks the request the provider
 * receives, the response the caller gets and the ps-telemetry event.
 */
jest.mock('jose', () => ({}));

import {
  json,
  post,
  resetHarness,
  telemetryEvent,
  upstream,
  upstreamCalls,
} from './harness';
import app from '../../index';

beforeEach(() => resetHarness());

const HOST = 'https://api.meta.ai/v1';
const MODEL = 'meta-test-model';
const headers = {
  'x-portkey-provider': 'meta-model-api',
  authorization: 'Bearer meta-regression',
};

describe('Meta Model API: inference', () => {
  test('chat completions keep the OpenAI shape', async () => {
    upstream(/api\.meta\.ai\/v1\/chat\/completions$/, () =>
      json({
        id: 'chatcmpl-1',
        object: 'chat.completion',
        created: 1,
        model: MODEL,
        choices: [
          {
            index: 0,
            message: { role: 'assistant', content: 'Hi' },
            finish_reason: 'stop',
          },
        ],
        usage: { prompt_tokens: 9, completion_tokens: 1, total_tokens: 10 },
      })
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      { model: MODEL, messages: [{ role: 'user', content: 'Say hi' }] },
      headers
    );
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.choices[0].message.content).toBe('Hi');
    expect(body.provider).toBe('meta-model-api');

    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].url).toBe(`${HOST}/chat/completions`);
    expect(upstreamCalls[0].headers.authorization).toBe(
      'Bearer meta-regression'
    );
    expect(upstreamCalls[0].body).toMatchObject({
      model: MODEL,
      messages: [{ role: 'user', content: 'Say hi' }],
    });
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'meta-model-api',
      model: MODEL,
      tokens_in: 9,
      tokens_out: 1,
    });
  });

  test('/v1/responses goes to the native Responses API', async () => {
    upstream(/api\.meta\.ai\/v1\/responses$/, () =>
      json({
        id: 'resp_1',
        object: 'response',
        created_at: 1,
        status: 'completed',
        model: MODEL,
        output: [
          {
            type: 'message',
            id: 'msg_1',
            role: 'assistant',
            status: 'completed',
            content: [{ type: 'output_text', text: 'Hi', annotations: [] }],
          },
        ],
        usage: { input_tokens: 7, output_tokens: 1, total_tokens: 8 },
      })
    );
    const res = await post(
      app,
      '/v1/responses',
      {
        model: MODEL,
        input: [
          {
            role: 'user',
            content: [
              { type: 'input_text', text: 'Summarise this file' },
              { type: 'input_file', file_id: 'file_abc' },
            ],
          },
        ],
      },
      headers
    );
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.id).toBe('resp_1');
    expect(body.output[0].content[0].text).toBe('Hi');

    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].url).toBe(`${HOST}/responses`);
    // Native: the Responses body reaches Meta, not a chat translation.
    expect(upstreamCalls[0].body.input[0].content[1]).toEqual({
      type: 'input_file',
      file_id: 'file_abc',
    });
    expect(upstreamCalls[0].body.messages).toBeUndefined();
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'meta-model-api',
      model: MODEL,
      tokens_in: 7,
      tokens_out: 1,
    });
  });

  test('/v1/messages goes to the native Messages API', async () => {
    upstream(/api\.meta\.ai\/v1\/messages$/, () =>
      json({
        id: 'msg_1',
        type: 'message',
        role: 'assistant',
        model: MODEL,
        content: [{ type: 'text', text: 'Hi' }],
        stop_reason: 'end_turn',
        stop_sequence: null,
        usage: { input_tokens: 11, output_tokens: 1 },
      })
    );
    const res = await post(
      app,
      '/v1/messages',
      {
        model: MODEL,
        max_tokens: 16,
        system: 'Be brief.',
        messages: [{ role: 'user', content: 'Say hi' }],
      },
      headers
    );
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body).toMatchObject({
      type: 'message',
      content: [{ type: 'text', text: 'Hi' }],
      stop_reason: 'end_turn',
    });

    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].url).toBe(`${HOST}/messages`);
    expect(upstreamCalls[0].headers.authorization).toBe(
      'Bearer meta-regression'
    );
    expect(upstreamCalls[0].headers['anthropic-version']).toBe('2023-06-01');
    expect(upstreamCalls[0].body).toMatchObject({
      model: MODEL,
      max_tokens: 16,
      system: 'Be brief.',
      messages: [{ role: 'user', content: 'Say hi' }],
    });
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'meta-model-api',
      model: MODEL,
      tokens_in: 11,
      tokens_out: 1,
    });
  });

  test('a Messages error keeps a 4xx status and the gateway error shape', async () => {
    upstream(/api\.meta\.ai\/v1\/messages$/, () =>
      json(
        {
          type: 'error',
          error: { type: 'invalid_request_error', message: 'bad model' },
        },
        400
      )
    );
    const res = await post(
      app,
      '/v1/messages',
      {
        model: MODEL,
        max_tokens: 16,
        messages: [{ role: 'user', content: 'x' }],
      },
      headers
    );
    expect(res.status).toBe(400);
    const text = await res.text();
    expect(text).toContain('bad model');
  });

  test('count_tokens uses Meta, or a local estimate when the target has hooks', async () => {
    upstream(/api\.meta\.ai\/v1\/messages\/count_tokens$/, () =>
      json({ input_tokens: 5 })
    );
    const countBody = {
      model: MODEL,
      messages: [{ role: 'user', content: 'Count me' }],
    };
    const res = await post(app, '/v1/messages/count_tokens', countBody, {
      ...headers,
      'anthropic-version': '2023-06-01',
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ input_tokens: 5 });
    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].url).toBe(`${HOST}/messages/count_tokens`);

    resetHarness();
    const hooked = await post(app, '/v1/messages/count_tokens', countBody, {
      'x-portkey-config': JSON.stringify({
        provider: 'meta-model-api',
        api_key: 'meta-regression',
        before_request_hooks: [
          {
            id: 'deny-all',
            type: 'guardrail',
            deny: true,
            checks: [{ id: 'default.contains', parameters: { words: ['x'] } }],
          },
        ],
      }),
    });
    expect(hooked.status).toBe(200);
    expect(hooked.headers.get('x-ps-token-count')).toBe('estimated');
    expect(upstreamCalls).toHaveLength(0);
  });
});

describe('Meta Model API: files', () => {
  const file = {
    id: 'file_abc',
    object: 'file',
    bytes: 5,
    filename: 'notes.txt',
    created_at: 1,
  };

  test('upload passes the multipart body through with its boundary', async () => {
    upstream(/api\.meta\.ai\/v1\/files$/, () => json(file));
    const form = new FormData();
    form.append(
      'file',
      new Blob(['hello'], { type: 'text/plain' }),
      'notes.txt'
    );
    const req = new Request('http://gateway.test/v1/files', {
      method: 'POST',
      body: form,
    });
    const res = await app.request('/v1/files', {
      method: 'POST',
      headers: {
        ...headers,
        'content-type': req.headers.get('content-type') as string,
      },
      body: req.body,
      // @ts-expect-error Node's fetch needs this for a streamed body.
      duplex: 'half',
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual(file);

    expect(upstreamCalls).toHaveLength(1);
    const call = upstreamCalls[0];
    expect(call.url).toBe(`${HOST}/files`);
    expect(call.method).toBe('POST');
    expect(call.headers.authorization).toBe('Bearer meta-regression');
    expect(call.headers['content-type']).toMatch(
      /^multipart\/form-data; boundary=/
    );
    const sent = await new Response(call.body, {
      headers: { 'content-type': call.headers['content-type'] },
    }).formData();
    const sentFile = sent.get('file') as unknown as File;
    expect(sentFile.name).toBe('notes.txt');
    expect(await sentFile.text()).toBe('hello');
  });

  test('list, retrieve and delete use the documented paths', async () => {
    upstream(/api\.meta\.ai\/v1\/files\?limit=2$/, () =>
      json({ object: 'list', data: [file] })
    );
    upstream(/api\.meta\.ai\/v1\/files\/file_abc$/, (call) =>
      call.method === 'DELETE'
        ? json({ id: 'file_abc', object: 'file', deleted: true })
        : json(file)
    );

    const list = await app.request('/v1/files?limit=2', { headers });
    expect(list.status).toBe(200);
    expect(((await list.json()) as any).data).toEqual([file]);

    const one = await app.request('/v1/files/file_abc', { headers });
    expect(one.status).toBe(200);
    expect(await one.json()).toEqual(file);

    const del = await app.request('/v1/files/file_abc', {
      method: 'DELETE',
      headers,
    });
    expect(del.status).toBe(200);
    expect(((await del.json()) as any).deleted).toBe(true);

    expect(upstreamCalls.map((c) => [c.method, c.url])).toEqual([
      ['GET', `${HOST}/files?limit=2`],
      ['GET', `${HOST}/files/file_abc`],
      ['DELETE', `${HOST}/files/file_abc`],
    ]);
    for (const call of upstreamCalls) {
      expect(call.headers.authorization).toBe('Bearer meta-regression');
      expect(call.body ?? null).toBeNull();
    }
  });

  test('a file error is returned in the gateway error shape', async () => {
    upstream(/api\.meta\.ai\/v1\/files\/file_missing$/, () =>
      json({ error: { message: 'No such file', type: 'not_found' } }, 404)
    );
    const res = await app.request('/v1/files/file_missing', { headers });
    expect(res.status).toBe(404);
    const body: any = await res.json();
    expect(body.error.message).toContain('No such file');
    expect(body.provider).toBe('meta-model-api');
  });
});
