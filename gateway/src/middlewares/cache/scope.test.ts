import {
  __resetCacheForTests,
  computeCacheScope,
  getFromCache,
  memoryCache,
  putInCache,
  setCacheScopeForTarget,
} from './index';
import { CacheService } from '../../handlers/services/cacheService';

jest.mock('hono/adapter', () => ({ env: () => ({}) }));

const body = { model: 'gpt-4o', messages: [{ role: 'user', content: 'hi' }] };
const maxAge = () => Date.now() + 10000;
const SECRET_A = 'sk-tenantA-0123456789abcdef';
const SECRET_B = 'sk-tenantB-0123456789abcdef';

const scopeFor = (
  apiKey: string,
  extra: Partial<Parameters<typeof computeCacheScope>[0]> = {}
) =>
  computeCacheScope({
    provider: 'openai',
    providerOption: { provider: 'openai', apiKey },
    requestHeaders: { authorization: `Bearer ${apiKey}` },
    endpoint: 'chatComplete',
    ...extra,
  });

// Mirrors how CacheService feeds getFromCache, so the test exercises the real
// read path rather than the scope helper alone.
const fakeHono = () => {
  const store = new Map<string, any>();
  store.set('getFromCache', getFromCache);
  return {
    get: (k: string) => store.get(k),
    set: (k: string, v: any) => store.set(k, v),
  } as any;
};

const requestContext = (honoContext: any, overrides: Record<string, any>) =>
  ({
    honoContext,
    endpoint: 'chatComplete',
    index: 0,
    provider: 'openai',
    requestURL: 'https://api.openai.com/v1/chat/completions',
    transformedRequestBody: body,
    cacheConfig: { mode: 'simple', maxAge: undefined },
    ...overrides,
  }) as any;

describe('cache key is scoped to the caller', () => {
  beforeEach(() => __resetCacheForTests());

  it('two callers with identical bodies get different keys', async () => {
    const a = await scopeFor(SECRET_A);
    const b = await scopeFor(SECRET_B);
    expect(a).not.toBe(b);

    await putInCache(
      {},
      {},
      body,
      { from: 'A' },
      'chatComplete',
      '',
      'simple',
      maxAge(),
      a
    );
    const [respB, statusB] = await getFromCache(
      {},
      {},
      body,
      'chatComplete',
      '',
      'simple',
      maxAge(),
      b
    );
    expect(statusB).toBe('MISS');
    expect(respB).toBeNull();
  });

  it('the same caller gets the same key across requests', async () => {
    const first = await scopeFor(SECRET_A, {
      // Non-identity fields that vary between requests must not matter.
      providerOption: {
        provider: 'openai',
        apiKey: SECRET_A,
        retry: { attempts: 1 },
        metadata: { run: '1' },
      },
      requestHeaders: {
        authorization: `Bearer ${SECRET_A}`,
        'x-portkey-trace-id': 'trace-1',
      },
    });
    const second = await scopeFor(SECRET_A, {
      providerOption: {
        provider: 'openai',
        apiKey: SECRET_A,
        retry: { attempts: 3 },
        metadata: { run: '2' },
      },
      requestHeaders: {
        authorization: `Bearer ${SECRET_A}`,
        'x-portkey-trace-id': 'trace-2',
      },
    });
    expect(first).toBe(second);

    await putInCache(
      {},
      {},
      body,
      { from: 'A' },
      'chatComplete',
      '',
      'simple',
      maxAge(),
      first
    );
    const [resp, status] = await getFromCache(
      {},
      {},
      body,
      'chatComplete',
      '',
      'simple',
      maxAge(),
      second
    );
    expect(status).toBe('HIT');
    expect(JSON.parse(resp as string)).toEqual({ from: 'A' });
  });

  it('separates callers by virtual key, service account and provider', async () => {
    const vkA = await computeCacheScope({
      provider: 'openai',
      providerOption: { provider: 'openai', virtualKey: 'vk-a' },
    });
    const vkB = await computeCacheScope({
      provider: 'openai',
      providerOption: { provider: 'openai', virtualKey: 'vk-b' },
    });
    expect(vkA).not.toBe(vkB);

    const saA = await computeCacheScope({
      provider: 'vertex-ai',
      providerOption: {
        provider: 'vertex-ai',
        vertexServiceAccountJson: { client_email: 'a@x', private_key: 'k1' },
      },
    });
    const saB = await computeCacheScope({
      provider: 'vertex-ai',
      providerOption: {
        provider: 'vertex-ai',
        vertexServiceAccountJson: { private_key: 'k2', client_email: 'b@x' },
      },
    });
    expect(saA).not.toBe(saB);

    expect(await scopeFor(SECRET_A, { provider: 'anthropic' })).not.toBe(
      await scopeFor(SECRET_A)
    );
  });

  it('keeps assumed-role keys stable while STS credentials rotate', async () => {
    const opt = (accessKeyId: string) => ({
      provider: 'bedrock',
      awsAuthType: 'assumedRole',
      awsRoleArn: 'arn:aws:iam::123:role/a',
      awsAccessKeyId: accessKeyId,
      awsSecretAccessKey: `secret-${accessKeyId}`,
    });
    const s1 = await computeCacheScope({
      provider: 'bedrock',
      providerOption: opt('ASIA1'),
    });
    const s2 = await computeCacheScope({
      provider: 'bedrock',
      providerOption: opt('ASIA2'),
    });
    expect(s1).toBe(s2);
    const otherRole = await computeCacheScope({
      provider: 'bedrock',
      providerOption: {
        ...opt('ASIA1'),
        awsRoleArn: 'arn:aws:iam::123:role/b',
      },
    });
    expect(otherRole).not.toBe(s1);
  });

  it('different proxy paths and queries get different keys', async () => {
    const proxy = (requestURL: string) =>
      scopeFor(SECRET_A, { endpoint: 'proxy', requestURL });
    const files = await proxy('https://api.openai.com/v1/files');
    const models = await proxy('https://api.openai.com/v1/models');
    const modelsQ = await proxy('https://api.openai.com/v1/models?limit=1');
    expect(new Set([files, models, modelsQ]).size).toBe(3);
    expect(await proxy('https://api.openai.com/v1/models')).toBe(models);
  });

  it('separates proxy callers by custom-named forwarded credential headers', async () => {
    const proxy = (headers: Record<string, string>) =>
      computeCacheScope({
        provider: 'azure-openai',
        providerOption: { provider: 'azure-openai' },
        requestHeaders: headers,
        endpoint: 'proxy',
        requestURL: 'https://apim.example.com/openai/chat',
      });
    const a = await proxy({ 'ocp-apim-subscription-key': 'tenant-a' });
    const b = await proxy({ 'ocp-apim-subscription-key': 'tenant-b' });
    expect(a).not.toBe(b);
    // Per-request tracing headers do not split the same caller's entries.
    expect(
      await proxy({
        'ocp-apim-subscription-key': 'tenant-a',
        traceparent: '00-abc-def-01',
        'x-request-id': 'r1',
      })
    ).toBe(a);
  });

  it('separates callers by headers named in forward_headers', async () => {
    const fwd = (value: string) =>
      scopeFor(SECRET_A, {
        requestHeaders: {
          authorization: `Bearer ${SECRET_A}`,
          'x-custom-key': value,
        },
        forwardHeaders: ['X-Custom-Key'],
      });
    expect(await fwd('one')).not.toBe(await fwd('two'));
  });

  it('separates keyless callers on different custom hosts', async () => {
    const host = (requestURL: string) =>
      computeCacheScope({
        provider: 'ollama',
        providerOption: { provider: 'ollama' },
        requestHeaders: {},
        endpoint: 'chatComplete',
        requestURL,
      });
    expect(await host('http://tenant-a:11434/v1/chat/completions')).not.toBe(
      await host('http://tenant-b:11434/v1/chat/completions')
    );
  });

  it('never puts raw secrets in the scope or the key', async () => {
    const honoContext = fakeHono();
    const service = new CacheService(honoContext, {
      results: undefined,
    } as any);
    const vertexKey = '-----BEGIN PRIVATE KEY-----abc';
    const ctx = requestContext(honoContext, {
      providerOption: {
        provider: 'openai',
        apiKey: SECRET_A,
        vertexServiceAccountJson: { private_key: vertexKey },
      },
      requestHeaders: { authorization: `Bearer ${SECRET_A}` },
    });
    await service.getCachedResponse(ctx, {});
    await putInCache(
      {},
      {},
      body,
      { ok: 1 },
      'chatComplete',
      '',
      'simple',
      maxAge(),
      honoContext.get('psCacheScopes')['0']
    );
    const hit = await service.getCachedResponse(ctx, {});

    expect(hit.cacheStatus).toBe('HIT');
    const scope = honoContext.get('psCacheScopes')['0'];
    for (const value of [scope, hit.cacheKey]) {
      expect(value).toMatch(/^[0-9a-f]{64}$/);
      expect(value).not.toContain(SECRET_A);
      expect(value).not.toContain('tenantA');
      expect(value).not.toContain(vertexKey);
    }
  });

  it('CacheService does not serve caller A a response cached for caller B', async () => {
    const forCaller = (apiKey: string) => {
      const honoContext = fakeHono();
      return {
        honoContext,
        service: new CacheService(honoContext, { results: undefined } as any),
        ctx: requestContext(honoContext, {
          providerOption: { provider: 'openai', apiKey },
          requestHeaders: { authorization: `Bearer ${apiKey}` },
        }),
      };
    };
    const a = forCaller(SECRET_A);
    await a.service.getCachedResponse(a.ctx, {});
    await putInCache(
      {},
      {},
      body,
      { from: 'A' },
      'chatComplete',
      '',
      'simple',
      maxAge(),
      a.honoContext.get('psCacheScopes')['0']
    );

    const b = forCaller(SECRET_B);
    expect((await b.service.getCachedResponse(b.ctx, {})).cacheStatus).toBe(
      'MISS'
    );
    const a2 = forCaller(SECRET_A);
    expect((await a2.service.getCachedResponse(a2.ctx, {})).cacheStatus).toBe(
      'HIT'
    );
  });

  it('fails closed without a scope', async () => {
    await putInCache(
      {},
      {},
      body,
      { x: 1 },
      'chatComplete',
      '',
      'simple',
      maxAge()
    );
    const scoped = await scopeFor(SECRET_A);
    const [, missStatus] = await getFromCache(
      {},
      {},
      body,
      'chatComplete',
      '',
      'simple',
      maxAge(),
      scoped
    );
    expect(missStatus).toBe('MISS');
    const [, status] = await getFromCache(
      {},
      {},
      body,
      'chatComplete',
      '',
      'simple',
      maxAge()
    );
    expect(status).toBe('DISABLED');
  });

  it('memoryCache writes back under the scope the read recorded', async () => {
    const scope = await scopeFor(SECRET_A);
    const store = new Map<string, any>();
    const c: any = {
      set: (k: string, v: any) => store.set(k, v),
      get: (k: string) => store.get(k),
    };
    setCacheScopeForTarget(c, 0, scope);
    store.set('requestOptions', [
      {
        requestParams: { stream: false },
        lastUsedOptionIndex: 0,
        cacheMode: 'simple',
        transformedRequest: { body },
        providerOptions: { rubeusURL: 'chatComplete' },
        response: { clone: () => ({ json: async () => ({ from: 'A' }) }) },
      },
    ]);
    await memoryCache()(c, async () => {});

    const other = await scopeFor(SECRET_B);
    expect(
      (
        await getFromCache(
          {},
          {},
          body,
          'chatComplete',
          '',
          'simple',
          maxAge(),
          other
        )
      )[1]
    ).toBe('MISS');
    expect(
      (
        await getFromCache(
          {},
          {},
          body,
          'chatComplete',
          '',
          'simple',
          maxAge(),
          scope
        )
      )[1]
    ).toBe('HIT');
  });
});
