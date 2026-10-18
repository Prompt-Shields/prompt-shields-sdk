/**
 * PS fork (#73): Messages and Responses adapter parity.
 * Usage and cached tokens, stop reasons, tool calls and results, stream
 * event order, thinking blocks and error shapes.
 */
// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../../utils/env', () => ({ Environment: () => ({}) }));

import { transformMessagesToChatCompletions } from '../messages/requestTransform';
import { transformChatCompletionsToMessages } from '../messages/responseTransform';
import {
  transformStreamChunk as messagesChunk,
  createStreamState as messagesState,
} from '../messages/streamTransform';
import { transformChatCompletionsToResponses } from '../responses/responseTransform';
import {
  transformStreamChunk as responsesChunk,
  createStreamState as responsesState,
} from '../responses/streamTransform';

const data = (o: unknown) => `data: ${JSON.stringify(o)}`;

/** Parse the SSE output of a stream transform into [event, data] pairs. */
function parseEvents(out: string): { event: string; data: any }[] {
  return out
    .split('\n\n')
    .filter((e) => e.startsWith('event:'))
    .map((e) => {
      const [eventLine, dataLine] = e.split('\n');
      return {
        event: eventLine.slice('event: '.length),
        data: JSON.parse(dataLine.slice('data: '.length)),
      };
    });
}

function runMessagesStream(chunks: string[]) {
  const state = messagesState();
  let out = '';
  for (const c of [...chunks, 'data: [DONE]'])
    out += messagesChunk(c, state) ?? '';
  return parseEvents(out);
}

function runResponsesStream(chunks: string[]) {
  const state = responsesState();
  let out = '';
  for (const c of [...chunks, 'data: [DONE]'])
    out += responsesChunk(c, state) ?? '';
  return parseEvents(out);
}

const chatResponse = (message: any, finish_reason: string, usage?: any) => ({
  id: 'chatcmpl-1',
  object: 'chat.completion',
  created: 1700000000,
  model: 'm',
  choices: [{ index: 0, message, finish_reason }],
  usage,
});

// ── Usage and cached tokens ─────────────────────────────────────────────────

describe('usage and cached tokens', () => {
  const openAiUsage = {
    prompt_tokens: 100,
    completion_tokens: 20,
    total_tokens: 120,
    prompt_tokens_details: { cached_tokens: 60 },
    completion_tokens_details: { reasoning_tokens: 7 },
  };
  const anthropicStyleUsage = {
    prompt_tokens: 100,
    completion_tokens: 20,
    total_tokens: 120,
    cache_read_input_tokens: 50,
    cache_creation_input_tokens: 30,
  };

  test('Messages JSON reports cache reads apart from input_tokens', () => {
    const out: any = transformChatCompletionsToMessages(
      chatResponse({ role: 'assistant', content: 'hi' }, 'stop', openAiUsage),
      200,
      'openai'
    );
    expect(out.usage).toEqual({
      input_tokens: 40,
      output_tokens: 20,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 60,
    });
  });

  test('Messages JSON maps Anthropic-style cache read and write', () => {
    const out: any = transformChatCompletionsToMessages(
      chatResponse(
        { role: 'assistant', content: 'hi' },
        'stop',
        anthropicStyleUsage
      ),
      200,
      'vertex-ai'
    );
    expect(out.usage).toEqual({
      input_tokens: 20,
      output_tokens: 20,
      cache_creation_input_tokens: 30,
      cache_read_input_tokens: 50,
    });
  });

  test('Messages JSON omits cache fields when nothing was cached', () => {
    const out: any = transformChatCompletionsToMessages(
      chatResponse({ role: 'assistant', content: 'hi' }, 'stop', {
        prompt_tokens: 10,
        completion_tokens: 2,
      }),
      200,
      'openai'
    );
    expect(out.usage).toEqual({ input_tokens: 10, output_tokens: 2 });
  });

  test('Messages stream message_delta carries input and cache usage', () => {
    const events = runMessagesStream([
      data({ model: 'm', choices: [{ index: 0, delta: { content: 'hi' } }] }),
      data({ choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }),
      data({ choices: [], usage: openAiUsage }),
    ]);
    const delta = events.find((e) => e.event === 'message_delta')!;
    expect(delta.data.usage).toEqual({
      input_tokens: 40,
      output_tokens: 20,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 60,
    });
  });

  test('Responses JSON reads cached and cache-write tokens from Anthropic-style usage', () => {
    const out: any = transformChatCompletionsToResponses(
      chatResponse(
        { role: 'assistant', content: 'hi' },
        'stop',
        anthropicStyleUsage
      ) as any,
      200,
      'google'
    );
    expect(out.usage).toEqual({
      input_tokens: 100,
      // cache writes (PS fork, #9) are only added when there are any
      input_tokens_details: { cached_tokens: 50, cache_creation_tokens: 30 },
      output_tokens: 20,
      output_tokens_details: { reasoning_tokens: 0 },
      total_tokens: 120,
    });
  });

  test('Responses stream reports reasoning and cached tokens', () => {
    const events = runResponsesStream([
      data({ model: 'm', choices: [{ index: 0, delta: { content: 'hi' } }] }),
      data({ choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }),
      data({ choices: [], usage: openAiUsage }),
    ]);
    const completed = events.find((e) => e.event === 'response.completed')!;
    expect(completed.data.response.usage).toEqual({
      input_tokens: 100,
      input_tokens_details: { cached_tokens: 60 },
      output_tokens: 20,
      output_tokens_details: { reasoning_tokens: 7 },
      total_tokens: 120,
    });
  });
});

// ── Stop reasons ────────────────────────────────────────────────────────────

describe('stop reasons', () => {
  test.each([
    ['stop', 'end_turn'],
    ['length', 'max_tokens'],
    ['tool_calls', 'tool_use'],
    ['content_filter', 'refusal'],
    ['STOP', 'end_turn'],
    ['MAX_TOKENS', 'max_tokens'],
    ['SAFETY', 'refusal'],
    ['stop_sequence', 'stop_sequence'],
    ['refusal', 'refusal'],
  ])('Messages JSON maps %s to %s', (finish, expected) => {
    const out: any = transformChatCompletionsToMessages(
      chatResponse({ role: 'assistant', content: 'x' }, finish) as any,
      200,
      'google'
    );
    expect(out.stop_reason).toBe(expected);
  });

  test('Messages JSON reports tool_use when the provider says stop', () => {
    const out: any = transformChatCompletionsToMessages(
      chatResponse(
        {
          role: 'assistant',
          content: '',
          tool_calls: [
            {
              id: 'c1',
              type: 'function',
              function: { name: 'f', arguments: '{}' },
            },
          ],
        },
        'STOP'
      ) as any,
      200,
      'google'
    );
    expect(out.stop_reason).toBe('tool_use');
  });

  test('Messages stream maps Gemini MAX_TOKENS and tool calls on STOP', () => {
    const maxTokens = runMessagesStream([
      data({ choices: [{ index: 0, delta: { content: 'a' } }] }),
      data({ choices: [{ index: 0, delta: {}, finish_reason: 'MAX_TOKENS' }] }),
    ]);
    expect(
      maxTokens.find((e) => e.event === 'message_delta')!.data.delta.stop_reason
    ).toBe('max_tokens');

    const toolOnStop = runMessagesStream([
      data({
        choices: [
          {
            index: 0,
            delta: {
              tool_calls: [
                {
                  index: 0,
                  id: 'c1',
                  function: { name: 'f', arguments: '{}' },
                },
              ],
            },
            finish_reason: 'STOP',
          },
        ],
      }),
    ]);
    expect(
      toolOnStop.find((e) => e.event === 'message_delta')!.data.delta
        .stop_reason
    ).toBe('tool_use');
  });

  test('Responses JSON marks content filtering as incomplete', () => {
    const out: any = transformChatCompletionsToResponses(
      chatResponse({ role: 'assistant', content: 'x' }, 'SAFETY') as any,
      200,
      'google'
    );
    expect(out.status).toBe('incomplete');
    expect(out.incomplete_details).toEqual({ reason: 'content_filter' });
  });

  test('Responses stream ends truncated output with response.incomplete', () => {
    const events = runResponsesStream([
      data({ choices: [{ index: 0, delta: { content: 'a' } }] }),
      data({ choices: [{ index: 0, delta: {}, finish_reason: 'length' }] }),
    ]);
    const last = events[events.length - 1];
    expect(last.event).toBe('response.incomplete');
    expect(last.data.response.status).toBe('incomplete');
    expect(last.data.response.incomplete_details).toEqual({
      reason: 'max_output_tokens',
    });
  });
});

// ── Tool calls and tool results ─────────────────────────────────────────────

describe('tool calls and tool results', () => {
  test('Messages request keeps text sent alongside tool results', () => {
    const out = transformMessagesToChatCompletions({
      model: 'm',
      max_tokens: 10,
      messages: [
        {
          role: 'user',
          content: [
            {
              type: 'tool_result',
              tool_use_id: 'c1',
              content: [
                { type: 'text', text: 'line 1' },
                { type: 'text', text: 'line 2' },
              ],
            },
            { type: 'text', text: 'now continue' },
          ],
        },
      ],
    });
    expect(out.messages).toEqual([
      { role: 'tool', tool_call_id: 'c1', content: 'line 1\nline 2' },
      { role: 'user', content: 'now continue' },
    ]);
  });

  test('Messages request flags tool_result errors', () => {
    const out = transformMessagesToChatCompletions({
      model: 'm',
      max_tokens: 10,
      messages: [
        {
          role: 'user',
          content: [
            {
              type: 'tool_result',
              tool_use_id: 'c1',
              content: 'boom',
              is_error: true,
            },
          ],
        },
      ],
    });
    expect(out.messages![0].content).toBe('Error: boom');
  });

  test('Messages request joins assistant text and drops thinking blocks', () => {
    const out = transformMessagesToChatCompletions({
      model: 'm',
      max_tokens: 10,
      messages: [
        {
          role: 'assistant',
          content: [
            { type: 'thinking', thinking: 'hmm', signature: 'sig' },
            { type: 'text', text: 'Let me ' },
            { type: 'text', text: 'check.' },
            { type: 'tool_use', id: 'c1', name: 'f', input: { a: 1 } },
          ],
        },
        {
          role: 'assistant',
          content: [{ type: 'redacted_thinking', data: 'x' }],
        },
      ],
    });
    expect(out.messages).toHaveLength(1);
    expect(out.messages![0]).toMatchObject({
      role: 'assistant',
      content: 'Let me check.',
      tool_calls: [{ id: 'c1', function: { name: 'f', arguments: '{"a":1}' } }],
    });
  });

  test('Messages request maps tool_choice none, parallel and thinking', () => {
    const out: any = transformMessagesToChatCompletions({
      model: 'm',
      max_tokens: 10,
      messages: [{ role: 'user', content: 'hi' }],
      tools: [{ name: 'f', input_schema: { type: 'object' } }],
      tool_choice: { type: 'auto', disable_parallel_tool_use: true },
      thinking: { type: 'enabled', budget_tokens: 1024 },
    });
    expect(out.tool_choice).toBe('auto');
    expect(out.parallel_tool_calls).toBe(false);
    expect(out.thinking).toEqual({ type: 'enabled', budget_tokens: 1024 });

    const none: any = transformMessagesToChatCompletions({
      model: 'm',
      max_tokens: 10,
      messages: [{ role: 'user', content: 'hi' }],
      tool_choice: { type: 'none' },
    });
    expect(none.tool_choice).toBe('none');
  });

  test('Messages request maps url image sources', () => {
    const out = transformMessagesToChatCompletions({
      model: 'm',
      max_tokens: 10,
      messages: [
        {
          role: 'user',
          content: [
            { type: 'text', text: 'what is this' },
            {
              type: 'image',
              source: { type: 'url', url: 'https://x.test/a.png' },
            },
          ],
        },
      ],
    });
    expect((out.messages![0].content as any)[1]).toEqual({
      type: 'image_url',
      image_url: { url: 'https://x.test/a.png' },
    });
  });

  test('Messages JSON orders text before tool_use', () => {
    const out: any = transformChatCompletionsToMessages(
      chatResponse(
        {
          role: 'assistant',
          content: 'Checking.',
          tool_calls: [
            {
              id: 'c1',
              type: 'function',
              function: { name: 'f', arguments: '{"a":1}' },
            },
          ],
        },
        'tool_calls'
      ) as any,
      200,
      'openai'
    );
    expect(out.content.map((b: any) => b.type)).toEqual(['text', 'tool_use']);
  });

  test('Responses JSON orders message before function_call', () => {
    const out: any = transformChatCompletionsToResponses(
      chatResponse(
        {
          role: 'assistant',
          content: 'Checking.',
          tool_calls: [
            {
              id: 'c1',
              type: 'function',
              function: { name: 'f', arguments: '{}' },
            },
          ],
        },
        'tool_calls'
      ) as any,
      200,
      'openai'
    );
    expect(out.output.map((o: any) => o.type)).toEqual([
      'message',
      'function_call',
    ]);
  });

  test('Responses stream gives each function call its own item and done events', () => {
    const events = runResponsesStream([
      data({
        model: 'm',
        choices: [{ index: 0, delta: { role: 'assistant' } }],
      }),
      data({
        choices: [
          {
            index: 0,
            delta: {
              tool_calls: [
                { index: 0, id: 'c1', function: { name: 'a', arguments: '' } },
              ],
            },
          },
        ],
      }),
      data({
        choices: [
          {
            index: 0,
            delta: {
              tool_calls: [{ index: 0, function: { arguments: '{"x":' } }],
            },
          },
        ],
      }),
      data({
        choices: [
          {
            index: 0,
            delta: {
              tool_calls: [{ index: 0, function: { arguments: '1}' } }],
            },
          },
        ],
      }),
      data({
        choices: [
          {
            index: 0,
            delta: {
              tool_calls: [
                {
                  index: 1,
                  id: 'c2',
                  function: { name: 'b', arguments: '{}' },
                },
              ],
            },
            finish_reason: 'tool_calls',
          },
        ],
      }),
    ]);

    const added = events.filter(
      (e) =>
        e.event === 'response.output_item.added' &&
        e.data.item.type === 'function_call'
    );
    expect(added.map((e) => e.data.output_index)).toEqual([1, 2]);
    const firstId = added[0].data.item.id;

    const deltas = events.filter(
      (e) => e.event === 'response.function_call_arguments.delta'
    );
    expect(deltas[0].data.item_id).toBe(firstId);
    expect(deltas[0].data.output_index).toBe(1);

    const argsDone = events.filter(
      (e) => e.event === 'response.function_call_arguments.done'
    );
    expect(argsDone.map((e) => e.data.arguments)).toEqual(['{"x":1}', '{}']);

    const completed = events.find((e) => e.event === 'response.completed')!;
    const output = completed.data.response.output;
    expect(output.map((o: any) => o.type)).toEqual([
      'message',
      'function_call',
      'function_call',
    ]);
    expect(output[1]).toMatchObject({
      id: firstId,
      call_id: 'c1',
      name: 'a',
      arguments: '{"x":1}',
      status: 'completed',
    });
  });
});

// ── Streaming event order ───────────────────────────────────────────────────

describe('Messages streaming event order', () => {
  test('text then tools: one start/stop per block, sequential indexes', () => {
    const events = runMessagesStream([
      data({
        model: 'm',
        choices: [{ index: 0, delta: { role: 'assistant' } }],
      }),
      data({ choices: [{ index: 0, delta: { content: 'Let me ' } }] }),
      data({ choices: [{ index: 0, delta: { content: 'check.' } }] }),
      data({
        choices: [
          {
            index: 0,
            delta: {
              tool_calls: [
                { index: 0, id: 'c1', function: { name: 'a', arguments: '' } },
              ],
            },
          },
        ],
      }),
      data({
        choices: [
          {
            index: 0,
            delta: {
              tool_calls: [{ index: 0, function: { arguments: '{}' } }],
            },
          },
        ],
      }),
      data({
        choices: [
          {
            index: 0,
            delta: {
              tool_calls: [
                {
                  index: 1,
                  id: 'c2',
                  function: { name: 'b', arguments: '{}' },
                },
              ],
            },
          },
        ],
      }),
      data({ choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }] }),
      data({
        choices: [],
        usage: { prompt_tokens: 5, completion_tokens: 3 },
      }),
    ]);

    expect(
      events.map((e) =>
        e.data.index === undefined ? e.event : `${e.event}:${e.data.index}`
      )
    ).toEqual([
      'message_start',
      'content_block_start:0',
      'content_block_delta:0',
      'content_block_delta:0',
      'content_block_stop:0',
      'content_block_start:1',
      'content_block_delta:1',
      'content_block_stop:1',
      'content_block_start:2',
      'content_block_delta:2',
      'content_block_stop:2',
      'message_delta',
      'message_stop',
    ]);
    expect(events[5].data.content_block).toMatchObject({
      type: 'tool_use',
      id: 'c1',
      name: 'a',
    });
    expect(events[11].data.delta.stop_reason).toBe('tool_use');
  });

  test('tool-only response starts at index 0 with no empty text block', () => {
    const events = runMessagesStream([
      data({
        choices: [
          {
            index: 0,
            delta: {
              tool_calls: [
                {
                  index: 0,
                  id: 'c1',
                  function: { name: 'a', arguments: '{}' },
                },
              ],
            },
          },
        ],
      }),
    ]);
    const starts = events.filter((e) => e.event === 'content_block_start');
    expect(starts).toHaveLength(1);
    expect(starts[0].data.index).toBe(0);
    expect(starts[0].data.content_block.type).toBe('tool_use');
  });

  test('Gemini-style repeated tool index with new ids opens new blocks', () => {
    const call = (id: string, name: string) =>
      data({
        choices: [
          {
            index: 0,
            delta: {
              tool_calls: [
                { index: 0, id, function: { name, arguments: '{}' } },
              ],
            },
          },
        ],
      });
    const events = runMessagesStream([call('g1', 'a'), call('g2', 'b')]);
    const starts = events.filter((e) => e.event === 'content_block_start');
    expect(starts.map((e) => e.data.content_block.id)).toEqual(['g1', 'g2']);
    expect(starts.map((e) => e.data.index)).toEqual([0, 1]);
  });

  test('closes the open block on [DONE] when no finish_reason arrived', () => {
    const events = runMessagesStream([
      data({ choices: [{ index: 0, delta: { content: 'hi' } }] }),
    ]);
    expect(events.map((e) => e.event)).toEqual([
      'message_start',
      'content_block_start',
      'content_block_delta',
      'content_block_stop',
      'message_delta',
      'message_stop',
    ]);
  });
});

// ── Reasoning / thinking blocks ─────────────────────────────────────────────

describe('reasoning and thinking blocks', () => {
  test('Messages stream emits thinking block with signature before text', () => {
    const events = runMessagesStream([
      data({
        choices: [
          {
            index: 0,
            delta: {
              content_blocks: [{ index: 0, delta: { thinking: 'Hm' } }],
            },
          },
        ],
      }),
      data({
        choices: [
          {
            index: 0,
            delta: {
              content_blocks: [{ index: 0, delta: { signature: 'sig' } }],
            },
          },
        ],
      }),
      data({
        choices: [
          {
            index: 0,
            delta: {
              content: 'Answer',
              content_blocks: [{ index: 1, delta: { text: 'Answer' } }],
            },
          },
        ],
      }),
    ]);
    const blockEvents = events.filter((e) =>
      e.event.startsWith('content_block')
    );
    expect(blockEvents.map((e) => [e.event, e.data.index])).toEqual([
      ['content_block_start', 0],
      ['content_block_delta', 0],
      ['content_block_delta', 0],
      ['content_block_stop', 0],
      ['content_block_start', 1],
      ['content_block_delta', 1],
      ['content_block_stop', 1],
    ]);
    expect(blockEvents[0].data.content_block.type).toBe('thinking');
    expect(blockEvents[1].data.delta).toEqual({
      type: 'thinking_delta',
      thinking: 'Hm',
    });
    expect(blockEvents[2].data.delta).toEqual({
      type: 'signature_delta',
      signature: 'sig',
    });
    // text from content_blocks is not duplicated
    expect(blockEvents[5].data.delta).toEqual({
      type: 'text_delta',
      text: 'Answer',
    });
  });

  test('Messages stream maps reasoning_content to thinking, once', () => {
    const events = runMessagesStream([
      data({
        choices: [{ index: 0, delta: { reasoning_content: 'step 1' } }],
      }),
      // OpenRouter-style: both `reasoning` and content_blocks thinking
      data({
        choices: [
          {
            index: 0,
            delta: {
              reasoning: 'step 2',
              content_blocks: [{ index: 0, delta: { thinking: 'step 2' } }],
            },
          },
        ],
      }),
    ]);
    const thinking = events
      .filter((e) => e.data.delta?.type === 'thinking_delta')
      .map((e) => e.data.delta.thinking);
    expect(thinking).toEqual(['step 1', 'step 2']);
  });

  test('Messages JSON keeps signature and maps reasoning_content', () => {
    const withBlocks: any = transformChatCompletionsToMessages(
      chatResponse(
        {
          role: 'assistant',
          content: 'A',
          content_blocks: [
            { type: 'thinking', thinking: 'T', signature: 'S' },
            { type: 'text', text: 'A' },
          ],
        },
        'stop'
      ) as any,
      200,
      'google'
    );
    expect(withBlocks.content).toEqual([
      { type: 'thinking', thinking: 'T', signature: 'S' },
      { type: 'text', text: 'A' },
    ]);

    const withReasoning: any = transformChatCompletionsToMessages(
      chatResponse(
        { role: 'assistant', content: 'A', reasoning_content: 'R' },
        'stop'
      ) as any,
      200,
      'deepseek'
    );
    expect(withReasoning.content[0]).toEqual({
      type: 'thinking',
      thinking: 'R',
      signature: '',
    });
  });

  test('Responses JSON maps reasoning_content to a reasoning item', () => {
    const out: any = transformChatCompletionsToResponses(
      chatResponse(
        { role: 'assistant', content: 'A', reasoning_content: 'R' },
        'stop'
      ) as any,
      200,
      'deepseek'
    );
    expect(out.output[0]).toMatchObject({
      type: 'reasoning',
      summary: [{ type: 'summary_text', text: 'R' }],
    });
  });
});

// ── Error shapes ────────────────────────────────────────────────────────────

describe('error shapes', () => {
  test('Messages JSON errors use the Anthropic error envelope', () => {
    const out: any = transformChatCompletionsToMessages(
      {
        error: {
          message: 'Rate limit reached',
          type: 'requests',
          param: null,
          code: '429',
        },
        provider: 'openai',
      } as any,
      429,
      'openai'
    );
    expect(out).toEqual({
      type: 'error',
      error: { type: 'rate_limit_error', message: 'Rate limit reached' },
      provider: 'openai',
    });
  });

  test('Messages JSON keeps a known Anthropic error type', () => {
    const out: any = transformChatCompletionsToMessages(
      { error: { message: 'bad', type: 'invalid_request_error' } } as any,
      422,
      'groq'
    );
    expect(out.error).toEqual({
      type: 'invalid_request_error',
      message: 'bad',
    });
    expect(out.type).toBe('error');
  });

  test('Messages JSON non-200 without an error body still has the envelope', () => {
    const out: any = transformChatCompletionsToMessages(
      { id: 'x', choices: [] } as any,
      503,
      'groq'
    );
    expect(out).toMatchObject({
      type: 'error',
      error: { type: 'api_error' },
    });
  });

  test('Messages stream turns a provider error chunk into an error event', () => {
    const state = messagesState();
    messagesChunk(
      data({ choices: [{ index: 0, delta: { content: 'a' } }] }),
      state
    );
    const out = messagesChunk(
      data({ error: { message: 'overloaded', type: 'overloaded_error' } }),
      state
    )!;
    expect(parseEvents(out)).toEqual([
      {
        event: 'error',
        data: {
          type: 'error',
          error: { type: 'overloaded_error', message: 'overloaded' },
        },
      },
    ]);
    // no message_stop after an error
    expect(messagesChunk('data: [DONE]', state)).toBeUndefined();
  });
});
