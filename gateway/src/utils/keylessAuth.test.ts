// utils/env uses top-level await, which ts-jest cannot compile here.
const mockEnv: Record<string, any> = {};
jest.mock('./env', () => ({ Environment: () => mockEnv }));

import { GatewayError } from '../errors/GatewayError';
import { Options } from '../types/requestBody';
import OpenAIAPIConfig from '../providers/openai/api';
import { GoogleApiConfig } from '../providers/google-vertex-ai/api';
import { _resetGcpHostTokenCache } from './gcpAmbientAuth';
import { isSensitiveKey } from './redact';
import {
  _resetOAuthTokenCache,
  getOAuthClientCredentialsToken,
  oauthCacheKey,
} from './oauthClientCredentials';

const fetchSpy = jest.spyOn(globalThis, 'fetch');

const SECRET = 'cs-super-secret-value-123';
const oauth = {
  oauthTokenUrl: 'https://idp.example.com/oauth2/token',
  oauthClientId: 'client-a',
  oauthClientSecret: SECRET,
  oauthScope: 'llm.invoke',
};

function tokenResponse(token: string, expiresIn = 3600) {
  return new Response(
    JSON.stringify({ access_token: token, expires_in: expiresIn })
  );
}

function vertexHeaders(providerOptions: Partial<Options>) {
  return GoogleApiConfig.headers({
    providerOptions: {
      provider: 'vertex-ai',
      vertexRegion: 'us-central1',
      vertexProjectId: 'proj',
      ...providerOptions,
    } as Options,
    fn: 'chatComplete',
    c: {} as any,
    gatewayRequestBody: {},
  } as any);
}

function openaiHeaders(providerOptions: Partial<Options>) {
  return OpenAIAPIConfig.headers({
    providerOptions: { provider: 'openai', ...providerOptions } as Options,
    fn: 'chatComplete',
    c: {} as any,
  } as any);
}

beforeEach(() => {
  for (const k of Object.keys(mockEnv)) delete mockEnv[k];
  fetchSpy.mockReset();
  fetchSpy.mockRejectedValue(new Error('no network in tests'));
  _resetGcpHostTokenCache();
  _resetOAuthTokenCache();
  jest.useRealTimers();
});

afterAll(() => fetchSpy.mockRestore());

describe('Vertex workload identity (host Google identity)', () => {
  test('is refused with 403 and makes no metadata request when the gate is off', async () => {
    const err: any = await vertexHeaders({ vertexAuthType: 'workload' }).catch(
      (e: unknown) => e
    );
    expect(err).toBeInstanceOf(GatewayError);
    expect(err.status).toBe(403);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('only "true" enables it', async () => {
    mockEnv.GCP_AMBIENT_CREDENTIALS_ENABLED = '1';
    await expect(
      vertexHeaders({ vertexAuthType: 'workload' })
    ).rejects.toMatchObject({ status: 403 });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('unknown auth types are rejected without a network call', async () => {
    mockEnv.GCP_AMBIENT_CREDENTIALS_ENABLED = 'true';
    await expect(
      vertexHeaders({ vertexAuthType: 'metadata' })
    ).rejects.toMatchObject({ status: 400 });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('uses the metadata server when enabled and caches the token', async () => {
    mockEnv.GCP_AMBIENT_CREDENTIALS_ENABLED = 'TRUE';
    fetchSpy.mockResolvedValue(tokenResponse('host-token'));
    const headers: any = await vertexHeaders({ vertexAuthType: 'workload' });
    expect(headers.Authorization).toBe('Bearer host-token');
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token'
    );
    expect((init.headers as any)['Metadata-Flavor']).toBe('Google');

    await vertexHeaders({ vertexAuthType: 'workload' });
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  test.each([
    ['a config custom host', { customHost: 'https://attacker.example' }],
    ['a region that rewrites the host', { vertexRegion: 'attacker.example#' }],
    ['a region with a path', { vertexRegion: 'x.attacker.example/a?' }],
  ])('refuses %s before minting the host token', async (_name, extra) => {
    mockEnv.GCP_AMBIENT_CREDENTIALS_ENABLED = 'true';
    fetchSpy.mockResolvedValue(tokenResponse('host-token'));
    await expect(
      vertexHeaders({ vertexAuthType: 'workload', ...extra })
    ).rejects.toMatchObject({ status: 400 });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('refuses a custom host request header before minting the host token', async () => {
    mockEnv.GCP_AMBIENT_CREDENTIALS_ENABLED = 'true';
    fetchSpy.mockResolvedValue(tokenResponse('host-token'));
    const c = {
      req: {
        header: (n: string) =>
          n === 'x-portkey-custom-host'
            ? 'https://attacker.example'
            : undefined,
      },
    };
    await expect(
      GoogleApiConfig.headers({
        providerOptions: {
          provider: 'vertex-ai',
          vertexRegion: 'us-central1',
          vertexAuthType: 'workload',
        } as Options,
        fn: 'chatComplete',
        c: c as any,
        gatewayRequestBody: {},
      } as any)
    ).rejects.toMatchObject({ status: 400 });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('a failed metadata request is not cached', async () => {
    mockEnv.GCP_AMBIENT_CREDENTIALS_ENABLED = 'true';
    fetchSpy.mockResolvedValueOnce(new Response('', { status: 500 }));
    await expect(
      vertexHeaders({ vertexAuthType: 'workload' })
    ).rejects.toMatchObject({ status: 502 });
    fetchSpy.mockResolvedValueOnce(tokenResponse('host-token'));
    const headers: any = await vertexHeaders({ vertexAuthType: 'workload' });
    expect(headers.Authorization).toBe('Bearer host-token');
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  test('a caller API key or service account is unaffected by the gate', async () => {
    const headers: any = await vertexHeaders({ apiKey: 'caller-key' });
    expect(headers.Authorization).toBe('Bearer caller-key');
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe('OAuth client credentials for OpenAI-compatible upstreams', () => {
  test('exchanges client credentials for a bearer token', async () => {
    fetchSpy.mockResolvedValue(tokenResponse('oauth-token'));
    const headers: any = await openaiHeaders({ apiKey: 'ignored', ...oauth });
    expect(headers.Authorization).toBe('Bearer oauth-token');
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(oauth.oauthTokenUrl);
    expect(init.redirect).toBe('manual');
    const body = new URLSearchParams(String(init.body));
    expect(body.get('grant_type')).toBe('client_credentials');
    expect(body.get('client_id')).toBe('client-a');
    expect(body.get('scope')).toBe('llm.invoke');
  });

  test('plain API keys are unaffected', async () => {
    const headers: any = await openaiHeaders({ apiKey: 'sk-caller' });
    expect(headers.Authorization).toBe('Bearer sk-caller');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('partial credentials are a 400 with no token request', async () => {
    await expect(
      openaiHeaders({ oauthTokenUrl: oauth.oauthTokenUrl })
    ).rejects.toMatchObject({ status: 400 });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test.each([
    'http://idp.example.com/token',
    'https://169.254.169.254/latest/meta-data',
    'https://metadata.google.internal/token',
    'https://localhost/token',
    'https://127.0.0.1/token',
    'https://10.0.0.5/token',
    'https://[::1]/token',
    'https://user:pass@idp.example.com/token',
    'https://idp.svc.cluster.local/token',
    'https://2130706433/token',
    'not a url',
  ])('refuses token URL %s without any request', async (oauthTokenUrl) => {
    const err: any = await openaiHeaders({ ...oauth, oauthTokenUrl }).catch(
      (e: unknown) => e
    );
    expect(err).toBeInstanceOf(GatewayError);
    expect(err.status).toBe(400);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('enforces OAUTH_TOKEN_URL_ALLOWED_HOSTS when set', async () => {
    mockEnv.OAUTH_TOKEN_URL_ALLOWED_HOSTS =
      'login.partner.example, idp2.example';
    await expect(openaiHeaders(oauth)).rejects.toMatchObject({ status: 403 });
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockResolvedValue(tokenResponse('ok'));
    const headers: any = await openaiHeaders({
      ...oauth,
      oauthTokenUrl: 'https://login.partner.example/token',
    });
    expect(headers.Authorization).toBe('Bearer ok');
  });

  test('caches per client identity, secret and scope; never across callers', async () => {
    let n = 0;
    fetchSpy.mockImplementation(async () => tokenResponse(`t${++n}`));
    expect(await getOAuthClientCredentialsToken(oauth)).toBe('t1');
    expect(await getOAuthClientCredentialsToken(oauth)).toBe('t1');
    // Same client id, wrong secret: must not get the cached token.
    expect(
      await getOAuthClientCredentialsToken({
        ...oauth,
        oauthClientSecret: 'guess',
      })
    ).toBe('t2');
    expect(
      await getOAuthClientCredentialsToken({ ...oauth, oauthScope: 'other' })
    ).toBe('t3');
    expect(
      await getOAuthClientCredentialsToken({ ...oauth, oauthClientId: 'b' })
    ).toBe('t4');
    expect(fetchSpy).toHaveBeenCalledTimes(4);
  });

  test('concurrent requests share one token request', async () => {
    fetchSpy.mockImplementation(async () => tokenResponse('shared'));
    const tokens = await Promise.all(
      [1, 2, 3].map(() => getOAuthClientCredentialsToken(oauth))
    );
    expect(tokens).toEqual(['shared', 'shared', 'shared']);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  test('a failed token request is not cached', async () => {
    fetchSpy.mockResolvedValueOnce(new Response('', { status: 503 }));
    await expect(getOAuthClientCredentialsToken(oauth)).rejects.toMatchObject({
      status: 502,
    });
    fetchSpy.mockResolvedValueOnce(tokenResponse('fresh'));
    expect(await getOAuthClientCredentialsToken(oauth)).toBe('fresh');
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  test.each([
    'https://LOCALHOST/token',
    'https://localhost./token',
    'https://0x7f.1/token',
    'https://0177.0.0.1/token',
    'https://[::ffff:127.0.0.1]/token',
    'https://metadata.google.internal./token',
    'https://idp.internal:8443/token',
  ])('refuses bypass attempt %s without any request', async (oauthTokenUrl) => {
    await expect(
      openaiHeaders({ ...oauth, oauthTokenUrl })
    ).rejects.toMatchObject({ status: 400 });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('refreshes before expiry', async () => {
    jest.useFakeTimers({ now: 1_000_000, doNotFake: ['nextTick'] });
    let n = 0;
    fetchSpy.mockImplementation(async () => tokenResponse(`t${++n}`, 600));
    expect(await getOAuthClientCredentialsToken(oauth)).toBe('t1');
    jest.setSystemTime(1_000_000 + 530_000);
    expect(await getOAuthClientCredentialsToken(oauth)).toBe('t1');
    // Within the 60s refresh window of the 600s token.
    jest.setSystemTime(1_000_000 + 545_000);
    expect(await getOAuthClientCredentialsToken(oauth)).toBe('t2');
  });

  test('the cache key holds no secret material', async () => {
    const key = await oauthCacheKey(
      oauth.oauthTokenUrl,
      oauth.oauthClientId,
      SECRET,
      oauth.oauthScope
    );
    expect(key).toMatch(/^[0-9a-f]{64}$/);
    expect(key).not.toContain(SECRET);
  });

  test.each([
    [
      'an error status echoing the secret',
      () => new Response(`invalid_client: ${SECRET}`, { status: 401 }),
    ],
    [
      'a body without access_token',
      () => new Response(JSON.stringify({ error: SECRET })),
    ],
    ['a non-JSON body', () => new Response(`<html>${SECRET}</html>`)],
  ])('errors on %s without leaking the secret', async (_name, make) => {
    fetchSpy.mockImplementation(async () => make());
    const err: any = await openaiHeaders(oauth).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(GatewayError);
    expect(err.status).toBe(502);
    expect(err.message).not.toContain(SECRET);
    expect(JSON.stringify(err)).not.toContain(SECRET);
    expect(err.cause).toBeUndefined();
  });

  test('network failures do not leak the secret', async () => {
    fetchSpy.mockRejectedValue(new Error(`connect failed body=${SECRET}`));
    const err: any = await openaiHeaders(oauth).catch((e: unknown) => e);
    expect(err.status).toBe(502);
    expect(err.message).not.toContain(SECRET);
  });

  test('the secret header and option name are masked by the redactor', () => {
    expect(isSensitiveKey('oauthClientSecret')).toBe(true);
    expect(isSensitiveKey('x-portkey-oauth-client-secret')).toBe(true);
  });
});
