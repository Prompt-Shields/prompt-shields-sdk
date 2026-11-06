/**
 * Recorded upstream fixtures for the adapter conformance suite (#73).
 *
 * Each scenario is served in the provider's own wire format, JSON and SSE:
 * - OpenAI-compatible Chat Completions (`openai`, `openrouter`): chunk
 *   shapes from the OpenAI streaming reference, with the trailing usage
 *   chunk that `stream_options.include_usage` adds (empty `choices`), and
 *   OpenRouter's `reasoning` field for reasoning models.
 * - Anthropic Messages: the event sequences of the Anthropic streaming docs
 *   (thinking + signature, text, tool_use with `input_json_delta`), with
 *   cache usage in `message_start` and output usage in `message_delta`.
 * - Gemini `streamGenerateContent?alt=sse`: `data:` frames separated by
 *   CRLF CRLF as Google sends them, `thought: true` parts for thinking,
 *   `functionCall` parts, and `usageMetadata` with cached and thought
 *   token counts. Gemini reports `finishReason: STOP` even for tool calls.
 *
 * Every scenario has the same meaning across providers so the suite can
 * assert one expected client-facing result per scenario.
 */

export type Upstream = 'openai' | 'anthropic' | 'gemini';
export type Scenario =
  | 'text'
  | 'tool'
  | 'parallel_tools'
  | 'thinking'
  | 'max_tokens';

/** Expected token counts, the same for every provider. */
export const USAGE = {
  /** Whole prompt including cache reads and writes. */
  prompt: 150,
  cacheRead: 100,
  output: 20,
};

export const TEXT = 'Hello there';
export const THINKING = 'Let me think.';
export const TOOL = {
  name: 'get_weather',
  args: { city: 'Paris' },
  preamble: 'Checking.',
};

const openaiUsage = {
  prompt_tokens: USAGE.prompt,
  completion_tokens: USAGE.output,
  total_tokens: USAGE.prompt + USAGE.output,
  prompt_tokens_details: { cached_tokens: USAGE.cacheRead },
  completion_tokens_details: { reasoning_tokens: 0 },
};

// Anthropic: input_tokens excludes cache reads (no cache writes here).
const anthropicUsage = {
  input_tokens: USAGE.prompt - USAGE.cacheRead,
  cache_creation_input_tokens: 0,
  cache_read_input_tokens: USAGE.cacheRead,
  output_tokens: USAGE.output,
};

const geminiUsage = (thoughts = 0) => ({
  promptTokenCount: USAGE.prompt,
  candidatesTokenCount: USAGE.output - thoughts,
  thoughtsTokenCount: thoughts || undefined,
  cachedContentTokenCount: USAGE.cacheRead,
  totalTokenCount: USAGE.prompt + USAGE.output,
});

// ── OpenAI-compatible Chat Completions ──────────────────────────────────────

function chatChunk(delta: object, finish_reason: string | null = null) {
  return {
    id: 'chatcmpl-conf',
    object: 'chat.completion.chunk',
    created: 1730000000,
    model: 'conf-model',
    choices: [{ index: 0, delta, finish_reason }],
  };
}

function openaiStream(chunks: object[]): string {
  const usageChunk = { ...chatChunk({}), choices: [], usage: openaiUsage };
  return (
    [...chunks, usageChunk]
      .map((c) => `data: ${JSON.stringify(c)}\n\n`)
      .join('') + 'data: [DONE]\n\n'
  );
}

function openaiFixture(s: Scenario): { json: object; sse: string } {
  const json = (message: object, finish_reason: string) => ({
    id: 'chatcmpl-conf',
    object: 'chat.completion',
    created: 1730000000,
    model: 'conf-model',
    choices: [{ index: 0, message, finish_reason }],
    usage: openaiUsage,
  });
  switch (s) {
    case 'text':
      return {
        json: json({ role: 'assistant', content: TEXT }, 'stop'),
        sse: openaiStream([
          chatChunk({ role: 'assistant', content: '' }),
          chatChunk({ content: 'Hello' }),
          chatChunk({ content: ' there' }),
          chatChunk({}, 'stop'),
        ]),
      };
    case 'max_tokens':
      return {
        json: json({ role: 'assistant', content: TEXT }, 'length'),
        sse: openaiStream([
          chatChunk({ role: 'assistant', content: TEXT }),
          chatChunk({}, 'length'),
        ]),
      };
    case 'thinking':
      // OpenRouter-style `reasoning` alongside content.
      return {
        json: json(
          { role: 'assistant', content: TEXT, reasoning: THINKING },
          'stop'
        ),
        sse: openaiStream([
          chatChunk({ role: 'assistant', content: '', reasoning: 'Let me ' }),
          chatChunk({ reasoning: 'think.' }),
          chatChunk({ content: TEXT }),
          chatChunk({}, 'stop'),
        ]),
      };
    case 'tool':
      return {
        json: json(
          {
            role: 'assistant',
            content: TOOL.preamble,
            tool_calls: [
              {
                id: 'call_conf1',
                type: 'function',
                function: {
                  name: TOOL.name,
                  arguments: JSON.stringify(TOOL.args),
                },
              },
            ],
          },
          'tool_calls'
        ),
        sse: openaiStream([
          chatChunk({ role: 'assistant', content: TOOL.preamble }),
          chatChunk({
            tool_calls: [
              {
                index: 0,
                id: 'call_conf1',
                type: 'function',
                function: { name: TOOL.name, arguments: '' },
              },
            ],
          }),
          chatChunk({
            tool_calls: [{ index: 0, function: { arguments: '{"city":' } }],
          }),
          chatChunk({
            tool_calls: [{ index: 0, function: { arguments: '"Paris"}' } }],
          }),
          chatChunk({}, 'tool_calls'),
        ]),
      };
    case 'parallel_tools':
      // Two calls, no text; arguments of the two calls interleave.
      return {
        json: json(
          {
            role: 'assistant',
            content: null,
            tool_calls: PARALLEL_CITIES.map((city, i) => ({
              id: `call_par${i}`,
              type: 'function',
              function: { name: TOOL.name, arguments: `{"city":"${city}"}` },
            })),
          },
          'tool_calls'
        ),
        sse: openaiStream([
          chatChunk({ role: 'assistant', content: null }),
          ...PARALLEL_CITIES.map((_, i) =>
            chatChunk({
              tool_calls: [
                {
                  index: i,
                  id: `call_par${i}`,
                  type: 'function',
                  function: { name: TOOL.name, arguments: '' },
                },
              ],
            })
          ),
          ...PARALLEL_CITIES.map((_, i) =>
            chatChunk({
              tool_calls: [{ index: i, function: { arguments: '{"city":' } }],
            })
          ),
          ...PARALLEL_CITIES.map((city, i) =>
            chatChunk({
              tool_calls: [{ index: i, function: { arguments: `"${city}"}` } }],
            })
          ),
          chatChunk({}, 'tool_calls'),
        ]),
      };
  }
}

export const PARALLEL_CITIES = ['Paris', 'Rome'];

// ── Anthropic Messages ──────────────────────────────────────────────────────

const anthropicEvent = (type: string, data: object) =>
  `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`;

function anthropicFixture(s: Scenario): { json: object; sse: string } {
  const blocks: any[] = [];
  let stop = 'end_turn';
  if (s === 'thinking') {
    blocks.push({ type: 'thinking', thinking: THINKING, signature: 'EqQBCg' });
  }
  if (s === 'tool') {
    blocks.push({ type: 'text', text: TOOL.preamble });
    blocks.push({
      type: 'tool_use',
      id: 'toolu_conf1',
      name: TOOL.name,
      input: TOOL.args,
    });
    stop = 'tool_use';
  } else if (s === 'parallel_tools') {
    PARALLEL_CITIES.forEach((city, i) =>
      blocks.push({
        type: 'tool_use',
        id: `toolu_par${i}`,
        name: TOOL.name,
        input: { city },
      })
    );
    stop = 'tool_use';
  } else {
    blocks.push({ type: 'text', text: TEXT });
  }
  if (s === 'max_tokens') stop = 'max_tokens';

  const json = {
    id: 'msg_conf',
    type: 'message',
    role: 'assistant',
    model: 'claude-sonnet-4-5',
    content: blocks,
    stop_reason: stop,
    stop_sequence: null,
    usage: anthropicUsage,
  };

  let sse = anthropicEvent('message_start', {
    message: {
      ...json,
      content: [],
      stop_reason: null,
      usage: { ...anthropicUsage, output_tokens: 1 },
    },
  });
  sse += anthropicEvent('ping', {});
  blocks.forEach((b, index) => {
    if (b.type === 'thinking') {
      sse += anthropicEvent('content_block_start', {
        index,
        content_block: { type: 'thinking', thinking: '', signature: '' },
      });
      sse += anthropicEvent('content_block_delta', {
        index,
        delta: { type: 'thinking_delta', thinking: 'Let me ' },
      });
      sse += anthropicEvent('content_block_delta', {
        index,
        delta: { type: 'thinking_delta', thinking: 'think.' },
      });
      sse += anthropicEvent('content_block_delta', {
        index,
        delta: { type: 'signature_delta', signature: b.signature },
      });
    } else if (b.type === 'text') {
      sse += anthropicEvent('content_block_start', {
        index,
        content_block: { type: 'text', text: '' },
      });
      sse += anthropicEvent('content_block_delta', {
        index,
        delta: { type: 'text_delta', text: b.text },
      });
    } else {
      sse += anthropicEvent('content_block_start', {
        index,
        content_block: { type: 'tool_use', id: b.id, name: b.name, input: {} },
      });
      sse += anthropicEvent('content_block_delta', {
        index,
        delta: { type: 'input_json_delta', partial_json: '{"city":' },
      });
      sse += anthropicEvent('content_block_delta', {
        index,
        delta: { type: 'input_json_delta', partial_json: `"${b.input.city}"}` },
      });
    }
    sse += anthropicEvent('content_block_stop', { index });
  });
  sse += anthropicEvent('message_delta', {
    delta: { stop_reason: stop, stop_sequence: null },
    usage: { output_tokens: USAGE.output },
  });
  sse += anthropicEvent('message_stop', {});
  return { json, sse };
}

// ── Gemini ──────────────────────────────────────────────────────────────────

function geminiChunk(parts: object[], finishReason?: string, usage?: object) {
  return {
    candidates: [
      {
        content: { parts, role: 'model' },
        index: 0,
        ...(finishReason && { finishReason }),
      },
    ],
    ...(usage && { usageMetadata: usage }),
    modelVersion: 'gemini-2.5-flash',
    responseId: 'gem-conf',
  };
}

function geminiFixture(s: Scenario): { json: object; sse: string } {
  const thoughts = s === 'thinking' ? 5 : 0;
  const finish = s === 'max_tokens' ? 'MAX_TOKENS' : 'STOP';
  let streamParts: object[][];
  let parts: object[];
  if (s === 'tool') {
    const call = { functionCall: { name: TOOL.name, args: TOOL.args } };
    parts = [{ text: TOOL.preamble }, call];
    streamParts = [[{ text: TOOL.preamble }], [call]];
  } else if (s === 'parallel_tools') {
    // Gemini sends parallel calls as parts of one chunk.
    parts = PARALLEL_CITIES.map((city) => ({
      functionCall: { name: TOOL.name, args: { city } },
    }));
    streamParts = [parts];
  } else if (s === 'thinking') {
    parts = [{ text: THINKING, thought: true }, { text: TEXT }];
    streamParts = [
      [{ text: 'Let me ', thought: true }],
      [{ text: 'think.', thought: true }],
      [{ text: TEXT }],
    ];
  } else {
    parts = [{ text: TEXT }];
    streamParts = [[{ text: 'Hello' }], [{ text: ' there' }]];
  }
  const json = geminiChunk(parts, finish, geminiUsage(thoughts));
  const chunks = streamParts.map((p, i) =>
    i === streamParts.length - 1
      ? geminiChunk(p, finish, geminiUsage(thoughts))
      : geminiChunk(p, undefined, {
          promptTokenCount: USAGE.prompt,
          totalTokenCount: USAGE.prompt,
        })
  );
  const sse = chunks.map((c) => `data: ${JSON.stringify(c)}\r\n\r\n`).join('');
  return { json, sse };
}

export function fixture(u: Upstream, s: Scenario) {
  if (u === 'openai') return openaiFixture(s);
  if (u === 'anthropic') return anthropicFixture(s);
  return geminiFixture(s);
}
