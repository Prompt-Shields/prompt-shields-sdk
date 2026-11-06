/**
 * PS fork (#73): Responses `include` and `max_tool_calls` on targets reached
 * through the Chat Completions adapter (requestFields.ts). Native Responses
 * targets are not adapted, so they are unchanged.
 */
// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../../utils/env', () => ({ Environment: () => ({}) }));

import { transformResponsesToChatCompletions } from '../responses/requestTransform';
import { transformChatCompletionsToResponses } from '../responses/responseTransform';
import {
  ANTHROPIC_WEB_SEARCH_TOOL,
  ResponsesAdapterRequestError,
} from '../responses/requestFields';
import { AnthropicChatCompleteConfig } from '../../providers/anthropic/chatComplete';
import { GoogleChatCompleteConfig } from '../../providers/google/chatComplete';
import { applyAdapterRequestTransform } from '../../handlers/adapterUtils';
import { transformUsingProviderConfig } from '../testUtils';

const MODEL = 'test-model';
const fnTool = {
  type: 'function',
  name: 'lookup',
  description: 'lookup tool',
  parameters: { type: 'object', properties: {} },
};

function chat(req: any, provider: string): any {
  return transformResponsesToChatCompletions(
    { model: MODEL, input: 'hi', ...req },
    provider
  );
}

function refusal(req: any, provider: string): ResponsesAdapterRequestError {
  try {
    chat(req, provider);
  } catch (err) {
    expect(err).toBeInstanceOf(ResponsesAdapterRequestError);
    return err as ResponsesAdapterRequestError;
  }
  throw new Error('expected a ResponsesAdapterRequestError');
}

async function adapted400(body: any, provider: string) {
  const out = applyAdapterRequestTransform(
    'createModelResponse',
    provider,
    body,
    body,
    false,
    'POST'
  );
  expect(out).toBeInstanceOf(Response);
  const res = out as Response;
  expect(res.status).toBe(400);
  return ((await res.json()) as any).error;
}

describe('include', () => {
  test('accepted values send nothing extra to the provider', () => {
    const out = chat(
      {
        include: [
          'reasoning.encrypted_content',
          'web_search_call.action.sources',
        ],
      },
      'anthropic'
    );
    expect(out).toEqual({
      model: MODEL,
      messages: [{ role: 'user', content: 'hi' }],
    });
  });

  test('unsupported values are a 400 naming only the value', async () => {
    const error = await adapted400(
      {
        model: MODEL,
        input: 'secret-input-text',
        include: ['reasoning.encrypted_content', 'file_search_call.results'],
      },
      'anthropic'
    );
    expect(error).toMatchObject({
      type: 'invalid_request_error',
      param: 'include[1]',
      code: 'unsupported_parameter',
    });
    expect(error.message).toContain(`'file_search_call.results'`);
    expect(error.message).not.toContain('secret-input-text');
    expect(error.message).not.toContain('reasoning.encrypted_content');
  });

  test.each([
    'code_interpreter_call.outputs',
    'computer_call_output.output.image_url',
    'message.input_image.image_url',
    'not.a.real.value',
  ])('%s is refused', (value) => {
    const err = refusal({ include: [value] }, 'google');
    expect(err.param).toBe('include[0]');
    expect(err.message).toContain(`'${value}'`);
  });

  test('long values are cut short in the message', () => {
    const value = 'x'.repeat(500);
    const err = refusal({ include: [value] }, 'anthropic');
    expect(err.message).toContain(`'${'x'.repeat(64)}...'`);
    expect(err.message).not.toContain('x'.repeat(65));
  });

  test('include must be an array of strings', () => {
    expect(
      refusal({ include: 'reasoning.encrypted_content' }, 'anthropic')
    ).toMatchObject({ param: 'include' });
    expect(refusal({ include: [{ a: 1 }] }, 'anthropic')).toMatchObject({
      param: 'include[0]',
    });
  });

  test('reasoning.encrypted_content: signed thinking is returned either way', () => {
    const response: any = {
      id: 'r1',
      model: MODEL,
      choices: [
        {
          index: 0,
          finish_reason: 'stop',
          message: {
            role: 'assistant',
            content: 'answer',
            content_blocks: [
              { type: 'thinking', thinking: 'hmm', signature: 'sig' },
              { type: 'text', text: 'answer' },
            ],
          },
        },
      ],
    };
    for (const include of [undefined, ['reasoning.encrypted_content']]) {
      const req = { model: MODEL, input: 'hi', include };
      expect(() => chat(req, 'anthropic')).not.toThrow();
      const out: any = transformChatCompletionsToResponses(
        response,
        200,
        'anthropic',
        req
      );
      expect(out.output[0].type).toBe('reasoning');
      expect(out.output[0].encrypted_content).toMatch(/^ps-thk1:anthropic:/);
    }
  });
});

describe('include: message.output_text.logprobs', () => {
  const include = ['message.output_text.logprobs'];

  test('asks the target for logprobs', () => {
    const out = chat({ include, top_logprobs: 2 }, 'google');
    expect(out.logprobs).toBe(true);
    expect(out.top_logprobs).toBe(2);
    const google: any = transformUsingProviderConfig(
      GoogleChatCompleteConfig,
      out
    );
    expect(google.generationConfig.responseLogprobs).toBe(true);
    expect(google.generationConfig.logprobs).toBe(2);
  });

  test('not asked for without include', () => {
    expect(chat({}, 'google').logprobs).toBeUndefined();
  });

  test('are returned in output_text.logprobs', () => {
    const token = (t: string, logprob: number) => ({
      token: t,
      logprob,
      bytes: [...Buffer.from(t)],
    });
    const response: any = {
      id: 'r1',
      model: MODEL,
      choices: [
        {
          index: 0,
          finish_reason: 'stop',
          message: { role: 'assistant', content: 'Hi' },
          logprobs: {
            content: [
              { ...token('Hi', -0.1), top_logprobs: [token('Hi', -0.1)] },
            ],
          },
        },
      ],
    };
    const withInclude: any = transformChatCompletionsToResponses(
      response,
      200,
      'google',
      { model: MODEL, include }
    );
    expect(withInclude.output[0].content[0].logprobs).toEqual([
      {
        token: 'Hi',
        logprob: -0.1,
        bytes: [72, 105],
        top_logprobs: [{ token: 'Hi', logprob: -0.1, bytes: [72, 105] }],
      },
    ]);
    const without: any = transformChatCompletionsToResponses(
      response,
      200,
      'google',
      { model: MODEL }
    );
    expect(without.output[0].content[0].logprobs).toEqual([]);
  });

  test.each([
    ['anthropic', MODEL],
    ['bedrock', MODEL],
    ['vertex-ai', 'anthropic.some-model'],
  ])('refused for %s, which returns no logprobs', (provider, model) => {
    const err = refusal({ model, include }, provider);
    expect(err.param).toBe('include[0]');
    expect(err.message).toContain(`'message.output_text.logprobs'`);
  });

  test('refused with stream: true', () => {
    const err = refusal({ include, stream: true }, 'google');
    expect(err.param).toBe('include[0]');
    expect(err.message).toContain('stream');
  });
});

describe('max_tool_calls', () => {
  test('is the Anthropic web_search max_uses', () => {
    const out = chat(
      { tools: [fnTool, { type: 'web_search' }], max_tool_calls: 3 },
      'anthropic'
    );
    expect(out.max_tool_calls).toBeUndefined();
    const anthropic: any = transformUsingProviderConfig(
      AnthropicChatCompleteConfig,
      out
    );
    expect(anthropic.tools[1]).toEqual({
      type: ANTHROPIC_WEB_SEARCH_TOOL,
      name: 'web_search',
      max_uses: 3,
    });
    // Function tools are not built-in tools and take no limit
    expect(anthropic.tools[0].max_uses).toBeUndefined();
  });

  test('without built-in tools it limits nothing and sends nothing', () => {
    for (const max_tool_calls of [0, 2]) {
      const out = chat({ tools: [fnTool], max_tool_calls }, 'google');
      expect(out.max_tool_calls).toBeUndefined();
      expect(out.tools).toHaveLength(1);
      expect(chat({ max_tool_calls }, 'anthropic')).toEqual({
        model: MODEL,
        messages: [{ role: 'user', content: 'hi' }],
      });
    }
  });

  test('refused with Google Search grounding, which has no limit', async () => {
    const error = await adapted400(
      {
        model: MODEL,
        input: 'hi',
        tools: [{ type: 'web_search' }],
        max_tool_calls: 2,
      },
      'google'
    );
    expect(error).toMatchObject({
      param: 'max_tool_calls',
      code: 'unsupported_parameter',
    });
  });

  test('a caller function named google_search is not the search tool', () => {
    const out = chat(
      { tools: [{ ...fnTool, name: 'google_search' }], max_tool_calls: 1 },
      'google'
    );
    expect(out.tools[0].function.name).toBe('google_search');
  });

  test('the grounding tool marker never reaches the provider', () => {
    const out = chat({ tools: [{ type: 'web_search' }] }, 'google');
    expect(JSON.parse(JSON.stringify(out.tools))).toEqual(out.tools);
    expect(Object.keys(out.tools[0])).toEqual(['type', 'function']);
  });

  test('0 with an Anthropic web_search tool is refused', () => {
    expect(
      refusal(
        { tools: [{ type: 'web_search' }], max_tool_calls: 0 },
        'anthropic'
      )
    ).toMatchObject({ param: 'max_tool_calls' });
  });

  test.each([-1, 1.5, '2'])('%p is refused', (max_tool_calls) => {
    expect(refusal({ max_tool_calls }, 'anthropic')).toMatchObject({
      param: 'max_tool_calls',
    });
  });

  test('a web_search tool left out by allowed_tools is not limited', () => {
    const out = chat(
      {
        tools: [fnTool, { type: 'web_search' }],
        tool_choice: {
          type: 'allowed_tools',
          mode: 'auto',
          tools: [{ type: 'function', name: 'lookup' }],
        },
        max_tool_calls: 2,
      },
      'google'
    );
    expect(out.tools).toHaveLength(1);
  });
});

describe('native Responses targets', () => {
  test('include and max_tool_calls pass through untouched', () => {
    const body: any = {
      model: 'any-model',
      input: 'hi',
      include: ['file_search_call.results', 'message.output_text.logprobs'],
      max_tool_calls: 0,
      tools: [{ type: 'web_search' }],
      stream: true,
    };
    const before = JSON.stringify(body);
    for (const provider of ['openai', 'azure-openai']) {
      expect(
        applyAdapterRequestTransform(
          'createModelResponse',
          provider,
          body,
          body,
          true,
          'POST'
        )
      ).toBeNull();
    }
    expect(JSON.stringify(body)).toBe(before);
  });
});
