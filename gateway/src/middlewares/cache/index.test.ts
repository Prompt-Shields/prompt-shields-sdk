import { getFromCache, putInCache, __resetCacheForTests, memoryCache } from './index';

describe('cache index (store-backed)', () => {
  beforeEach(() => __resetCacheForTests());

  it('MISS then HIT for identical body+url', async () => {
    const body = { model: 'm', messages: [{ role: 'user', content: 'hi' }] };
    let [resp, status] = await getFromCache({}, {}, body, '/chat', '', 'simple', Date.now() + 10000);
    expect(status).toBe('MISS');
    await putInCache({}, {}, body, { ok: true }, '/chat', '', 'simple', Date.now() + 10000);
    [resp, status] = await getFromCache({}, {}, body, '/chat', '', 'simple', Date.now() + 10000);
    expect(status).toBe('HIT');
    expect(JSON.parse(resp as string)).toEqual({ ok: true });
  });

  it('does NOT cache streaming responses', async () => {
    const body = { model: 'm', stream: true };
    await putInCache({}, {}, body, { ok: true }, '/chat', '', 'simple', Date.now() + 10000);
    const [, status] = await getFromCache({}, {}, body, '/chat', '', 'simple', Date.now() + 10000);
    expect(status).toBe('MISS');
  });

  it('DOES cache when stream is explicitly false', async () => {
    const body = { model: 'm', stream: false };
    await putInCache({}, {}, body, { ok: true }, '/chat', '', 'simple', Date.now() + 10000);
    const [, status] = await getFromCache({}, {}, body, '/chat', '', 'simple', Date.now() + 10000);
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
    const c: any = {
      set: (k: string, v: any) => store.set(k, v),
      get: (k: string) => store.get(k),
    };
    const next = async () => {};

    await expect(mw(c, next)).resolves.toBeUndefined();
  });
});
