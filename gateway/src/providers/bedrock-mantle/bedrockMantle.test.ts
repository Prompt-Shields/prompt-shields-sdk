// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../../utils/env', () => ({ Environment: () => ({}) }));

import Providers from '..';
import { BEDROCK_MANTLE, VALID_PROVIDERS } from '../../globals';
import { supportsMessagesApiNatively } from '../../adapters/messages';
import { supportsResponsesApiNatively } from '../../adapters/responses';
import { isSafeUpstreamURL } from '../../middlewares/requestValidator';
import { transformUsingProviderConfig } from '../../services/transformToProviderRequest';
import { filterAnthropicBeta, openAIApiPath as apiPath } from './api';

const openAIApiPath = (model: string, options: object) =>
  apiPath(model, options as any);

const config: any = Providers[BEDROCK_MANTLE];
const c: any = { req: { method: 'POST' }, env: {}, get: () => undefined };
const URL_ = 'https://bedrock-mantle.eu-west-1.api.aws/v1/chat/completions';

const baseURL = (providerOptions: object) =>
  config.api.getBaseURL({ providerOptions, fn: 'chatComplete', c });

const headers = (providerOptions: object, extra: object = {}) =>
  config.api.headers({
    c,
    fn: 'chatComplete',
    providerOptions,
    transformedRequestBody: { model: 'openai.gpt-oss-20b' },
    transformedRequestUrl: URL_,
    gatewayRequestBody: {},
    ...extra,
  });

const endpoint = (fn: string, model?: string, providerOptions: object = {}) =>
  config.api.getEndpoint({
    fn,
    gatewayRequestBodyJSON: { model },
    providerOptions,
  });

const sigv4 = {
  awsAccessKeyId: 'AKIATEST',
  awsSecretAccessKey: 'secret',
  awsRegion: 'eu-west-1',
};

test('is registered, native for Responses and for Claude Messages', () => {
  expect(VALID_PROVIDERS).toContain('bedrock-mantle');
  expect(config).toBeDefined();
  expect(supportsResponsesApiNatively('bedrock-mantle')).toBe(true);
  expect(
    supportsMessagesApiNatively('bedrock-mantle', 'anthropic.claude-opus-4-7')
  ).toBe(true);
  expect(supportsMessagesApiNatively('bedrock-mantle', 'openai.gpt-oss')).toBe(
    false
  );
});

describe('URL', () => {
  test('is the regional api.aws host', () => {
    const url = baseURL({ awsRegion: 'eu-west-1' });
    expect(url).toBe('https://bedrock-mantle.eu-west-1.api.aws');
    expect(isSafeUpstreamURL(url)).toBe(true);
  });

  test('needs a valid region and has no default', () => {
    expect(() => baseURL({})).toThrow(/region/);
    expect(() => baseURL({ awsRegion: 'evil.example/#' })).toThrow(
      /Invalid AWS region/
    );
  });

  test('routes by API and model', () => {
    expect(endpoint('chatComplete', 'openai.gpt-oss-120b')).toBe(
      '/v1/chat/completions'
    );
    expect(endpoint('chatComplete', 'qwen.qwen3-32b')).toBe(
      '/v1/chat/completions'
    );
    expect(endpoint('chatComplete', 'openai.gpt-5.5')).toBe(
      '/openai/v1/chat/completions'
    );
    expect(endpoint('createModelResponse', 'gpt-5.4')).toBe(
      '/openai/v1/responses'
    );
    expect(endpoint('createModelResponse', 'openai.gpt-oss-20b')).toBe(
      '/v1/responses'
    );
    expect(endpoint('messages', 'anthropic.claude-opus-4-7')).toBe(
      '/anthropic/v1/messages'
    );
    expect(endpoint('embed', 'x')).toBe('');
  });

  test('the base path can be chosen, but only from the known ones', () => {
    expect(
      openAIApiPath('openai.gpt-oss-20b', {
        bedrockMantleApiPath: '/openai/v1',
      })
    ).toBe('/openai/v1');
    expect(
      openAIApiPath('openai.gpt-5.5', { bedrockMantleApiPath: 'v1' })
    ).toBe('/v1');
    for (const bad of ['../x', '/anthropic/v1', '//evil.example', 'v2']) {
      expect(() => openAIApiPath('m', { bedrockMantleApiPath: bad })).toThrow(
        /bedrock_mantle_api_path/
      );
    }
  });

  test('proxy keeps /openai and /anthropic paths, else adds /v1', () => {
    const p = (reqPath: string) =>
      config.api.getProxyEndpoint({
        reqPath,
        reqQuery: '',
        providerOptions: {},
      });
    expect(p('/models')).toBe('/v1/models');
    expect(p('/openai/v1/models')).toBe('/openai/v1/models');
    expect(p('/anthropic/v1/messages')).toBe('/anthropic/v1/messages');
  });
});

describe('headers', () => {
  test('SigV4 with service bedrock-mantle', async () => {
    const h = await headers({ ...sigv4, awsSessionToken: 'tok' });
    expect(h.authorization).toMatch(
      /^AWS4-HMAC-SHA256 Credential=AKIATEST\/\d{8}\/eu-west-1\/bedrock-mantle\/aws4_request/
    );
    expect(h['x-amz-security-token']).toBe('tok');
    expect(h['anthropic-version']).toBeUndefined();
  });

  test('a Bedrock API key is a Bearer token', async () => {
    const h = await headers({ apiKey: 'bk', awsRegion: 'eu-west-1' });
    expect(h.authorization).toBe('Bearer bk');
    expect(h['x-amz-date']).toBeUndefined();
    const forced = await headers({
      apiKey: 'bk',
      awsAuthType: 'apiKey',
      ...sigv4,
    });
    expect(forced.authorization).toBe('Bearer bk');
  });

  test('messages carry anthropic-version and only supported betas', async () => {
    const h = await headers(
      {
        ...sigv4,
        anthropicBeta:
          'advanced-tool-use-2025-11-20, context-1m-2025-08-07,prompt-caching-scope-2026-01-05',
      },
      {
        fn: 'messages',
        transformedRequestUrl:
          'https://bedrock-mantle.eu-west-1.api.aws/anthropic/v1/messages',
      }
    );
    expect(h['anthropic-version']).toBe('2023-06-01');
    expect(h['anthropic-beta']).toBe('context-1m-2025-08-07');
    expect(filterAnthropicBeta('redact-thinking-2026-02-01')).toBeUndefined();
  });

  test.each([
    ['another host', 'https://evil.example/v1/chat/completions'],
    [
      'a look-alike host',
      'https://bedrock-mantle.eu-west-1.api.aws.evil.example/v1',
    ],
    ['userinfo', 'https://bedrock-mantle.eu-west-1.api.aws@evil.example/v1'],
    ['a port', 'https://bedrock-mantle.eu-west-1.api.aws:444/v1'],
    ['http', 'http://bedrock-mantle.eu-west-1.api.aws/v1'],
    ['another region', 'https://bedrock-mantle.us-east-1.api.aws/v1'],
    ['bedrock-runtime', 'https://bedrock-runtime.eu-west-1.amazonaws.com/v1'],
  ])('never signs for %s', async (_, url) => {
    await expect(
      headers(sigv4, { transformedRequestUrl: url })
    ).rejects.toThrow(
      /only signs requests to its regional endpoint|Invalid AWS/
    );
  });

  test('serviceRole needs AWS_AMBIENT_CREDENTIALS_ENABLED, like Bedrock', async () => {
    await expect(
      headers({ awsAuthType: 'serviceRole', awsRegion: 'eu-west-1' })
    ).rejects.toThrow(/serviceRole is disabled/);
    await expect(
      headers({ awsAuthType: 'assumedRole', awsRegion: 'eu-west-1' })
    ).rejects.toThrow(/role ARN/);
  });

  test('needs some credential', async () => {
    await expect(headers({ awsRegion: 'eu-west-1' })).rejects.toThrow(
      /API key or AWS credentials/
    );
    await expect(
      headers({ awsRegion: 'eu-west-1', awsAuthType: 'apiKey' })
    ).rejects.toThrow(/API key or AWS credentials/);
  });
});

describe('transforms', () => {
  test('chat passes the OpenAI request through', () => {
    const body = transformUsingProviderConfig(config.chatComplete, {
      model: 'openai.gpt-oss-120b',
      messages: [{ role: 'user', content: 'hi' }],
      max_tokens: 5,
      reasoning_effort: 'low',
    } as any);
    expect(body).toMatchObject({
      model: 'openai.gpt-oss-120b',
      messages: [{ role: 'user', content: 'hi' }],
      max_tokens: 5,
    });
  });

  test('messages keep safeguards and thinking', () => {
    const body = transformUsingProviderConfig(config.messages, {
      model: 'anthropic.claude-opus-4-7',
      max_tokens: 5,
      messages: [{ role: 'user', content: 'hi' }],
      thinking: { type: 'enabled', budget_tokens: 1024 },
      safeguards: { level: 'standard' },
    } as any);
    expect(body).toMatchObject({
      thinking: { type: 'enabled', budget_tokens: 1024 },
      safeguards: { level: 'standard' },
    });
  });

  test('responses are tagged with the provider; errors keep their message', () => {
    const chat = config.responseTransforms.chatComplete(
      { id: 'x', choices: [], usage: { prompt_tokens: 1 } },
      200
    );
    expect(chat.provider).toBe('bedrock-mantle');
    const resp = config.responseTransforms.createModelResponse(
      { id: 'resp_1', object: 'response', output: [] },
      200
    );
    expect(resp.provider).toBe('bedrock-mantle');
    const err = config.responseTransforms.messages(
      {
        type: 'error',
        error: { type: 'invalid_request_error', message: 'bad' },
      },
      400
    );
    expect(err.provider).toBe('bedrock-mantle');
    expect(err.error.message).toContain('bad');
  });
});
