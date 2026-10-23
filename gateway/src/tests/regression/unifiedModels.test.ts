/**
 * Regression tests for the unified `GET /v1/models` (#79) through the whole
 * gateway app, offline. Claude Code lists models with an Anthropic SDK call:
 * `x-api-key` and `anthropic-version`, plus the gateway's provider header
 * (set with `ANTHROPIC_CUSTOM_HEADERS`). It must get Anthropic's list shape
 * whichever provider serves it; every other caller keeps OpenAI's shape.
 * Also checks that the legacy control-plane `modelsHandler`, mounted earlier
 * on the same path, does not shadow the unified handler.
 */
jest.mock('jose', () => ({}));

import { json, resetHarness, upstream, upstreamCalls } from './harness';
import app from '../../index';

const anthropicList = {
  data: [
    {
      type: 'model',
      id: 'claude-sonnet-4-5',
      display_name: 'Claude Sonnet 4.5',
      created_at: '2025-09-29T00:00:00Z',
    },
  ],
  has_more: true,
  first_id: 'claude-sonnet-4-5',
  last_id: 'claude-sonnet-4-5',
};

const openAIList = {
  object: 'list',
  data: [
    { id: 'gpt-4o', object: 'model', created: 1715367049, owned_by: 'system' },
    { id: 'gpt-4o-mini', object: 'model', created: 1721172741, owned_by: 'x' },
    { id: 'o3', object: 'model', created: 1744225308, owned_by: 'system' },
  ],
};

/** The headers an Anthropic SDK (Claude Code) sends, plus the provider. */
const claudeCode = (provider: string, key: Record<string, string>) => ({
  'anthropic-version': '2023-06-01',
  'x-portkey-provider': provider,
  ...key,
});

const list = (path: string, headers: Record<string, string>) =>
  app.request(path, { headers });

beforeEach(() => resetHarness());

describe('GET /v1/models (#79)', () => {
  test('Claude Code on Anthropic: native list and paging passed through', async () => {
    upstream(/^https:\/\/api\.anthropic\.com\/v1\/models/, () =>
      json(anthropicList)
    );
    const res = await list(
      '/v1/models?limit=1&after_id=claude-opus-4-1',
      claudeCode('anthropic', { 'x-api-key': 'sk-ant-regression' })
    );
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual(anthropicList);
    const sent = upstreamCalls[0];
    expect(sent.method).toBe('GET');
    expect(sent.url).toBe(
      'https://api.anthropic.com/v1/models?limit=1&after_id=claude-opus-4-1'
    );
    expect(sent.headers['x-api-key']).toBe('sk-ant-regression');
    expect(sent.headers['anthropic-version']).toBe('2023-06-01');
  });

  test('Claude Code on OpenAI: Anthropic shape, paged by the gateway', async () => {
    // OpenAI ignores the paging parameters and returns the whole list.
    upstream(/^https:\/\/api\.openai\.com\/v1\/models(\?|$)/, () =>
      json(openAIList)
    );
    const res = await list(
      '/v1/models?limit=2',
      claudeCode('openai', { authorization: 'Bearer sk-regression' })
    );
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      data: [
        {
          type: 'model',
          id: 'gpt-4o',
          display_name: 'gpt-4o',
          created_at: new Date(1715367049 * 1000).toISOString(),
        },
        {
          type: 'model',
          id: 'gpt-4o-mini',
          display_name: 'gpt-4o-mini',
          created_at: new Date(1721172741 * 1000).toISOString(),
        },
      ],
      has_more: true,
      first_id: 'gpt-4o',
      last_id: 'gpt-4o-mini',
    });
    expect(upstreamCalls).toHaveLength(1);
  });

  test('OpenAI shape stays the default, also for an Anthropic provider', async () => {
    upstream(/^https:\/\/api\.anthropic\.com\/v1\/models/, () =>
      json(anthropicList)
    );
    const res = await list('/v1/models', {
      'x-portkey-provider': 'anthropic',
      'x-api-key': 'sk-ant-regression',
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      object: 'list',
      data: [
        {
          id: 'claude-sonnet-4-5',
          object: 'model',
          created: Date.parse('2025-09-29T00:00:00Z') / 1000,
          owned_by: 'system',
        },
      ],
    });
  });

  test('provider errors keep their status and body', async () => {
    const error = {
      type: 'error',
      error: { type: 'authentication_error', message: 'invalid x-api-key' },
    };
    upstream(/^https:\/\/api\.anthropic\.com\/v1\/models/, () =>
      json(error, 401)
    );
    const res = await list(
      '/v1/models',
      claudeCode('anthropic', { 'x-api-key': 'bad' })
    );
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual(error);
  });

  test('without a provider or config it is a 400 and nothing is called', async () => {
    const res = await list('/v1/models', {
      'anthropic-version': '2023-06-01',
      'x-api-key': 'sk-ant-regression',
    });
    expect(res.status).toBe(400);
    expect(upstreamCalls).toHaveLength(0);
  });
});
