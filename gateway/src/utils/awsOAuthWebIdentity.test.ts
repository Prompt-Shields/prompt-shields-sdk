// utils/env uses top-level await, which ts-jest cannot compile here.
const mockEnv: Record<string, any> = {};
jest.mock('./env', () => ({ Environment: () => mockEnv }));

import { GatewayError } from '../errors/GatewayError';
import { Options } from '../types/requestBody';
import { providerAssumedRoleCredentials } from '../providers/bedrock/utils';
import {
  _resetAwsOAuthCredentialCache,
  getAwsOAuthWebIdentityCredentials,
} from './awsOAuthWebIdentity';
import { _resetOAuthTokenCache } from './oauthClientCredentials';

const fetchSpy = jest.spyOn(globalThis, 'fetch');

const SECRET = 'cs-aws-oauth-secret-123';
const ROLE = 'arn:aws:iam::123456789012:role/ps-bedrock';
const TOKEN = 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJjbGllbnQtYSJ9.sig';
const base = {
  provider: 'bedrock',
  awsAuthType: 'oauth',
  awsRoleArn: ROLE,
  awsRegion: 'eu-west-1',
  oauthTokenUrl: 'https://idp.example.com/oauth2/token',
  oauthClientId: 'client-a',
  oauthClientSecret: SECRET,
  oauthAudience: 'sts.amazonaws.com',
} as Options;

function tokenResponse(token = TOKEN, expiresIn = 3600) {
  return new Response(
    JSON.stringify({ access_token: token, expires_in: expiresIn })
  );
}

function stsResponse(keyId = 'ASIAOAUTH1', expiresInMs = 3600 * 1000) {
  const exp = new Date(Date.now() + expiresInMs).toISOString();
  return new Response(
    `<AssumeRoleWithWebIdentityResponse><AssumeRoleWithWebIdentityResult><Credentials><AccessKeyId>${keyId}</AccessKeyId><SecretAccessKey>sts-secret</SecretAccessKey><SessionToken>sts-session</SessionToken><Expiration>${exp}</Expiration></Credentials></AssumeRoleWithWebIdentityResult></AssumeRoleWithWebIdentityResponse>`
  );
}

const urlOf = (i: number) => String(fetchSpy.mock.calls[i][0]);
const initOf = (i: number) => fetchSpy.mock.calls[i][1] as RequestInit;

beforeEach(() => {
  for (const k of Object.keys(mockEnv)) delete mockEnv[k];
  fetchSpy.mockReset();
  fetchSpy.mockRejectedValue(new Error('no network in tests'));
  _resetOAuthTokenCache();
  _resetAwsOAuthCredentialCache();
  jest.useRealTimers();
});

afterAll(() => fetchSpy.mockRestore());

describe('AWS awsAuthType oauth (client credentials -> web identity)', () => {
  test('gets an IdP token, then STS AssumeRoleWithWebIdentity', async () => {
    fetchSpy
      .mockResolvedValueOnce(tokenResponse())
      .mockResolvedValueOnce(stsResponse());
    const opts = { ...base };
    await providerAssumedRoleCredentials(opts, {});

    expect(opts.awsAccessKeyId).toBe('ASIAOAUTH1');
    expect(opts.awsSecretAccessKey).toBe('sts-secret');
    expect(opts.awsSessionToken).toBe('sts-session');

    expect(urlOf(0)).toBe('https://idp.example.com/oauth2/token');
    const idpBody = new URLSearchParams(String(initOf(0).body));
    expect(idpBody.get('grant_type')).toBe('client_credentials');
    expect(idpBody.get('audience')).toBe('sts.amazonaws.com');
    expect(initOf(0).redirect).toBe('manual');

    expect(urlOf(1)).toBe('https://sts.eu-west-1.amazonaws.com/');
    const stsInit = initOf(1);
    expect(stsInit.method).toBe('POST');
    expect(stsInit.redirect).toBe('manual');
    const stsBody = new URLSearchParams(String(stsInit.body));
    expect(stsBody.get('Action')).toBe('AssumeRoleWithWebIdentity');
    expect(stsBody.get('RoleArn')).toBe(ROLE);
    expect(stsBody.get('WebIdentityToken')).toBe(TOKEN);
    // Unsigned: the gateway's own identity is never involved.
    expect(JSON.stringify(stsInit.headers)).not.toMatch(/authorization/i);
  });

  test('caches the role credentials until shortly before expiry', async () => {
    jest.useFakeTimers({ now: Date.now(), doNotFake: ['nextTick'] });
    let sts = 0;
    fetchSpy.mockImplementation(async (url: any) =>
      String(url).includes('idp.example.com')
        ? tokenResponse('t1', 7200)
        : stsResponse(sts++ ? 'ASIASECOND' : 'ASIAFIRST', 3600 * 1000)
    );

    const first = await getAwsOAuthWebIdentityCredentials({ ...base });
    const again = await getAwsOAuthWebIdentityCredentials({ ...base });
    expect(again.accessKeyId).toBe(first.accessKeyId);
    expect(fetchSpy).toHaveBeenCalledTimes(2);

    // 5 minutes of skew: refreshed at 55 minutes, not at 54.
    jest.advanceTimersByTime(54 * 60 * 1000);
    await getAwsOAuthWebIdentityCredentials({ ...base });
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    jest.advanceTimersByTime(2 * 60 * 1000);
    const refreshed = await getAwsOAuthWebIdentityCredentials({ ...base });
    expect(refreshed.accessKeyId).toBe('ASIASECOND');
    // The IdP token (2 h) is still cached; only STS is called again.
    expect(fetchSpy).toHaveBeenCalledTimes(3);
  });

  test('concurrent requests share one exchange', async () => {
    fetchSpy
      .mockResolvedValueOnce(tokenResponse())
      .mockResolvedValueOnce(stsResponse());
    const all = await Promise.all(
      [1, 2, 3].map(() => getAwsOAuthWebIdentityCredentials({ ...base }))
    );
    expect(new Set(all.map((c) => c.accessKeyId))).toEqual(
      new Set(['ASIAOAUTH1'])
    );
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  test('a different secret, role or region is a different cache entry', async () => {
    fetchSpy.mockImplementation(async (url: any) =>
      String(url).includes('idp.example.com')
        ? tokenResponse()
        : stsResponse(`ASIA${Math.random()}`)
    );
    const a = await getAwsOAuthWebIdentityCredentials({ ...base });
    const b = await getAwsOAuthWebIdentityCredentials({
      ...base,
      oauthClientSecret: 'another-secret',
    });
    const c = await getAwsOAuthWebIdentityCredentials({
      ...base,
      awsRoleArn: 'arn:aws:iam::123456789012:role/other',
    });
    const d = await getAwsOAuthWebIdentityCredentials({
      ...base,
      awsRegion: 'us-east-1',
    });
    expect(new Set([a, b, c, d].map((x) => x.accessKeyId)).size).toBe(4);
  });

  test.each([
    ['missing role ARN', { awsRoleArn: undefined }],
    ['malformed role ARN', { awsRoleArn: 'arn:aws:iam::1:role/x' }],
    ['role ARN with injection', { awsRoleArn: `${ROLE}&Action=Other` }],
    ['hostile region', { awsRegion: 'evil.example#' }],
    ['missing secret', { oauthClientSecret: undefined }],
    ['http token URL', { oauthTokenUrl: 'http://idp.example.com/token' }],
    ['metadata token URL', { oauthTokenUrl: 'https://169.254.169.254/x' }],
    ['loopback token URL', { oauthTokenUrl: 'https://localhost/token' }],
    ['internal token URL', { oauthTokenUrl: 'https://idp.corp/token' }],
    ['k8s service token URL', { oauthTokenUrl: 'https://kube.ns.svc/token' }],
    ['single-label token URL', { oauthTokenUrl: 'https://keycloak/token' }],
  ])('refuses %s with no network call', async (_name, extra) => {
    const err: any = await providerAssumedRoleCredentials(
      { ...base, ...extra } as Options,
      {}
    ).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(GatewayError);
    expect(err.status).toBe(400);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('OAUTH_TOKEN_URL_ALLOWED_HOSTS is honoured', async () => {
    mockEnv.OAUTH_TOKEN_URL_ALLOWED_HOSTS = 'login.example.org';
    await expect(
      getAwsOAuthWebIdentityCredentials({ ...base })
    ).rejects.toMatchObject({ status: 403 });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('STS goes to AWS_ENDPOINT_DOMAIN', async () => {
    mockEnv.AWS_ENDPOINT_DOMAIN = 'amazonaws.com.cn';
    fetchSpy
      .mockResolvedValueOnce(tokenResponse())
      .mockResolvedValueOnce(stsResponse());
    await getAwsOAuthWebIdentityCredentials(
      { ...base, awsRegion: 'cn-north-1' },
      mockEnv
    );
    expect(urlOf(1)).toBe('https://sts.cn-north-1.amazonaws.com.cn/');
  });

  test.each([
    [
      'an IdP redirect',
      () => [
        new Response(null, {
          status: 302,
          headers: { location: 'http://169.254.169.254/' },
        }),
      ],
    ],
    [
      'an STS redirect',
      () => [
        tokenResponse(),
        new Response(null, {
          status: 307,
          headers: { location: 'https://evil.example/' },
        }),
      ],
    ],
    [
      'an STS error',
      () => [
        tokenResponse(),
        new Response(`<Error><Message>${TOKEN}</Message></Error>`, {
          status: 403,
        }),
      ],
    ],
    [
      'an STS body without credentials',
      () => [tokenResponse(), new Response('<x/>')],
    ],
  ])(
    'fails closed on %s without leaking secrets or following it',
    async (_name, responses) => {
      for (const r of responses()) fetchSpy.mockResolvedValueOnce(r);
      const err: any = await getAwsOAuthWebIdentityCredentials({
        ...base,
      }).catch((e: unknown) => e);
      expect(err).toBeInstanceOf(GatewayError);
      expect(err.status).toBe(502);
      expect(String(err.message)).not.toContain(SECRET);
      expect(String(err.message)).not.toContain(TOKEN);
      // No hop to the Location host.
      for (const [url] of fetchSpy.mock.calls) {
        expect(String(url)).not.toMatch(/169\.254|evil\.example/);
      }
      // A failure is not cached.
      fetchSpy
        .mockResolvedValueOnce(tokenResponse())
        .mockResolvedValueOnce(stsResponse('ASIARETRY'));
      _resetOAuthTokenCache();
      const ok = await getAwsOAuthWebIdentityCredentials({ ...base });
      expect(ok.accessKeyId).toBe('ASIARETRY');
    }
  );
});
