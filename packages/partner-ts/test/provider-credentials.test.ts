/**
 * Provider admin credentials and key fingerprint links, against a mocked
 * fetch.
 */
import { APIConnectionError, APIError, NotFoundError, PartnerClient, PartnerError, PermissionDeniedError } from '../src';
import type { ProviderCredential, ProviderKeyFingerprintLink } from '../src';
import { json, mockFetch, noSleep, token } from './helpers';

const client = (fetch: ReturnType<typeof mockFetch>['fetch']) =>
  new PartnerClient({ apiKey: 'k', fetch, sleep: noSleep });

const ADMIN_KEY = 'sk-admin-0123456789abcdefWXYZ';

const CREDENTIAL_WIRE = {
  provider: 'openai',
  key_hint: '****WXYZ',
  key_id: 'k2',
  created_at: '2026-11-01T10:00:00Z',
  rotated_at: null,
};

const CREDENTIAL: ProviderCredential = {
  provider: 'openai',
  keyHint: '****WXYZ',
  keyId: 'k2',
  createdAt: '2026-11-01T10:00:00Z',
  rotatedAt: null,
};

const LINK_WIRE = {
  provider: 'anthropic',
  api_key_id: 'key_abc123',
  api_key_fingerprint: '3f9a1c0b7d2e4f60',
  created_at: '2026-11-02T09:00:00Z',
};

const LINK: ProviderKeyFingerprintLink = {
  provider: 'anthropic',
  apiKeyId: 'key_abc123',
  apiKeyFingerprint: '3f9a1c0b7d2e4f60',
  createdAt: '2026-11-02T09:00:00Z',
};

describe('providerCredentials', () => {
  it('lists stored keys, masked, with the env fallback', async () => {
    const m = mockFetch(json({ data: [CREDENTIAL_WIRE], meta: { providers: ['openai', 'anthropic'], env_fallback: ['anthropic'] } }));
    const res = await client(m.fetch).providerCredentials.list();
    expect(res).toEqual({ data: [CREDENTIAL], meta: { providers: ['openai', 'anthropic'], envFallback: ['anthropic'] } });
    expect(m.calls[0].method).toBe('GET');
    expect(m.calls[0].url).toBe('https://api.promptshields.io/partner/v1/provider-credentials');
    expect(m.calls[0].headers.Authorization).toBe('Bearer k');
  });

  it('gets one key', async () => {
    const m = mockFetch(json(CREDENTIAL_WIRE));
    expect(await client(m.fetch).providerCredentials.get('openai')).toEqual(CREDENTIAL);
    expect(m.calls[0].url).toBe('https://api.promptshields.io/partner/v1/provider-credentials/openai');
  });

  it('throws NotFoundError when no key is stored', async () => {
    const m = mockFetch(json({ type: 'about:blank', title: 'Not Found', status: 404, detail: 'No stored credential for this provider' }, 404));
    await expect(client(m.fetch).providerCredentials.get('anthropic')).rejects.toBeInstanceOf(NotFoundError);
  });

  it('stores a key as admin_key and returns it masked', async () => {
    const m = mockFetch(json(CREDENTIAL_WIRE, 201));
    const res = await client(m.fetch).providerCredentials.create('openai', { adminKey: ADMIN_KEY });
    expect(res).toEqual(CREDENTIAL);
    expect(m.calls[0].method).toBe('POST');
    expect(m.calls[0].url).toBe('https://api.promptshields.io/partner/v1/provider-credentials/openai');
    expect(m.calls[0].headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(m.calls[0].body as string)).toEqual({ admin_key: ADMIN_KEY });
  });

  it('sends only admin_key, whatever else the request object holds', async () => {
    const m = mockFetch(json(CREDENTIAL_WIRE, 201));
    const request = { adminKey: ADMIN_KEY, extra: 'x' } as unknown as { adminKey: string };
    await client(m.fetch).providerCredentials.create('openai', request);
    expect(JSON.parse(m.calls[0].body as string)).toEqual({ admin_key: ADMIN_KEY });
  });

  it('surfaces a 409 as an APIError and does not retry create', async () => {
    const m = mockFetch(json({ title: 'Conflict', status: 409, detail: 'A key is already stored' }, 409));
    const err = await client(m.fetch)
      .providerCredentials.create('openai', { adminKey: ADMIN_KEY })
      .catch((e) => e);
    expect(err).toBeInstanceOf(APIError);
    expect(err.status).toBe(409);
    expect(m.calls).toHaveLength(1);
  });

  it('does not resend a create after a network error or a 503', async () => {
    const network = mockFetch(new TypeError('socket hang up'));
    await expect(
      client(network.fetch).providerCredentials.create('openai', { adminKey: ADMIN_KEY }),
    ).rejects.toBeInstanceOf(APIConnectionError);
    expect(network.calls).toHaveLength(1);

    const unavailable = mockFetch(json({ title: 'Service Unavailable', status: 503, detail: 'keyring not configured' }, 503));
    const err = await client(unavailable.fetch)
      .providerCredentials.rotate('openai', { adminKey: ADMIN_KEY })
      .catch((e) => e);
    expect(err).toBeInstanceOf(APIError);
    expect(err.status).toBe(503);
    expect(unavailable.calls).toHaveLength(1);
  });

  it('never puts the admin key in an error message', async () => {
    const m = mockFetch(json({ title: 'Bad Request', status: 400, detail: 'admin_key must be 20 to 512 printable characters' }, 400));
    const err = await client(m.fetch)
      .providerCredentials.create('openai', { adminKey: ADMIN_KEY })
      .catch((e) => e);
    expect(err).toBeInstanceOf(APIError);
    expect(String(err.message)).not.toContain(ADMIN_KEY);
    expect(JSON.stringify(err)).not.toContain(ADMIN_KEY);
  });

  it('scrubs the admin key from an error response that echoes it', async () => {
    const echo = {
      detail: [{ type: 'string_too_long', loc: ['body', 'admin_key'], msg: `bad value ${ADMIN_KEY}`, input: ADMIN_KEY }],
    };
    const m = mockFetch(
      json(echo, 422),
      new Response(`upstream rejected ${ADMIN_KEY}`, { status: 502, headers: { 'x-echo': ADMIN_KEY } }),
    );
    const c = client(m.fetch);
    for (const call of [
      () => c.providerCredentials.create('openai', { adminKey: ADMIN_KEY }),
      () => c.providerCredentials.rotate('openai', { adminKey: ADMIN_KEY }),
    ]) {
      const err = await call().catch((e) => e);
      expect(err).toBeInstanceOf(APIError);
      expect(err.message).toContain('[redacted]');
      for (const text of [err.message, String(err), JSON.stringify(err.body), JSON.stringify(err.headers), JSON.stringify(err)]) {
        expect(text).not.toContain(ADMIN_KEY);
      }
    }
    expect(m.calls).toHaveLength(2);
  });

  it('rotates a key', async () => {
    const m = mockFetch(json({ ...CREDENTIAL_WIRE, rotated_at: '2026-11-05T12:00:00Z' }));
    const res = await client(m.fetch).providerCredentials.rotate('openai', { adminKey: ADMIN_KEY });
    expect(res.rotatedAt).toBe('2026-11-05T12:00:00Z');
    expect(m.calls[0].method).toBe('POST');
    expect(m.calls[0].url).toBe('https://api.promptshields.io/partner/v1/provider-credentials/openai/rotate');
    expect(JSON.parse(m.calls[0].body as string)).toEqual({ admin_key: ADMIN_KEY });
  });

  it('deletes a key (204)', async () => {
    const m = mockFetch(new Response(null, { status: 204 }));
    await expect(client(m.fetch).providerCredentials.delete('openai')).resolves.toBeUndefined();
    expect(m.calls[0].method).toBe('DELETE');
    expect(m.calls[0].url).toBe('https://api.promptshields.io/partner/v1/provider-credentials/openai');
  });

  it('refuses an empty provider or admin key before sending', async () => {
    const m = mockFetch();
    const c = client(m.fetch);
    expect(() => c.providerCredentials.get('')).toThrow(PartnerError);
    expect(() => c.providerCredentials.create('openai', { adminKey: '' })).toThrow(PartnerError);
    expect(() => c.providerCredentials.rotate('openai', {} as { adminKey: string })).toThrow(PartnerError);
    expect(m.calls).toHaveLength(0);
  });

  it('encodes the provider into the path', async () => {
    const m = mockFetch(json(CREDENTIAL_WIRE));
    await client(m.fetch).providerCredentials.get('../admin');
    expect(m.calls[0].url).toBe('https://api.promptshields.io/partner/v1/provider-credentials/..%2Fadmin');
  });

  it('works with OAuth credentials and surfaces a missing scope as PermissionDeniedError', async () => {
    const m = mockFetch(token('t.o.k'), json({ title: 'Forbidden', status: 403, detail: 'missing scope provider-credentials:manage' }, 403));
    const c = new PartnerClient({ clientId: 'id', clientSecret: 'secret', fetch: m.fetch, sleep: noSleep });
    await expect(c.providerCredentials.list()).rejects.toBeInstanceOf(PermissionDeniedError);
    expect(m.calls[1].headers.Authorization).toBe('Bearer t.o.k');
  });
});

describe('providerKeyFingerprints', () => {
  it('lists links', async () => {
    const m = mockFetch(json({ data: [LINK_WIRE] }));
    expect(await client(m.fetch).providerKeyFingerprints.list()).toEqual([LINK]);
    expect(m.calls[0].url).toBe('https://api.promptshields.io/partner/v1/provider-key-fingerprints');
  });

  it('puts a link and returns the corroboration count', async () => {
    const m = mockFetch(json({ data: LINK_WIRE, meta: { corroborated_assets: 2 } }, 201));
    const res = await client(m.fetch).providerKeyFingerprints.put('anthropic', 'key_abc123', {
      apiKeyFingerprint: '3f9a1c0b7d2e4f60',
    });
    expect(res).toEqual({ data: LINK, meta: { corroboratedAssets: 2 } });
    expect(m.calls[0].method).toBe('PUT');
    expect(m.calls[0].url).toBe('https://api.promptshields.io/partner/v1/provider-key-fingerprints/anthropic/key_abc123');
    expect(JSON.parse(m.calls[0].body as string)).toEqual({ api_key_fingerprint: '3f9a1c0b7d2e4f60' });
  });

  it('retries a put after a 503, since it is idempotent', async () => {
    const m = mockFetch(json({ title: 'Service Unavailable', status: 503 }, 503), json({ data: LINK_WIRE, meta: { corroborated_assets: 0 } }));
    const res = await client(m.fetch).providerKeyFingerprints.put('anthropic', 'key_abc123', {
      apiKeyFingerprint: '3f9a1c0b7d2e4f60',
    });
    expect(res.meta.corroboratedAssets).toBe(0);
    expect(m.calls).toHaveLength(2);
  });

  it.each([
    ['a raw key', 'sk-ant-api03-not-a-fingerprint'],
    ['upper-case hex', '3F9A1C0B7D2E4F60'],
    ['too short', '3f9a1c0b'],
    ['empty', ''],
  ])('refuses %s as a fingerprint before sending it', async (_what, value) => {
    const m = mockFetch();
    expect(() =>
      client(m.fetch).providerKeyFingerprints.put('openai', 'key_1', { apiKeyFingerprint: value }),
    ).toThrow(PartnerError);
    expect(m.calls).toHaveLength(0);
  });

  it('does not echo a refused fingerprint in the error', () => {
    const raw = 'sk-proj-supersecretvalue1234567890';
    let message = '';
    try {
      client(mockFetch().fetch).providerKeyFingerprints.put('openai', 'key_1', { apiKeyFingerprint: raw });
    } catch (e) {
      message = (e as Error).message;
    }
    expect(message).not.toBe('');
    expect(message).not.toContain(raw);
  });

  it('deletes a link (204)', async () => {
    const m = mockFetch(new Response(null, { status: 204 }));
    await expect(client(m.fetch).providerKeyFingerprints.delete('openai', 'key_1')).resolves.toBeUndefined();
    expect(m.calls[0].method).toBe('DELETE');
    expect(m.calls[0].url).toBe('https://api.promptshields.io/partner/v1/provider-key-fingerprints/openai/key_1');
  });

  it('throws NotFoundError for a missing link', async () => {
    const m = mockFetch(json({ title: 'Not Found', status: 404 }, 404));
    await expect(client(m.fetch).providerKeyFingerprints.delete('openai', 'key_1')).rejects.toBeInstanceOf(NotFoundError);
  });
});
