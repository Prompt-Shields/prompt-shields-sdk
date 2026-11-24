import {
  globToRegExp,
  modelAllowed,
  PoolCredential,
  PoolState,
  UNHEALTHY_MS,
} from './pool';
import {
  parseResolution,
  ResolutionError,
  virtualKeyOf,
  VirtualKeyResolver,
  virtualKeySettings,
} from './resolver';

const cred = (id: string, provider = 'openai'): PoolCredential => ({
  id,
  provider,
  key_fingerprint: id.padEnd(16, '0'),
  api_key: `sk-${id}`,
});

describe('modelAllowed (fnmatchcase, as the collector)', () => {
  test.each([
    [['gpt-4o*'], 'gpt-4o-mini', true],
    [['gpt-4o*'], 'gpt-4.1', false],
    [['o3?'], 'o3x', true],
    [['o3?'], 'o3', false],
    [['claude-[sh]*'], 'claude-sonnet-4-5', true],
    [['claude-[!sh]*'], 'claude-sonnet-4-5', false],
    [['models/gemini-*'], 'models/gemini-2.5-pro', true],
    [['gpt-4o.mini'], 'gpt-4oxmini', false],
    [['a+b'], 'a+b', true],
    [['[unclosed'], '[unclosed', true],
    [[], 'anything', false],
  ])('%j vs %s -> %s', (patterns, model, expected) => {
    expect(modelAllowed(patterns as string[], model as string)).toBe(expected);
  });

  test('null or undefined allows every model', () => {
    expect(modelAllowed(null, 'x')).toBe(true);
    expect(modelAllowed(undefined, 'x')).toBe(true);
  });

  test('globs compile to anchored expressions', () => {
    expect(globToRegExp('gpt-*').source.startsWith('^')).toBe(true);
    expect(globToRegExp('gpt-*').test('xgpt-4')).toBe(false);
  });
});

describe('PoolState.order', () => {
  const pool = [cred('a'), cred('b'), cred('c'), cred('z', 'anthropic')];

  test('round_robin rotates per key and provider; peek does not advance', () => {
    const s = new PoolState();
    const first = (vk = 'vk') =>
      s.order(vk, 'round_robin', 'openai', pool).map((c) => c.id);
    expect(s.order('vk', 'round_robin', 'openai', pool, false)[0].id).toBe('a');
    expect(first()).toEqual(['a', 'b', 'c']);
    expect(first()).toEqual(['b', 'c', 'a']);
    expect(first()).toEqual(['c', 'a', 'b']);
    expect(first('other')).toEqual(['a', 'b', 'c']);
    expect(
      s.order('vk', 'round_robin', 'anthropic', pool).map((c) => c.id)
    ).toEqual(['z']);
    expect(s.order('vk', 'round_robin', 'mistral-ai', pool)).toEqual([]);
  });

  test('least_recently_limited: never limited first, then oldest limit', () => {
    let now = 1_000;
    const s = new PoolState(() => now);
    const ids = () =>
      s.order('vk', 'least_recently_limited', 'openai', pool).map((c) => c.id);
    expect(ids()).toEqual(['a', 'b', 'c']);
    s.markLimited('a');
    now += 10;
    s.markLimited('b');
    expect(ids()).toEqual(['c', 'a', 'b']);
    now += 10;
    s.markLimited('c');
    expect(ids()).toEqual(['a', 'b', 'c']);
  });

  test('unhealthy credentials go last until UNHEALTHY_MS has passed or they succeed', () => {
    let now = 0;
    const s = new PoolState(() => now);
    s.markUnhealthy('a');
    expect(
      s.order('vk', 'least_recently_limited', 'openai', pool).map((c) => c.id)
    ).toEqual(['b', 'c', 'a']);
    now = UNHEALTHY_MS + 1;
    expect(s.isUnhealthy('a')).toBe(false);
    s.markUnhealthy('b');
    s.markHealthy('b');
    expect(s.isUnhealthy('b')).toBe(false);
  });
});

describe('virtualKeyOf', () => {
  test('finds the key in X-PS-Virtual-Key, a ps-vk bearer or x-api-key', () => {
    expect(
      virtualKeyOf(new Headers({ 'x-ps-virtual-key': 'ps-vk-1' }))
    ).toEqual({
      key: 'ps-vk-1',
      header: 'x-ps-virtual-key',
    });
    expect(
      virtualKeyOf(new Headers({ authorization: 'Bearer ps-vk-2' }))
    ).toEqual({ key: 'ps-vk-2', header: 'authorization' });
    expect(virtualKeyOf(new Headers({ 'x-api-key': 'ps-vk-3' }))).toEqual({
      key: 'ps-vk-3',
      header: 'x-api-key',
    });
    expect(
      virtualKeyOf(new Headers({ authorization: 'Bearer sk-own' }))
    ).toBeUndefined();
    expect(
      virtualKeyOf(new Headers({ 'x-api-key': 'sk-ant-own' }))
    ).toBeUndefined();
  });
});

describe('virtualKeySettings', () => {
  test('off unless enabled', () => {
    expect(virtualKeySettings({})).toEqual({ enabled: false });
  });

  test('needs a key and https (or an in-cluster / loopback http host)', () => {
    expect(
      virtualKeySettings({ PS_VIRTUAL_KEYS_ENABLED: 'true' }).error
    ).toMatch(/virtual-keys:resolve/);
    const base = { PS_VIRTUAL_KEYS_ENABLED: 'true', PS_API_KEY: 'ps-ik-x' };
    expect(
      virtualKeySettings({
        ...base,
        PS_COLLECTOR_URL: 'http://collector.example.com',
      }).error
    ).toMatch(/https/);
    expect(
      virtualKeySettings({
        ...base,
        PS_COLLECTOR_URL: 'http://ps-collector:8000',
      })
    ).toMatchObject({
      enabled: true,
      url: 'http://ps-collector:8000/api/v1/virtual-keys/resolve',
      apiKey: 'ps-ik-x',
      cacheMs: 30_000,
    });
    expect(
      virtualKeySettings({
        ...base,
        PS_VIRTUAL_KEYS_API_KEY: 'ps-ik-resolver',
        PS_COLLECTOR_URL: 'https://collector.example.com/',
        PS_VIRTUAL_KEYS_CACHE_SECONDS: '10',
      })
    ).toMatchObject({
      url: 'https://collector.example.com/api/v1/virtual-keys/resolve',
      apiKey: 'ps-ik-resolver',
      cacheMs: 10_000,
    });
  });

  test('the cache cannot exceed 30 s (revocation within 30 s)', () => {
    expect(
      virtualKeySettings({
        PS_VIRTUAL_KEYS_ENABLED: 'true',
        PS_API_KEY: 'k',
        PS_VIRTUAL_KEYS_CACHE_SECONDS: '31',
      }).error
    ).toMatch(/0-30/);
  });
});

describe('parseResolution', () => {
  test('refuses malformed answers', () => {
    for (const body of [
      null,
      {},
      { active: true },
      { active: true, virtual_key: { id: 'v' }, credentials: [{ id: 'c' }] },
      {
        active: true,
        virtual_key: { id: 'v', allowed_models: 'gpt-4o' },
        credentials: [],
      },
    ])
      expect(() => parseResolution(body)).toThrow(ResolutionError);
  });

  test('lower-cases providers and keeps the TTL', () => {
    const { resolution, ttlSeconds } = parseResolution({
      active: true,
      cache_ttl_seconds: 12,
      virtual_key: { id: 'v', allowed_models: null },
      credentials: [
        { id: 'c', provider: 'OpenAI', api_key: 'sk-1', key_fingerprint: 'f' },
      ],
    });
    expect(ttlSeconds).toBe(12);
    expect(resolution.active && resolution.credentials[0].provider).toBe(
      'openai'
    );
  });
});

describe('VirtualKeyResolver', () => {
  const settings = {
    enabled: true,
    url: 'https://collector.test/api/v1/virtual-keys/resolve',
    apiKey: 'ps-ik-gw',
    cacheMs: 30_000,
  };
  const active = {
    active: true,
    cache_ttl_seconds: 30,
    virtual_key: { id: 'vk-1', allowed_models: null },
    credentials: [
      { id: 'c', provider: 'openai', api_key: 'sk-1', key_fingerprint: 'f' },
    ],
  };
  const respond = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status });

  test('caches an answer for 30 s, so a revocation lands within 30 s', async () => {
    let now = 0;
    let answer: unknown = active;
    const fetchFn = jest.fn(async () => respond(answer));
    const r = new VirtualKeyResolver(fetchFn, () => now);
    expect((await r.resolve('ps-vk-a', settings)).active).toBe(true);
    answer = { active: false, reason: 'revoked' };
    now = 29_999;
    expect((await r.resolve('ps-vk-a', settings)).active).toBe(true);
    expect(fetchFn).toHaveBeenCalledTimes(1);
    now = 30_000;
    expect(await r.resolve('ps-vk-a', settings)).toEqual({
      active: false,
      reason: 'revoked',
    });
    expect(fetchFn).toHaveBeenCalledTimes(2);
    const [, init] = fetchFn.mock.calls[0] as any;
    expect(init.headers.authorization).toBe('Bearer ps-ik-gw');
    expect(JSON.parse(init.body)).toEqual({ key: 'ps-vk-a' });
  });

  test('a shorter collector TTL wins; 0 disables caching', async () => {
    let now = 0;
    const fetchFn = jest.fn(async () =>
      respond({ ...active, cache_ttl_seconds: 0 })
    );
    const r = new VirtualKeyResolver(fetchFn, () => now);
    await r.resolve('ps-vk-a', settings);
    await r.resolve('ps-vk-a', settings);
    expect(fetchFn).toHaveBeenCalledTimes(2);
  });

  test('concurrent lookups share one request; failures are not cached', async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((r) => (release = r));
    let status = 503;
    const fetchFn = jest.fn(async () => {
      await gate;
      return respond(status === 200 ? active : { detail: 'x' }, status);
    });
    const r = new VirtualKeyResolver(fetchFn);
    const a = r.resolve('ps-vk-a', settings);
    const b = r.resolve('ps-vk-a', settings);
    release();
    await expect(a).rejects.toThrow(ResolutionError);
    await expect(b).rejects.toThrow(ResolutionError);
    expect(fetchFn).toHaveBeenCalledTimes(1);
    status = 200;
    expect((await r.resolve('ps-vk-a', settings)).active).toBe(true);
    expect(fetchFn).toHaveBeenCalledTimes(2);
  });

  test('a network error or a key that is not a virtual key', async () => {
    const r = new VirtualKeyResolver(async () => {
      throw new TypeError('fetch failed');
    });
    await expect(r.resolve('ps-vk-a', settings)).rejects.toThrow('unreachable');
    expect(await r.resolve('sk-not-virtual', settings)).toEqual({
      active: false,
      reason: 'not_found',
    });
  });
});
