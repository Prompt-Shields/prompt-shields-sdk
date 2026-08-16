import { resolveCacheMode } from './resolveMode';

describe('resolveCacheMode', () => {
  it('header on -> simple', () => {
    expect(resolveCacheMode({ 'x-ps-cache': 'on' }, {})).toEqual({ mode: 'simple', forceRefresh: false });
  });
  it('header off -> DISABLED (overrides env default on)', () => {
    expect(resolveCacheMode({ 'x-ps-cache': 'off' }, { PS_CACHE_DEFAULT: 'on' }))
      .toEqual({ mode: 'DISABLED', forceRefresh: false });
  });
  it('header refresh -> simple + forceRefresh', () => {
    expect(resolveCacheMode({ 'x-ps-cache': 'refresh' }, {}))
      .toEqual({ mode: 'simple', forceRefresh: true });
  });
  it('no header, env on -> simple', () => {
    expect(resolveCacheMode({}, { PS_CACHE_DEFAULT: 'on' })).toEqual({ mode: 'simple', forceRefresh: false });
  });
  it('no header, no env -> null (defer to providerOption.cache)', () => {
    expect(resolveCacheMode({}, {})).toBeNull();
  });
  it('malformed header -> treated as default (null when no env)', () => {
    expect(resolveCacheMode({ 'x-ps-cache': 'banana' }, {})).toBeNull();
  });
});
