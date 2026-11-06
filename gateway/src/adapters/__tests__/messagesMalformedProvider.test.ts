/**
 * PS fork (#73): the Messages adapter tolerates malformed provider output.
 * Null, numeric, string and badly typed entries in `choices`,
 * `content_blocks` and `tool_calls` are skipped (never dereferenced) in the
 * JSON and streamed paths, the result is still a valid Messages object or
 * event sequence, and provider keys such as `__proto__` / `constructor` never
 * reach Object.prototype. No live calls.
 */
// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../../utils/env', () => ({ Environment: () => ({}) }));

import { transformChatCompletionsToMessages } from '../messages/responseTransform';
import {
  transformStreamChunk,
  createStreamState,
} from '../messages/streamTransform';
import { adaptResponse } from '../../handlers/adapterUtils';

const MODEL = 'test-model';

const BLOCK_TYPES = new Set([
  'text',
  'thinking',
  'redacted_thinking',
  'tool_use',
]);

function expectNoPollution() {
  const probe: any = {};
  expect(probe.polluted).toBeUndefined();
  expect(probe.args).toBeUndefined();
  expect(probe.id).toBeUndefined();
  expect(Object.prototype.hasOwnProperty.call(Object.prototype, 'args')).toBe(
    false
  );
}

/** Checks a Messages response object. */
function expectValidMessage(res: any) {
  expect(res.type).toBe('message');
  expect(res.role).toBe('assistant');
  expect(typeof res.id).toBe('string');
  expect(Array.isArray(res.content)).toBe(true);
  for (const block of res.content) {
    expect(BLOCK_TYPES.has(block.type)).toBe(true);
    if (block.type === 'text') expect(typeof block.text).toBe('string');
    if (block.type === 'thinking') {
      expect(typeof block.thinking).toBe('string');
      expect(typeof block.signature).toBe('string');
    }
    if (block.type === 'redacted_thinking') {
      expect(typeof block.data).toBe('string');
    }
    if (block.type === 'tool_use') {
      expect(typeof block.id).toBe('string');
      expect(typeof block.name).toBe('string');
      expect(block.input !== null && typeof block.input === 'object').toBe(
        true
      );
    }
  }
  expect(typeof res.stop_reason).toBe('string');
  expect(() => JSON.stringify(res)).not.toThrow();
}

const json = (body: any) =>
  transformChatCompletionsToMessages(body, 200, 'openai') as any;

/** A provider body parsed from JSON text, so `__proto__` is an own key. */
const parsed = (text: string) => JSON.parse(text);

/** Runs chunks through one stream state; returns the parsed events. */
function stream(chunks: string[]) {
  const state = createStreamState(MODEL);
  let out = '';
  for (const c of chunks) out += transformStreamChunk(c, state) ?? '';
  return out
    .split('\n\n')
    .filter(Boolean)
    .map((e) => JSON.parse(e.split('\n')[1].slice(6)));
}

const data = (o: unknown) => `data: ${JSON.stringify(o)}`;

afterEach(() => {
  expectNoPollution();
});

// ── JSON ────────────────────────────────────────────────────────────────────

describe('Messages adapter, malformed JSON responses', () => {
  test.each([
    ['null', null],
    ['a string', 'abc'],
    ['a number', 3],
    ['an array', [{ choices: [] }]],
    ['a boolean', true],
  ])('a body that is %s is a failed request', (_label, body) => {
    const res = json(body);
    expect(res.type).toBe('error');
    expect(res.error).toEqual({ type: 'api_error', message: 'Request failed' });
    expect(res.provider).toBe('openai');
  });

  test('null, numeric and string choices and messages are skipped', () => {
    const res = json({
      id: 'chat-1',
      model: MODEL,
      choices: [
        null,
        7,
        'choice',
        [],
        { index: 0, message: null, finish_reason: 5 },
        { index: 1, message: 'text' },
        {
          index: 2,
          finish_reason: 'stop',
          message: { role: 'assistant', content: 'Hello.' },
        },
      ],
    });
    expectValidMessage(res);
    expect(res.content).toEqual([{ type: 'text', text: 'Hello.' }]);
    expect(res.stop_reason).toBe('end_turn');
  });

  test.each([
    ['a string', 'abc'],
    ['a number', 3],
    ['an object', { 0: { type: 'text', text: 'x' } }],
  ])('choices that is %s is ignored', (_label, choices) => {
    const res = json({ id: 'chat-1', model: MODEL, choices });
    expectValidMessage(res);
    expect(res.content).toEqual([]);
  });

  test('null and non-object content_blocks entries are skipped', () => {
    const res = json({
      id: 'chat-1',
      model: MODEL,
      choices: [
        {
          finish_reason: 'stop',
          message: {
            content: 'Hi.',
            content_blocks: [
              null,
              7,
              'thinking',
              [],
              { type: 'thinking', thinking: 'Let me think.', signature: 'sig' },
              { type: 'thinking', thinking: 42, signature: { s: 1 } },
              { thinking: { nested: true } },
              { type: 'redacted_thinking', data: 5 },
              { type: 'redacted_thinking', data: 'opaque' },
            ],
          },
        },
      ],
    });
    expectValidMessage(res);
    expect(res.content).toEqual([
      { type: 'thinking', thinking: 'Let me think.', signature: 'sig' },
      // a non-string thinking or signature field is not copied
      { type: 'thinking', thinking: '', signature: '' },
      { type: 'redacted_thinking', data: 'opaque' },
      { type: 'text', text: 'Hi.' },
    ]);
  });

  test.each([
    ['a string', 'abc'],
    ['a number', 3],
    ['an object', { 0: { type: 'thinking', thinking: 'x' } }],
  ])('content_blocks that is %s is ignored', (_label, blocks) => {
    const res = json({
      choices: [{ message: { content: 'Hi.', content_blocks: blocks } }],
    });
    expectValidMessage(res);
    expect(res.content).toEqual([{ type: 'text', text: 'Hi.' }]);
  });

  test('malformed tool_calls entries are skipped or typed', () => {
    const res = json({
      choices: [
        {
          finish_reason: 'tool_calls',
          message: {
            content: null,
            tool_calls: [
              null,
              7,
              'call',
              [],
              { id: 'no_fn', type: 'function' },
              { id: 'null_fn', type: 'function', function: null },
              { id: 'str_fn', function: 'f' },
              { id: 'custom', type: 'custom', function: { name: 'c' } },
              {
                id: 'ok',
                type: 'function',
                function: { name: 'get', arguments: '{"q":1}' },
              },
              { id: 9, function: { name: { n: 1 }, arguments: { a: 1 } } },
              { id: 'arr', function: { name: 'arr', arguments: '[1,2]' } },
              { id: 'num', function: { name: 'num', arguments: 5 } },
              { id: 'bad', function: { name: 'bad', arguments: '{oops' } },
            ],
          },
        },
      ],
    });
    expectValidMessage(res);
    const tools = res.content.filter((b: any) => b.type === 'tool_use');
    expect(tools.map((t: any) => [t.name, t.input])).toEqual([
      ['get', { q: 1 }],
      // an already parsed arguments object is used as is
      ['', { a: 1 }],
      // JSON that is an array (an object) is kept, as before
      ['arr', [1, 2]],
      ['num', {}],
      ['bad', {}],
    ]);
    expect(tools[0].id).toBe('ok');
    // a non-string id gets a generated one
    expect(tools[1].id).toMatch(/^toolu_/);
    expect(res.stop_reason).toBe('tool_use');
  });

  test.each([
    ['a string', 'abc'],
    ['an object', { 0: { function: { name: 'f' } } }],
    ['null', null],
  ])('tool_calls that is %s is ignored', (_label, toolCalls) => {
    const res = json({
      choices: [{ message: { content: 'Hi.', tool_calls: toolCalls } }],
    });
    expectValidMessage(res);
    expect(res.content).toEqual([{ type: 'text', text: 'Hi.' }]);
  });

  test('content parts and content of other types never throw', () => {
    const text = (content: unknown) =>
      json({ choices: [{ message: { content } }] }).content;
    expect(
      text([
        null,
        7,
        'x',
        { type: 'text', text: 'a' },
        { text: 5 },
        { text: 'b' },
      ])
    ).toEqual([{ type: 'text', text: 'ab' }]);
    expect(text({ text: 'object' })).toEqual([]);
    expect(text(12)).toEqual([{ type: 'text', text: '12' }]);
    expect(text(undefined)).toEqual([]);
  });

  test('badly typed id, model, finish_reason and usage fall back', () => {
    const res = json({
      id: { x: 1 },
      model: 42,
      usage: 'lots',
      choices: [{ finish_reason: { r: 1 }, message: { content: 'Hi.' } }],
    });
    expectValidMessage(res);
    expect(res.id).toMatch(/^msg_/);
    expect(res.model).toBeUndefined();
    expect(res.stop_reason).toBe('end_turn');
    expect(res.usage).toEqual({ input_tokens: 0, output_tokens: 0 });
  });

  test('__proto__ and constructor keys from the provider do not pollute', () => {
    const res = json(
      parsed(
        '{"__proto__":{"polluted":1},"id":"chat-1","choices":[{"__proto__":{"polluted":1},"message":{"__proto__":{"args":1},"content":"Hi.","content_blocks":[{"__proto__":{"polluted":1},"type":"thinking","thinking":"t"}],"tool_calls":[{"__proto__":{"id":1},"id":"c1","function":{"__proto__":{"polluted":1},"name":"f","arguments":"{\\"__proto__\\":{\\"polluted\\":1},\\"constructor\\":{\\"prototype\\":{\\"polluted\\":1}}}"}}]}}]}'
      )
    );
    expectValidMessage(res);
    const tool = res.content.find((b: any) => b.type === 'tool_use');
    expect(Object.keys(tool.input)).toEqual(['__proto__', 'constructor']);
  });

  test('a well-formed response is unchanged', () => {
    const res = json({
      id: 'chat-1',
      model: MODEL,
      choices: [
        {
          index: 0,
          finish_reason: 'tool_calls',
          message: {
            role: 'assistant',
            content: 'Checking.',
            content_blocks: [
              { type: 'thinking', thinking: 'Think.', signature: 'sig' },
              { type: 'redacted_thinking', data: 'opaque' },
            ],
            tool_calls: [
              {
                id: 'call_1',
                type: 'function',
                function: { name: 'get', arguments: '{"city":"Paris"}' },
              },
            ],
          },
        },
      ],
      usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
    });
    expect(res).toEqual({
      id: 'chat-1',
      type: 'message',
      role: 'assistant',
      content: [
        { type: 'thinking', thinking: 'Think.', signature: 'sig' },
        { type: 'redacted_thinking', data: 'opaque' },
        { type: 'text', text: 'Checking.' },
        {
          type: 'tool_use',
          id: 'call_1',
          name: 'get',
          input: { city: 'Paris' },
        },
      ],
      model: MODEL,
      stop_reason: 'tool_use',
      stop_sequence: null,
      usage: { input_tokens: 10, output_tokens: 5 },
    });
  });

  test('a provider error body keeps the Anthropic error envelope', () => {
    const res = transformChatCompletionsToMessages(
      { error: { message: 'boom', type: 'x' } } as any,
      400,
      'openai'
    ) as any;
    expect(res).toEqual({
      type: 'error',
      error: { type: 'invalid_request_error', message: 'boom' },
      provider: 'openai',
    });
  });
});

describe('Adapter handler, a success whose body is not an object', () => {
  test.each([
    ['messages', 'null'],
    ['messages', '[1]'],
    ['createModelResponse', '"text"'],
    ['createModelResponse', '7'],
  ])('%s with body %s is a 502 failed request', async (fn, body) => {
    const response = new Response(body, {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
    const adapted = await adaptResponse(
      response,
      {
        isActive: true,
        originalFn: fn as any,
        originalRequest: { model: MODEL },
        provider: 'openai',
      },
      { get: () => undefined } as any
    );
    expect(adapted.status).toBe(502);
    const out: any = await adapted.json();
    if (fn === 'messages') {
      expect(out.type).toBe('error');
      expect(out.error.type).toBe('api_error');
    } else {
      expect(out.error.type).toBe('api_error');
    }
  });
});

// ── Stream ──────────────────────────────────────────────────────────────────

describe('Messages adapter, malformed streamed chunks', () => {
  test('non-object chunks, choices and deltas are skipped', () => {
    const events = stream([
      'data: null',
      'data: 3',
      'data: "text"',
      'data: [1,2]',
      'data: true',
      data({ model: 'm', choices: [null] }),
      data({ choices: 'abc' }),
      data({ choices: [7, { delta: null }] }),
      data({ choices: [{ delta: 'text' }] }),
      data({ choices: [{ delta: { content: 'Hi' } }] }),
      data({ choices: [{ delta: {}, finish_reason: 'stop' }] }),
      'data: [DONE]',
    ]);
    expect(events.map((e) => e.type)).toEqual([
      'message_start',
      'content_block_start',
      'content_block_delta',
      'content_block_stop',
      'message_delta',
      'message_stop',
    ]);
    expect(events[0].message.model).toBe('m');
    expect(events[2].delta).toEqual({ type: 'text_delta', text: 'Hi' });
    expect(events[4].delta.stop_reason).toBe('end_turn');
  });

  test('a badly typed model falls back to the requested one', () => {
    const events = stream([
      data({ model: { m: 1 }, choices: [{ delta: { content: 'Hi' } }] }),
      'data: [DONE]',
    ]);
    expect(events[0].message.model).toBe(MODEL);
  });

  test('null and non-object content_blocks entries are skipped', () => {
    const events = stream([
      data({
        model: 'm',
        choices: [
          {
            delta: {
              content_blocks: [
                null,
                7,
                'x',
                [],
                { delta: null },
                { delta: 'thinking' },
                { delta: { thinking: 5, signature: { s: 1 } } },
                { delta: { thinking: 'Think.' } },
                { delta: { signature: 'sig' } },
              ],
            },
          },
        ],
      }),
      data({ choices: [{ delta: { content_blocks: 3 } }] }),
      data({ choices: [{ delta: { content_blocks: { 0: { delta: {} } } } }] }),
      data({ choices: [{ delta: { content: 'Hi' }, finish_reason: 'stop' }] }),
      'data: [DONE]',
    ]);
    const deltas = events
      .filter((e) => e.type === 'content_block_delta')
      .map((e) => e.delta);
    expect(deltas).toEqual([
      { type: 'thinking_delta', thinking: 'Think.' },
      { type: 'signature_delta', signature: 'sig' },
      { type: 'text_delta', text: 'Hi' },
    ]);
  });

  test('malformed tool_calls entries are skipped or typed', () => {
    const events = stream([
      data({
        model: 'm',
        choices: [
          {
            delta: {
              tool_calls: [
                null,
                7,
                'call',
                [],
                {
                  index: 0,
                  id: { i: 1 },
                  function: { name: { n: 1 }, arguments: { a: 1 } },
                },
              ],
            },
          },
        ],
      }),
      data({
        choices: [
          {
            delta: {
              tool_calls: [
                { index: 0, function: null },
                { index: 0, function: 'f' },
                { index: 0, function: { arguments: '{"a":1}' } },
                { index: true, id: 'c2', function: { name: 'g' } },
              ],
            },
            finish_reason: 'tool_calls',
          },
        ],
      }),
      'data: [DONE]',
    ]);
    const starts = events
      .filter((e) => e.type === 'content_block_start')
      .map((e) => e.content_block);
    expect(starts).toHaveLength(2);
    expect(starts[0].id).toMatch(/^toolu_/);
    expect(starts[0].name).toBe('');
    expect(starts[1]).toEqual({
      type: 'tool_use',
      id: 'c2',
      name: 'g',
      input: {},
    });
    const deltas = events
      .filter((e) => e.type === 'content_block_delta')
      .map((e) => e.delta.partial_json);
    // only string argument deltas are sent
    expect(deltas).toEqual(['{"a":1}']);
    expect(events.at(-2).delta.stop_reason).toBe('tool_use');
  });

  test('a badly typed finish_reason is ignored', () => {
    const events = stream([
      data({ model: 'm', choices: [{ delta: { content: 'Hi' } }] }),
      data({ choices: [{ delta: {}, finish_reason: { r: 1 } }] }),
      data({ choices: [{ delta: {}, finish_reason: 7 }] }),
      'data: [DONE]',
    ]);
    expect(events.at(-2).delta.stop_reason).toBe('end_turn');
  });

  test('a badly typed error chunk still ends the stream with an error', () => {
    const events = stream([
      data({ model: 'm', choices: [{ delta: { content: 'Hi' } }] }),
      data({ error: { type: 7, message: { m: 1 } } }),
      'data: [DONE]',
    ]);
    expect(events.at(-1)).toEqual({
      type: 'error',
      error: { type: 'api_error', message: 'Stream error' },
    });
  });

  test('__proto__ keys in chunks do not pollute', () => {
    const events = stream([
      'data: {"__proto__":{"polluted":1},"model":"m","choices":[{"__proto__":{"polluted":1},"delta":{"__proto__":{"args":1},"content_blocks":[{"__proto__":{"polluted":1},"delta":{"__proto__":{"polluted":1},"thinking":"t"}}],"tool_calls":[{"__proto__":{"id":1},"index":"__proto__","id":"c1","function":{"__proto__":{"polluted":1},"name":"f","arguments":"{}"}}]}}]}',
      'data: {"choices":[{"delta":{"tool_calls":[{"index":"constructor","id":"c2","function":{"name":"g","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}',
      'data: [DONE]',
    ]);
    const tools = events
      .filter(
        (e) =>
          e.type === 'content_block_start' &&
          e.content_block.type === 'tool_use'
      )
      .map((e) => e.content_block.id);
    expect(tools).toEqual(['c1', 'c2']);
  });

  test('a well-formed stream with numeric and string indices merges calls', () => {
    const events = stream([
      data({
        model: 'm',
        choices: [
          {
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
            delta: {
              tool_calls: [{ index: '0', function: { arguments: '{"x":1}' } }],
            },
            finish_reason: 'tool_calls',
          },
        ],
      }),
      'data: [DONE]',
    ]);
    expect(events.map((e) => e.type)).toEqual([
      'message_start',
      'content_block_start',
      'content_block_delta',
      'content_block_stop',
      'message_delta',
      'message_stop',
    ]);
    expect(events[2].delta.partial_json).toBe('{"x":1}');
  });
});
