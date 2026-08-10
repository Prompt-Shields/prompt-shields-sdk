import { getFromCache, putInCache, __resetCacheForTests } from './index';

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
