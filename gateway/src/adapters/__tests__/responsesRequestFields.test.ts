/**
 * PS fork (#73): Responses request fields without a direct Chat Completions
 * counterpart are mapped for the target provider or refused, never silently
 * dropped (requestFields.ts).
 */
// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../../utils/env', () => ({ Environment: () => ({}) }));

import { transformResponsesToChatCompletions } from '../responses/requestTransform';
import {
  ANTHROPIC_WEB_SEARCH_TOOL,
  ResponsesAdapterRequestError,
} from '../responses/requestFields';
import {
  AnthropicChatCompleteConfig,
  getAnthropicStreamChunkTransform,
} from '../../providers/anthropic/chatComplete';
import { GoogleChatCompleteConfig } from '../../providers/google/chatComplete';
import { applyAdapterRequestTransform } from '../../handlers/adapterUtils';
import { transformUsingProviderConfig } from '../testUtils';

const MODEL = 'test-model';
const fnTool = (name: string) => ({
  type: 'function',
  name,
  description: `${name} tool`,
  parameters: { type: 'object', properties: {} },
});

function refusal(req: any, provider: string): ResponsesAdapterRequestError {
  try {
    transformResponsesToChatCompletions(
      { model: MODEL, input: 'hi', ...req },
      provider
    );
  } catch (err) {
    expect(err).toBeInstanceOf(ResponsesAdapterRequestError);
    return err as ResponsesAdapterRequestError;
  }
  throw new Error('expected a ResponsesAdapterRequestError');
}

describe('web_search tools', () => {
  test('map to the Anthropic web_search server tool', () => {
    const chat: any = transformResponsesToChatCompletions(
      {
        model: MODEL,
        input: 'news?',
        tools: [
          fnTool('lookup'),
          {
            type: 'web_search',
            filters: { allowed_domains: ['example.com'] },
            user_location: {
              type: 'approximate',
              city: 'Berlin',
              country: 'DE',
              timezone: 'Europe/Berlin',
            },
            search_context_size: 'high',
          },
        ],
      },
      'anthropic'
    );
    const anthropic: any = transformUsingProviderConfig(
      AnthropicChatCompleteConfig,
      chat
    );
    expect(anthropic.tools).toHaveLength(2);
    expect(anthropic.tools[0].name).toBe('lookup');
    expect(anthropic.tools[1]).toEqual({
      type: ANTHROPIC_WEB_SEARCH_TOOL,
      name: 'web_search',
      allowed_domains: ['example.com'],
      user_location: {
        type: 'approximate',
        city: 'Berlin',
        country: 'DE',
        timezone: 'Europe/Berlin',
      },
    });
  });

  test.each(['web_search_preview', 'web_search_2025_08_26'])(
    '%s maps for Claude Platform on AWS too',
    (type) => {
      const chat: any = transformResponsesToChatCompletions(
        { model: MODEL, input: 'hi', tools: [{ type }] },
        'claude-platform-aws'
      );
      expect(chat.tools).toEqual([
        { type: 'web_search', web_search: { name: ANTHROPIC_WEB_SEARCH_TOOL } },
      ]);
    }
  );

  test('map to Google Search grounding on Gemini', () => {
    const chat: any = transformResponsesToChatCompletions(
      {
        model: MODEL,
        input: 'hi',
        tools: [{ type: 'web_search_preview', search_context_size: 'low' }],
      },
      'google'
    );
    const gemini: any = transformUsingProviderConfig(
      GoogleChatCompleteConfig,
      chat
    );
    expect(gemini.tools).toEqual([{ googleSearch: {} }]);
  });

  test('Vertex AI: Gemini models map, other publishers are refused', () => {
    const chat: any = transformResponsesToChatCompletions(
      { model: 'gemini-pro', input: 'hi', tools: [{ type: 'web_search' }] },
      'vertex-ai'
    );
    expect(chat.tools[0].function.name).toBe('google_search');
    const err = refusal(
      { model: 'anthropic.some-model', tools: [{ type: 'web_search' }] },
      'vertex-ai'
    );
    expect(err.param).toBe('tools[0].type');
  });

  test('allowed_domains cannot be honoured on Gemini', () => {
    const err = refusal(
      {
        tools: [{ type: 'web_search', filters: { allowed_domains: ['a.io'] } }],
      },
      'google'
    );
    expect(err.param).toBe('tools[0].filters.allowed_domains');
  });

  test.each(['bedrock', 'mistral-ai', 'cohere'])(
    'are refused on %s',
    (provider) => {
      const err = refusal({ tools: [{ type: 'web_search' }] }, provider);
      expect(err.param).toBe('tools[0].type');
      expect(err.message).toContain(`'web_search'`);
      expect(err.message).toContain(provider);
    }
  );
});

describe('other built-in tools', () => {
  test.each([
    { type: 'file_search', vector_store_ids: ['vs_1'] },
    { type: 'custom', name: 'grammar', format: { type: 'text' } },
    { type: 'code_interpreter', container: { type: 'auto' } },
    { type: 'image_generation' },
    { type: 'computer_use_preview' },
  ])('$type is refused, naming the tool type', (tool) => {
    const err = refusal({ tools: [fnTool('lookup'), tool] }, 'anthropic');
    expect(err.param).toBe('tools[1].type');
    expect(err.message).toContain(`'${tool.type}'`);
  });

  test('an mcp tool is refused without echoing its credentials', () => {
    const err = refusal(
      {
        tools: [
          {
            type: 'mcp',
            server_label: 'docs',
            server_url: 'https://mcp.example.com',
            headers: { Authorization: 'Bearer secret-token' },
            authorization: 'secret-token',
          },
        ],
      },
      'anthropic'
    );
    expect(err.message).not.toContain('secret-token');
    expect(err.message).not.toContain('mcp.example.com');
  });
});

describe('tool_choice', () => {
  test('allowed_tools sends only the allowed tools with the mode', () => {
    const chat: any = transformResponsesToChatCompletions(
      {
        model: MODEL,
        input: 'hi',
        tools: [fnTool('a'), fnTool('b'), { type: 'web_search' }],
        tool_choice: {
          type: 'allowed_tools',
          mode: 'required',
          tools: [{ type: 'function', name: 'b' }, { type: 'web_search' }],
        },
      },
      'anthropic'
    );
    expect(chat.tools.map((t: any) => t.function?.name ?? t.type)).toEqual([
      'b',
      'web_search',
    ]);
    expect(chat.tool_choice).toBe('required');
    const anthropic: any = transformUsingProviderConfig(
      AnthropicChatCompleteConfig,
      chat
    );
    expect(anthropic.tool_choice).toEqual({ type: 'any' });
    expect(anthropic.tools.map((t: any) => t.name)).toEqual([
      'b',
      'web_search',
    ]);
  });

  test('allowed_tools mode defaults to auto', () => {
    const chat: any = transformResponsesToChatCompletions(
      {
        model: MODEL,
        input: 'hi',
        tools: [fnTool('a'), fnTool('b')],
        tool_choice: {
          type: 'allowed_tools',
          tools: [{ type: 'function', name: 'a' }],
        },
      },
      'bedrock'
    );
    expect(chat.tools.map((t: any) => t.function.name)).toEqual(['a']);
    expect(chat.tool_choice).toBe('auto');
  });

  test('allowed_tools naming a tool not in tools is refused', () => {
    const err = refusal(
      {
        tools: [fnTool('a')],
        tool_choice: {
          type: 'allowed_tools',
          mode: 'auto',
          tools: [{ type: 'function', name: 'missing' }],
        },
      },
      'anthropic'
    );
    expect(err.param).toBe('tool_choice.tools[0]');
  });

  test('empty allowed_tools and a bad mode are refused', () => {
    expect(
      refusal(
        {
          tools: [fnTool('a')],
          tool_choice: { type: 'allowed_tools', tools: [] },
        },
        'anthropic'
      ).param
    ).toBe('tool_choice.tools');
    expect(
      refusal(
        {
          tools: [fnTool('a')],
          tool_choice: {
            type: 'allowed_tools',
            mode: 'none',
            tools: [{ type: 'function', name: 'a' }],
          },
        },
        'anthropic'
      ).param
    ).toBe('tool_choice.mode');
  });

  test('a built-in tool choice is refused', () => {
    const err = refusal(
      {
        tools: [{ type: 'web_search' }],
        tool_choice: { type: 'web_search_preview' },
      },
      'anthropic'
    );
    expect(err.param).toBe('tool_choice');
    expect(err.message).toContain(`'web_search_preview'`);
  });

  test('a function choice without a name is refused', () => {
    expect(
      refusal(
        { tools: [fnTool('a')], tool_choice: { type: 'function' } },
        'anthropic'
      ).param
    ).toBe('tool_choice.name');
  });

  test('string and function choices map as before', () => {
    const chat: any = transformResponsesToChatCompletions(
      {
        model: MODEL,
        input: 'hi',
        tools: [fnTool('a')],
        tool_choice: { type: 'function', name: 'a' },
      },
      'anthropic'
    );
    expect(chat.tool_choice).toEqual({
      type: 'function',
      function: { name: 'a' },
    });
    expect(
      (
        transformResponsesToChatCompletions(
          {
            model: MODEL,
            input: 'hi',
            tools: [fnTool('a')],
            tool_choice: 'required',
          },
          'anthropic'
        ) as any
      ).tool_choice
    ).toBe('required');
  });
});

describe('scalar fields', () => {
  test('safety_identifier reaches Anthropic as metadata.user_id', () => {
    const chat: any = transformResponsesToChatCompletions(
      { model: MODEL, input: 'hi', safety_identifier: 'hash-1', user: 'old' },
      'anthropic'
    );
    expect(chat.user).toBe('hash-1');
    const anthropic: any = transformUsingProviderConfig(
      AnthropicChatCompleteConfig,
      chat
    );
    expect(anthropic.metadata).toEqual({ user_id: 'hash-1' });
  });

  test('user is still used without safety_identifier', () => {
    const chat: any = transformResponsesToChatCompletions(
      { model: MODEL, input: 'hi', user: 'u-1' },
      'anthropic'
    );
    expect(chat.user).toBe('u-1');
  });

  test('advisory fields are ignored, not sent', () => {
    const chat: any = transformResponsesToChatCompletions(
      {
        model: MODEL,
        input: 'hi',
        text: { verbosity: 'low' },
        prompt_cache_key: 'k-1',
        service_tier: 'flex',
      },
      'anthropic'
    );
    expect(chat).toEqual({
      model: MODEL,
      messages: [{ role: 'user', content: 'hi' }],
    });
  });
});

describe('applyAdapterRequestTransform', () => {
  test('answers an unsupported field with a 400 invalid_request_error', async () => {
    const body: any = {
      model: MODEL,
      input: 'hi',
      tools: [{ type: 'file_search', vector_store_ids: ['vs_1'] }],
    };
    const out = applyAdapterRequestTransform(
      'createModelResponse',
      'anthropic',
      body,
      body,
      false,
      'POST'
    );
    expect(out).toBeInstanceOf(Response);
    const res = out as Response;
    expect(res.status).toBe(400);
    const json: any = await res.json();
    expect(json.error).toMatchObject({
      type: 'invalid_request_error',
      param: 'tools[0].type',
      code: 'unsupported_parameter',
    });
    expect(json.error.message).toContain(`'file_search'`);
  });
});

describe('Anthropic stream: server tool input is not a client tool call', () => {
  test('server_tool_use partial_json is skipped; the next tool_use is call 0', () => {
    const sse = [
      `event: content_block_start\ndata: ${JSON.stringify({
        type: 'content_block_start',
        index: 0,
        content_block: {
          type: 'server_tool_use',
          id: 'srvtoolu_1',
          name: 'web_search',
        },
      })}`,
      `event: content_block_delta\ndata: ${JSON.stringify({
        type: 'content_block_delta',
        index: 0,
        delta: { type: 'input_json_delta', partial_json: '{"query":"x"}' },
      })}`,
      `event: content_block_start\ndata: ${JSON.stringify({
        type: 'content_block_start',
        index: 1,
        content_block: { type: 'tool_use', id: 'toolu_1', name: 'lookup' },
      })}`,
      `event: content_block_delta\ndata: ${JSON.stringify({
        type: 'content_block_delta',
        index: 1,
        delta: { type: 'input_json_delta', partial_json: '{"q":1}' },
      })}`,
    ];
    const toChat = getAnthropicStreamChunkTransform('anthropic');
    const state: any = {};
    const toolCalls = sse
      .map((raw) => toChat(raw, 'id-1', state, false) ?? '')
      .flatMap((out) => {
        const data = out.split('\n').find((l) => l.startsWith('data: {'));
        return data
          ? JSON.parse(data.slice(6)).choices[0].delta.tool_calls ?? []
          : [];
      });
    expect(toolCalls).toEqual([
      {
        index: 0,
        id: 'toolu_1',
        type: 'function',
        function: { name: 'lookup', arguments: '' },
      },
      { index: 0, function: { arguments: '{"q":1}' } },
    ]);
  });
});
