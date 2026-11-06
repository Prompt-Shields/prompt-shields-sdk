// utils/env uses top-level await, which ts-jest cannot compile here (#69).
jest.mock('../../../utils/env', () => ({ Environment: () => ({}) }));
jest.mock('../../../utils/fetch', () => ({ upstreamFetch: jest.fn() }));

import { upstreamFetch } from '../../../utils/fetch';
import {
  VERTEX_TOKEN_CACHE_MAX_ENTRIES,
  VERTEX_TOKEN_SCOPE,
  clearVertexTokenCache,
  getAccessToken,
  vertexTokenCacheKey,
  vertexTokenCacheTtl,
} from '../utils';

// PS fork (#208): Vertex tokens are cached for their real lifetime and the
// token exchange is retried once (Portkey enterprise changelog 2.32,
// re-implemented).
const fetchMock = upstreamFetch as jest.Mock;
const ctx = { get: () => undefined } as any;

let privateKeyPem: string;

const generatePem = async (): Promise<string> => {
  const pair = (await crypto.subtle.generateKey(
    {
      name: 'RSASSA-PKCS1-v1_5',
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: 'SHA-256',
    },
    true,
    ['sign', 'verify']
  )) as CryptoKeyPair;
  const der = (await crypto.subtle.exportKey(
    'pkcs8',
    pair.privateKey
  )) as ArrayBuffer;
  const b64 = Buffer.from(der).toString('base64');
  return `-----BEGIN PRIVATE KEY-----\n${b64.match(/.{1,64}/g)!.join('\n')}\n-----END PRIVATE KEY-----\n`;
};

beforeAll(async () => {
  privateKeyPem = await generatePem();
});

const account = (extra: Record<string, unknown> = {}) => ({
  project_id: 'proj',
  client_email: 'sa@proj.iam.gserviceaccount.com',
  private_key_id: 'kid-1',
  private_key: privateKeyPem,
  ...extra,
});

const tokenResponse = (body: Record<string, unknown>, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });

beforeEach(() => {
  fetchMock.mockReset();
  clearVertexTokenCache();
});

describe('vertexTokenCacheTtl', () => {
  it('uses expires_in less a five-minute margin', () => {
    expect(vertexTokenCacheTtl(3599)).toBe(3299);
    expect(vertexTokenCacheTtl(900)).toBe(600);
  });

  it('does not cache a token that expires within the margin', () => {
    expect(vertexTokenCacheTtl(120)).toBe(0);
  });

  it('falls back to one hour when expires_in is missing or invalid', () => {
    expect(vertexTokenCacheTtl(undefined)).toBe(3300);
    expect(vertexTokenCacheTtl('3600')).toBe(3300);
    expect(vertexTokenCacheTtl(-1)).toBe(3300);
  });
});

describe('vertexTokenCacheKey', () => {
  it('depends on the private key, not only on the public identifiers', async () => {
    const a = await vertexTokenCacheKey(account());
    const b = await vertexTokenCacheKey(account({ private_key: 'other' }));
    expect(a).not.toBe(b);
    expect(a).not.toContain('kid-1');
    expect(a).not.toContain('sa@proj');
  });

  it('depends on the OAuth scope and audience', async () => {
    const base = await vertexTokenCacheKey(account());
    expect(
      await vertexTokenCacheKey(account(), VERTEX_TOKEN_SCOPE, undefined)
    ).toBe(base);
    expect(
      await vertexTokenCacheKey(
        account(),
        'https://www.googleapis.com/auth/devstorage.read_only'
      )
    ).not.toBe(base);
    expect(
      await vertexTokenCacheKey(
        account(),
        VERTEX_TOKEN_SCOPE,
        'https://example.test/token'
      )
    ).not.toBe(base);
  });
});

describe('getAccessToken single-flight', () => {
  const deferred = () => {
    let resolve!: (r: Response) => void;
    let reject!: (e: unknown) => void;
    const promise = new Promise<Response>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  };
  // Lets the callers reach the token exchange (key digest, JWT signing).
  const waitForFetch = async (calls: number) => {
    for (let i = 0; i < 200 && fetchMock.mock.calls.length < calls; i++) {
      await new Promise((r) => setTimeout(r, 5));
    }
  };

  it('shares one token exchange between concurrent callers', async () => {
    const pending = deferred();
    fetchMock.mockReturnValueOnce(pending.promise);
    const callers = Array.from({ length: 8 }, () =>
      getAccessToken(ctx, account())
    );
    await waitForFetch(1);
    pending.resolve(
      tokenResponse({ access_token: 'shared', expires_in: 3599 })
    );
    expect(await Promise.all(callers)).toEqual(Array(8).fill('shared'));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // And the result is cached for later callers.
    expect(await getAccessToken(ctx, account())).toBe('shared');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('does not share an exchange between different credentials', async () => {
    fetchMock.mockImplementation(async () =>
      tokenResponse({ access_token: 'tok' })
    );
    await Promise.all([
      getAccessToken(ctx, account()),
      getAccessToken(ctx, account({ private_key_id: 'kid-2' })),
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('fails every waiting caller on a failed exchange; the next call refetches', async () => {
    const first = deferred();
    const retry = deferred();
    fetchMock
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(retry.promise);
    const callers = Array.from({ length: 5 }, () =>
      getAccessToken(ctx, account())
    );
    await waitForFetch(1);
    first.reject(new Error('ECONNRESET'));
    await waitForFetch(2);
    retry.resolve(tokenResponse({ error: 'backend' }, 503));
    expect(await Promise.all(callers)).toEqual(Array(5).fill(''));
    expect(fetchMock).toHaveBeenCalledTimes(2);

    fetchMock.mockResolvedValueOnce(tokenResponse({ access_token: 'later' }));
    expect(await getAccessToken(ctx, account())).toBe('later');
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});

describe('getAccessToken', () => {
  it('caches the token for later requests with the same credential', async () => {
    fetchMock.mockResolvedValue(
      tokenResponse({ access_token: 'tok-1', expires_in: 3599 })
    );
    expect(await getAccessToken(ctx, account())).toBe('tok-1');
    expect(await getAccessToken(ctx, account())).toBe('tok-1');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('does not reuse a cached token for a different private key', async () => {
    fetchMock
      .mockResolvedValueOnce(tokenResponse({ access_token: 'tok-a' }))
      .mockResolvedValueOnce(tokenResponse({ access_token: 'tok-b' }));
    expect(await getAccessToken(ctx, account())).toBe('tok-a');
    const pem = await generatePem();
    expect(await getAccessToken(ctx, account({ private_key: pem }))).toBe(
      'tok-b'
    );
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('refetches once the cached token is past its lifetime', async () => {
    const now = jest.spyOn(Date, 'now');
    try {
      now.mockReturnValue(1_000_000);
      fetchMock
        .mockResolvedValueOnce(
          tokenResponse({ access_token: 'old', expires_in: 900 })
        )
        .mockResolvedValueOnce(
          tokenResponse({ access_token: 'new', expires_in: 900 })
        );
      expect(await getAccessToken(ctx, account())).toBe('old');
      now.mockReturnValue(1_000_000 + 599_000);
      expect(await getAccessToken(ctx, account())).toBe('old');
      now.mockReturnValue(1_000_000 + 600_000);
      expect(await getAccessToken(ctx, account())).toBe('new');
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      now.mockRestore();
    }
  });

  it('retries once on a 5xx or a network error', async () => {
    fetchMock
      .mockResolvedValueOnce(tokenResponse({ error: 'backend' }, 503))
      .mockResolvedValueOnce(tokenResponse({ access_token: 'tok' }));
    expect(await getAccessToken(ctx, account())).toBe('tok');
    expect(fetchMock).toHaveBeenCalledTimes(2);

    clearVertexTokenCache();
    fetchMock.mockReset();
    fetchMock
      .mockRejectedValueOnce(new Error('ECONNRESET'))
      .mockResolvedValueOnce(tokenResponse({ access_token: 'tok2' }));
    expect(await getAccessToken(ctx, account())).toBe('tok2');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('retries only once and caches nothing on failure', async () => {
    fetchMock.mockResolvedValue(tokenResponse({ error: 'backend' }, 500));
    expect(await getAccessToken(ctx, account())).toBe('');
    expect(fetchMock).toHaveBeenCalledTimes(2);
    fetchMock.mockReset();
    fetchMock.mockResolvedValue(tokenResponse({ access_token: 'later' }));
    expect(await getAccessToken(ctx, account())).toBe('later');
  });

  it('does not retry a rejected credential', async () => {
    fetchMock.mockResolvedValue(tokenResponse({ error: 'invalid_grant' }, 400));
    expect(await getAccessToken(ctx, account())).toBe('');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('never returns or caches a missing access_token', async () => {
    fetchMock.mockResolvedValue(tokenResponse({ token_type: 'Bearer' }));
    expect(await getAccessToken(ctx, account())).toBe('');
    expect(await getAccessToken(ctx, account())).toBe('');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('uses the gateway cache hooks with the hashed key and real TTL when set', async () => {
    const store = new Map<string, unknown>();
    const put = jest.fn(async (_env, key, value) => store.set(key, value));
    const hooked = {
      get: (name: string) =>
        name === 'getFromCacheByKey'
          ? async (_env: unknown, key: string) => store.get(key)
          : name === 'putInCacheWithValue'
            ? put
            : undefined,
    } as any;
    fetchMock.mockResolvedValue(
      tokenResponse({ access_token: 'tok', expires_in: 1800 })
    );
    expect(await getAccessToken(hooked, account())).toBe('tok');
    expect(await getAccessToken(hooked, account())).toBe('tok');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(put).toHaveBeenCalledWith(
      expect.anything(),
      await vertexTokenCacheKey(account()),
      'tok',
      1500
    );
  });

  it('keeps the in-process cache bounded, evicting the oldest entry', async () => {
    fetchMock.mockImplementation(async () =>
      tokenResponse({ access_token: 'tok' })
    );
    for (let i = 0; i <= VERTEX_TOKEN_CACHE_MAX_ENTRIES; i++) {
      await getAccessToken(ctx, account({ private_key_id: `kid-${i}` }));
    }
    expect(fetchMock).toHaveBeenCalledTimes(VERTEX_TOKEN_CACHE_MAX_ENTRIES + 1);
    // The newest is still cached; the first was evicted.
    await getAccessToken(
      ctx,
      account({ private_key_id: `kid-${VERTEX_TOKEN_CACHE_MAX_ENTRIES}` })
    );
    expect(fetchMock).toHaveBeenCalledTimes(VERTEX_TOKEN_CACHE_MAX_ENTRIES + 1);
    await getAccessToken(ctx, account({ private_key_id: 'kid-0' }));
    expect(fetchMock).toHaveBeenCalledTimes(VERTEX_TOKEN_CACHE_MAX_ENTRIES + 2);
  });
});
