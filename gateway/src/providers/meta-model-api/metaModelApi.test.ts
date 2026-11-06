// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../../utils/env', () => ({ Environment: () => ({}) }));

import Providers from '..';
import { META_MODEL_API, VALID_PROVIDERS } from '../../globals';
import { supportsMessagesApiNatively } from '../../adapters/messages';
import { supportsResponsesApiNatively } from '../../adapters/responses';
import { isSafeUpstreamURL } from '../../middlewares/requestValidator';
import { transformUsingProviderConfig } from '../../services/transformToProviderRequest';
import { filePath, META_MODEL_API_BASE_URL } from './api';
import {
  MetaModelAPIFilesResponseTransform,
  MetaModelAPIMessagesResponseTransform,
} from '.';

const config: any = Providers[META_MODEL_API];
const c: any = { get: () => undefined, env: {} };
const GW = 'http://localhost:8787';

const endpoint = (fn: string, gatewayRequestURL = `${GW}/v1/x`) =>
  config.api.getEndpoint({ fn, gatewayRequestURL, providerOptions: {} });

const headers = (fn: string, providerOptions: object = {}, body = {}) =>
  config.api.headers({
    c,
    fn,
    providerOptions: { apiKey: 'k', ...providerOptions },
    gatewayRequestBody: body,
  });

test('is registered, native for Responses and for Messages on every model', () => {
  expect(VALID_PROVIDERS).toContain('meta-model-api');
  expect(config).toBeDefined();
  expect(supportsResponsesApiNatively('meta-model-api')).toBe(true);
  expect(supportsMessagesApiNatively('meta-model-api', 'any-model')).toBe(true);
  // The Llama API provider is unchanged: adapters onto its chat endpoint.
  expect(supportsResponsesApiNatively('meta')).toBe(false);
  expect(supportsMessagesApiNatively('meta', 'any-model')).toBe(false);
});

test('uses the fixed vendor base URL, ignoring caller-supplied hosts', async () => {
  const url = await config.api.getBaseURL({
    providerOptions: {
      apiKey: 'k',
      customHost: 'http://169.254.169.254',
      region: '169.254.169.254/latest#',
    },
    fn: 'chatComplete',
    c,
    gatewayRequestURL: '',
  });
  expect(url).toBe('https://api.meta.ai/v1');
  expect(url).toBe(META_MODEL_API_BASE_URL);
  expect(isSafeUpstreamURL(url)).toBe(true);
});

describe('endpoints', () => {
  test('inference APIs', () => {
    expect(endpoint('chatComplete')).toBe('/chat/completions');
    expect(endpoint('createModelResponse')).toBe('/responses');
    expect(endpoint('messages')).toBe('/messages');
    expect(endpoint('messagesCountTokens')).toBe('/messages/count_tokens');
  });

  test('files', () => {
    expect(endpoint('uploadFile', `${GW}/v1/files`)).toBe('/files');
    expect(endpoint('listFiles', `${GW}/v1/files`)).toBe('/files');
    expect(endpoint('listFiles', `${GW}/v1/files?limit=5&after=f1`)).toBe(
      '/files?limit=5&after=f1'
    );
    expect(endpoint('retrieveFile', `${GW}/v1/files/file_abc`)).toBe(
      '/files/file_abc'
    );
    expect(endpoint('deleteFile', `${GW}/v1/files/file-1.x_Y`)).toBe(
      '/files/file-1.x_Y'
    );
  });

  test('a file path never leaves /files', () => {
    for (const bad of [
      `${GW}/v1/files/..`,
      `${GW}/v1/files/.`,
      `${GW}/v1/files/a/content`,
      `${GW}/v1/files/a%2F..%2Fmodels`,
      `${GW}/v1/files/`,
      `${GW}/v1/batches/b1`,
      '',
    ]) {
      expect(filePath(bad)).toBe('');
    }
  });

  test('APIs Meta does not document are not mapped', () => {
    for (const fn of [
      'embed',
      'retrieveFileContent',
      'createBatch',
      'getModelResponse',
      'imageGenerate',
    ]) {
      expect(endpoint(fn)).toBe('');
    }
  });
});

describe('headers', () => {
  test('Bearer key; no content type of its own for JSON calls', () => {
    expect(headers('chatComplete')).toEqual({ Authorization: 'Bearer k' });
    expect(headers('createModelResponse')).toEqual({
      Authorization: 'Bearer k',
    });
    expect(headers('listFiles')).toEqual({ Authorization: 'Bearer k' });
  });

  test('upload is multipart (the gateway adds the boundary)', () => {
    expect(headers('uploadFile')).toEqual({
      Authorization: 'Bearer k',
      'Content-Type': 'multipart/form-data',
    });
  });

  test('Messages carry anthropic-version, default or caller-chosen', () => {
    expect(headers('messages')['anthropic-version']).toBe('2023-06-01');
    expect(
      headers('messagesCountTokens', { anthropicVersion: '2024-01-01' })[
        'anthropic-version'
      ]
    ).toBe('2024-01-01');
    expect(
      headers('messages', {}, { anthropic_version: '2023-01-01' })[
        'anthropic-version'
      ]
    ).toBe('2023-01-01');
  });
});

describe('request transforms', () => {
  test('chat keeps the OpenAI shape', () => {
    const body: any = transformUsingProviderConfig(config.chatComplete, {
      model: 'm',
      messages: [{ role: 'user', content: 'hi' }],
      temperature: 0.2,
      max_tokens: 10,
    } as any);
    expect(body).toMatchObject({
      model: 'm',
      messages: [{ role: 'user', content: 'hi' }],
      temperature: 0.2,
      max_tokens: 10,
    });
  });

  test('Responses keeps input and file references', () => {
    const body: any = transformUsingProviderConfig(config.createModelResponse, {
      model: 'm',
      input: [
        {
          role: 'user',
          content: [{ type: 'input_file', file_id: 'file_abc' }],
        },
      ],
      max_output_tokens: 32,
    } as any);
    expect(body).toMatchObject({
      model: 'm',
      input: [
        {
          role: 'user',
          content: [{ type: 'input_file', file_id: 'file_abc' }],
        },
      ],
      max_output_tokens: 32,
    });
  });

  test('Messages keeps the Anthropic shape', () => {
    const body: any = transformUsingProviderConfig(config.messages, {
      model: 'm',
      max_tokens: 16,
      system: 'Be brief.',
      messages: [{ role: 'user', content: 'hi' }],
    } as any);
    expect(body).toEqual({
      model: 'm',
      max_tokens: 16,
      system: 'Be brief.',
      messages: [{ role: 'user', content: 'hi' }],
    });
  });

  test('upload passes the caller body through', () => {
    const stream = new ReadableStream();
    expect(config.requestTransforms.uploadFile(stream)).toBe(stream);
  });
});

describe('response transforms', () => {
  test('chat success and error', () => {
    const ok = config.responseTransforms.chatComplete(
      {
        id: 'c1',
        object: 'chat.completion',
        created: 1,
        model: 'm',
        choices: [
          {
            index: 0,
            message: { role: 'assistant', content: 'Hi' },
            finish_reason: 'stop',
          },
        ],
        usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
      },
      200
    );
    expect(ok.provider).toBe('meta-model-api');
    expect(ok.choices[0].message.content).toBe('Hi');

    const err = config.responseTransforms.chatComplete(
      { error: { message: 'bad key', type: 'auth' } },
      401
    );
    expect(err.error.message).toContain('bad key');
    expect(err.provider).toBe('meta-model-api');
  });

  test('Responses success and error', () => {
    const ok = config.responseTransforms.createModelResponse(
      { id: 'resp_1', object: 'response', output: [] },
      200
    );
    expect(ok.id).toBe('resp_1');
    const err = config.responseTransforms.createModelResponse(
      { error: { message: 'nope' } },
      400
    );
    expect(err.error.message).toContain('nope');
    expect(err.provider).toBe('meta-model-api');
  });

  test('Messages success is unchanged; errors and junk are gateway errors', () => {
    const msg = {
      id: 'msg_1',
      type: 'message',
      role: 'assistant',
      model: 'm',
      content: [{ type: 'text', text: 'Hi' }],
      stop_reason: 'end_turn',
      usage: { input_tokens: 1, output_tokens: 1 },
    };
    expect(MetaModelAPIMessagesResponseTransform(msg, 200)).toBe(msg);
    const err: any = MetaModelAPIMessagesResponseTransform(
      { type: 'error', error: { type: 'overloaded', message: 'busy' } },
      529
    );
    expect(err.error.message).toContain('busy');
    expect(err.provider).toBe('meta-model-api');
    const junk: any = MetaModelAPIMessagesResponseTransform({ foo: 1 }, 200);
    expect(junk.error).toBeDefined();
    expect(junk.provider).toBe('meta-model-api');
  });

  test('files success is unchanged; errors are gateway errors', () => {
    const file: any = { id: 'file_abc', object: 'file' };
    expect(MetaModelAPIFilesResponseTransform(file, 200)).toBe(file);
    for (const fn of [
      'uploadFile',
      'listFiles',
      'retrieveFile',
      'deleteFile',
    ]) {
      const err = config.responseTransforms[fn](
        { error: { message: 'No such file' } },
        404
      );
      expect(err.error.message).toContain('No such file');
      expect(err.provider).toBe('meta-model-api');
    }
  });
});
