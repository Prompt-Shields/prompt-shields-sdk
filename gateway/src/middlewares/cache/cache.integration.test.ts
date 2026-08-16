import { getFromCache, putInCache, __resetCacheForTests } from './index';
import { resolveCacheMode } from './resolveMode';

const body = { model: 'm', messages: [{ role: 'user', content: 'hello world' }] };
const url = '/chat/completions';

describe('cache integration', () => {
  beforeEach(() => __resetCacheForTests());

  it('second identical request is a HIT', async () => {
    let [, status] = await getFromCache({}, {}, body, url, '', 'simple', Date.now() + 10000);
    expect(status).toBe('MISS');
    await putInCache({}, {}, body, { answer: 42 }, url, '', 'simple', Date.now() + 10000);
    [, status] = await getFromCache({}, {}, body, url, '', 'simple', Date.now() + 10000);
    expect(status).toBe('HIT');
  });

  it('read and write derive the same key for identical endpoint+body', async () => {
    const endpoint = 'chatComplete';
    await putInCache({}, {}, body, { answer: 7 }, endpoint, '', 'simple', Date.now() + 10000);
    const [, status] = await getFromCache({}, {}, body, endpoint, '', 'simple', Date.now() + 10000);
    expect(status).toBe('HIT'); // same-endpoint + same-body -> same key
  });

  it('X-PS-Cache: off resolves to DISABLED regardless of env', () => {
    expect(resolveCacheMode({ 'x-ps-cache': 'off' }, { PS_CACHE_DEFAULT: 'on' })!.mode).toBe('DISABLED');
  });

  it('PS_CACHE_DEFAULT=on caches when no header present', () => {
    expect(resolveCacheMode({}, { PS_CACHE_DEFAULT: 'on' })!.mode).toBe('simple');
  });

  it('refresh triggers force-refresh header behavior', async () => {
    await putInCache({}, {}, body, { answer: 1 }, url, '', 'simple', Date.now() + 10000);
    const [, status] = await getFromCache(
      {}, { 'x-portkey-cache-force-refresh': 'true' }, body, url, '', 'simple', Date.now() + 10000
    );
    expect(status).toBe('REFRESH');
  });
});
