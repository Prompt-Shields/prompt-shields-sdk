/**
 * Regression tests for Messages → Responses routing (#73): `/v1/messages`
 * for OpenAI models that only the Responses API serves (codex), through the
 * whole gateway app and offline, with the ps-telemetry event each emits.
 */
jest.mock('jose', () => ({}));

import {
  json,
  post,
  resetHarness,
  sse,
  telemetryEvent,
  upstream,
  upstreamCalls,
} from './harness';
import app from '../../index';
import {
  messagesViaResponsesOverride,
  routesMessagesViaResponses,
  transformMessagesToResponses,
} from '../../adapters/messagesViaResponses';

beforeEach(() => resetHarness());

const OPENAI = {
  'x-portkey-provider': 'openai',
  authorization: 'Bearer sk-regression',
};

const RESPONSES_URL = /api\.openai\.com\/v1\/responses$/;

/** Parse an Anthropic SSE body into its data payloads. */
function messagesEvents(text: string): any[] {
  return text
    .split('\n\n')
    .map((m) => m.split('\n').find((l) => l.startsWith('data: ')))
    .filter(Boolean)
    .map((l) => JSON.parse((l as string).slice(6)));
}

function responsesSSE(evs: any[]): string {
  return evs
    .map((e) => `event: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`)
    .join('');
}

describe('routing decision', () => {
  test.each([
    ['openai', 'gpt-5-codex', true],
    ['openai', 'gpt-5.1-codex-max', true],
    ['azure-openai', 'codex-mini-latest', true],
    ['openai', 'o3-pro', true],
    ['openai', 'gpt-5-pro-2025-10-06', true],
    ['openai', 'gpt-5', false],
    ['openai', 'gpt-4o-mini', false],
    ['groq', 'gpt-5-codex', false],
    ['anthropic', 'claude-sonnet-4-5', false],
  ])('%s %s → %s', (provider, model, expected) => {
    expect(routesMessagesViaResponses(provider, model)).toBe(expected);
  });
});

describe('Messages via Responses (#73)', () => {
  test('non-streaming: request and response are translated', async () => {
    upstream(RESPONSES_URL, () =>
      json({
        id: 'resp_1',
        object: 'response',
        status: 'completed',
        model: 'gpt-5-codex',
        output: [
          {
            id: 'rs_1',
            type: 'reasoning',
            summary: [{ type: 'summary_text', text: 'Thinking it over' }],
          },
          {
            id: 'msg_1',
            type: 'message',
            role: 'assistant',
            content: [{ type: 'output_text', text: 'Hi there' }],
          },
        ],
        usage: {
          input_tokens: 120,
          input_tokens_details: { cached_tokens: 100 },
          output_tokens: 30,
          output_tokens_details: { reasoning_tokens: 20 },
          total_tokens: 150,
        },
      })
    );
    const res = await post(
      app,
      '/v1/messages',
      {
        model: 'gpt-5-codex',
        max_tokens: 1024,
        temperature: 1,
        system: [{ type: 'text', text: 'Be brief.' }],
        thinking: { type: 'enabled', budget_tokens: 2048 },
        output_config: { effort: 'low' },
        messages: [
          { role: 'user', content: 'Hello' },
          {
            role: 'assistant',
            content: [
              { type: 'thinking', thinking: 'old', signature: 'sig' },
              { type: 'text', text: 'Hey' },
            ],
          },
          { role: 'user', content: [{ type: 'text', text: 'Again' }] },
        ],
      },
      OPENAI
    );
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      id: 'resp_1',
      type: 'message',
      role: 'assistant',
      model: 'gpt-5-codex',
      content: [
        { type: 'thinking', thinking: 'Thinking it over', signature: '' },
        { type: 'text', text: 'Hi there' },
      ],
      stop_reason: 'end_turn',
      stop_sequence: null,
      usage: {
        input_tokens: 20,
        output_tokens: 30,
        cache_creation_input_tokens: 0,
        cache_read_input_tokens: 100,
      },
    });

    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].url).toBe('https://api.openai.com/v1/responses');
    expect(upstreamCalls[0].body).toEqual({
      model: 'gpt-5-codex',
      instructions: 'Be brief.',
      max_output_tokens: 1024,
      store: false,
      reasoning: { effort: 'low', summary: 'auto' },
      include: ['reasoning.encrypted_content'],
      // The Anthropic-signed thinking block in the history is not sent.
      input: [
        {
          type: 'message',
          role: 'user',
          content: [{ type: 'input_text', text: 'Hello' }],
        },
        {
          type: 'message',
          role: 'assistant',
          content: [{ type: 'output_text', text: 'Hey' }],
        },
        {
          type: 'message',
          role: 'user',
          content: [{ type: 'input_text', text: 'Again' }],
        },
      ],
    });

    expect(await telemetryEvent()).toMatchObject({
      vendor: 'openai',
      api_surface: 'messages',
      model: 'gpt-5-codex',
      tokens_in: 120,
      tokens_out: 30,
    });
  });

  test('tool use round trip: tools, tool_use history and function_call output', async () => {
    upstream(RESPONSES_URL, () =>
      json({
        id: 'resp_2',
        object: 'response',
        status: 'completed',
        model: 'gpt-5-codex',
        output: [
          {
            id: 'fc_9',
            type: 'function_call',
            call_id: 'call_9',
            name: 'read_file',
            arguments: '{"path":"b.ts"}',
          },
        ],
        usage: { input_tokens: 50, output_tokens: 8, total_tokens: 58 },
      })
    );
    const res = await post(
      app,
      '/v1/messages',
      {
        model: 'gpt-5-codex',
        max_tokens: 256,
        tools: [
          {
            name: 'read_file',
            description: 'Read a file',
            input_schema: {
              type: 'object',
              properties: { path: { type: 'string' } },
            },
          },
          { type: 'web_search_20250305', name: 'web_search' },
        ],
        tool_choice: { type: 'any', disable_parallel_tool_use: true },
        messages: [
          { role: 'user', content: 'Read a.ts' },
          {
            role: 'assistant',
            content: [
              { type: 'text', text: 'Reading.' },
              {
                type: 'tool_use',
                id: 'toolu_1',
                name: 'read_file',
                input: { path: 'a.ts' },
              },
            ],
          },
          {
            role: 'user',
            content: [
              {
                type: 'tool_result',
                tool_use_id: 'toolu_1',
                content: [{ type: 'text', text: 'export {}' }],
              },
              { type: 'text', text: 'Now b.ts' },
            ],
          },
        ],
      },
      OPENAI
    );
    expect(res.status).toBe(200);
    const out: any = await res.json();
    expect(out.content).toEqual([
      {
        type: 'tool_use',
        id: 'call_9',
        name: 'read_file',
        input: { path: 'b.ts' },
      },
    ]);
    expect(out.stop_reason).toBe('tool_use');

    const sent = upstreamCalls[0].body;
    expect(sent.tools).toEqual([
      {
        type: 'function',
        name: 'read_file',
        description: 'Read a file',
        parameters: {
          type: 'object',
          properties: { path: { type: 'string' } },
        },
        strict: false,
      },
      { type: 'web_search' },
    ]);
    expect(sent.include).toEqual(['web_search_call.action.sources']);
    expect(sent.tool_choice).toBe('required');
    expect(sent.parallel_tool_calls).toBe(false);
    expect(sent.input).toEqual([
      {
        type: 'message',
        role: 'user',
        content: [{ type: 'input_text', text: 'Read a.ts' }],
      },
      {
        type: 'message',
        role: 'assistant',
        content: [{ type: 'output_text', text: 'Reading.' }],
      },
      {
        type: 'function_call',
        call_id: 'toolu_1',
        name: 'read_file',
        arguments: '{"path":"a.ts"}',
      },
      { type: 'function_call_output', call_id: 'toolu_1', output: 'export {}' },
      {
        type: 'message',
        role: 'user',
        content: [{ type: 'input_text', text: 'Now b.ts' }],
      },
    ]);

    expect(await telemetryEvent()).toMatchObject({
      api_surface: 'messages',
      tokens_in: 50,
      tokens_out: 8,
    });
  });

  test('streaming: thinking, text and tool_use blocks, usage at the end', async () => {
    upstream(RESPONSES_URL, () =>
      sse(
        responsesSSE([
          {
            type: 'response.created',
            response: {
              id: 'resp_3',
              model: 'gpt-5-codex',
              status: 'in_progress',
            },
          },
          {
            type: 'response.output_item.added',
            output_index: 0,
            item: { id: 'rs_1', type: 'reasoning', summary: [] },
          },
          {
            type: 'response.reasoning_summary_text.delta',
            item_id: 'rs_1',
            summary_index: 0,
            delta: 'Plan',
          },
          {
            type: 'response.output_item.done',
            output_index: 0,
            item: { id: 'rs_1', type: 'reasoning' },
          },
          {
            type: 'response.output_item.added',
            output_index: 1,
            item: {
              id: 'msg_1',
              type: 'message',
              role: 'assistant',
              content: [],
            },
          },
          {
            type: 'response.output_text.delta',
            item_id: 'msg_1',
            content_index: 0,
            delta: 'Hel',
          },
          {
            type: 'response.output_text.delta',
            item_id: 'msg_1',
            content_index: 0,
            delta: 'lo',
          },
          {
            type: 'response.output_item.done',
            output_index: 1,
            item: { id: 'msg_1', type: 'message' },
          },
          {
            type: 'response.output_item.added',
            output_index: 2,
            item: {
              id: 'fc_1',
              type: 'function_call',
              call_id: 'call_1',
              name: 'ls',
              arguments: '',
            },
          },
          {
            type: 'response.function_call_arguments.delta',
            item_id: 'fc_1',
            delta: '{"dir":',
          },
          {
            type: 'response.function_call_arguments.delta',
            item_id: 'fc_1',
            delta: '"."}',
          },
          {
            type: 'response.output_item.done',
            output_index: 2,
            item: {
              id: 'fc_1',
              type: 'function_call',
              arguments: '{"dir":"."}',
            },
          },
          {
            type: 'response.completed',
            response: {
              id: 'resp_3',
              model: 'gpt-5-codex',
              status: 'completed',
              usage: {
                input_tokens: 40,
                input_tokens_details: { cached_tokens: 10 },
                output_tokens: 12,
                total_tokens: 52,
              },
            },
          },
        ])
      )
    );
    const res = await post(
      app,
      '/v1/messages',
      {
        model: 'gpt-5-codex',
        max_tokens: 256,
        stream: true,
        tools: [{ name: 'ls', input_schema: { type: 'object' } }],
        messages: [{ role: 'user', content: 'List' }],
      },
      OPENAI
    );
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toContain('text/event-stream');
    const evs = messagesEvents(await res.text());
    expect(evs.map((e) => e.type)).toEqual([
      'message_start',
      'content_block_start',
      'content_block_delta',
      'content_block_stop',
      'content_block_start',
      'content_block_delta',
      'content_block_delta',
      'content_block_stop',
      'content_block_start',
      'content_block_delta',
      'content_block_delta',
      'content_block_stop',
      'message_delta',
      'message_stop',
    ]);
    expect(evs[0].message).toMatchObject({
      id: 'resp_3',
      model: 'gpt-5-codex',
    });
    expect(evs[1]).toMatchObject({
      index: 0,
      content_block: { type: 'thinking' },
    });
    expect(evs[2].delta).toEqual({ type: 'thinking_delta', thinking: 'Plan' });
    expect(evs[4]).toMatchObject({ index: 1, content_block: { type: 'text' } });
    expect(evs[5].delta).toEqual({ type: 'text_delta', text: 'Hel' });
    expect(evs[8]).toEqual({
      type: 'content_block_start',
      index: 2,
      content_block: { type: 'tool_use', id: 'call_1', name: 'ls', input: {} },
    });
    expect(evs[9].delta.partial_json + evs[10].delta.partial_json).toBe(
      '{"dir":"."}'
    );
    expect(evs[12]).toEqual({
      type: 'message_delta',
      delta: { stop_reason: 'tool_use', stop_sequence: null },
      usage: {
        input_tokens: 30,
        output_tokens: 12,
        cache_creation_input_tokens: 0,
        cache_read_input_tokens: 10,
      },
    });

    expect(upstreamCalls[0].body).toMatchObject({ stream: true, store: false });
    expect(await telemetryEvent()).toMatchObject({
      vendor: 'openai',
      api_surface: 'messages',
      stream: true,
      tokens_in: 40,
      tokens_out: 12,
    });
  });

  test('streaming: an upstream that ends early yields an error event, marked truncated', async () => {
    upstream(RESPONSES_URL, () =>
      sse(
        responsesSSE([
          {
            type: 'response.created',
            response: { id: 'resp_4', model: 'gpt-5-codex' },
          },
          {
            type: 'response.output_text.delta',
            item_id: 'msg_1',
            delta: 'Hal',
          },
        ])
      )
    );
    const res = await post(
      app,
      '/v1/messages',
      {
        model: 'gpt-5-codex',
        max_tokens: 64,
        stream: true,
        messages: [{ role: 'user', content: 'Hi' }],
      },
      OPENAI
    );
    const evs = messagesEvents(await res.text());
    expect(evs.map((e) => e.type)).toEqual([
      'message_start',
      'content_block_start',
      'content_block_delta',
      'error',
    ]);
    expect(await telemetryEvent()).toMatchObject({
      stream: true,
      stream_truncated: true,
    });
  });

  test('streaming: an upstream that sends no events yields an error event', async () => {
    upstream(RESPONSES_URL, () => sse(''));
    const res = await post(
      app,
      '/v1/messages',
      {
        model: 'gpt-5-codex',
        max_tokens: 64,
        stream: true,
        messages: [{ role: 'user', content: 'Hi' }],
      },
      OPENAI
    );
    const evs = messagesEvents(await res.text());
    expect(evs.map((e) => e.type)).toEqual(['error']);
    expect(evs[0].error.type).toBe('api_error');
  });

  test('incomplete response maps to max_tokens; provider errors use the Anthropic envelope', async () => {
    upstream(RESPONSES_URL, () =>
      json({
        id: 'resp_5',
        object: 'response',
        status: 'incomplete',
        incomplete_details: { reason: 'max_output_tokens' },
        model: 'gpt-5-codex',
        output: [],
        usage: { input_tokens: 5, output_tokens: 16 },
      })
    );
    const body = {
      model: 'gpt-5-codex',
      max_tokens: 16,
      messages: [{ role: 'user', content: 'Hi' }],
    };
    let res = await post(app, '/v1/messages', body, OPENAI);
    expect(((await res.json()) as any).stop_reason).toBe('max_tokens');

    resetHarness();
    upstream(RESPONSES_URL, () =>
      json(
        {
          error: {
            message: 'Invalid value for input',
            type: 'invalid_request_error',
          },
        },
        400
      )
    );
    res = await post(app, '/v1/messages', body, OPENAI);
    expect(res.status).toBe(400);
    expect(await res.json()).toMatchObject({
      type: 'error',
      error: {
        type: 'invalid_request_error',
        message: expect.stringContaining('Invalid value for input'),
      },
    });
  });

  test('azure-openai: codex deployment reached at /openai/responses', async () => {
    upstream(/regres\.openai\.azure\.com\/openai\/responses\?/, () =>
      json({
        id: 'resp_6',
        object: 'response',
        status: 'completed',
        model: 'gpt-5.1-codex',
        output: [
          {
            type: 'message',
            content: [{ type: 'output_text', text: 'ok' }],
          },
        ],
        usage: { input_tokens: 3, output_tokens: 1 },
      })
    );
    const res = await post(
      app,
      '/v1/messages',
      {
        model: 'gpt-5.1-codex',
        max_tokens: 16,
        messages: [{ role: 'user', content: 'Hi' }],
      },
      {
        'x-portkey-provider': 'azure-openai',
        'x-portkey-azure-resource-name': 'regres',
        'x-portkey-azure-deployment-id': 'codex',
        'x-portkey-azure-api-version': '2025-04-01-preview',
        authorization: 'Bearer az-key',
      }
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].url).toBe(
      'https://regres.openai.azure.com/openai/responses?api-version=2025-04-01-preview'
    );
    expect(((await res.json()) as any).content).toEqual([
      { type: 'text', text: 'ok' },
    ]);
    expect(await telemetryEvent()).toMatchObject({
      api_surface: 'messages',
      tokens_in: 3,
      tokens_out: 1,
    });
  });

  test('non-codex OpenAI models still use chat completions', async () => {
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
      json({
        id: 'chatcmpl-1',
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
        usage: { prompt_tokens: 3, completion_tokens: 1, total_tokens: 4 },
      })
    );
    const res = await post(
      app,
      '/v1/messages',
      {
        model: 'gpt-5',
        max_tokens: 16,
        messages: [{ role: 'user', content: 'Hi' }],
      },
      OPENAI
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].url).toBe(
      'https://api.openai.com/v1/chat/completions'
    );
  });
});

describe('messages → responses request edge cases', () => {
  test('max_tokens below 16 is raised to the Responses minimum', () => {
    const base = { model: 'gpt-5-codex', messages: [] };
    expect(
      transformMessagesToResponses({ ...base, max_tokens: 1 })
    ).toMatchObject({ max_output_tokens: 16 });
    expect(
      transformMessagesToResponses({ ...base, max_tokens: 64 })
    ).toMatchObject({ max_output_tokens: 64 });
    expect(
      (transformMessagesToResponses(base) as any).max_output_tokens
    ).toBeUndefined();
  });

  test('base64 image without media_type gets a sniffed type, never undefined', () => {
    const urlFor = (data: string) =>
      (
        transformMessagesToResponses({
          model: 'gpt-5-codex',
          messages: [
            {
              role: 'user',
              content: [{ type: 'image', source: { type: 'base64', data } }],
            },
          ],
        }) as any
      ).input[0].content[0].image_url;
    expect(urlFor('/9j/4AAQ')).toBe('data:image/jpeg;base64,/9j/4AAQ');
    expect(urlFor('iVBORw0K')).toBe('data:image/png;base64,iVBORw0K');
    expect(urlFor('R0lGODlh')).toBe('data:image/gif;base64,R0lGODlh');
    expect(urlFor('UklGRiQA')).toBe('data:image/webp;base64,UklGRiQA');
    expect(urlFor('AAAA')).not.toContain('undefined');
  });
});

// ── #73 follow-ups ──────────────────────────────────────────────────────────

const okResponse = (model = 'gpt-5', output: any[] = []) =>
  json({
    id: 'resp_x',
    object: 'response',
    status: 'completed',
    model,
    output: output.length
      ? output
      : [{ type: 'message', content: [{ type: 'output_text', text: 'ok' }] }],
    usage: { input_tokens: 3, output_tokens: 1 },
  });

const CHAT_URL = /api\.openai\.com\/v1\/chat\/completions$/;
const chatOk = () =>
  json({
    id: 'chatcmpl-1',
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
    usage: { prompt_tokens: 3, completion_tokens: 1, total_tokens: 4 },
  });

const hi = (model: string, extra: Record<string, any> = {}) => ({
  model,
  max_tokens: 16,
  messages: [{ role: 'user', content: 'Hi' }],
  ...extra,
});

describe('explicit routing opt-in / opt-out', () => {
  test.each([
    ['azure-openai', 'prod-deployment', true, true],
    ['openai', 'gpt-5', true, true],
    ['openai', 'gpt-5-codex', false, false],
    ['groq', 'llama', true, false],
    ['anthropic', 'claude-sonnet-4-5', true, false],
  ])('%s %s override=%s → %s', (provider, model, override, expected) => {
    expect(routesMessagesViaResponses(provider, model, override)).toBe(
      expected
    );
  });

  test('target setting wins over the header; other header values are ignored', () => {
    expect(messagesViaResponsesOverride(true, 'false')).toBe(true);
    expect(messagesViaResponsesOverride(false, 'true')).toBe(false);
    expect(messagesViaResponsesOverride(undefined, ' TRUE ')).toBe(true);
    expect(messagesViaResponsesOverride(undefined, 'false')).toBe(false);
    expect(messagesViaResponsesOverride(undefined, '1')).toBeUndefined();
    expect(messagesViaResponsesOverride('yes', undefined)).toBeUndefined();
  });

  test('header forces an Azure deployment whose name does not say codex', async () => {
    upstream(/regres\.openai\.azure\.com\/openai\/responses\?/, () =>
      okResponse('gpt-5.1-codex')
    );
    const res = await post(app, '/v1/messages', hi('prod-coder'), {
      'x-portkey-provider': 'azure-openai',
      'x-portkey-azure-resource-name': 'regres',
      'x-portkey-azure-deployment-id': 'prod-coder',
      'x-portkey-azure-api-version': '2025-04-01-preview',
      'x-portkey-messages-via-responses': 'true',
      authorization: 'Bearer az-key',
    });
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].url).toContain('/openai/responses?');
    expect(upstreamCalls[0].body).toMatchObject({
      model: 'prod-coder',
      store: false,
    });
    expect(((await res.json()) as any).content).toEqual([
      { type: 'text', text: 'ok' },
    ]);
  });

  test('header false keeps a codex model on chat completions', async () => {
    upstream(CHAT_URL, chatOk);
    const res = await post(app, '/v1/messages', hi('gpt-5-codex'), {
      ...OPENAI,
      'x-portkey-messages-via-responses': 'false',
    });
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].url).toMatch(CHAT_URL);
  });

  test('config target key forces the route and beats the header', async () => {
    upstream(RESPONSES_URL, () => okResponse());
    const config = {
      provider: 'openai',
      api_key: 'sk-regression',
      messages_via_responses: true,
    };
    const res = await post(app, '/v1/messages', hi('gpt-5'), {
      'x-portkey-config': JSON.stringify(config),
      'x-portkey-messages-via-responses': 'false',
    });
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].url).toMatch(RESPONSES_URL);
  });

  test('a strategy-level setting is inherited by its targets (opt-out)', async () => {
    upstream(CHAT_URL, chatOk);
    const config = {
      strategy: { mode: 'fallback' },
      messages_via_responses: false,
      targets: [{ provider: 'openai', api_key: 'sk-regression' }],
    };
    const res = await post(app, '/v1/messages', hi('gpt-5-codex'), {
      'x-portkey-config': JSON.stringify(config),
    });
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].url).toMatch(CHAT_URL);
  });

  test('the header cannot force the route onto a target with guardrails', async () => {
    // Guardrails do not run on Responses-format requests: a caller must not
    // be able to switch them off with a header.
    expect(
      messagesViaResponsesOverride(undefined, 'true', true)
    ).toBeUndefined();
    expect(messagesViaResponsesOverride(undefined, 'false', true)).toBe(false);
    expect(messagesViaResponsesOverride(true, undefined, true)).toBe(true);
    upstream(CHAT_URL, chatOk);
    upstream(RESPONSES_URL, () => okResponse());
    const config = {
      provider: 'openai',
      api_key: 'sk-regression',
      input_guardrails: [
        {
          'default.contains': { words: ['SECRET'], operator: 'none' },
          deny: true,
        },
      ],
    };
    const res = await post(
      app,
      '/v1/messages',
      {
        model: 'gpt-5',
        max_tokens: 16,
        messages: [{ role: 'user', content: 'SECRET' }],
      },
      {
        'x-portkey-config': JSON.stringify(config),
        'x-portkey-messages-via-responses': 'true',
      }
    );
    expect(res.status).toBe(446);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('a non-boolean config value is rejected by config validation', async () => {
    const config = {
      provider: 'openai',
      api_key: 'sk-regression',
      messages_via_responses: 'yes',
    };
    const res = await post(app, '/v1/messages', hi('gpt-5'), {
      'x-portkey-config': JSON.stringify(config),
    });
    expect(res.status).toBe(400);
    expect(upstreamCalls).toHaveLength(0);
  });
});

describe('thinking round trip via encrypted reasoning', () => {
  const THINKING = { thinking: { type: 'enabled', budget_tokens: 1024 } };

  test('non-streaming: encrypted reasoning is returned in signatures', async () => {
    upstream(RESPONSES_URL, () =>
      okResponse('gpt-5-codex', [
        {
          id: 'rs_1',
          type: 'reasoning',
          summary: [{ type: 'summary_text', text: 'Plan' }],
          encrypted_content: 'gAAAA-enc-1',
        },
        { id: 'rs_2', type: 'reasoning', summary: [], encrypted_content: 'e2' },
        {
          type: 'message',
          content: [{ type: 'output_text', text: 'Done' }],
        },
      ])
    );
    const res = await post(
      app,
      '/v1/messages',
      hi('gpt-5-codex', THINKING),
      OPENAI
    );
    expect(((await res.json()) as any).content).toEqual([
      {
        type: 'thinking',
        thinking: 'Plan',
        signature: 'ps-rsn1:openai:gAAAA-enc-1',
      },
      { type: 'redacted_thinking', data: 'ps-rsn1:openai:e2' },
      { type: 'text', text: 'Done' },
    ]);
    expect(upstreamCalls[0].body.include).toEqual([
      'reasoning.encrypted_content',
    ]);
  });

  test('only reasoning this adapter issued for the same provider is sent back', async () => {
    upstream(RESPONSES_URL, () => okResponse('gpt-5-codex'));
    await post(
      app,
      '/v1/messages',
      {
        model: 'gpt-5-codex',
        max_tokens: 64,
        ...THINKING,
        messages: [
          { role: 'user', content: 'Go' },
          {
            role: 'assistant',
            content: [
              // Anthropic-signed: never sent to OpenAI
              {
                type: 'thinking',
                thinking: 'a',
                signature: 'EqQBCkgIARABGAI=',
              },
              // Issued for Azure: OpenAI cannot decrypt it
              {
                type: 'thinking',
                thinking: 'b',
                signature: 'ps-rsn1:azure-openai:enc-az',
              },
              { type: 'redacted_thinking', data: 'EmwKAhgB' },
              {
                type: 'thinking',
                thinking: 'Plan',
                signature: 'ps-rsn1:openai:enc-1',
              },
              { type: 'redacted_thinking', data: 'ps-rsn1:openai:enc-2' },
              { type: 'tool_use', id: 'call_1', name: 'ls', input: {} },
            ],
          },
          {
            role: 'user',
            content: [
              { type: 'tool_result', tool_use_id: 'call_1', content: 'a.ts' },
            ],
          },
        ],
        tools: [{ name: 'ls', input_schema: { type: 'object' } }],
      },
      OPENAI
    );
    expect(upstreamCalls[0].body.input).toEqual([
      {
        type: 'message',
        role: 'user',
        content: [{ type: 'input_text', text: 'Go' }],
      },
      {
        type: 'reasoning',
        summary: [{ type: 'summary_text', text: 'Plan' }],
        encrypted_content: 'enc-1',
      },
      { type: 'reasoning', summary: [], encrypted_content: 'enc-2' },
      { type: 'function_call', call_id: 'call_1', name: 'ls', arguments: '{}' },
      { type: 'function_call_output', call_id: 'call_1', output: 'a.ts' },
    ]);
  });

  test('streaming: signature_delta closes the thinking block; unsummarised reasoning is redacted_thinking', async () => {
    upstream(RESPONSES_URL, () =>
      sse(
        responsesSSE([
          {
            type: 'response.created',
            response: { id: 'resp_7', model: 'gpt-5-codex' },
          },
          {
            type: 'response.reasoning_summary_text.delta',
            item_id: 'rs_1',
            summary_index: 0,
            delta: 'Plan',
          },
          {
            type: 'response.output_item.done',
            item: {
              id: 'rs_1',
              type: 'reasoning',
              summary: [{ type: 'summary_text', text: 'Plan' }],
              encrypted_content: 'enc-1',
            },
          },
          {
            type: 'response.output_item.done',
            item: {
              id: 'rs_2',
              type: 'reasoning',
              summary: [],
              encrypted_content: 'enc-2',
            },
          },
          {
            type: 'response.output_text.delta',
            item_id: 'msg_1',
            delta: 'Hi',
          },
          {
            type: 'response.completed',
            response: {
              id: 'resp_7',
              status: 'completed',
              usage: { input_tokens: 4, output_tokens: 2 },
            },
          },
        ])
      )
    );
    const res = await post(
      app,
      '/v1/messages',
      hi('gpt-5-codex', { ...THINKING, stream: true }),
      OPENAI
    );
    const evs = messagesEvents(await res.text());
    expect(evs.map((e) => e.type)).toEqual([
      'message_start',
      'content_block_start',
      'content_block_delta',
      'content_block_delta',
      'content_block_stop',
      'content_block_start',
      'content_block_stop',
      'content_block_start',
      'content_block_delta',
      'content_block_stop',
      'message_delta',
      'message_stop',
    ]);
    expect(evs[3]).toEqual({
      type: 'content_block_delta',
      index: 0,
      delta: { type: 'signature_delta', signature: 'ps-rsn1:openai:enc-1' },
    });
    expect(evs[5]).toEqual({
      type: 'content_block_start',
      index: 1,
      content_block: {
        type: 'redacted_thinking',
        data: 'ps-rsn1:openai:enc-2',
      },
    });
  });
});

describe('gateway-issued reasoning and native Messages targets', () => {
  const history = {
    model: 'claude-sonnet-4-5',
    max_tokens: 64,
    messages: [
      { role: 'user', content: 'Go' },
      {
        role: 'assistant',
        content: [
          {
            type: 'thinking',
            thinking: 'Plan',
            signature: 'ps-rsn1:openai:enc-1',
          },
          { type: 'redacted_thinking', data: 'ps-rsn1:openai:enc-2' },
          { type: 'thinking', thinking: 'a', signature: 'EqQBCkgIARABGAI=' },
          { type: 'text', text: 'Done' },
        ],
      },
      {
        role: 'assistant',
        content: [{ type: 'redacted_thinking', data: 'ps-rsn1:openai:x' }],
      },
      { role: 'user', content: 'Again' },
    ],
  };

  test('blocks signed by this route are not sent to a native target; other targets keep them', async () => {
    upstream(/api\.anthropic\.com\/v1\/messages$/, () =>
      json({ type: 'error', error: { type: 'overloaded_error' } }, 529)
    );
    upstream(RESPONSES_URL, () => okResponse('gpt-5-codex'));
    const config = {
      strategy: { mode: 'fallback' },
      targets: [
        { provider: 'anthropic', api_key: 'sk-ant-regression' },
        {
          provider: 'openai',
          api_key: 'sk-regression',
          override_params: { model: 'gpt-5-codex' },
          messages_via_responses: true,
        },
      ],
    };
    const res = await post(app, '/v1/messages', history, {
      'x-portkey-config': JSON.stringify(config),
    });
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(2);
    expect(upstreamCalls[0].body.messages).toEqual([
      { role: 'user', content: 'Go' },
      {
        role: 'assistant',
        content: [
          { type: 'thinking', thinking: 'a', signature: 'EqQBCkgIARABGAI=' },
          { type: 'text', text: 'Done' },
        ],
      },
      { role: 'user', content: 'Again' },
    ]);
    expect(
      upstreamCalls[1].body.input.filter((i: any) => i.type === 'reasoning')
    ).toEqual([
      {
        type: 'reasoning',
        summary: [{ type: 'summary_text', text: 'Plan' }],
        encrypted_content: 'enc-1',
      },
      { type: 'reasoning', summary: [], encrypted_content: 'enc-2' },
      { type: 'reasoning', summary: [], encrypted_content: 'x' },
    ]);
  });
});

describe('server tools', () => {
  test('web_search maps to the built-in tool; max_uses dropped', () => {
    const out: any = transformMessagesToResponses({
      model: 'gpt-5-codex',
      messages: [],
      tools: [
        {
          type: 'web_search_20250305',
          name: 'web_search',
          max_uses: 5,
          allowed_domains: ['example.com'],
          user_location: {
            type: 'approximate',
            city: 'Berlin',
            country: 'DE',
            timezone: 'Europe/Berlin',
          },
        },
        { type: 'code_execution_20250522', name: 'code_execution' },
        { type: 'bash_20250124', name: 'bash' },
      ],
      tool_choice: { type: 'tool', name: 'web_search' },
    });
    expect(out.tools).toEqual([
      {
        type: 'web_search',
        filters: { allowed_domains: ['example.com'] },
        user_location: {
          type: 'approximate',
          city: 'Berlin',
          country: 'DE',
          timezone: 'Europe/Berlin',
        },
      },
    ]);
    expect(out.tool_choice).toEqual({
      type: 'allowed_tools',
      mode: 'required',
      tools: [{ type: 'web_search' }],
    });
    expect(out.include).toEqual(['web_search_call.action.sources']);
  });

  test.each([
    [
      'blocked_domains',
      {
        tools: [
          {
            type: 'web_search_20250305',
            name: 'web_search',
            blocked_domains: ['evil.test'],
          },
        ],
      },
      'blocked_domains',
    ],
    [
      'tool_choice naming a dropped server tool',
      {
        tools: [
          { name: 'ls', input_schema: { type: 'object' } },
          { type: 'code_execution_20250522', name: 'code_execution' },
        ],
        tool_choice: { type: 'tool', name: 'code_execution' },
      },
      "'code_execution'",
    ],
    [
      'tool_choice naming an unknown tool',
      { tool_choice: { type: 'tool', name: 'nope' } },
      "'nope'",
    ],
  ])('%s → clean 400, nothing sent upstream', async (_, extra, needle) => {
    const res = await post(
      app,
      '/v1/messages',
      hi('gpt-5-codex', extra),
      OPENAI
    );
    expect(res.status).toBe(400);
    const body: any = await res.json();
    expect(body).toMatchObject({
      type: 'error',
      error: { type: 'invalid_request_error' },
    });
    expect(body.error.message).toContain(needle);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('web_search_call becomes server_tool_use + web_search_tool_result', async () => {
    upstream(RESPONSES_URL, () =>
      okResponse('gpt-5-codex', [
        {
          id: 'ws_1',
          type: 'web_search_call',
          status: 'completed',
          action: {
            type: 'search',
            query: 'gateway news',
            sources: [{ type: 'url', url: 'https://example.com/a' }],
          },
        },
        { id: 'ws_2', type: 'web_search_call', action: { type: 'open_page' } },
        {
          type: 'message',
          content: [{ type: 'output_text', text: 'Found it' }],
        },
      ])
    );
    const res = await post(
      app,
      '/v1/messages',
      hi('gpt-5-codex', {
        tools: [{ type: 'web_search_20250305', name: 'web_search' }],
      }),
      OPENAI
    );
    const out: any = await res.json();
    expect(out.content).toEqual([
      {
        type: 'server_tool_use',
        id: 'ws_1',
        name: 'web_search',
        input: { query: 'gateway news' },
      },
      {
        type: 'web_search_tool_result',
        tool_use_id: 'ws_1',
        content: [
          {
            type: 'web_search_result',
            url: 'https://example.com/a',
            title: 'https://example.com/a',
            encrypted_content: '',
            page_age: null,
          },
        ],
      },
      { type: 'text', text: 'Found it' },
    ]);
    expect(out.stop_reason).toBe('end_turn');
    expect(out.usage.server_tool_use).toEqual({ web_search_requests: 1 });
  });

  test('streaming web_search_call emits both blocks and the search count', async () => {
    upstream(RESPONSES_URL, () =>
      sse(
        responsesSSE([
          {
            type: 'response.created',
            response: { id: 'resp_8', model: 'gpt-5-codex' },
          },
          {
            type: 'response.output_item.done',
            item: {
              id: 'ws_1',
              type: 'web_search_call',
              action: { type: 'search', query: 'q' },
            },
          },
          {
            type: 'response.completed',
            response: {
              status: 'completed',
              usage: { input_tokens: 4, output_tokens: 2 },
            },
          },
        ])
      )
    );
    const res = await post(
      app,
      '/v1/messages',
      hi('gpt-5-codex', {
        stream: true,
        tools: [{ type: 'web_search_20250305', name: 'web_search' }],
      }),
      OPENAI
    );
    const evs = messagesEvents(await res.text());
    expect(evs.slice(1, 6)).toEqual([
      {
        type: 'content_block_start',
        index: 0,
        content_block: {
          type: 'server_tool_use',
          id: 'ws_1',
          name: 'web_search',
          input: {},
        },
      },
      {
        type: 'content_block_delta',
        index: 0,
        delta: { type: 'input_json_delta', partial_json: '{"query":"q"}' },
      },
      { type: 'content_block_stop', index: 0 },
      {
        type: 'content_block_start',
        index: 1,
        content_block: {
          type: 'web_search_tool_result',
          tool_use_id: 'ws_1',
          content: [],
        },
      },
      { type: 'content_block_stop', index: 1 },
    ]);
    const delta = evs.find((e) => e.type === 'message_delta');
    expect(delta.usage.server_tool_use).toEqual({ web_search_requests: 1 });
  });
});

describe('images in tool results', () => {
  test('tool_result with an image is sent as input parts', () => {
    const out: any = transformMessagesToResponses({
      model: 'gpt-5-codex',
      messages: [
        {
          role: 'user',
          content: [
            {
              type: 'tool_result',
              tool_use_id: 'call_1',
              is_error: true,
              content: [
                { type: 'text', text: 'Screenshot' },
                {
                  type: 'image',
                  source: {
                    type: 'base64',
                    media_type: 'image/png',
                    data: 'iVBORw0K',
                  },
                },
                {
                  type: 'image',
                  source: { type: 'url', url: 'https://example.com/i.png' },
                },
              ],
            },
          ],
        },
      ],
    });
    expect(out.input).toEqual([
      {
        type: 'function_call_output',
        call_id: 'call_1',
        output: [
          { type: 'input_text', text: 'Error:' },
          { type: 'input_text', text: 'Screenshot' },
          {
            type: 'input_image',
            image_url: 'data:image/png;base64,iVBORw0K',
            detail: 'auto',
          },
          {
            type: 'input_image',
            image_url: 'https://example.com/i.png',
            detail: 'auto',
          },
        ],
      },
    ]);
  });
});
