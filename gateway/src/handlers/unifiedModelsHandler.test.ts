jest.mock('../utils/env', () => ({ Environment: () => ({}) }));
// The plugin registry pulls in ESM-only deps; no guardrails run here.
jest.mock('../../plugins', () => ({ plugins: {} }));

import { Hono } from 'hono';
import { requestValidator } from '../middlewares/requestValidator';
import { hooks } from '../middlewares/hooks';
import {
  extractModel,
  extractModels,
  normaliseModelResponse,
  normaliseModelsResponse,
  toAnthropicModelList,
  toOpenAIModelList,
  unifiedModelHandler,
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

describe('normaliseModelResponse (single model)', () => {
  const openAIModel = {
    id: 'model-a',
    object: 'model',
    created: 1700000000,
    owned_by: 'org',
  };
  const anthropicModel = {
    type: 'model',
    id: 'model-a',
    display_name: 'Model A',
    created_at: '2025-01-01T00:00:00Z',
  };

  test('extractModel accepts OpenAI, Anthropic and Gemini objects', () => {
    expect(extractModel(openAIModel)).toEqual({
      id: 'model-a',
      created: 1700000000,
      ownedBy: 'org',
      displayName: 'model-a',
    });
    expect(extractModel(anthropicModel)?.displayName).toBe('Model A');
    expect(
      extractModel({ name: 'models/model-g', displayName: 'Model G' })
    ).toEqual(
      expect.objectContaining({ id: 'models/model-g', displayName: 'Model G' })
    );
    expect(extractModel({ foo: 1 })).toBeNull();
    expect(extractModel([openAIModel])).toBeNull();
  });

  test('OpenAI object to Anthropic shape for Anthropic callers', async () => {
    const out = await normaliseModelResponse(jsonResponse(openAIModel), true);
    expect(await out.json()).toEqual({
      type: 'model',
      id: 'model-a',
      display_name: 'model-a',
      created_at: new Date(1700000000 * 1000).toISOString(),
    });
  });

  test('Anthropic object to OpenAI shape for everyone else', async () => {
    const out = await normaliseModelResponse(
      jsonResponse(anthropicModel),
      false
    );
    expect(await out.json()).toEqual({
      id: 'model-a',
      object: 'model',
      created: Date.parse('2025-01-01T00:00:00Z') / 1000,
      owned_by: 'system',
    });
  });

  test('objects already in the caller shape, errors and non-JSON pass through', async () => {
    const a = jsonResponse(anthropicModel);
    expect(await normaliseModelResponse(a, true)).toBe(a);
    const o = jsonResponse(openAIModel);
    expect(await normaliseModelResponse(o, false)).toBe(o);
    const err = jsonResponse({ error: 'not found' }, 404);
    expect(await normaliseModelResponse(err, true)).toBe(err);
    const text = new Response('not json', { status: 200 });
    expect(await normaliseModelResponse(text, true)).toBe(text);
    const unknown = jsonResponse({ foo: 1 });
    expect(await normaliseModelResponse(unknown, true)).toBe(unknown);
  });
});

describe('GET /v1/models route', () => {
  const app = new Hono();
  app.use('*', hooks);
  app.get('/v1/models', requestValidator, unifiedModelsHandler);
  app.get('/v1/models/:id{.+}', requestValidator, unifiedModelHandler);

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

  test('retrieve: proxies the id (slashes kept) and reshapes the object', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        id: 'org/model-a',
        object: 'model',
        created: 1700000000,
        owned_by: 'org',
      })
    );
    const res = await app.request('/v1/models/org/model-a', {
      headers: {
        'x-portkey-provider': 'openai',
        authorization: 'Bearer sk-test',
        'anthropic-version': '2023-06-01',
      },
    });
    expect(res.status).toBe(200);
    expect(String(fetchMock.mock.calls[0][0])).toContain(
      'api.openai.com/v1/models/org/model-a'
    );
    expect(await res.json()).toEqual({
      type: 'model',
      id: 'org/model-a',
      display_name: 'org/model-a',
      created_at: new Date(1700000000 * 1000).toISOString(),
    });
  });

  test('retrieve requires a provider or config header', async () => {
    const res = await app.request('/v1/models/model-a', {
      headers: { 'anthropic-version': '2023-06-01' },
    });
    expect(res.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
