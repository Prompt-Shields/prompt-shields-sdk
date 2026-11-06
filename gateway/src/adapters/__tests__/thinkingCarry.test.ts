/**
 * PS fork (#73): signed thinking round-trips through the Responses adapter
 * in reasoning items' encrypted_content (thinkingCarry.ts).
 */
import { transformResponsesToChatCompletions } from '../responses/requestTransform';
import { transformChatCompletionsToResponses } from '../responses/responseTransform';
import {
  createStreamState,
  transformStreamChunk,
} from '../responses/streamTransform';
import {
  decodeThinkingCarry,
  encodeThinkingCarry,
  stripIssuedThinking,
} from '../responses/thinkingCarry';
import {
  AnthropicChatCompleteConfig,
  getAnthropicStreamChunkTransform,
} from '../../providers/anthropic/chatComplete';
import { transformUsingProviderConfig } from '../testUtils';

const MODEL = 'test-model';

/** Parse `event: x\ndata: {...}` SSE text into event payloads. */
function sseEvents(text: string): any[] {
  return text
    .split('\n\n')
    .map((e) => e.split('\n').find((l) => l.startsWith('data: ')))
    .filter(Boolean)
    .map((l) => JSON.parse(l!.slice(6)));
}

/** Run Chat Completions chunks through the Responses stream adapter. */
function streamResponses(chunks: object[], provider: string): any[] {
  const state = createStreamState({ model: MODEL }, provider);
  let out = '';
  for (const c of chunks) {
    out += transformStreamChunk(`data: ${JSON.stringify(c)}`, state) ?? '';
  }
  out += transformStreamChunk('data: [DONE]', state) ?? '';
  return sseEvents(out);
}

const signedChunks = [
  { model: MODEL, choices: [{ delta: { role: 'assistant', content: '' } }] },
  {
    choices: [
      {
        delta: {
          content_blocks: [
            { index: 0, delta: { thinking: '', signature: '' } },
          ],
        },
      },
    ],
  },
  {
    choices: [
      {
        delta: {
          content_blocks: [{ index: 0, delta: { thinking: 'Need the ' } }],
        },
      },
    ],
  },
  {
    choices: [
      {
        delta: {
          content_blocks: [{ index: 0, delta: { thinking: 'weather.' } }],
        },
      },
    ],
  },
  {
    choices: [
      {
        delta: {
          content_blocks: [{ index: 0, delta: { signature: 'sig-abc' } }],
        },
      },
    ],
  },
  {
    choices: [
      {
        delta: {
          tool_calls: [
            {
              index: 0,
              id: 'toolu_1',
              type: 'function',
              function: { name: 'get_weather', arguments: '' },
            },
          ],
        },
      },
    ],
  },
  {
    choices: [
      {
        delta: {
          tool_calls: [{ index: 0, function: { arguments: '{"city":"P"}' } }],
        },
      },
    ],
  },
  { choices: [{ delta: {}, finish_reason: 'tool_calls' }] },
];

describe('thinking carry encoding', () => {
  test('round-trips signed and redacted thinking for the issuing provider', () => {
    const blocks = [
      { type: 'thinking', thinking: 'ünïcode ✓', signature: 'sig' },
      { type: 'redacted_thinking', data: 'opaque' },
    ];
    const enc = encodeThinkingCarry('anthropic', blocks)!;
    expect(enc.startsWith('ps-thk1:anthropic:')).toBe(true);
    expect(decodeThinkingCarry(enc, 'anthropic')).toEqual(blocks);
  });

  test('is not restored for another provider, a native payload or garbage', () => {
    const enc = encodeThinkingCarry('bedrock', [
      { type: 'thinking', thinking: 'x', signature: 's' },
    ]);
    expect(decodeThinkingCarry(enc, 'anthropic')).toBeUndefined();
    expect(decodeThinkingCarry(enc, 'openai')).toBeUndefined();
    expect(decodeThinkingCarry('gAAAAB-native', 'anthropic')).toBeUndefined();
    expect(
      decodeThinkingCarry('ps-thk1:anthropic:!!notbase64', 'anthropic')
    ).toBeUndefined();
    expect(decodeThinkingCarry(undefined, 'anthropic')).toBeUndefined();
  });

  test('issues nothing for providers that do not sign thinking, or unsigned blocks', () => {
    const signed = [{ type: 'thinking', thinking: 'x', signature: 's' }];
    expect(encodeThinkingCarry('google', signed)).toBeUndefined();
    expect(encodeThinkingCarry('openai', signed)).toBeUndefined();
    expect(
      encodeThinkingCarry('anthropic', [{ type: 'thinking', thinking: 'x' }])
    ).toBeUndefined();
  });

  test('encodes only the thinking fields, never other block properties', () => {
    const enc = encodeThinkingCarry('anthropic', [
      {
        type: 'thinking',
        thinking: 'x',
        signature: 's',
        authorization: 'Bearer sk-secret',
        headers: { 'x-api-key': 'sk-secret' },
      },
    ])!;
    const json = Buffer.from(enc.split(':')[2], 'base64').toString();
    expect(json).not.toContain('sk-secret');
    expect(JSON.parse(json)).toEqual([
      { type: 'thinking', thinking: 'x', signature: 's' },
    ]);
  });
});

describe('Responses adapter output carries signed thinking', () => {
  test('JSON response: thinking and redacted thinking get encrypted_content', () => {
    const res: any = transformChatCompletionsToResponses(
      {
        id: 'chatcmpl-1',
        object: 'chat.completion',
        created: 1,
        model: MODEL,
        choices: [
          {
            index: 0,
            finish_reason: 'stop',
            message: {
              role: 'assistant',
              content: 'Done',
              content_blocks: [
                { type: 'thinking', thinking: 'Hm', signature: 'sig-1' },
                { type: 'redacted_thinking', data: 'red-1' },
                { type: 'text', text: 'Done' },
              ],
            } as any,
          },
        ],
      } as any,
      200,
      'anthropic',
      { model: MODEL }
    );
    const reasoning = res.output.filter((o: any) => o.type === 'reasoning');
    expect(reasoning).toHaveLength(2);
    expect(reasoning[0].summary).toEqual([
      { type: 'summary_text', text: 'Hm' },
    ]);
    expect(
      decodeThinkingCarry(reasoning[0].encrypted_content, 'anthropic')
    ).toEqual([{ type: 'thinking', thinking: 'Hm', signature: 'sig-1' }]);
    expect(reasoning[1].summary).toEqual([]);
    expect(
      decodeThinkingCarry(reasoning[1].encrypted_content, 'anthropic')
    ).toEqual([{ type: 'redacted_thinking', data: 'red-1' }]);
  });

  test('JSON response: no encrypted_content for a provider that does not sign', () => {
    const res: any = transformChatCompletionsToResponses(
      {
        id: 'x',
        object: 'chat.completion',
        created: 1,
        model: MODEL,
        choices: [
          {
            index: 0,
            finish_reason: 'stop',
            message: {
              role: 'assistant',
              content: 'ok',
              content_blocks: [{ type: 'thinking', thinking: 'Hm' }],
            } as any,
          },
        ],
      } as any,
      200,
      'google',
      { model: MODEL }
    );
    expect(res.output[0].type).toBe('reasoning');
    expect(res.output[0]).not.toHaveProperty('encrypted_content');
  });

  test('stream: the reasoning item is done with the signed thinking', () => {
    const events = streamResponses(signedChunks, 'anthropic');
    const done = events.filter(
      (e) =>
        e.type === 'response.output_item.done' && e.item.type === 'reasoning'
    );
    expect(done).toHaveLength(1);
    expect(done[0].item.summary).toEqual([
      { type: 'summary_text', text: 'Need the weather.' },
    ]);
    const expected = [
      { type: 'thinking', thinking: 'Need the weather.', signature: 'sig-abc' },
    ];
    expect(
      decodeThinkingCarry(done[0].item.encrypted_content, 'anthropic')
    ).toEqual(expected);

    // The terminal snapshot has it too, and output order is unchanged
    const completed = events.find((e) => e.type === 'response.completed');
    expect(completed.response.output.map((o: any) => o.type)).toEqual([
      'reasoning',
      'function_call',
    ]);
    expect(
      decodeThinkingCarry(
        completed.response.output[0].encrypted_content,
        'anthropic'
      )
    ).toEqual(expected);
    // Nothing is issued while the item is in progress
    const added = events.find(
      (e) =>
        e.type === 'response.output_item.added' && e.item.type === 'reasoning'
    );
    expect(added.item).not.toHaveProperty('encrypted_content');
  });

  test('stream: redacted thinking opens a reasoning item without summary text', () => {
    const events = streamResponses(
      [
        { model: MODEL, choices: [{ delta: { role: 'assistant' } }] },
        {
          choices: [
            {
              delta: { content_blocks: [{ index: 0, delta: { data: 'r1' } }] },
            },
          ],
        },
        {
          choices: [
            {
              delta: {
                content: 'Hi',
                content_blocks: [{ index: 1, delta: { text: 'Hi' } }],
              },
            },
          ],
        },
        { choices: [{ delta: {}, finish_reason: 'stop' }] },
      ],
      'bedrock'
    );
    const completed = events.find((e) => e.type === 'response.completed');
    const [rs, msg] = completed.response.output;
    expect(rs.type).toBe('reasoning');
    expect(rs.summary).toEqual([]);
    expect(decodeThinkingCarry(rs.encrypted_content, 'bedrock')).toEqual([
      { type: 'redacted_thinking', data: 'r1' },
    ]);
    expect(msg.content[0].text).toBe('Hi');
  });

  test('stream: unchanged for providers that do not sign thinking', () => {
    const events = streamResponses(signedChunks, 'google');
    const completed = events.find((e) => e.type === 'response.completed');
    expect(completed.response.output[0].type).toBe('reasoning');
    expect(completed.response.output[0]).not.toHaveProperty(
      'encrypted_content'
    );
  });
});

describe('Responses adapter input restores signed thinking', () => {
  const carried = (provider: string, thinking: string, signature: string) => ({
    type: 'reasoning',
    id: 'rs_1',
    summary: [{ type: 'summary_text', text: thinking }],
    encrypted_content: encodeThinkingCarry(provider, [
      { type: 'thinking', thinking, signature },
    ]),
  });
  const toolTurn = (reasoning: object) => ({
    model: MODEL,
    input: [
      { role: 'user', content: 'Weather in P?' },
      reasoning,
      {
        type: 'function_call',
        call_id: 'toolu_1',
        name: 'get_weather',
        arguments: '{"city":"P"}',
      },
      { type: 'function_call_output', call_id: 'toolu_1', output: 'sunny' },
    ],
  });

  test('a carried reasoning item leads the tool-use turn sent to Anthropic', () => {
    const chat = transformResponsesToChatCompletions(
      toolTurn(carried('anthropic', 'Need the weather.', 'sig-abc')),
      'anthropic'
    );
    const assistant = (chat.messages as any[])[1];
    expect(assistant.content_blocks).toEqual([
      { type: 'thinking', thinking: 'Need the weather.', signature: 'sig-abc' },
    ]);

    const body = transformUsingProviderConfig(AnthropicChatCompleteConfig, {
      ...chat,
      max_tokens: 100,
    });
    expect(body.messages[1]).toEqual({
      role: 'assistant',
      content: [
        {
          type: 'thinking',
          thinking: 'Need the weather.',
          signature: 'sig-abc',
        },
        {
          type: 'tool_use',
          id: 'toolu_1',
          name: 'get_weather',
          input: { city: 'P' },
        },
      ],
    });
  });

  test('reasoning issued for another provider, or native reasoning, is dropped', () => {
    for (const reasoning of [
      carried('bedrock', 'x', 's'),
      { type: 'reasoning', id: 'rs_n', summary: [], encrypted_content: 'gA' },
    ]) {
      const chat = transformResponsesToChatCompletions(
        toolTurn(reasoning),
        'anthropic'
      );
      const assistant = (chat.messages as any[])[1];
      expect(assistant.content_blocks).toBeUndefined();
      expect(assistant.tool_calls).toHaveLength(1);
    }
    // Without a provider (older callers) nothing is restored either
    const chat = transformResponsesToChatCompletions(
      toolTurn(carried('anthropic', 'x', 's'))
    );
    expect((chat.messages as any[])[1].content_blocks).toBeUndefined();
  });

  test('keeps content order across text and parallel calls in one turn', () => {
    const chat = transformResponsesToChatCompletions(
      {
        model: MODEL,
        input: [
          { role: 'user', content: 'Go' },
          carried('anthropic', 'A', 'sa'),
          {
            type: 'message',
            role: 'assistant',
            content: [{ type: 'output_text', text: 'Checking.' }],
          },
          carried('anthropic', 'B', 'sb'),
          { type: 'function_call', call_id: 'c1', name: 'f', arguments: '{}' },
          { type: 'function_call', call_id: 'c2', name: 'f', arguments: '{}' },
          { type: 'function_call_output', call_id: 'c1', output: '1' },
          { type: 'function_call_output', call_id: 'c2', output: '2' },
        ],
      },
      'anthropic'
    );
    const msgs = chat.messages as any[];
    expect(msgs.map((m) => m.role)).toEqual([
      'user',
      'assistant',
      'tool',
      'tool',
    ]);
    expect(msgs[1].content_blocks).toEqual([
      { type: 'thinking', thinking: 'A', signature: 'sa' },
      { type: 'text', text: 'Checking.' },
      { type: 'thinking', thinking: 'B', signature: 'sb' },
    ]);
    expect(msgs[1].tool_calls.map((t: any) => t.id)).toEqual(['c1', 'c2']);
  });

  test('reasoning not followed by an assistant item is dropped', () => {
    const chat = transformResponsesToChatCompletions(
      {
        model: MODEL,
        input: [
          carried('anthropic', 'A', 'sa'),
          { role: 'user', content: 'Hi' },
          { role: 'assistant', content: 'Hello' },
        ],
      },
      'anthropic'
    );
    expect((chat.messages as any[])[1]).toEqual({
      role: 'assistant',
      content: 'Hello',
    });
  });
});

describe('end to end: Anthropic stream → Responses → next Anthropic request', () => {
  test('the second step of a tool loop sends the signed thinking back', () => {
    const anthropicSse = [
      `event: message_start\ndata: ${JSON.stringify({
        type: 'message_start',
        message: { model: MODEL, usage: { input_tokens: 10 } },
      })}`,
      `event: content_block_start\ndata: ${JSON.stringify({
        type: 'content_block_start',
        index: 0,
        content_block: { type: 'thinking', thinking: '', signature: '' },
      })}`,
      `event: content_block_delta\ndata: ${JSON.stringify({
        type: 'content_block_delta',
        index: 0,
        delta: { type: 'thinking_delta', thinking: 'Call the tool.' },
      })}`,
      `event: content_block_delta\ndata: ${JSON.stringify({
        type: 'content_block_delta',
        index: 0,
        delta: { type: 'signature_delta', signature: 'EqAnthropicSig==' },
      })}`,
      `event: content_block_start\ndata: ${JSON.stringify({
        type: 'content_block_start',
        index: 1,
        content_block: { type: 'tool_use', id: 'toolu_9', name: 'lookup' },
      })}`,
      `event: content_block_delta\ndata: ${JSON.stringify({
        type: 'content_block_delta',
        index: 1,
        delta: { type: 'input_json_delta', partial_json: '{"q":"x"}' },
      })}`,
      `event: message_delta\ndata: ${JSON.stringify({
        type: 'message_delta',
        delta: { stop_reason: 'tool_use' },
        usage: { output_tokens: 5 },
      })}`,
      'event: message_stop\ndata: {"type":"message_stop"}',
    ];
    const toChat = getAnthropicStreamChunkTransform('anthropic');
    const anthropicState: any = {};
    const state = createStreamState({ model: MODEL }, 'anthropic');
    let out = '';
    for (const raw of anthropicSse) {
      const chat = toChat(raw, 'id-1', anthropicState, false);
      for (const part of (chat ?? '').split('\n\n')) {
        if (part.trim()) out += transformStreamChunk(part, state) ?? '';
      }
    }
    const completed = sseEvents(out).find(
      (e) => e.type === 'response.completed'
    );
    expect(completed).toBeDefined();

    // The client sends the output back with the tool result
    const next = transformResponsesToChatCompletions(
      {
        model: MODEL,
        input: [
          { role: 'user', content: 'Look up x' },
          ...completed.response.output,
          { type: 'function_call_output', call_id: 'toolu_9', output: 'y' },
        ],
      },
      'anthropic'
    );
    const body = transformUsingProviderConfig(AnthropicChatCompleteConfig, {
      ...next,
      max_tokens: 100,
    });
    expect(body.messages[1].content).toEqual([
      {
        type: 'thinking',
        thinking: 'Call the tool.',
        signature: 'EqAnthropicSig==',
      },
      { type: 'tool_use', id: 'toolu_9', name: 'lookup', input: { q: 'x' } },
    ]);
  });
});

describe('stripIssuedThinking (native Responses targets)', () => {
  test('removes only reasoning items the adapter issued', () => {
    const issued = {
      type: 'reasoning',
      id: 'rs_1',
      summary: [],
      encrypted_content: encodeThinkingCarry('anthropic', [
        { type: 'thinking', thinking: 'x', signature: 's' },
      ]),
    };
    const native = {
      type: 'reasoning',
      id: 'rs_2',
      summary: [],
      encrypted_content: 'gAAAA',
    };
    const body = {
      model: MODEL,
      input: [{ role: 'user', content: 'hi' }, issued, native],
    };
    const out = stripIssuedThinking(body);
    expect(out.input).toEqual([{ role: 'user', content: 'hi' }, native]);
    expect(body.input).toHaveLength(3); // the original is not mutated
  });

  test('returns the same object when there is nothing to strip', () => {
    const body = { model: MODEL, input: 'hi' };
    expect(stripIssuedThinking(body)).toBe(body);
    const body2 = { model: MODEL, input: [{ role: 'user', content: 'hi' }] };
    expect(stripIssuedThinking(body2)).toBe(body2);
  });
});
