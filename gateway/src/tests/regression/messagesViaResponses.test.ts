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
    ]);
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
