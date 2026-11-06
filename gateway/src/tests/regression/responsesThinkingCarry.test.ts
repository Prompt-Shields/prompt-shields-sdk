/**
 * Regression tests (#73): a `/v1/responses` tool loop with extended thinking
 * against an Anthropic target, through the whole gateway app and offline.
 * The signed thinking returned in a reasoning item's encrypted_content comes
 * back on the next request, and never reaches a native Responses target.
 */
jest.mock('jose', () => ({}));

import { json, post, resetHarness, upstream, upstreamCalls } from './harness';
import app from '../../index';

beforeEach(() => resetHarness());

const MODEL = 'test-model';
const ANTHROPIC_URL = /api\.anthropic\.com\/v1\/messages$/;
const RESPONSES_URL = /api\.openai\.com\/v1\/responses$/;
const ANTHROPIC = {
  'x-portkey-provider': 'anthropic',
  authorization: 'Bearer sk-ant-regression',
};

const anthropicToolUse = () =>
  json({
    id: 'msg_1',
    type: 'message',
    role: 'assistant',
    model: MODEL,
    content: [
      { type: 'thinking', thinking: 'Use the tool.', signature: 'EqSig==' },
      { type: 'redacted_thinking', data: 'EqRedacted==' },
      { type: 'tool_use', id: 'toolu_1', name: 'lookup', input: { q: 'x' } },
    ],
    stop_reason: 'tool_use',
    usage: { input_tokens: 10, output_tokens: 5 },
  });

const anthropicText = () =>
  json({
    id: 'msg_2',
    type: 'message',
    role: 'assistant',
    model: MODEL,
    content: [{ type: 'text', text: 'It is y.' }],
    stop_reason: 'end_turn',
    usage: { input_tokens: 20, output_tokens: 3 },
  });

const request = (input: any[]) => ({
  model: MODEL,
  max_output_tokens: 256,
  reasoning: { effort: 'medium' },
  tools: [
    {
      type: 'function',
      name: 'lookup',
      parameters: { type: 'object', properties: { q: { type: 'string' } } },
    },
  ],
  input,
});

async function firstTurn(): Promise<any[]> {
  upstream(ANTHROPIC_URL, anthropicToolUse);
  const res = await post(
    app,
    '/v1/responses',
    request([{ role: 'user', content: 'Look up x' }]),
    ANTHROPIC
  );
  expect(res.status).toBe(200);
  const body: any = await res.json();
  return body.output;
}

describe('Responses adapter: signed thinking across a tool loop', () => {
  test('the second request sends the signed and redacted thinking back first', async () => {
    const output = await firstTurn();
    expect(output.map((o: any) => o.type)).toEqual([
      'reasoning',
      'reasoning',
      'function_call',
    ]);
    for (const rs of output.slice(0, 2)) {
      expect(rs.encrypted_content).toMatch(/^ps-thk1:anthropic:/);
      // Credentials are never part of the carried payload
      expect(rs.encrypted_content).not.toContain('sk-ant-regression');
      expect(
        Buffer.from(rs.encrypted_content.split(':')[2], 'base64').toString()
      ).not.toContain('sk-ant-regression');
    }

    resetHarness();
    upstream(ANTHROPIC_URL, anthropicText);
    const res = await post(
      app,
      '/v1/responses',
      request([
        { role: 'user', content: 'Look up x' },
        ...output,
        { type: 'function_call_output', call_id: 'toolu_1', output: 'y' },
      ]),
      ANTHROPIC
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].body.messages).toEqual([
      { role: 'user', content: 'Look up x' },
      {
        role: 'assistant',
        content: [
          { type: 'thinking', thinking: 'Use the tool.', signature: 'EqSig==' },
          { type: 'redacted_thinking', data: 'EqRedacted==' },
          {
            type: 'tool_use',
            id: 'toolu_1',
            name: 'lookup',
            input: { q: 'x' },
          },
        ],
      },
      {
        role: 'user',
        content: [
          { type: 'tool_result', tool_use_id: 'toolu_1', content: 'y' },
        ],
      },
    ]);
  });

  test('a native Responses fallback target never receives the carried items', async () => {
    const output = await firstTurn();

    resetHarness();
    upstream(ANTHROPIC_URL, () =>
      json({ type: 'error', error: { type: 'overloaded_error' } }, 529)
    );
    upstream(RESPONSES_URL, () =>
      json({
        id: 'resp_1',
        object: 'response',
        status: 'completed',
        model: MODEL,
        output: [],
        usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 },
      })
    );
    const config = {
      strategy: { mode: 'fallback' },
      targets: [
        { provider: 'anthropic', api_key: 'sk-ant-regression' },
        { provider: 'openai', api_key: 'sk-regression' },
      ],
    };
    const res = await post(
      app,
      '/v1/responses',
      request([
        { role: 'user', content: 'Look up x' },
        ...output,
        { type: 'function_call_output', call_id: 'toolu_1', output: 'y' },
      ]),
      { 'x-portkey-config': JSON.stringify(config) }
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(2);
    // The Anthropic attempt had the thinking ...
    expect(upstreamCalls[0].body.messages[1].content[0].type).toBe('thinking');
    // ... the native OpenAI attempt gets the history without it
    expect(
      upstreamCalls[1].body.input.map((i: any) => i.type ?? i.role)
    ).toEqual(['user', 'function_call', 'function_call_output']);
  });
});
