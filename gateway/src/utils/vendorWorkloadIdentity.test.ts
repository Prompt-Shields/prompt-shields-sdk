// utils/env uses top-level await, which ts-jest cannot compile here.
const mockEnv: Record<string, any> = {};
jest.mock('./env', () => ({ Environment: () => mockEnv }));

import fs from 'fs';
import { createHash } from 'crypto';
import { GatewayError } from '../errors/GatewayError';
import { Options } from '../types/requestBody';
import OpenAIAPIConfig from '../providers/openai/api';
import AnthropicAPIConfig from '../providers/anthropic/api';
import { credentialFingerprint } from './credentialFingerprint';
import {
  _resetWorkloadIdentityTokenCache,
  getWorkloadIdentityToken,
  withAnthropicOAuthBeta,
} from './vendorWorkloadIdentity';

const fetchSpy = jest.spyOn(globalThis, 'fetch');
const readFileSpy = jest.spyOn(fs, 'readFileSync');

const SUBJECT =
  'eyJhbGciOiJSUzI1NiIsImtpZCI6ImsxIn0.eyJzdWIiOiJ3b3JrZXIifQ.sig';
const TOKEN_FILE = '/var/run/secrets/tokens/llm';

const openaiEnv = {
  OPENAI_AMBIENT_CREDENTIALS_ENABLED: 'true',
  OPENAI_IDENTITY_TOKEN_FILE: TOKEN_FILE,
  OPENAI_IDENTITY_PROVIDER_ID: 'idp_123',
  OPENAI_SERVICE_ACCOUNT_ID: 'svc_acct_456',
};
const anthropicEnv = {
  ANTHROPIC_AMBIENT_CREDENTIALS_ENABLED: 'true',
  ANTHROPIC_IDENTITY_TOKEN_FILE: TOKEN_FILE,
  ANTHROPIC_FEDERATION_RULE_ID: 'fdrl_abc',
  ANTHROPIC_ORGANIZATION_ID: '00000000-0000-0000-0000-000000000001',
  ANTHROPIC_SERVICE_ACCOUNT_ID: 'svac_def',
};

function tokenResponse(token: string, expiresIn = 3600) {
  return new Response(
    JSON.stringify({ access_token: token, expires_in: expiresIn })
  );
}

function setEnv(values: Record<string, string>) {
  for (const k of Object.keys(mockEnv)) delete mockEnv[k];
  Object.assign(mockEnv, values);
}

function ctx(requestHeaders: Record<string, string> = {}) {
  return { req: { header: (name: string) => requestHeaders[name] } } as any;
}

async function openaiHeaders(o: Partial<Options>, c = ctx()) {
  return OpenAIAPIConfig.headers({
    providerOptions: { provider: 'openai', ...o } as Options,
    fn: 'chatComplete',
    c,
  } as any);
}

async function anthropicHeaders(o: Partial<Options>, c = ctx(), body = {}) {
  return AnthropicAPIConfig.headers({
    providerOptions: { provider: 'anthropic', ...o } as Options,
    fn: 'chatComplete',
    c,
    gatewayRequestBody: body,
  } as any);
}

async function expectGatewayError(p: Promise<unknown>, status: number) {
  const err = await p.then(
    () => undefined,
    (e) => e
  );
  expect(err).toBeInstanceOf(GatewayError);
  expect(err.status).toBe(status);
  return err as GatewayError;
}

beforeEach(() => {
  setEnv({});
  fetchSpy.mockReset();
  fetchSpy.mockRejectedValue(new Error('no network in tests'));
  readFileSpy.mockReset();
  readFileSpy.mockImplementation(((path: string) => {
    if (path === TOKEN_FILE) return `${SUBJECT}\n`;
    throw new Error('ENOENT');
  }) as any);
  _resetWorkloadIdentityTokenCache();
});

afterAll(() => {
  readFileSpy.mockRestore();
});

describe('OpenAI workload identity federation', () => {
  test('RFC 8693 exchange at auth.openai.com; the minted token is the bearer', async () => {
    setEnv(openaiEnv);
    fetchSpy.mockResolvedValueOnce(tokenResponse('oa-minted-1'));
    const headers = await openaiHeaders({
      openaiAuthType: 'workload',
      apiKey: 'sk-ignored',
    });
    expect(headers.Authorization).toBe('Bearer oa-minted-1');

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('https://auth.openai.com/oauth/token');
    expect(init.method).toBe('POST');
    expect(init.redirect).toBe('manual');
    expect((init.headers as any)['Content-Type']).toBe('application/json');
    expect(JSON.parse(init.body as string)).toEqual({
      grant_type: 'urn:ietf:params:oauth:grant-type:token-exchange',
      subject_token: SUBJECT,
      subject_token_type: 'urn:ietf:params:oauth:token-type:jwt',
      identity_provider_id: 'idp_123',
      service_account_id: 'svc_acct_456',
    });
  });

  test('OPENAI_SUBJECT_TOKEN_TYPE=id sends an id_token subject', async () => {
    setEnv({ ...openaiEnv, OPENAI_SUBJECT_TOKEN_TYPE: 'id' });
    fetchSpy.mockResolvedValueOnce(tokenResponse('oa-minted'));
    await openaiHeaders({ openaiAuthType: 'workload' });
    const body = JSON.parse(
      (fetchSpy.mock.calls[0][1] as RequestInit).body as string
    );
    expect(body.subject_token_type).toBe(
      'urn:ietf:params:oauth:token-type:id_token'
    );
  });

  test('refused (403) without the operator opt-in; no file read, no network', async () => {
    setEnv({ ...openaiEnv, OPENAI_AMBIENT_CREDENTIALS_ENABLED: 'false' });
    await expectGatewayError(
      openaiHeaders({ openaiAuthType: 'workload' }),
      403
    );
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(readFileSpy).not.toHaveBeenCalled();
  });

  test('an unknown auth type is a 400', async () => {
    setEnv(openaiEnv);
    await expectGatewayError(openaiHeaders({ openaiAuthType: 'host' }), 400);
  });

  test('a custom host is refused before the exchange (config or header)', async () => {
    setEnv(openaiEnv);
    await expectGatewayError(
      openaiHeaders({
        openaiAuthType: 'workload',
        customHost: 'https://evil.example/v1',
      }),
      400
    );
    await expectGatewayError(
      openaiHeaders(
        { openaiAuthType: 'workload' },
        ctx({ 'x-portkey-custom-host': 'https://evil.example/v1' })
      ),
      400
    );
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(readFileSpy).not.toHaveBeenCalled();
  });

  test('missing operator settings: 500 naming the variables, not values', async () => {
    setEnv({ OPENAI_AMBIENT_CREDENTIALS_ENABLED: 'true' });
    const err = await expectGatewayError(
      openaiHeaders({ openaiAuthType: 'workload' }),
      500
    );
    expect(err.message).toContain('OPENAI_IDENTITY_TOKEN_FILE');
    expect(err.message).toContain('OPENAI_SERVICE_ACCOUNT_ID');
    expect(err.message).toContain('OPENAI_IDENTITY_PROVIDER_ID');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('without workload auth the static key is unchanged', async () => {
    setEnv(openaiEnv);
    const headers = await openaiHeaders({ apiKey: 'sk-static' });
    expect(headers.Authorization).toBe('Bearer sk-static');
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe('Anthropic workload identity federation', () => {
  test('RFC 7523 jwt-bearer exchange; Bearer + OAuth beta, no X-API-Key', async () => {
    setEnv({ ...anthropicEnv, ANTHROPIC_WORKSPACE_ID: 'wrkspc_1' });
    fetchSpy.mockResolvedValueOnce(tokenResponse('sk-ant-oat01-minted'));
    const headers = await anthropicHeaders(
      { anthropicAuthType: 'workload', apiKey: 'sk-ant-ignored' },
      ctx(),
      { anthropic_beta: 'prompt-caching-2024-07-31' }
    );
    expect(headers['Authorization']).toBe('Bearer sk-ant-oat01-minted');
    expect(headers).not.toHaveProperty('X-API-Key');
    expect(headers['anthropic-beta']).toBe(
      'prompt-caching-2024-07-31,oauth-2025-04-20'
    );
    expect(headers['anthropic-version']).toBe('2023-06-01');

    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('https://api.anthropic.com/v1/oauth/token');
    expect(init.redirect).toBe('manual');
    expect((init.headers as any)['anthropic-beta']).toBe(
      'oauth-2025-04-20,oidc-federation-2026-04-01'
    );
    expect(JSON.parse(init.body as string)).toEqual({
      grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer',
      assertion: SUBJECT,
      federation_rule_id: 'fdrl_abc',
      organization_id: '00000000-0000-0000-0000-000000000001',
      service_account_id: 'svac_def',
      workspace_id: 'wrkspc_1',
    });
  });

  test('workspace_id is omitted when not configured', async () => {
    setEnv(anthropicEnv);
    fetchSpy.mockResolvedValueOnce(tokenResponse('sk-ant-oat01-x'));
    await anthropicHeaders({ anthropicAuthType: 'workload' });
    const body = JSON.parse(
      (fetchSpy.mock.calls[0][1] as RequestInit).body as string
    );
    expect(body).not.toHaveProperty('workspace_id');
  });

  test('refused (403) without the operator opt-in', async () => {
    setEnv({ ...anthropicEnv, ANTHROPIC_AMBIENT_CREDENTIALS_ENABLED: '' });
    await expectGatewayError(
      anthropicHeaders({ anthropicAuthType: 'workload' }),
      403
    );
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('a custom host is refused before the exchange', async () => {
    setEnv(anthropicEnv);
    await expectGatewayError(
      anthropicHeaders({
        anthropicAuthType: 'workload',
        customHost: 'https://evil.example',
      }),
      400
    );
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('a static key still uses X-API-Key and the caller beta unchanged', async () => {
    const headers = await anthropicHeaders({ apiKey: 'sk-ant-static' });
    expect(headers['X-API-Key']).toBe('sk-ant-static');
    expect(headers['anthropic-beta']).toBe('messages-2023-12-15');
    expect(headers).not.toHaveProperty('Authorization');
  });

  test('withAnthropicOAuthBeta adds the flag once', () => {
    expect(withAnthropicOAuthBeta(undefined)).toBe('oauth-2025-04-20');
    expect(withAnthropicOAuthBeta('a, oauth-2025-04-20')).toBe(
      'a,oauth-2025-04-20'
    );
  });
});

describe('caching and refresh', () => {
  test('cached until shortly before expiry, then re-exchanged with a re-read token', async () => {
    setEnv(openaiEnv);
    const now = jest.spyOn(Date, 'now').mockReturnValue(1_000_000);
    fetchSpy
      .mockResolvedValueOnce(tokenResponse('t1', 3600))
      .mockResolvedValueOnce(tokenResponse('t2', 3600));
    try {
      expect(await getWorkloadIdentityToken('openai')).toBe('t1');
      now.mockReturnValue(1_000_000 + 54 * 60 * 1000); // 54 min: still cached
      expect(await getWorkloadIdentityToken('openai')).toBe('t1');
      expect(fetchSpy).toHaveBeenCalledTimes(1);
      now.mockReturnValue(1_000_000 + 56 * 60 * 1000); // within 5 min of expiry
      expect(await getWorkloadIdentityToken('openai')).toBe('t2');
      expect(fetchSpy).toHaveBeenCalledTimes(2);
      // The token file is read for every exchange (projected tokens rotate).
      expect(
        readFileSpy.mock.calls.filter((c) => c[0] === TOKEN_FILE)
      ).toHaveLength(2);
    } finally {
      now.mockRestore();
    }
  });

  test('short tokens refresh at half their lifetime', async () => {
    setEnv(anthropicEnv);
    const now = jest.spyOn(Date, 'now').mockReturnValue(5_000_000);
    fetchSpy
      .mockResolvedValueOnce(tokenResponse('s1', 120))
      .mockResolvedValueOnce(tokenResponse('s2', 120));
    try {
      expect(await getWorkloadIdentityToken('anthropic')).toBe('s1');
      now.mockReturnValue(5_000_000 + 59 * 1000);
      expect(await getWorkloadIdentityToken('anthropic')).toBe('s1');
      now.mockReturnValue(5_000_000 + 61 * 1000);
      expect(await getWorkloadIdentityToken('anthropic')).toBe('s2');
    } finally {
      now.mockRestore();
    }
  });

  test('concurrent requests share one exchange', async () => {
    setEnv(openaiEnv);
    fetchSpy.mockResolvedValueOnce(tokenResponse('shared'));
    const tokens = await Promise.all([
      getWorkloadIdentityToken('openai'),
      getWorkloadIdentityToken('openai'),
      getWorkloadIdentityToken('openai'),
    ]);
    expect(tokens).toEqual(['shared', 'shared', 'shared']);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  test('a failed exchange is not cached and its body is dropped', async () => {
    setEnv(openaiEnv);
    fetchSpy
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ error: 'bad', echoed: SUBJECT }), {
          status: 401,
        })
      )
      .mockResolvedValueOnce(tokenResponse('after-failure'));
    const err = await expectGatewayError(
      getWorkloadIdentityToken('openai'),
      502
    );
    expect(err.message).not.toContain(SUBJECT);
    expect(err.message).toContain('401');
    expect(await getWorkloadIdentityToken('openai')).toBe('after-failure');
  });

  test('a redirect is an error, not followed', async () => {
    setEnv(openaiEnv);
    fetchSpy.mockResolvedValueOnce(
      new Response(null, {
        status: 307,
        headers: { location: 'https://evil.example/collect' },
      })
    );
    await expectGatewayError(getWorkloadIdentityToken('openai'), 502);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  test('an unreadable or oversized identity token: 500 without a path', async () => {
    setEnv(openaiEnv);
    readFileSpy.mockImplementation((() => {
      throw new Error('ENOENT: /var/run/secrets/tokens/llm');
    }) as any);
    const err = await expectGatewayError(
      getWorkloadIdentityToken('openai'),
      500
    );
    expect(err.message).not.toContain(TOKEN_FILE);
    readFileSpy.mockImplementation((() => 'x'.repeat(17 * 1024)) as any);
    await expectGatewayError(getWorkloadIdentityToken('openai'), 500);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe('token URL safe-egress checks', () => {
  test.each([
    'http://auth.example.com/oauth/token',
    'https://169.254.169.254/latest/api/token',
    'https://localhost/token',
    'https://idp.svc/token',
    'https://metadata.google.internal/token',
  ])('operator override %s is refused before any read or call', async (url) => {
    setEnv({ ...openaiEnv, OPENAI_WIF_TOKEN_URL: url });
    await expectGatewayError(getWorkloadIdentityToken('openai'), 400);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(readFileSpy).not.toHaveBeenCalled();
  });

  test('the vendor host passes OAUTH_TOKEN_URL_ALLOWED_HOSTS; other hosts must be listed', async () => {
    setEnv({ ...anthropicEnv, OAUTH_TOKEN_URL_ALLOWED_HOSTS: 'idp.corp.com' });
    fetchSpy.mockResolvedValueOnce(tokenResponse('ok'));
    expect(await getWorkloadIdentityToken('anthropic')).toBe('ok');

    _resetWorkloadIdentityTokenCache();
    setEnv({
      ...anthropicEnv,
      OAUTH_TOKEN_URL_ALLOWED_HOSTS: 'idp.corp.com',
      ANTHROPIC_WIF_TOKEN_URL: 'https://other.example.com/v1/oauth/token',
    });
    await expectGatewayError(getWorkloadIdentityToken('anthropic'), 403);
  });
});

describe('credential fingerprint', () => {
  const sha16 = (s: string) =>
    createHash('sha256').update(s).digest('hex').slice(0, 16);

  test('OpenAI: the service account, not the subject or minted token', async () => {
    const fp = await credentialFingerprint(
      { openaiAuthType: 'workload', apiKey: 'sk-ignored' },
      openaiEnv
    );
    expect(fp).toBe(sha16('openai-workload\nsvc_acct_456'));
    // The SDK test suite pins the same vector (test_credentials.py).
    expect(fp).toBe('a2488441e9dcd04f');
  });

  test('Anthropic: organization + service account', async () => {
    const fp = await credentialFingerprint(
      { anthropicAuthType: 'workload' },
      anthropicEnv
    );
    expect(fp).toBe(
      sha16(
        'anthropic-workload\n00000000-0000-0000-0000-000000000001\nsvac_def'
      )
    );
  });

  test('not configured: no fingerprint', async () => {
    expect(
      await credentialFingerprint({ openaiAuthType: 'workload' }, {})
    ).toBeUndefined();
  });
});
