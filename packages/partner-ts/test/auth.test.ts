import { AuthenticationError, PartnerClient, PartnerError } from '../src';
import { asset, json, mockFetch, noSleep, token } from './helpers';

const page = (ids: string[]) => json({ data: ids.map((id) => asset(id)), meta: { total: ids.length, has_more: false, next_cursor: null } });

describe('authentication', () => {
  afterEach(() => jest.restoreAllMocks());

  it('sends the API key as a bearer token', async () => {
    const m = mockFetch(page(['a']));
    const client = new PartnerClient({ apiKey: 'ps-pk-test', fetch: m.fetch });
    await client.assets.listPage();
    expect(m.calls[0].headers.Authorization).toBe('Bearer ps-pk-test');
    expect(m.calls[0].url).toBe('https://api.promptshields.io/partner/v1/assets');
    expect(client.usesOAuth).toBe(false);
  });

  it('exchanges client credentials for a token and caches it', async () => {
    const m = mockFetch(token('jwt.one.x'), page(['a']), page(['b']));
    const client = new PartnerClient({
      clientId: 'ps-partner-ardoq-abc123',
      clientSecret: 'sk-partner-secret-xyz789',
      baseUrl: 'https://sandbox.api.promptshields.io/',
      fetch: m.fetch,
    });
    await client.assets.listPage();
    await client.assets.listPage();

    expect(m.calls).toHaveLength(3);
    const [tokenCall, first, second] = m.calls;
    expect(tokenCall.url).toBe('https://sandbox.api.promptshields.io/oauth/token');
    expect(tokenCall.method).toBe('POST');
    expect(tokenCall.headers['Content-Type']).toBe('application/x-www-form-urlencoded');
    expect(tokenCall.headers.Authorization).toBeUndefined();
    expect(Object.fromEntries(new URLSearchParams(tokenCall.body))).toEqual({
      grant_type: 'client_credentials',
      client_id: 'ps-partner-ardoq-abc123',
      client_secret: 'sk-partner-secret-xyz789',
    });
    expect(first.headers.Authorization).toBe('Bearer jwt.one.x');
    expect(second.headers.Authorization).toBe('Bearer jwt.one.x');
  });

  it('prefers OAuth when both credentials are given', async () => {
    const m = mockFetch(token('jwt.x.y'), page([]));
    const client = new PartnerClient({ apiKey: 'ps-pk-x', clientId: 'id', clientSecret: 's', fetch: m.fetch });
    await client.assets.listPage();
    expect(m.calls[1].headers.Authorization).toBe('Bearer jwt.x.y');
  });

  it('refreshes the token shortly before it expires', async () => {
    let now = 1_000_000;
    jest.spyOn(Date, 'now').mockImplementation(() => now);
    const m = mockFetch(token('t.1.x', 3600), page([]), token('t.2.x', 3600), page([]));
    const client = new PartnerClient({ clientId: 'id', clientSecret: 's', fetch: m.fetch });

    await client.assets.listPage();
    now += (3600 - 30) * 1000; // inside the 60s refresh window
    await client.assets.listPage();

    expect(m.calls.map((c) => c.url.endsWith('/oauth/token'))).toEqual([true, false, true, false]);
    expect(m.calls[3].headers.Authorization).toBe('Bearer t.2.x');
  });

  it('shares one token request across concurrent calls', async () => {
    const m = mockFetch(token('t.c.x'), page([]), page([]));
    const client = new PartnerClient({ clientId: 'id', clientSecret: 's', fetch: m.fetch });
    await Promise.all([client.assets.listPage(), client.assets.listPage()]);
    expect(m.calls.filter((c) => c.url.endsWith('/oauth/token'))).toHaveLength(1);
  });

  it('re-authenticates once when a request returns 401', async () => {
    const unauthorized = json(
      { detail: { type: 'about:blank', title: 'Unauthorized', status: 401, detail: 'Partner credential revoked' } },
      401,
    );
    const m = mockFetch(token('old.t.x'), unauthorized, token('new.t.x'), page(['a']));
    const client = new PartnerClient({ clientId: 'id', clientSecret: 's', fetch: m.fetch });
    const res = await client.assets.listPage();
    expect(res.data).toHaveLength(1);
    expect(m.calls[3].headers.Authorization).toBe('Bearer new.t.x');
  });

  it('gives up after one re-authentication', async () => {
    const unauthorized = () =>
      json({ detail: { type: 'about:blank', title: 'Unauthorized', status: 401, detail: 'nope' } }, 401);
    const m = mockFetch(token(), unauthorized, token(), unauthorized);
    const client = new PartnerClient({ clientId: 'id', clientSecret: 's', fetch: m.fetch, sleep: noSleep });
    await expect(client.assets.listPage()).rejects.toBeInstanceOf(AuthenticationError);
    expect(m.remaining()).toBe(0);
  });

  it('does not retry a 401 for API keys', async () => {
    const m = mockFetch(json({ detail: { title: 'Unauthorized', status: 401, detail: 'Invalid API key' } }, 401));
    const client = new PartnerClient({ apiKey: 'bad', fetch: m.fetch });
    await expect(client.assets.get('x')).rejects.toMatchObject({
      status: 401,
      title: 'Unauthorized',
      detail: 'Invalid API key',
    });
    expect(m.calls).toHaveLength(1);
  });

  it('surfaces OAuth token errors as AuthenticationError', async () => {
    const m = mockFetch(
      json({ detail: { error: 'invalid_client', error_description: 'Invalid client_secret' } }, 401),
    );
    const client = new PartnerClient({ clientId: 'id', clientSecret: 'wrong', fetch: m.fetch });
    const err = await client.assets.listPage().catch((e) => e);
    expect(err).toBeInstanceOf(AuthenticationError);
    expect(err.title).toBe('invalid_client');
    expect(err.detail).toBe('Invalid client_secret');
  });

  it('validates credentials at construction', () => {
    expect(() => new PartnerClient({})).toThrow(PartnerError);
    expect(() => new PartnerClient({ clientId: 'id' })).toThrow(/together/);
    expect(() => new PartnerClient({ clientSecret: 's', apiKey: 'k' })).toThrow(/together/);
  });

  it('health() sends no Authorization header', async () => {
    const m = mockFetch(json({ status: 'ok', version: '1.0.0' }));
    const client = new PartnerClient({ clientId: 'id', clientSecret: 's', fetch: m.fetch });
    await expect(client.health()).resolves.toEqual({ status: 'ok', version: '1.0.0' });
    expect(m.calls).toHaveLength(1);
    expect(m.calls[0].headers.Authorization).toBeUndefined();
  });

  it('uses the global fetch by default', async () => {
    const spy = jest
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(json({ status: 'ok', version: '1.0.0' }));
    const client = new PartnerClient({ apiKey: 'k' });
    await client.health();
    expect(spy).toHaveBeenCalledWith('https://api.promptshields.io/partner/v1/health', expect.any(Object));
  });
});
