import {
  getFromCache,
  putInCache,
  __resetCacheForTests,
  memoryCache,
  isCacheableResponse,
  isCompleteResponseBody,
} from './index';

const scope = 'scope-a';

describe('cache index (store-backed)', () => {
  beforeEach(() => __resetCacheForTests());

  it('MISS then HIT for identical body+url', async () => {
    const body = { model: 'm', messages: [{ role: 'user', content: 'hi' }] };
    let [resp, status] = await getFromCache(
      {},
      {},
      body,
      '/chat',
      '',
      'simple',
      Date.now() + 10000,
      scope
    );
    expect(status).toBe('MISS');
    await putInCache(
      {},
      {},
      body,
      { ok: true },
      '/chat',
      '',
      'simple',
      Date.now() + 10000,
      scope
    );
    [resp, status] = await getFromCache(
      {},
      {},
      body,
      '/chat',
      '',
      'simple',
      Date.now() + 10000,
      scope
    );
    expect(status).toBe('HIT');
    expect(JSON.parse(resp as string)).toEqual({ ok: true });
  });

  it('does NOT cache streaming responses', async () => {
    const body = { model: 'm', stream: true };
    await putInCache(
      {},
      {},
      body,
      { ok: true },
      '/chat',
      '',
      'simple',
      Date.now() + 10000,
      scope
    );
    const [, status] = await getFromCache(
      {},
      {},
      body,
      '/chat',
      '',
      'simple',
      Date.now() + 10000,
      scope
    );
    expect(status).toBe('MISS');
  });

  it('DOES cache when stream is explicitly false', async () => {
    const body = { model: 'm', stream: false };
    await putInCache(
      {},
      {},
      body,
      { ok: true },
      '/chat',
      '',
      'simple',
      Date.now() + 10000,
      scope
    );
    const [, status] = await getFromCache(
      {},
      {},
      body,
      '/chat',
      '',
      'simple',
      Date.now() + 10000,
      scope
    );
    expect(status).toBe('HIT');
  });
});

describe('memoryCache middleware (fail-open write-back)', () => {
  beforeEach(() => __resetCacheForTests());

  it('does not throw when response body is non-JSON', async () => {
    const mw = memoryCache();
    const store = new Map<string, any>();
    const requestOptions = [
      {
        requestParams: { stream: false },
        lastUsedOptionIndex: 0,
        cacheMode: 'simple',
        transformedRequest: { body: { model: 'm' } },
        providerOptions: { rubeusURL: 'chatComplete' },
        cacheMaxAge: null,
        response: {
          clone: () => ({
            json: async () => {
              throw new SyntaxError('not json');
            },
          }),
        },
      },
    ];
    store.set('requestOptions', requestOptions);
    store.set('psCacheScopes', { '0': scope });
    const c: any = {
      set: (k: string, v: any) => store.set(k, v),
      get: (k: string) => store.get(k),
    };
    const next = async () => {};

    await expect(mw(c, next)).resolves.toBeUndefined();
  });
});

describe('write-back eligibility (#72)', () => {
  const res = (status: number, type: string) =>
    new Response('{}', { status, headers: { 'content-type': type } });

  it('only a 200 JSON response is cacheable', () => {
    expect(isCacheableResponse(res(200, 'application/json'))).toBe(true);
    expect(
      isCacheableResponse(res(200, 'application/json; charset=UTF-8'))
    ).toBe(true);
    expect(isCacheableResponse(res(500, 'application/json'))).toBe(false);
    expect(isCacheableResponse(res(429, 'application/json'))).toBe(false);
    // after-request hooks failed
    expect(isCacheableResponse(res(246, 'application/json'))).toBe(false);
    expect(isCacheableResponse(res(200, 'text/event-stream'))).toBe(false);
    expect(isCacheableResponse(undefined)).toBe(false);
  });

  it('a body that reports an error or an unfinished response is not', () => {
    expect(isCompleteResponseBody({ choices: [] })).toBe(true);
    expect(isCompleteResponseBody({ status: 'completed' })).toBe(true);
    expect(isCompleteResponseBody({ error: { message: 'x' } })).toBe(false);
    expect(isCompleteResponseBody({ type: 'error' })).toBe(false);
    for (const status of ['incomplete', 'failed', 'cancelled', 'in_progress'])
      expect(isCompleteResponseBody({ status })).toBe(false);
    expect(isCompleteResponseBody(null)).toBe(false);
    expect(isCompleteResponseBody([])).toBe(false);
  });
});
