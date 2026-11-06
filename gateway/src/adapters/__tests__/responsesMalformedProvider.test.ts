/**
 * PS fork (#73): the Responses adapter tolerates malformed provider output.
 * Null, numeric, string and badly typed entries in `choices`,
 * `content_blocks` and `tool_calls` are skipped (never dereferenced) in the
 * JSON and streamed paths, the result is still a valid Responses object or
 * event sequence, and provider keys such as `__proto__` / `constructor` never
 * reach Object.prototype. No live calls.
 */
// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../../utils/env', () => ({ Environment: () => ({}) }));

import {
  GoogleChatCompleteResponseTransform,
  GoogleChatCompleteStreamChunkTransform,
} from '../../providers/google/chatComplete';
import {
  transformChatCompletionsToResponses,
  transformStreamChunk,
  createStreamState,
} from '../responses';
import {
  transformStreamChunk as transformMessagesStreamChunk,
  createStreamState as createMessagesStreamState,
} from '../messages/streamTransform';

const MODEL = 'test-model';
const request = (extra: object = {}) => ({
  model: MODEL,
  input: 'hi',
  ...extra,
});

const OUTPUT_TYPES = new Set([
  'message',
  'reasoning',
  'function_call',
  'web_search_call',
]);

function expectNoPollution() {
  const probe: any = {};
  expect(probe.polluted).toBeUndefined();
  expect(probe.text).toBeUndefined();
  expect(probe.call_id).toBeUndefined();
  expect(probe.args).toBeUndefined();
  expect(Object.prototype.hasOwnProperty.call(Object.prototype, 'text')).toBe(
    false
  );
}

/** Checks the output items of a Responses object or snapshot. */
function expectValidOutput(output: any[]) {
  expect(Array.isArray(output)).toBe(true);
  for (const item of output) {
    expect(OUTPUT_TYPES.has(item.type)).toBe(true);
    expect(typeof item.id).toBe('string');
    if (item.type === 'message') {
      for (const part of item.content) {
        expect(part.type).toBe('output_text');
        expect(typeof part.text).toBe('string');
        expect(Array.isArray(part.annotations)).toBe(true);
      }
    } else if (item.type === 'reasoning') {
      for (const s of item.summary) expect(typeof s.text).toBe('string');
    } else if (item.type === 'function_call') {
      expect(typeof item.call_id).toBe('string');
      expect(typeof item.name).toBe('string');
      expect(typeof item.arguments).toBe('string');
    }
  }
}

function expectValidResponse(res: any) {
  expect(res.error).toBeNull();
  expect(res.object).toBe('response');
  expect(typeof res.id).toBe('string');
  expect(typeof res.model).toBe('string');
  expect(typeof res.created_at).toBe('number');
  expectValidOutput(res.output);
  // serialisable, as the gateway sends it
  expect(() => JSON.stringify(res)).not.toThrow();
}

const json = (body: any, provider = 'anthropic', req: any = request()) =>
  transformChatCompletionsToResponses(body, 200, provider, req) as any;

/** A provider body parsed from JSON text, so `__proto__` is an own key. */
const parsed = (text: string) => JSON.parse(text);

afterEach(() => {
  expectNoPollution();
});

describe('Responses adapter, malformed JSON responses', () => {
  test('null and non-object content_blocks entries are skipped', () => {
    const res = json({
      id: 'chat-1',
      model: MODEL,
      choices: [
        {
          index: 0,
          finish_reason: 'stop',
          message: {
            role: 'assistant',
            content: 'Hello.',
            content_blocks: [
              null,
              7,
              'thinking',
              true,
              [],
              { type: 'thinking', thinking: 'Let me think.', signature: 'sig' },
              { type: 'thinking', thinking: 42 },
              { thinking: { nested: true } },
              { type: 'redacted_thinking', data: 5 },
              { type: 'text', text: 'Hello.' },
            ],
          },
        },
      ],
    });
    expectValidResponse(res);
    expect(res.output.map((o: any) => o.type)).toEqual([
      'reasoning',
      'reasoning',
      'message',
    ]);
    expect(res.output[0].summary[0].text).toBe('Let me think.');
    expect(res.output[0].encrypted_content).toMatch(/^ps-thk1:anthropic:/);
    // a non-string thinking field is not copied
    expect(res.output[1].summary[0].text).toBe('');
    expect(res.output[2].content[0].text).toBe('Hello.');
  });

  test.each([
    ['a string', 'abc'],
    ['a number', 3],
    ['an object', { 0: { type: 'thinking', thinking: 'x' } }],
    ['null', null],
  ])('content_blocks that is %s is ignored', (_label, blocks) => {
    const res = json({
      choices: [
        {
          finish_reason: 'stop',
          message: { content: 'ok', content_blocks: blocks },
        },
      ],
    });
    expectValidResponse(res);
    expect(res.output.map((o: any) => o.type)).toEqual(['message']);
  });

  test('null, numeric and string choices and messages are skipped', () => {
    const res = json({
      choices: [
        null,
        5,
        'choice',
        [],
        { finish_reason: 'stop', message: null },
        { finish_reason: 'stop', message: 'text' },
        { finish_reason: 'stop', message: { content: 'kept' } },
      ],
    });
    expectValidResponse(res);
    expect(res.output).toHaveLength(1);
    expect(res.output[0].content[0].text).toBe('kept');
  });

  test.each([['a string'], [7], [{ 0: {} }], [null]])(
    'choices that is %p gives an empty response',
    (choices) => {
      const res = json({ id: 'chat-1', choices });
      expectValidResponse(res);
      expect(res.output).toEqual([]);
      expect(res.status).toBe('completed');
    }
  );

  test('malformed tool_calls entries are skipped or typed', () => {
    const res = json({
      choices: [
        {
          finish_reason: 'tool_calls',
          message: {
            content: null,
            tool_calls: [
              null,
              4,
              'call',
              { type: 'function' },
              { type: 'function', function: null },
              { type: 'function', function: 'get_time' },
              { type: 'custom', function: { name: 'x', arguments: '{}' } },
              {
                id: 9,
                type: 'function',
                function: { name: 12, arguments: { city: 'Paris' } },
              },
              { id: 'call_ok', function: { name: 'ok', arguments: '{}' } },
            ],
          },
        },
      ],
    });
    expectValidResponse(res);
    expect(res.output.map((o: any) => [o.name, o.arguments])).toEqual([
      ['', '{"city":"Paris"}'],
      ['ok', '{}'],
    ]);
    expect(res.output[0].call_id).toMatch(/^call_/);
    expect(res.output[1].call_id).toBe('call_ok');
  });

  test('tool_calls that is not an array is ignored', () => {
    const res = json({
      choices: [
        { message: { content: 'x', tool_calls: { 0: { function: {} } } } },
      ],
    });
    expectValidResponse(res);
    expect(res.output.map((o: any) => o.type)).toEqual(['message']);
  });

  test('content parts and content of other types never throw', () => {
    const parts = json({
      choices: [
        {
          message: {
            content: [
              null,
              3,
              'raw',
              { text: 5 },
              { text: 'a' },
              { text: 'b' },
            ],
          },
        },
      ],
    });
    expectValidResponse(parts);
    expect(parts.output[0].content[0].text).toBe('ab');

    // an object whose toString is not callable: String() would throw
    const obj = json(
      parsed('{"choices":[{"message":{"content":{"toString":1}}}]}')
    );
    expectValidResponse(obj);
    expect(obj.output).toEqual([]);

    const num = json({ choices: [{ message: { content: 12 } }] });
    expect(num.output[0].content[0].text).toBe('12');
  });

  test('badly typed id, model, created and usage fall back', () => {
    const res = json({
      id: { x: 1 },
      model: ['m'],
      created: 'yesterday',
      usage: 'lots',
      choices: [{ finish_reason: { r: 1 }, message: { content: 'x' } }],
    });
    expectValidResponse(res);
    expect(res.id).toMatch(/^resp_/);
    expect(res.model).toBe(MODEL);
    expect(res.usage.input_tokens).toBe(0);
    expect(res.status).toBe('completed');
  });

  test.each([[null], ['body'], [5]])(
    'a body of %p is a failed request, not a throw',
    (body) => {
      const res = json(body);
      expect(res.error).toEqual(expect.objectContaining({ type: 'api_error' }));
    }
  );

  test('__proto__ and constructor keys from the provider do not pollute', () => {
    const body = parsed(`{
      "__proto__": {"polluted": "yes"},
      "constructor": {"prototype": {"polluted": "yes"}},
      "choices": [{
        "__proto__": {"polluted": "yes"},
        "finish_reason": "stop",
        "message": {
          "__proto__": {"polluted": "yes"},
          "content": "Searched.",
          "content_blocks": [
            null,
            {"__proto__": {"polluted": "yes"}, "type": "thinking", "thinking": "t"},
            {"type": "server_tool_use", "id": "__proto__", "name": "web_search",
             "input": {"query": "q", "__proto__": {"polluted": "yes"}}},
            {"type": "web_search_tool_result", "tool_use_id": "__proto__",
             "content": [null, {"type": "web_search_result", "url": "https://a.example"}]},
            {"type": "server_tool_use", "id": "constructor", "name": "web_search", "input": "{\\"query\\":\\"c\\"}"},
            {"type": "web_search_tool_result", "tool_use_id": "constructor", "content": null},
            {"type": "text", "text": "Searched.",
             "citations": [null, 3, {"type": "web_search_result_location", "url": "https://a.example", "title": 5}]}
          ],
          "tool_calls": [
            {"id": "__proto__", "type": "function",
             "function": {"name": "constructor", "arguments": "{\\"__proto__\\":{\\"polluted\\":1}}"}}
          ]
        },
        "groundingMetadata": {
          "__proto__": {"polluted": "yes"},
          "webSearchQueries": [null, "__proto__", 4],
          "groundingChunks": [null, {"web": {"uri": "https://g.example", "title": null}}],
          "groundingSupports": [null, {"segment": null},
            {"segment": {"text": "Searched"}, "groundingChunkIndices": ["length", "__proto__", 1, -1, 1.5]}]
        }
      }]
    }`);
    const res = json(
      body,
      'anthropic',
      request({ include: ['web_search_call.action.sources'] })
    );
    expectValidResponse(res);
    expect(res.output.map((o: any) => o.type)).toEqual([
      'reasoning',
      'web_search_call',
      'web_search_call',
      'message',
      'function_call',
    ]);
    expect(res.output[1].status).toBe('completed');
    expect(res.output[1].action.sources).toEqual([
      { type: 'url', url: 'https://a.example' },
    ]);
    expect(res.output[2].status).toBe('failed');
    expect(res.output[2].action.query).toBe('c');
    expect(res.output[3].content[0].annotations).toEqual([
      expect.objectContaining({ url: 'https://a.example', title: '' }),
    ]);
    expect(res.output[4].call_id).toBe('__proto__');
    expect(res.output[4].name).toBe('constructor');
  });

  test('Google grounding with null entries and odd chunk indices', () => {
    const res = json(
      parsed(`{
        "choices": [{
          "finish_reason": "stop",
          "message": {"content": "Searched text."},
          "groundingMetadata": {
            "webSearchQueries": [null, "q", 4],
            "groundingChunks": [null, 3, {"web": null}, {"web": {"uri": "https://g.example"}}],
            "groundingSupports": [null, 5, {"segment": "x"},
              {"segment": {"text": "Searched"}, "groundingChunkIndices": ["length", "3", 3, 99, -1]}]
          }
        }]
      }`),
      'google',
      request({ include: ['web_search_call.action.sources'] })
    );
    expectValidResponse(res);
    expect(res.output.map((o: any) => o.type)).toEqual([
      'message',
      'web_search_call',
    ]);
    expect(res.output[0].content[0].annotations).toEqual([
      expect.objectContaining({ url: 'https://g.example', start_index: 0 }),
    ]);
    expect(res.output[1].action).toEqual({
      type: 'search',
      query: 'q',
      sources: [{ type: 'url', url: 'https://g.example' }],
    });
  });

  test('a well-formed response is unchanged', () => {
    const res = json(
      {
        id: 'chat-1',
        model: MODEL,
        created: 1700000000,
        choices: [
          {
            finish_reason: 'tool_calls',
            message: {
              content: 'Calling.',
              reasoning_content: 'why',
              tool_calls: [
                {
                  id: 'call_1',
                  type: 'function',
                  function: { name: 'f', arguments: '{"a":1}' },
                },
              ],
            },
          },
        ],
        usage: { prompt_tokens: 3, completion_tokens: 4, total_tokens: 7 },
      },
      'openai'
    );
    expectValidResponse(res);
    expect(res.id).toBe('chat-1');
    expect(res.created_at).toBe(1700000000);
    expect(
      res.output.map((o: any) => ({ ...o, id: o.id.slice(0, 3) }))
    ).toEqual([
      {
        id: 'rs_',
        type: 'reasoning',
        summary: [{ type: 'summary_text', text: 'why' }],
      },
      {
        id: 'msg',
        type: 'message',
        role: 'assistant',
        status: 'completed',
        content: [
          {
            type: 'output_text',
            text: 'Calling.',
            annotations: [],
            logprobs: [],
          },
        ],
      },
      {
        id: 'fc_',
        type: 'function_call',
        call_id: 'call_1',
        name: 'f',
        arguments: '{"a":1}',
        status: 'completed',
      },
    ]);
    expect(res.usage.total_tokens).toBe(7);
  });
});

// ── Streaming ───────────────────────────────────────────────────────────────

/** Run raw `data:` chunks through the adapter and parse the events. */
function stream(chunks: string[], provider = 'anthropic', req = request()) {
  const state = createStreamState(req, provider);
  let out = '';
  for (const c of chunks) out += transformStreamChunk(c, state) ?? '';
  out += transformStreamChunk('data: [DONE]', state) ?? '';
  return out
    .split('\n\n')
    .filter((e) => e.trim())
    .map((e) => JSON.parse(e.split('\n')[1].slice(6)));
}

/** Checks a Responses event sequence: framing, numbering, item lifecycle. */
function expectValidStream(events: any[]) {
  expect(events[0].type).toBe('response.created');
  expect(events[1].type).toBe('response.in_progress');
  const last = events[events.length - 1];
  expect(['response.completed', 'response.incomplete']).toContain(last.type);
  events.forEach((e, i) => expect(e.sequence_number).toBe(i));
  const added = events.filter((e) => e.type === 'response.output_item.added');
  const done = events.filter((e) => e.type === 'response.output_item.done');
  expect(done.map((e) => e.item.id)).toEqual(
    expect.arrayContaining(added.map((e) => e.item.id))
  );
  expect(done).toHaveLength(added.length);
  added.forEach((e, i) => expect(e.output_index).toBe(i));
  expectValidOutput(done.map((e) => e.item));
  expectValidOutput(last.response.output);
  expect(last.response.output.map((o: any) => o.id)).toEqual(
    added.map((e) => e.item.id)
  );
  return last.response;
}

const data = (v: unknown) => `data: ${JSON.stringify(v)}`;
const chunk = (delta: unknown, extra: object = {}) =>
  data({ id: 'c', model: MODEL, choices: [{ index: 0, delta, ...extra }] });

describe('Responses adapter, malformed streamed chunks', () => {
  test('non-object chunks, choices and deltas are skipped', () => {
    const events = stream([
      'data: null',
      'data: 5',
      'data: "text"',
      'data: []',
      data({ choices: 'x' }),
      data({ choices: [null] }),
      data({ choices: [{ delta: null }] }),
      data({ choices: [{ delta: 'text' }] }),
      data({ choices: [{ delta: [] }] }),
      chunk({ content: 'Hello' }),
      chunk({ content: 7 }),
      chunk({}, { finish_reason: { odd: true } }),
      chunk({}, { finish_reason: 'stop' }),
      data({ usage: 'lots' }),
    ]);
    const res = expectValidStream(events);
    expect(res.status).toBe('completed');
    expect(res.output).toHaveLength(1);
    expect(res.output[0].content[0].text).toBe('Hello');
  });

  test('null and non-object content_blocks entries are skipped', () => {
    const events = stream([
      chunk({
        content_blocks: [
          null,
          3,
          'thinking',
          [],
          { index: 0, delta: null },
          { index: 0, delta: 'raw' },
          { index: 0, delta: 5 },
          { index: 0, delta: { thinking: 9 } },
          { index: 0, delta: { thinking: 'Considering.' } },
          { index: 0, delta: { signature: 'sig' } },
          { index: 1, delta: { data: 4 } },
        ],
      }),
      chunk({ content_blocks: 'abc' }),
      chunk({ content_blocks: { 0: { delta: { thinking: 'x' } } } }),
      chunk({ content: 'Answer.' }),
      chunk({}, { finish_reason: 'stop' }),
    ]);
    const res = expectValidStream(events);
    expect(res.output.map((o: any) => o.type)).toEqual([
      'reasoning',
      'message',
    ]);
    expect(res.output[0].summary[0].text).toBe('Considering.');
    expect(res.output[0].encrypted_content).toMatch(/^ps-thk1:anthropic:/);
  });

  test('malformed tool_calls entries are skipped or typed', () => {
    const events = stream([
      chunk({
        tool_calls: [
          null,
          4,
          'call',
          [],
          { index: 0, id: 5, function: 'f' },
          { index: 0, function: { name: 9, arguments: { a: 1 } } },
          { index: 0, function: { arguments: '{"a":' } },
          { index: 0, function: { arguments: '1}' } },
          { index: { toString: 1 }, id: 'call_2', function: { name: 'g' } },
        ],
      }),
      chunk({}, { finish_reason: 'tool_calls' }),
    ]);
    const res = expectValidStream(events);
    expect(res.output.map((o: any) => [o.name, o.arguments])).toEqual([
      ['', '{"a":1}'],
      ['g', ''],
    ]);
    expect(res.output[1].call_id).toBe('call_2');
  });

  test('__proto__ and constructor tool call indices do not pollute', () => {
    const events = stream([
      `data: {"choices":[{"delta":{"tool_calls":[
        {"index":"__proto__","id":"call_a","function":{"name":"a","arguments":"{\\"x\\":"}},
        {"index":"constructor","id":"call_b","function":{"name":"b","arguments":"{}"}}
      ]}}]}`.replace(/\n\s*/g, ''),
      `data: {"choices":[{"delta":{"tool_calls":[
        {"index":"__proto__","function":{"arguments":"1}"}},
        {"index":"hasOwnProperty","function":{"arguments":""}}
      ]},"finish_reason":"tool_calls"}]}`.replace(/\n\s*/g, ''),
    ]);
    const res = expectValidStream(events);
    expect(
      res.output.map((o: any) => [o.call_id, o.name, o.arguments])
    ).toEqual([
      ['call_a', 'a', '{"x":1}'],
      ['call_b', 'b', '{}'],
      [expect.stringMatching(/^call_/), '', ''],
    ]);
  });

  test('__proto__ keys in blocks and web search ids do not pollute', () => {
    const events = stream(
      [
        `data: {"__proto__":{"polluted":"yes"},"choices":[{"__proto__":{"polluted":"yes"},"delta":{
          "__proto__":{"polluted":"yes"},
          "content_blocks":[
            {"index":"__proto__","delta":{"id":"__proto__","name":"web_search","input":{"__proto__":{"polluted":"yes"}}}},
            {"index":"__proto__","delta":{"partial_json":"{\\"query\\":\\"q\\"}"}},
            {"index":"constructor","delta":{"tool_use_id":"__proto__","content":[null,{"type":"web_search_result","url":"https://a.example"}]}},
            {"index":"toString","delta":{"citation":null}},
            {"index":"toString","delta":{"citations":[null,{"type":"web_search_result_location","url":"https://a.example"}]}}
          ]}}]}`.replace(/\n\s*/g, ''),
        chunk({ content: 'Found it.' }),
        `data: {"choices":[{"delta":{},"finish_reason":"stop","groundingMetadata":{
          "__proto__":{"polluted":"yes"},
          "webSearchQueries":[null,"__proto__"],
          "groundingChunks":[null,{"web":{"uri":"https://g.example"}}],
          "groundingSupports":[null,{"segment":{"text":"Found"},"groundingChunkIndices":["length","__proto__",1]}]
        }}]}`.replace(/\n\s*/g, ''),
      ],
      'anthropic',
      request({ include: ['web_search_call.action.sources'] })
    );
    const res = expectValidStream(events);
    expect(res.output.map((o: any) => o.type)).toEqual([
      'web_search_call',
      'message',
      'web_search_call',
    ]);
    expect(res.output[0]).toEqual(
      expect.objectContaining({
        status: 'completed',
        action: {
          type: 'search',
          // an input object with any key wins over partial_json
          query: '',
          sources: [{ type: 'url', url: 'https://a.example' }],
        },
      })
    );
    // grounding annotations are added at the end, then the last text
    // block's citations as the message closes
    expect(res.output[1].content[0].annotations).toEqual([
      expect.objectContaining({ url: 'https://g.example', start_index: 0 }),
      expect.objectContaining({ url: 'https://a.example' }),
    ]);
    expect(res.output[2].action.query).toBe('__proto__');
  });

  test("the last text block's citations are kept when the stream ends", () => {
    const events = stream([
      chunk({
        content_blocks: [
          {
            index: 0,
            delta: {
              citation: {
                type: 'web_search_result_location',
                url: 'https://a.example',
                title: 'A',
              },
            },
          },
        ],
      }),
      chunk({ content: 'Cited.' }),
      chunk({}, { finish_reason: 'stop' }),
    ]);
    const res = expectValidStream(events);
    expect(res.output[0].content[0].annotations).toEqual([
      {
        type: 'url_citation',
        start_index: 0,
        end_index: 6,
        url: 'https://a.example',
        title: 'A',
      },
    ]);
    expect(
      events.filter((e) => e.type === 'response.output_text.annotation.added')
    ).toHaveLength(1);
  });
});

describe('Messages adapter stream, provider tool call indices', () => {
  test('a __proto__ index is a call like any other', () => {
    const state = createMessagesStreamState(MODEL);
    let out = '';
    for (const c of [
      `data: {"model":"m","choices":[{"delta":{"tool_calls":[{"index":"__proto__","id":"call_a","function":{"name":"a","arguments":"{\\"x\\":"}}]}}]}`,
      `data: {"choices":[{"delta":{"tool_calls":[{"index":"__proto__","function":{"arguments":"1}"}},{"index":"constructor","id":"call_b","function":{"name":"b","arguments":"{}"}},null,7]},"finish_reason":"tool_calls"}]}`,
      'data: [DONE]',
    ]) {
      out += transformMessagesStreamChunk(c, state) ?? '';
    }
    const starts = out
      .split('\n\n')
      .filter((e) => e.includes('content_block_start'))
      .map((e) => JSON.parse(e.split('\n')[1].slice(6)).content_block);
    expect(starts.map((b: any) => [b.id, b.name])).toEqual([
      ['call_a', 'a'],
      ['call_b', 'b'],
    ]);
    expect(out).toContain('"partial_json":"1}"');
  });
});

// ── Google grounding with a function call: JSON and stream agree ────────────

describe('Google grounding with a function call', () => {
  const grounding = {
    webSearchQueries: ['paris time'],
    groundingChunks: [{ web: { uri: 'https://g.example', title: 'g' } }],
    groundingSupports: [
      { segment: { text: 'Checking.' }, groundingChunkIndices: [0] },
    ],
  };
  const parts = [
    { text: 'Checking.' },
    { functionCall: { name: 'get_time', args: { city: 'Paris' } } },
  ];
  const usageMetadata = {
    promptTokenCount: 1,
    candidatesTokenCount: 2,
    totalTokenCount: 3,
  };

  test('the output order is the same in JSON and streamed', () => {
    const chat = GoogleChatCompleteResponseTransform(
      {
        modelVersion: MODEL,
        candidates: [
          {
            content: { role: 'model', parts },
            finishReason: 'STOP',
            index: 0,
            groundingMetadata: grounding,
          },
        ],
        usageMetadata,
      } as any,
      200,
      new Headers(),
      false
    );
    const res = json(
      chat,
      'google',
      request({ tools: [{ type: 'web_search' }] })
    );
    expectValidResponse(res);

    const chatState: any = {};
    const state = createStreamState(
      request({ tools: [{ type: 'web_search' }] }),
      'google'
    );
    let out = '';
    for (const raw of [
      data({
        modelVersion: MODEL,
        candidates: [
          { content: { role: 'model', parts: [parts[0]] }, index: 0 },
        ],
      }),
      data({
        modelVersion: MODEL,
        candidates: [
          {
            content: { role: 'model', parts: [parts[1]] },
            finishReason: 'STOP',
            index: 0,
            groundingMetadata: grounding,
          },
        ],
        usageMetadata,
      }),
    ]) {
      const c =
        GoogleChatCompleteStreamChunkTransform(
          raw,
          'chat-1',
          chatState,
          false
        ) ?? '';
      for (const msg of String(c).split('\n\n')) {
        if (msg.trim()) out += transformStreamChunk(msg, state) ?? '';
      }
    }
    out += transformStreamChunk('data: [DONE]', state) ?? '';
    const events = out
      .split('\n\n')
      .filter((e) => e.trim())
      .map((e) => JSON.parse(e.split('\n')[1].slice(6)));
    const streamed = expectValidStream(events);

    const types = (o: any[]) => o.map((i) => i.type);
    expect(types(res.output)).toEqual([
      'message',
      'function_call',
      'web_search_call',
    ]);
    expect(types(streamed.output)).toEqual(types(res.output));
  });
});
