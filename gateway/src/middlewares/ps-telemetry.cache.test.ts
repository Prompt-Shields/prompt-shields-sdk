import { cacheLookupFields, cacheStatusOf } from './ps-telemetry';

describe('cacheLookupFields', () => {
  it('estimates tokens saved on HIT (ceil chars/4)', () => {
    const body = {
      model: 'm',
      messages: [{ role: 'user', content: 'x'.repeat(40) }],
    };
    expect(cacheLookupFields('HIT', body)).toEqual({
      cache_status: 'HIT',
      est_tokens_saved: Math.ceil(JSON.stringify(body).length / 4),
    });
    expect(cacheLookupFields('SEMANTIC HIT', body).est_tokens_saved).toBe(
      Math.ceil(JSON.stringify(body).length / 4)
    );
  });

  it('reports zero tokens saved on MISS and other lookups', () => {
    expect(cacheLookupFields('MISS', { model: 'm' })).toEqual({
      cache_status: 'MISS',
      est_tokens_saved: 0,
    });
    expect(cacheLookupFields('SEMANTIC BYPASS', undefined)).toEqual({
      cache_status: 'SEMANTIC BYPASS',
      est_tokens_saved: 0,
    });
  });

  it('reports nothing when the cache made no lookup', () => {
    expect(cacheLookupFields('', { model: 'm' })).toEqual({});
    expect(cacheLookupFields('DISABLED', { model: 'm' })).toEqual({});
  });
});

describe('cacheStatusOf', () => {
  const res = (value?: string) =>
    new Response('{}', {
      headers: value ? { 'x-portkey-cache-status': value } : {},
    });

  it('reads the response cache status header', () => {
    expect(cacheStatusOf(res('MISS'))).toBe('MISS');
    expect(cacheStatusOf(res())).toBe('');
  });

  it('takes the last status when the header was appended twice', () => {
    expect(cacheStatusOf(res('MISS, HIT'))).toBe('HIT');
  });
});
