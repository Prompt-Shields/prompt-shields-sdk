import { CacheService } from './cacheService';

jest.mock('hono/adapter', () => ({ env: () => ({}) }));

describe('CacheService.getCachedResponse cache-mode resolution', () => {
  const buildHonoContext = (spy: jest.Mock) =>
    ({
      get: (k: string) => (k === 'getFromCache' ? spy : undefined),
    }) as any;

  const buildRequestContext = (cacheConfig: any, honoContext: any) =>
    ({
      endpoint: 'chatComplete',
      requestHeaders: {},
      transformedRequestBody: { model: 'm' },
      honoContext,
      cacheConfig,
    }) as any;

  it('(a) short-circuits DISABLED mode without reading the cache', async () => {
    const spy = jest.fn().mockResolvedValue([null, 'MISS', null]);
    const honoContext = buildHonoContext(spy);
    const service = new CacheService(honoContext, {} as any);
    const ctx = buildRequestContext(
      { mode: 'DISABLED', maxAge: undefined, cacheStatus: 'DISABLED' },
      honoContext
    );

    const result = await service.getCachedResponse(ctx, {});

    expect(result.cacheStatus).toBe('DISABLED');
    expect(spy).not.toHaveBeenCalled();
  });

  it('(b) injects force-refresh header when forceRefresh is set', async () => {
    const spy = jest.fn().mockResolvedValue([null, 'MISS', null]);
    const honoContext = buildHonoContext(spy);
    const service = new CacheService(honoContext, {} as any);
    const ctx = buildRequestContext(
      {
        mode: 'simple',
        maxAge: undefined,
        cacheStatus: 'MISS',
        forceRefresh: true,
      },
      honoContext
    );

    await service.getCachedResponse(ctx, {});

    expect(spy).toHaveBeenCalledTimes(1);
    const headersArg = spy.mock.calls[0][1];
    expect(headersArg['x-portkey-cache-force-refresh']).toBe('true');
  });

  it('(c) does not inject force-refresh header when forceRefresh is absent', async () => {
    const spy = jest.fn().mockResolvedValue([null, 'MISS', null]);
    const honoContext = buildHonoContext(spy);
    const service = new CacheService(honoContext, {} as any);
    const ctx = buildRequestContext(
      { mode: 'simple', maxAge: undefined, cacheStatus: 'MISS' },
      honoContext
    );

    await service.getCachedResponse(ctx, {});

    expect(spy).toHaveBeenCalledTimes(1);
    const headersArg = spy.mock.calls[0][1];
    expect(headersArg).not.toHaveProperty('x-portkey-cache-force-refresh');
  });
});
