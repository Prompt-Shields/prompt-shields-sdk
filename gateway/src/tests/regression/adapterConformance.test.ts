/**
 * Adapter conformance suite (#73): `/v1/messages` and `/v1/responses`
 * through the whole gateway app, offline, against stub OpenAI-compatible,
 * Anthropic and Gemini upstreams.
 *
 * Every adapted response is checked against the client contracts in
 * ./conformance/clientContracts.ts (Anthropic SDK / Claude Code stream
 * accumulator, openai SDK ResponseStream accumulator and Codex `.done`
 * events, golden JSON key sets; provenance is documented there). Upstream
 * wire formats are recorded in ./conformance/upstreams.ts.
 */
jest.mock('jose', () => ({}));

import {
  json,
  post,
  resetHarness,
  sse,
  upstream,
  upstreamCalls,
} from './harness';
import app from '../../index';
import {
  checkAnthropicMessage,
  checkResponse,
  parseSSE,
  replayAnthropicStream,
  replayResponsesStream,
} from './conformance/clientContracts';
import {
  fixture,
  PARALLEL_CITIES,
  Scenario,
  TEXT,
  THINKING,
  TOOL,
  Upstream,
  USAGE,
} from './conformance/upstreams';

beforeEach(() => resetHarness());

interface Target {
  wire: Upstream;
  model: string;
  headers: Record<string, string>;
  url: RegExp;
  /** Scenarios this upstream can express. */
  scenarios: Scenario[];
}

const ALL: Scenario[] = [
  'text',
  'tool',
  'parallel_tools',
  'thinking',
  'max_tokens',
];

const TARGETS: Record<string, Target> = {
  openai: {
    wire: 'openai',
    model: 'gpt-4o',
    headers: {
      'x-portkey-provider': 'openai',
      authorization: 'Bearer sk-conf',
    },
    url: /api\.openai\.com\/v1\/chat\/completions$/,
    scenarios: ['text', 'tool', 'parallel_tools', 'max_tokens'],
  },
  openrouter: {
    wire: 'openai',
    model: 'deepseek/deepseek-r1',
    headers: {
      'x-portkey-provider': 'openrouter',
      authorization: 'Bearer or-conf',
    },
    url: /openrouter\.ai\/api\/v1\/chat\/completions$/,
    scenarios: ALL,
  },
  anthropic: {
    wire: 'anthropic',
    model: 'claude-sonnet-4-5',
    headers: {
      'x-portkey-provider': 'anthropic',
      'x-api-key': 'sk-ant-conf',
    },
    url: /api\.anthropic\.com\/v1\/messages$/,
    scenarios: ALL,
  },
  gemini: {
    wire: 'gemini',
    model: 'gemini-2.5-flash',
    headers: {
      'x-portkey-provider': 'google',
      authorization: 'Bearer goog-conf',
    },
    url: /generativelanguage\.googleapis\.com\/v1beta\/models\/gemini-2\.5-flash:(stream)?[gG]enerateContent/,
    scenarios: ALL,
  },
};

function serve(t: Target, s: Scenario, stream: boolean) {
  const f = fixture(t.wire, s);
  upstream(t.url, () => (stream ? sse(f.sse) : json(f.json)));
}

const WEATHER_TOOL_MESSAGES = {
  name: TOOL.name,
  description: 'Weather for a city',
  input_schema: {
    type: 'object',
    properties: { city: { type: 'string' } },
    required: ['city'],
  },
};

const WEATHER_TOOL_RESPONSES = {
  type: 'function',
  name: TOOL.name,
  description: 'Weather for a city',
  parameters: WEATHER_TOOL_MESSAGES.input_schema,
};

function messagesRequest(t: Target, s: Scenario, stream: boolean) {
  return {
    model: t.model,
    max_tokens: 1024,
    stream,
    messages: [{ role: 'user', content: 'Weather in Paris?' }],
    ...(s.includes('tool') && { tools: [WEATHER_TOOL_MESSAGES] }),
    ...(s === 'thinking' && {
      thinking: { type: 'enabled', budget_tokens: 1024 },
    }),
  };
}

function responsesRequest(t: Target, s: Scenario, stream: boolean) {
  return {
    model: t.model,
    stream,
    input: 'Weather in Paris?',
    ...(s.includes('tool') && { tools: [WEATHER_TOOL_RESPONSES] }),
    ...(s === 'thinking' && {
      reasoning: { effort: 'low', summary: 'auto' },
    }),
  };
}

// ── Expected client-facing results, one per scenario ────────────────────────

function expectMessagesResult(msg: any, s: Scenario) {
  const types = msg.content.map((b: any) => b.type);
  switch (s) {
    case 'text':
      expect(types).toEqual(['text']);
      expect(msg.content[0].text).toBe(TEXT);
      expect(msg.stop_reason).toBe('end_turn');
      break;
    case 'max_tokens':
      expect(types).toEqual(['text']);
      expect(msg.stop_reason).toBe('max_tokens');
      break;
    case 'thinking':
      expect(types).toEqual(['thinking', 'text']);
      expect(msg.content[0].thinking).toBe(THINKING);
      expect(msg.content[1].text).toBe(TEXT);
      expect(msg.stop_reason).toBe('end_turn');
      break;
    case 'tool':
      expect(types).toEqual(['text', 'tool_use']);
      expect(msg.content[0].text).toBe(TOOL.preamble);
      expect(msg.content[1]).toMatchObject({
        name: TOOL.name,
        input: TOOL.args,
      });
      expect(msg.content[1].id).toMatch(/\S/);
      expect(msg.stop_reason).toBe('tool_use');
      break;
    case 'parallel_tools':
      expect(types).toEqual(['tool_use', 'tool_use']);
      expect(msg.content.map((b: any) => b.input.city)).toEqual(
        PARALLEL_CITIES
      );
      expect(new Set(msg.content.map((b: any) => b.id)).size).toBe(2);
      expect(msg.stop_reason).toBe('tool_use');
      break;
  }
  // Anthropic usage: input_tokens excludes cache reads, reported apart.
  expect(msg.usage).toMatchObject({
    input_tokens: USAGE.prompt - USAGE.cacheRead,
    output_tokens: USAGE.output,
    cache_read_input_tokens: USAGE.cacheRead,
  });
}

function expectResponsesResult(r: any, s: Scenario) {
  const types = r.output.map((i: any) => i.type);
  const message = r.output.find((i: any) => i.type === 'message');
  switch (s) {
    case 'text':
      expect(types).toEqual(['message']);
      expect(message.content[0].text).toBe(TEXT);
      expect(r.status).toBe('completed');
      break;
    case 'max_tokens':
      expect(types).toEqual(['message']);
      expect(r.status).toBe('incomplete');
      expect(r.incomplete_details).toEqual({ reason: 'max_output_tokens' });
      break;
    case 'thinking':
      expect(types).toEqual(['reasoning', 'message']);
      expect(r.output[0].summary.map((p: any) => p.text).join('')).toBe(
        THINKING
      );
      expect(message.content[0].text).toBe(TEXT);
      expect(r.status).toBe('completed');
      break;
    case 'tool': {
      expect(types).toEqual(['message', 'function_call']);
      expect(message.content[0].text).toBe(TOOL.preamble);
      const fc = r.output[1];
      expect(fc.name).toBe(TOOL.name);
      expect(JSON.parse(fc.arguments)).toEqual(TOOL.args);
      expect(fc.call_id).toMatch(/\S/);
      expect(r.status).toBe('completed');
      break;
    }
    case 'parallel_tools':
      // No text: no (empty) message item, as with the native API.
      expect(types).toEqual(['function_call', 'function_call']);
      expect(r.output.map((i: any) => JSON.parse(i.arguments).city)).toEqual(
        PARALLEL_CITIES
      );
      expect(new Set(r.output.map((i: any) => i.call_id)).size).toBe(2);
      expect(r.status).toBe('completed');
      break;
  }
  // Responses usage: input_tokens includes cached tokens, broken out.
  expect(r.usage.input_tokens).toBe(USAGE.prompt);
  expect(r.usage.output_tokens).toBe(USAGE.output);
  expect(r.usage.input_tokens_details.cached_tokens).toBe(USAGE.cacheRead);
}

const cases = (endpoint: 'messages' | 'responses') =>
  Object.entries(TARGETS)
    // Anthropic serves /v1/messages and OpenAI /v1/responses natively.
    .filter(([name]) =>
      endpoint === 'messages' ? name !== 'anthropic' : name !== 'openai'
    )
    .flatMap(([name, t]) =>
      t.scenarios.flatMap((s) =>
        [false, true].map(
          (stream) => [name, s, stream ? 'stream' : 'json', t] as const
        )
      )
    );

describe('/v1/messages conformance (Claude Code shape)', () => {
  test.each(cases('messages'))('%s %s %s', async (_name, s, mode, t) => {
    const stream = mode === 'stream';
    serve(t, s, stream);
    const res = await post(
      app,
      '/v1/messages',
      messagesRequest(t, s, stream),
      t.headers
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(1);
    let msg: any;
    if (stream) {
      expect(res.headers.get('content-type')).toContain('text/event-stream');
      msg = replayAnthropicStream(parseSSE(await res.text()));
    } else {
      msg = await res.json();
      checkAnthropicMessage(msg);
    }
    expectMessagesResult(msg, s);
  });
});

describe('/v1/responses conformance (OpenAI SDK / Codex shape)', () => {
  test.each(cases('responses'))('%s %s %s', async (_name, s, mode, t) => {
    const stream = mode === 'stream';
    serve(t, s, stream);
    const res = await post(
      app,
      '/v1/responses',
      responsesRequest(t, s, stream),
      t.headers
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(1);
    let r: any;
    if (stream) {
      expect(res.headers.get('content-type')).toContain('text/event-stream');
      r = replayResponsesStream(parseSSE(await res.text()));
    } else {
      r = await res.json();
      checkResponse(r);
    }
    expectResponsesResult(r, s);
  });
});

// ── Request side: multi-turn tool round trip ────────────────────────────────

interface Turn {
  role: 'user' | 'assistant';
  text: string;
  calls: { id?: string; name: string; args: any }[];
  results: { id?: string; content: string }[];
}

const partsText = (c: any): string =>
  typeof c === 'string'
    ? c
    : Array.isArray(c)
      ? c.map((p: any) => p?.text ?? '').join('')
      : '';

/**
 * The upstream request, normalised to turns. Checks each provider's own
 * rule for tool results: OpenAI needs a `tool` message per call right after
 * the assistant's `tool_calls`; Anthropic needs every `tool_result` in the
 * user message right after the assistant's `tool_use` blocks; Gemini needs
 * as many `functionResponse` parts as the model turn had `functionCall`
 * parts, in the next content.
 */
function upstreamTurns(wire: Upstream, body: any): Turn[] {
  const turns: Turn[] = [];
  const turn = (role: 'user' | 'assistant') => {
    const last = turns[turns.length - 1];
    if (last?.role === role) return last;
    const t: Turn = { role, text: '', calls: [], results: [] };
    turns.push(t);
    return t;
  };
  if (wire === 'openai') {
    for (const m of body.messages) {
      if (m.role === 'system') continue;
      const t = turn(m.role === 'tool' ? 'user' : m.role);
      if (m.role === 'tool') {
        t.results.push({ id: m.tool_call_id, content: partsText(m.content) });
        continue;
      }
      t.text += partsText(m.content);
      for (const tc of m.tool_calls ?? []) {
        t.calls.push({
          id: tc.id,
          name: tc.function.name,
          args: JSON.parse(tc.function.arguments),
        });
      }
    }
    // The tool messages must directly follow the assistant tool_calls.
    body.messages.forEach((m: any, i: number) => {
      const ids = (m.tool_calls ?? []).map((tc: any) => tc.id);
      if (!ids.length) return;
      const next = body.messages.slice(i + 1, i + 1 + ids.length);
      expect(next.map((n: any) => [n.role, n.tool_call_id])).toEqual(
        ids.map((id: string) => ['tool', id])
      );
    });
  } else if (wire === 'anthropic') {
    for (const m of body.messages) {
      const t = turn(m.role);
      const blocks =
        typeof m.content === 'string'
          ? [{ type: 'text', text: m.content }]
          : m.content;
      for (const b of blocks) {
        if (b.type === 'text') t.text += b.text;
        if (b.type === 'tool_use') {
          t.calls.push({ id: b.id, name: b.name, args: b.input });
        }
        if (b.type === 'tool_result') {
          t.results.push({ id: b.tool_use_id, content: partsText(b.content) });
        }
      }
    }
    // The API combines consecutive same-role messages into one turn
    // (Messages API reference), so check on the combined turns.
    turns.forEach((t, i) => {
      if (!t.calls.length) return;
      expect(turns[i + 1]?.results.map((r) => r.id)).toEqual(
        t.calls.map((c) => c.id)
      );
    });
  } else {
    for (const c of body.contents) {
      const t = turn(c.role === 'model' ? 'assistant' : 'user');
      for (const p of c.parts) {
        if (typeof p.text === 'string' && !p.thought) t.text += p.text;
        if (p.functionCall) {
          t.calls.push({
            name: p.functionCall.name,
            args: p.functionCall.args,
          });
        }
        if (p.functionResponse) {
          const r = p.functionResponse.response;
          t.results.push({
            content:
              typeof r === 'string'
                ? r
                : typeof r?.content === 'string'
                  ? r.content
                  : r?.output ?? JSON.stringify(r),
          });
        }
      }
    }
    // Gemini rejects a function turn whose part count differs.
    body.contents.forEach((c: any, i: number) => {
      const calls = c.parts.filter((p: any) => p.functionCall).length;
      if (!calls) return;
      const next = body.contents[i + 1];
      expect(next.parts.filter((p: any) => p.functionResponse)).toHaveLength(
        calls
      );
    });
  }
  return turns;
}

function expectRoundTrip(wire: Upstream, body: any) {
  const turns = upstreamTurns(wire, body);
  expect(turns.map((t) => t.role)).toEqual(['user', 'assistant', 'user']);
  expect(turns[0].text).toBe('Weather in Paris and Rome?');
  expect(turns[1].text).toBe('Checking both.');
  expect(turns[1].calls.map((c) => [c.name, c.args.city])).toEqual(
    PARALLEL_CITIES.map((city) => [TOOL.name, city])
  );
  expect(turns[2].results.map((r) => r.content)).toEqual(['18C', '21C']);
  expect(turns[2].text).toBe('Which is warmer?');
}

const roundTripTargets = (endpoint: 'messages' | 'responses') =>
  Object.entries(TARGETS).filter(([name]) =>
    endpoint === 'messages' ? name !== 'anthropic' : name !== 'openai'
  );

describe('tool round trip reaches each upstream intact', () => {
  test.each(roundTripTargets('messages'))(
    '/v1/messages → %s',
    async (_name, t) => {
      serve(t, 'text', false);
      const res = await post(
        app,
        '/v1/messages',
        {
          model: t.model,
          max_tokens: 1024,
          system: [
            {
              type: 'text',
              text: 'You are a coding agent.',
              cache_control: { type: 'ephemeral' },
            },
          ],
          tools: [WEATHER_TOOL_MESSAGES],
          messages: [
            { role: 'user', content: 'Weather in Paris and Rome?' },
            {
              role: 'assistant',
              content: [
                { type: 'thinking', thinking: 'Two calls.', signature: 'c2ln' },
                { type: 'text', text: 'Checking both.' },
                ...PARALLEL_CITIES.map((city, i) => ({
                  type: 'tool_use',
                  id: `toolu_rt${i}`,
                  name: TOOL.name,
                  input: { city },
                })),
              ],
            },
            {
              role: 'user',
              content: [
                {
                  type: 'tool_result',
                  tool_use_id: 'toolu_rt0',
                  content: [{ type: 'text', text: '18C' }],
                },
                {
                  type: 'tool_result',
                  tool_use_id: 'toolu_rt1',
                  content: '21C',
                },
                { type: 'text', text: 'Which is warmer?' },
              ],
            },
          ],
        },
        t.headers
      );
      expect(res.status).toBe(200);
      expectRoundTrip(t.wire, upstreamCalls[0].body);
    }
  );

  test.each(roundTripTargets('responses'))(
    '/v1/responses → %s',
    async (_name, t) => {
      serve(t, 'text', false);
      const res = await post(
        app,
        '/v1/responses',
        {
          model: t.model,
          instructions: 'You are a coding agent.',
          tools: [WEATHER_TOOL_RESPONSES],
          store: false,
          input: [
            {
              type: 'message',
              role: 'user',
              content: [
                { type: 'input_text', text: 'Weather in Paris and Rome?' },
              ],
            },
            {
              type: 'reasoning',
              id: 'rs_rt',
              summary: [{ type: 'summary_text', text: 'Two calls.' }],
            },
            {
              type: 'message',
              role: 'assistant',
              content: [{ type: 'output_text', text: 'Checking both.' }],
            },
            ...PARALLEL_CITIES.map((city, i) => ({
              type: 'function_call',
              call_id: `call_rt${i}`,
              name: TOOL.name,
              arguments: JSON.stringify({ city }),
            })),
            {
              type: 'function_call_output',
              call_id: 'call_rt0',
              output: '18C',
            },
            {
              type: 'function_call_output',
              call_id: 'call_rt1',
              output: [{ type: 'input_text', text: '21C' }],
            },
            {
              type: 'message',
              role: 'user',
              content: [{ type: 'input_text', text: 'Which is warmer?' }],
            },
          ],
        },
        t.headers
      );
      expect(res.status).toBe(200);
      expectRoundTrip(t.wire, upstreamCalls[0].body);
    }
  );
});
