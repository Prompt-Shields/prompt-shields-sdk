/**
 * PS fork (#73): web search results through the Responses adapter become
 * `web_search_call` output items and `url_citation` annotations
 * (responses/webSearchOutput.ts), in JSON and streamed responses. Fixtures
 * follow the shapes Anthropic and Gemini return; no live calls.
 */
// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../../utils/env', () => ({ Environment: () => ({}) }));

import {
  getAnthropicChatCompleteResponseTransform,
  getAnthropicStreamChunkTransform,
} from '../../providers/anthropic/chatComplete';
import {
  GoogleChatCompleteResponseTransform,
  GoogleChatCompleteStreamChunkTransform,
} from '../../providers/google/chatComplete';
import {
  transformChatCompletionsToResponses,
  transformStreamChunk,
  createStreamState,
} from '../responses';
import { applyAdapterRequestTransform } from '../../handlers/adapterUtils';

const MODEL = 'test-model';
const SOURCES = { include: ['web_search_call.action.sources'] };
const request = (extra: object = {}) => ({
  model: MODEL,
  input: 'What is new?',
  tools: [{ type: 'web_search' }],
  ...extra,
});

/** The code point slice [start, end) of text, as a Python client reads it. */
const cpSlice = (text: string, start: number, end: number) =>
  Array.from(text).slice(start, end).join('');

// ── Anthropic fixtures ──────────────────────────────────────────────────────

const PREAMBLE = "I'll search for that.";
const CITED = 'Rustls 1.0 shipped in March. 🎉';
const PLAIN = ' Nothing else changed.';

const anthropicSearchResults = [
  {
    type: 'web_search_result',
    url: 'https://example.com/rustls',
    title: 'Rustls 1.0',
    encrypted_content: 'ENC_CONTENT_SECRET_1',
    page_age: 'March 3, 2026',
  },
  {
    type: 'web_search_result',
    url: 'https://example.org/news',
    title: 'News',
    encrypted_content: 'ENC_CONTENT_SECRET_2',
    page_age: null,
  },
];
const citation = {
  type: 'web_search_result_location',
  url: 'https://example.com/rustls',
  title: 'Rustls 1.0',
  encrypted_index: 'ENC_INDEX_SECRET',
  cited_text: 'Rustls 1.0 was released in March...',
};

const anthropicMessage = {
  id: 'msg_01',
  type: 'message',
  role: 'assistant',
  model: MODEL,
  stop_reason: 'end_turn',
  stop_sequence: null,
  usage: {
    input_tokens: 100,
    output_tokens: 50,
    server_tool_use: { web_search_requests: 1 },
  },
  content: [
    { type: 'text', text: PREAMBLE },
    {
      type: 'server_tool_use',
      id: 'srvtoolu_01',
      name: 'web_search',
      input: { query: 'rustls 1.0 release' },
    },
    {
      type: 'web_search_tool_result',
      tool_use_id: 'srvtoolu_01',
      content: anthropicSearchResults,
    },
    { type: 'text', text: CITED, citations: [citation, citation] },
    { type: 'text', text: PLAIN },
  ],
};

function anthropicJson(message: any, req: any) {
  const chat = getAnthropicChatCompleteResponseTransform('anthropic')(
    message,
    200,
    new Headers(),
    false
  );
  return transformChatCompletionsToResponses(
    chat as any,
    200,
    'anthropic',
    req
  ) as any;
}

const sse = (type: string, data: object) =>
  `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}`;

const anthropicStream = [
  sse('message_start', {
    message: {
      id: 'msg_01',
      type: 'message',
      role: 'assistant',
      model: MODEL,
      content: [],
      usage: { input_tokens: 100, output_tokens: 1 },
    },
  }),
  sse('content_block_start', {
    index: 0,
    content_block: { type: 'text', text: '' },
  }),
  sse('content_block_delta', {
    index: 0,
    delta: { type: 'text_delta', text: PREAMBLE },
  }),
  sse('content_block_stop', { index: 0 }),
  sse('content_block_start', {
    index: 1,
    content_block: {
      type: 'server_tool_use',
      id: 'srvtoolu_01',
      name: 'web_search',
      input: {},
    },
  }),
  sse('content_block_delta', {
    index: 1,
    delta: { type: 'input_json_delta', partial_json: '{"query": "rustls' },
  }),
  sse('content_block_delta', {
    index: 1,
    delta: { type: 'input_json_delta', partial_json: ' 1.0 release"}' },
  }),
  sse('content_block_stop', { index: 1 }),
  sse('content_block_start', {
    index: 2,
    content_block: {
      type: 'web_search_tool_result',
      tool_use_id: 'srvtoolu_01',
      content: anthropicSearchResults,
    },
  }),
  sse('content_block_stop', { index: 2 }),
  sse('content_block_start', {
    index: 3,
    content_block: { type: 'text', text: '', citations: [] },
  }),
  sse('content_block_delta', {
    index: 3,
    delta: { type: 'citations_delta', citation },
  }),
  sse('content_block_delta', {
    index: 3,
    delta: { type: 'text_delta', text: CITED.slice(0, 12) },
  }),
  sse('content_block_delta', {
    index: 3,
    delta: { type: 'text_delta', text: CITED.slice(12) },
  }),
  sse('content_block_stop', { index: 3 }),
  sse('content_block_start', {
    index: 4,
    content_block: { type: 'text', text: '' },
  }),
  sse('content_block_delta', {
    index: 4,
    delta: { type: 'text_delta', text: PLAIN },
  }),
  sse('content_block_stop', { index: 4 }),
  sse('message_delta', {
    delta: { stop_reason: 'end_turn', stop_sequence: null },
    usage: { output_tokens: 50 },
  }),
  sse('message_stop', {}),
];

/** Run provider SSE through the provider chat transform and the adapter. */
function streamThrough(
  providerChunks: string[],
  toChat: (raw: string, state: any) => string | undefined,
  req: any,
  provider: string
) {
  const chatState: any = {};
  const state = createStreamState(req, provider);
  let out = '';
  for (const raw of providerChunks) {
    const chat = toChat(raw, chatState) ?? '';
    for (const msg of chat.split('\n\n')) {
      if (msg.trim()) out += transformStreamChunk(msg, state) ?? '';
    }
  }
  out += transformStreamChunk('data: [DONE]', state) ?? '';
  return {
    raw: out,
    events: out
      .split('\n\n')
      .filter((e) => e.trim())
      .map((e) => JSON.parse(e.split('\n')[1].slice(6))),
  };
}

const anthropicToChat = (raw: string, state: any) =>
  getAnthropicStreamChunkTransform('anthropic')(raw, 'chat-1', state, false);

describe('Anthropic web search, JSON', () => {
  test('server tool blocks become a web_search_call between the message parts', () => {
    const res = anthropicJson(anthropicMessage, request(SOURCES));
    expect(res.output.map((o: any) => o.type)).toEqual([
      'message',
      'web_search_call',
      'message',
    ]);
    expect(res.output[0].content[0]).toMatchObject({
      text: PREAMBLE,
      annotations: [],
    });
    expect(res.output[1]).toEqual({
      id: expect.stringMatching(/^ws_/),
      type: 'web_search_call',
      status: 'completed',
      action: {
        type: 'search',
        query: 'rustls 1.0 release',
        sources: [
          { type: 'url', url: 'https://example.com/rustls' },
          { type: 'url', url: 'https://example.org/news' },
        ],
      },
    });
  });

  test('citations are url_citation annotations over the cited text block', () => {
    const res = anthropicJson(anthropicMessage, request());
    const part = res.output[2].content[0];
    expect(part.text).toBe(CITED + PLAIN);
    // repeated citations of one URL are one annotation
    expect(part.annotations).toEqual([
      {
        type: 'url_citation',
        start_index: 0,
        end_index: Array.from(CITED).length,
        url: 'https://example.com/rustls',
        title: 'Rustls 1.0',
      },
    ]);
    const a = part.annotations[0];
    expect(cpSlice(part.text, a.start_index, a.end_index)).toBe(CITED);
  });

  test('sources only when include asks for them', () => {
    const res = anthropicJson(anthropicMessage, request());
    expect(res.output[1].action).toEqual({
      type: 'search',
      query: 'rustls 1.0 release',
    });
  });

  test('encrypted content, encrypted index and cited text never reach the response', () => {
    const body = JSON.stringify(
      anthropicJson(anthropicMessage, request(SOURCES))
    );
    expect(body).not.toContain('ENC_');
    expect(body).not.toContain('encrypted');
    expect(body).not.toContain('was released in March');
  });

  test('a search error result is a failed web_search_call', () => {
    const message = {
      ...anthropicMessage,
      content: [
        anthropicMessage.content[1],
        {
          type: 'web_search_tool_result',
          tool_use_id: 'srvtoolu_01',
          content: {
            type: 'web_search_tool_result_error',
            error_code: 'max_uses_exceeded',
          },
        },
        { type: 'text', text: 'I could not search.' },
      ],
    };
    const res = anthropicJson(message, request(SOURCES));
    expect(res.output.map((o: any) => [o.type, o.status])).toEqual([
      ['web_search_call', 'failed'],
      ['message', 'completed'],
    ]);
    expect(res.output[0].action.sources).toEqual([]);
  });

  test('a provider tool id of __proto__ does not touch Object.prototype', () => {
    const blocks = [
      {
        type: 'server_tool_use',
        id: '__proto__',
        name: 'web_search',
        input: {},
      },
      { type: 'web_search_tool_result', tool_use_id: '__proto__', content: [] },
      {
        type: 'web_search_tool_result',
        tool_use_id: 'constructor',
        content: [],
      },
    ];
    anthropicJson({ ...anthropicMessage, content: blocks }, request(SOURCES));
    const state = createStreamState(request(SOURCES), 'anthropic');
    for (const delta of [
      { id: '__proto__', name: 'web_search', input: {} },
      { tool_use_id: '__proto__', content: [] },
      { tool_use_id: 'toString', content: [] },
    ]) {
      transformStreamChunk(
        `data: ${JSON.stringify({ choices: [{ delta: { content_blocks: [{ index: 0, delta }] } }] })}`,
        state
      );
    }
    const probe: any = {};
    expect(probe.status).toBeUndefined();
    expect(probe.done).toBeUndefined();
    expect(probe.searchStatus).toBeUndefined();
    expect(probe.sources).toBeUndefined();
  });

  test('a response without web search is unchanged', () => {
    const res = anthropicJson(
      { ...anthropicMessage, content: [{ type: 'text', text: 'Hello' }] },
      request()
    );
    expect(res.output).toHaveLength(1);
    expect(res.output[0].content[0]).toMatchObject({
      text: 'Hello',
      annotations: [],
    });
  });
});

describe('Anthropic web search, streamed', () => {
  const run = (req: any) =>
    streamThrough(anthropicStream, anthropicToChat, req, 'anthropic');

  test('events arrive in a valid order', () => {
    const { events } = run(request(SOURCES));
    const types = events.map((e) => e.type);
    expect(types).toEqual([
      'response.created',
      'response.in_progress',
      // preamble message
      'response.output_item.added',
      'response.content_part.added',
      'response.output_text.delta',
      'response.output_text.done',
      'response.content_part.done',
      'response.output_item.done',
      // the search
      'response.output_item.added',
      'response.web_search_call.in_progress',
      'response.web_search_call.searching',
      'response.web_search_call.completed',
      'response.output_item.done',
      // cited answer: the annotation once the cited block's text is complete
      'response.output_item.added',
      'response.content_part.added',
      'response.output_text.delta',
      'response.output_text.delta',
      'response.output_text.annotation.added',
      'response.output_text.delta',
      'response.output_text.done',
      'response.content_part.done',
      'response.output_item.done',
      'response.completed',
    ]);
    expect(events.map((e) => e.sequence_number)).toEqual(
      events.map((_, i) => i)
    );
  });

  test('the web_search_call item and annotation match the JSON response', () => {
    const { events } = run(request(SOURCES));
    const wsAdded = events.find(
      (e) =>
        e.type === 'response.output_item.added' &&
        e.item.type === 'web_search_call'
    );
    expect(wsAdded.output_index).toBe(1);
    expect(wsAdded.item.status).toBe('in_progress');
    for (const t of ['in_progress', 'searching', 'completed']) {
      expect(
        events.find((e) => e.type === `response.web_search_call.${t}`)
      ).toMatchObject({ item_id: wsAdded.item.id, output_index: 1 });
    }

    const annotation = events.find(
      (e) => e.type === 'response.output_text.annotation.added'
    );
    expect(annotation).toMatchObject({
      output_index: 2,
      content_index: 0,
      annotation_index: 0,
      annotation: {
        type: 'url_citation',
        start_index: 0,
        end_index: Array.from(CITED).length,
        url: 'https://example.com/rustls',
        title: 'Rustls 1.0',
      },
    });

    const final = events[events.length - 1].response;
    const json = anthropicJson(anthropicMessage, request(SOURCES));
    const strip = (items: any[]) =>
      items.map(({ id: _id, ...rest }: any) => rest);
    expect(strip(final.output)).toEqual(strip(json.output));
    expect(final.output[1].action.query).toBe('rustls 1.0 release');
  });

  test('no partial_json reaches the output as a function call', () => {
    const { events } = run(request());
    expect(events.some((e) => e.item?.type === 'function_call')).toBe(false);
    const final = events[events.length - 1].response;
    expect(final.output[1].action).toEqual({
      type: 'search',
      query: 'rustls 1.0 release',
    });
  });

  test('encrypted content and index never reach the stream', () => {
    const { raw } = run(request(SOURCES));
    expect(raw).not.toContain('ENC_');
    expect(raw).not.toContain('encrypted');
  });

  test('a search with no result block ends failed', () => {
    const { events } = streamThrough(
      [
        ...anthropicStream.slice(0, 8),
        sse('message_delta', {
          delta: { stop_reason: 'pause_turn' },
          usage: { output_tokens: 5 },
        }),
        sse('message_stop', {}),
      ],
      anthropicToChat,
      request(),
      'anthropic'
    );
    expect(
      events.some((e) => e.type === 'response.web_search_call.completed')
    ).toBe(false);
    const done = events.filter(
      (e) =>
        e.type === 'response.output_item.done' &&
        e.item.type === 'web_search_call'
    );
    expect(done).toHaveLength(1);
    expect(done[0].item.status).toBe('failed');
  });
});

// ── Google fixtures ─────────────────────────────────────────────────────────

const G_TEXT_1 = 'Le café ouvre à 8h.';
const G_TEXT_2 = ' Il ferme à 18h.';
const utf8 = (s: string) => Buffer.byteLength(s, 'utf8');

const groundingMetadata = {
  webSearchQueries: ['café horaires', 'café adresse'],
  searchEntryPoint: { renderedContent: '<div>chips</div>' },
  groundingChunks: [
    { web: { uri: 'https://grounding.example/redirect/1', title: 'cafe.fr' } },
    { web: { uri: 'https://grounding.example/redirect/2', title: 'guide.fr' } },
  ],
  groundingSupports: [
    {
      // proto3 JSON leaves out startIndex 0
      segment: { endIndex: utf8(G_TEXT_1), text: G_TEXT_1 },
      groundingChunkIndices: [0, 1],
      confidenceScores: [0.9, 0.8],
    },
    {
      segment: {
        startIndex: utf8(G_TEXT_1) + 1,
        endIndex: utf8(G_TEXT_1 + G_TEXT_2),
        text: G_TEXT_2.trim(),
      },
      groundingChunkIndices: [1],
      confidenceScores: [0.7],
    },
  ],
  retrievalMetadata: {},
};

const geminiResponse = {
  modelVersion: MODEL,
  candidates: [
    {
      content: { role: 'model', parts: [{ text: G_TEXT_1 + G_TEXT_2 }] },
      finishReason: 'STOP',
      index: 0,
      groundingMetadata,
    },
  ],
  usageMetadata: {
    promptTokenCount: 10,
    candidatesTokenCount: 20,
    totalTokenCount: 30,
  },
};

function geminiJson(response: any, req: any) {
  const chat = GoogleChatCompleteResponseTransform(
    response,
    200,
    new Headers(),
    false
  );
  return transformChatCompletionsToResponses(
    chat as any,
    200,
    'google',
    req
  ) as any;
}

const expectedGoogleAnnotations = () => {
  const end1 = Array.from(G_TEXT_1).length;
  const start2 = end1 + 1;
  const end2 = Array.from(G_TEXT_1 + G_TEXT_2).length;
  return [
    {
      type: 'url_citation',
      start_index: 0,
      end_index: end1,
      url: 'https://grounding.example/redirect/1',
      title: 'cafe.fr',
    },
    {
      type: 'url_citation',
      start_index: 0,
      end_index: end1,
      url: 'https://grounding.example/redirect/2',
      title: 'guide.fr',
    },
    {
      type: 'url_citation',
      start_index: start2,
      end_index: end2,
      url: 'https://grounding.example/redirect/2',
      title: 'guide.fr',
    },
  ];
};

describe('Google Search grounding, JSON', () => {
  test('grounding supports become annotations, byte offsets as code points', () => {
    const res = geminiJson(geminiResponse, request());
    const part = res.output[0].content[0];
    expect(part.text).toBe(G_TEXT_1 + G_TEXT_2);
    expect(part.annotations).toEqual(expectedGoogleAnnotations());
    expect(
      cpSlice(
        part.text,
        part.annotations[2].start_index,
        part.annotations[2].end_index
      )
    ).toBe(G_TEXT_2.trim());
  });

  test('each search query is a completed web_search_call after the message', () => {
    const res = geminiJson(geminiResponse, request(SOURCES));
    expect(res.output.map((o: any) => o.type)).toEqual([
      'message',
      'web_search_call',
      'web_search_call',
    ]);
    expect(res.output[1]).toMatchObject({
      status: 'completed',
      action: {
        type: 'search',
        query: 'café horaires',
        sources: [
          { type: 'url', url: 'https://grounding.example/redirect/1' },
          { type: 'url', url: 'https://grounding.example/redirect/2' },
        ],
      },
    });
    expect(res.output[2].action).toEqual({
      type: 'search',
      query: 'café adresse',
      sources: [],
    });
    expect(JSON.stringify(res)).not.toContain('renderedContent');
  });

  test('a segment whose offsets do not match is found by its text, or skipped', () => {
    const res = geminiJson(
      {
        ...geminiResponse,
        candidates: [
          {
            ...geminiResponse.candidates[0],
            groundingMetadata: {
              ...groundingMetadata,
              groundingSupports: [
                {
                  segment: {
                    startIndex: 99,
                    endIndex: 120,
                    text: 'ferme à 18h',
                  },
                  groundingChunkIndices: [0],
                },
                {
                  segment: { startIndex: 0, endIndex: 3, text: 'absent' },
                  groundingChunkIndices: [1],
                },
              ],
            },
          },
        ],
      },
      request()
    );
    const part = res.output[0].content[0];
    expect(part.annotations).toHaveLength(1);
    const a = part.annotations[0];
    expect(cpSlice(part.text, a.start_index, a.end_index)).toBe('ferme à 18h');
  });
});

describe('Google Search grounding, streamed', () => {
  const geminiChunks = [
    `data: ${JSON.stringify({
      modelVersion: MODEL,
      candidates: [
        { content: { role: 'model', parts: [{ text: G_TEXT_1 }] }, index: 0 },
      ],
    })}`,
    `data: ${JSON.stringify({
      modelVersion: MODEL,
      candidates: [
        {
          content: { role: 'model', parts: [{ text: G_TEXT_2 }] },
          finishReason: 'STOP',
          index: 0,
          groundingMetadata,
        },
      ],
      usageMetadata: geminiResponse.usageMetadata,
    })}`,
  ];
  const geminiToChat = (raw: string, state: any) =>
    GoogleChatCompleteStreamChunkTransform(raw, 'chat-1', state, false);
  const run = (req: any) =>
    streamThrough(geminiChunks, geminiToChat, req, 'google');

  test('annotations are added before the text is done, then the searches', () => {
    const { events } = run(request(SOURCES));
    const types = events.map((e) => e.type);
    expect(types).toEqual([
      'response.created',
      'response.in_progress',
      'response.output_item.added',
      'response.content_part.added',
      'response.output_text.delta',
      'response.output_text.delta',
      'response.output_text.annotation.added',
      'response.output_text.annotation.added',
      'response.output_text.annotation.added',
      'response.output_text.done',
      'response.content_part.done',
      'response.output_item.done',
      'response.output_item.added',
      'response.web_search_call.in_progress',
      'response.web_search_call.searching',
      'response.web_search_call.completed',
      'response.output_item.done',
      'response.output_item.added',
      'response.web_search_call.in_progress',
      'response.web_search_call.searching',
      'response.web_search_call.completed',
      'response.output_item.done',
      'response.completed',
    ]);
    expect(
      events
        .filter((e) => e.type === 'response.output_text.annotation.added')
        .map((e) => [e.annotation_index, e.annotation])
    ).toEqual(expectedGoogleAnnotations().map((a, i) => [i, a]));
  });

  test('the final snapshot matches the JSON response', () => {
    const { events } = run(request(SOURCES));
    const final = events[events.length - 1].response;
    const json = geminiJson(geminiResponse, request(SOURCES));
    const strip = (items: any[]) =>
      items.map(({ id: _id, ...rest }: any) => rest);
    expect(strip(final.output)).toEqual(strip(json.output));
  });

  test('grounding split across chunks is merged', () => {
    const [first, ...restSupports] = groundingMetadata.groundingSupports;
    const chunks = [
      `data: ${JSON.stringify({
        modelVersion: MODEL,
        candidates: [
          {
            content: { role: 'model', parts: [{ text: G_TEXT_1 }] },
            index: 0,
            groundingMetadata: {
              webSearchQueries: ['café horaires'],
              groundingChunks: [groundingMetadata.groundingChunks[0]],
              groundingSupports: [{ ...first, groundingChunkIndices: [0] }],
            },
          },
        ],
      })}`,
      `data: ${JSON.stringify({
        modelVersion: MODEL,
        candidates: [
          {
            content: { role: 'model', parts: [{ text: G_TEXT_2 }] },
            finishReason: 'STOP',
            index: 0,
            groundingMetadata: {
              webSearchQueries: ['café horaires'],
              groundingChunks: [groundingMetadata.groundingChunks[1]],
              groundingSupports: restSupports.map((s) => ({
                ...s,
                groundingChunkIndices: [0],
              })),
            },
          },
        ],
      })}`,
    ];
    const { events } = streamThrough(chunks, geminiToChat, request(), 'google');
    const final = events[events.length - 1].response;
    expect(final.output.map((o: any) => o.type)).toEqual([
      'message',
      'web_search_call',
    ]);
    expect(
      final.output[0].content[0].annotations.map((a: any) => a.url)
    ).toEqual([
      'https://grounding.example/redirect/1',
      'https://grounding.example/redirect/2',
    ]);
  });
});

describe('native Responses targets', () => {
  test('are not adapted, so their output passes through unchanged', () => {
    for (const provider of ['openai', 'azure-openai']) {
      expect(
        applyAdapterRequestTransform(
          'createModelResponse',
          provider,
          request(SOURCES) as any,
          request(SOURCES) as any,
          false,
          'POST'
        )
      ).toBeNull();
    }
  });
});
