jest.mock('../utils/env', () => ({ Environment: () => ({}) }));
// The plugin registry pulls in ESM-only deps; no guardrails run here.
jest.mock('../../plugins', () => ({ plugins: {} }));

import { Hono } from 'hono';
import { requestValidator } from '../middlewares/requestValidator';
import { hooks } from '../middlewares/hooks';
import {
  extractModels,
  normaliseModelsResponse,
  toAnthropicModelList,
  toOpenAIModelList,
  unifiedModelsHandler,
} from './unifiedModelsHandler';

const openAIList = {
  object: 'list',
  data: [
    { id: 'gpt-4o', object: 'model', created: 1715367049, owned_by: 'system' },
    { id: 'gpt-4o-mini', object: 'model', created: 1721172741, owned_by: 'x' },
    { id: 'o3', object: 'model', created: 1744225308, owned_by: 'system' },
  ],
};

const anthropicList = {
  data: [
    {
      type: 'model',
      id: 'claude-sonnet-4-5',
      display_name: 'Claude Sonnet 4.5',
      created_at: '2025-09-29T00:00:00Z',
    },
  ],
  has_more: false,
  first_id: 'claude-sonnet-4-5',
  last_id: 'claude-sonnet-4-5',
};

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });

describe('shape conversion', () => {
  test('OpenAI list to Anthropic shape', () => {
    const out = toAnthropicModelList(extractModels(openAIList)!);
    expect(out).toEqual({
      data: [
        {
          type: 'model',
          id: 'gpt-4o',
          display_name: 'gpt-4o',
          created_at: '2024-05-10T18:50:49.000Z',
        },
        expect.objectContaining({ id: 'gpt-4o-mini' }),
        expect.objectContaining({ id: 'o3' }),
      ],
      has_more: false,
      first_id: 'gpt-4o',
      last_id: 'o3',
    });
  });

  test('Anthropic list to OpenAI shape', () => {
    expect(toOpenAIModelList(extractModels(anthropicList)!)).toEqual({
      object: 'list',
      data: [
        {
          id: 'claude-sonnet-4-5',
          object: 'model',
          created: 1759104000,
          owned_by: 'system',
        },
      ],
    });
  });

  test('Gemini-style models[].name is accepted', () => {
    const models = extractModels({
      models: [{ name: 'models/gemini-2.5-pro', displayName: 'Gemini' }],
    });
    expect(models).toEqual([
      {
        id: 'models/gemini-2.5-pro',
        created: 0,
        ownedBy: '',
        displayName: 'Gemini',
      },
    ]);
  });

  test('unrecognised bodies yield null', () => {
    expect(extractModels({ foo: 1 })).toBeNull();
    expect(extractModels('nope')).toBeNull();
  });

  test('Anthropic pagination: limit, after_id, before_id', () => {
    const models = extractModels(openAIList)!;
    const first = toAnthropicModelList(models, { limit: '2' });
    expect(first.data.map((m) => m.id)).toEqual(['gpt-4o', 'gpt-4o-mini']);
    expect(first.has_more).toBe(true);

    const next = toAnthropicModelList(models, {
      limit: '2',
      after_id: first.last_id,
    });
    expect(next.data.map((m) => m.id)).toEqual(['o3']);
    expect(next.has_more).toBe(false);

    const prev = toAnthropicModelList(models, { limit: '1', before_id: 'o3' });
    expect(prev.data.map((m) => m.id)).toEqual(['gpt-4o-mini']);
    expect(prev.has_more).toBe(true);

    expect(toAnthropicModelList(models, { limit: 'abc' }).data).toHaveLength(3);
  });
});

describe('normaliseModelsResponse', () => {
  test('passes errors through untouched', async () => {
    const res = jsonResponse({ error: 'bad key' }, 401);
    expect(await normaliseModelsResponse(res, true, {})).toBe(res);
  });

  test('passes native Anthropic lists through for Anthropic callers', async () => {
    const res = jsonResponse(anthropicList);
    expect(await normaliseModelsResponse(res, true, {})).toBe(res);
  });

  test('passes non-JSON bodies through', async () => {
    const res = new Response('not json', { status: 200 });
    expect(await normaliseModelsResponse(res, false, {})).toBe(res);
  });
});

describe('GET /v1/models route', () => {
  const app = new Hono();
  app.use('*', hooks);
  app.get('/v1/models', requestValidator, unifiedModelsHandler);

  const fetchMock = jest.fn();
  const realFetch = global.fetch;
  beforeEach(() => {
    fetchMock.mockReset();
    global.fetch = fetchMock as any;
  });
  afterAll(() => {
    global.fetch = realFetch;
  });

  test('requires a provider or config header like other /v1 routes', async () => {
    const res = await app.request('/v1/models', {
      headers: { 'anthropic-version': '2023-06-01' },
    });
    expect(res.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  test('returns Anthropic shape to anthropic-version callers', async () => {
    fetchMock.mockResolvedValue(jsonResponse(openAIList));
    const res = await app.request('/v1/models?limit=2', {
      headers: {
        'x-portkey-provider': 'openai',
        authorization: 'Bearer sk-test',
        'anthropic-version': '2023-06-01',
      },
    });
    expect(res.status).toBe(200);
    expect(String(fetchMock.mock.calls[0][0])).toContain(
      'api.openai.com/v1/models'
    );
    const body: any = await res.json();
    expect(body.data.map((m: any) => m.id)).toEqual(['gpt-4o', 'gpt-4o-mini']);
    expect(body.data[0].type).toBe('model');
    expect(body.has_more).toBe(true);
  });

  test('returns OpenAI shape to everyone else', async () => {
    fetchMock.mockResolvedValue(jsonResponse(anthropicList));
    const res = await app.request('/v1/models', {
      headers: {
        'x-portkey-provider': 'anthropic',
        'x-api-key': 'sk-ant-test',
      },
    });
    expect(res.status).toBe(200);
    expect(String(fetchMock.mock.calls[0][0])).toContain(
      'api.anthropic.com/v1/models'
    );
    const body: any = await res.json();
    expect(body.object).toBe('list');
    expect(body.data[0]).toEqual(
      expect.objectContaining({ id: 'claude-sonnet-4-5', object: 'model' })
    );
  });
});
