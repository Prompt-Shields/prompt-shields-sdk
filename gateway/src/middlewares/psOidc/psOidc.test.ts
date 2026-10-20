/**
 * psOidc units: configuration parsing, policy helpers, and the middleware
 * on the Workers path (settings from bindings, `c.env`, not process.env).
 * End-to-end behaviour through the gateway is in
 * src/tests/regression/oidc.test.ts.
 */
// Real jose (ES modules only) through Node's own require; see
// src/tests/regression/oidc.test.ts.
jest.mock('jose', () =>
  (process as any).getBuiltinModule('module').createRequire(__filename)('jose')
);

import { Hono } from 'hono';
import { exportJWK, generateKeyPair, SignJWT } from 'jose';
import { parseOidcConfig, parsePolicy } from './config';
import { _resetJwksForTests, ttlFromCacheControl } from './jwks';
import {
  _resetRateLimitsForTests,
  checkTarget,
  groupsFromClaims,
  hasGroupsOverage,
  matchGroup,
  modelAllowed,
} from './policy';
import { oidcIdentityOf, psOidc } from '.';

const ISSUER = 'https://login.example.com/tenant/v2.0';

// The settings union, loosely typed for assertions.
const parse = (raw: Record<string, string>): any => parseOidcConfig(raw);

describe('parseOidcConfig', () => {
  const base = {
    PS_OIDC_ENABLED: 'true',
    PS_OIDC_ISSUER: ISSUER,
    PS_OIDC_AUDIENCE: 'api://gw',
  };

  test('off unless PS_OIDC_ENABLED is true', () => {
    expect(parse({}).enabled).toBe(false);
    expect(parse({ ...base, PS_OIDC_ENABLED: 'false' }).enabled).toBe(false);
  });

  test('defaults', () => {
    const s = parse(base);
    expect(s.error).toBeUndefined();
    expect(s.config).toMatchObject({
      issuer: ISSUER,
      audiences: ['api://gw'],
      clockSkewSeconds: 60,
      groupsClaim: 'groups',
      telemetrySubject: 'hash',
      providerKeys: {},
    });
    expect(s.config!.algorithms).toContain('RS256');
    expect(s.config!.algorithms.some((a: string) => a.startsWith('HS'))).toBe(
      false
    );
    expect(s.config!.policy).toBeUndefined();
  });

  test.each([
    [{ PS_OIDC_ISSUER: '' }, 'PS_OIDC_ISSUER is required'],
    [{ PS_OIDC_ISSUER: 'http://idp.example.com' }, 'https://'],
    [{ PS_OIDC_AUDIENCE: ' , ' }, 'PS_OIDC_AUDIENCE is required'],
    [{ PS_OIDC_JWKS_URL: 'http://idp.example.com/keys' }, 'https://'],
    [{ PS_OIDC_ALGORITHMS: 'RS256,HS256' }, '"HS256" is not allowed'],
    [{ PS_OIDC_ALGORITHMS: 'none' }, '"none" is not allowed'],
    [{ PS_OIDC_CLOCK_SKEW_SECONDS: '3600' }, 'PS_OIDC_CLOCK_SKEW_SECONDS'],
    [{ PS_OIDC_GROUPS_CLAIM: 'a..b' }, 'PS_OIDC_GROUPS_CLAIM'],
    [{ PS_OIDC_POLICY: '{nope' }, 'not valid JSON'],
    [{ PS_OIDC_TELEMETRY_SUBJECT: 'email' }, 'hash, raw or off'],
    [{ PS_OIDC_PROVIDER_KEYS: '["sk"]' }, 'JSON object'],
  ])('refuses %j', (override, message) => {
    const s = parse({ ...base, ...override });
    expect(s.enabled).toBe(true);
    expect(s.error).toContain(message);
  });

  test('provider keys are keyed by lowercase provider', () => {
    const s = parse({
      ...base,
      PS_OIDC_PROVIDER_KEYS: '{"OpenAI":" sk-1 "}',
    });
    expect(s.config!.providerKeys).toEqual({ openai: 'sk-1' });
  });
});

describe('parsePolicy', () => {
  test('parses groups, rate limits and the default', () => {
    const p = parsePolicy({
      groups: [
        {
          name: 'a',
          match: ['g1'],
          providers: ['OpenAI'],
          models: ['gpt-4o*'],
          rate_limit: { requests_per_minute: 10, per: 'group' },
        },
      ],
    });
    expect(p.defaultAction).toBe('deny');
    expect(p.groups[0]).toEqual({
      name: 'a',
      match: ['g1'],
      providers: ['openai'],
      models: ['gpt-4o*'],
      rateLimit: { requestsPerMinute: 10, per: 'group' },
    });
  });

  test.each([
    [{ groups: [{ name: 'a', match: [] }] }, 'non-empty array'],
    [{ groups: [{ name: 'a b', match: ['x'] }] }, '.name'],
    [
      {
        groups: [
          { name: 'a', match: ['x'] },
          { name: 'a', match: ['y'] },
        ],
      },
      'used twice',
    ],
    [{ groups: [{ name: 'a', match: ['x'], model: ['m'] }] }, 'unknown key'],
    [
      {
        groups: [
          { name: 'a', match: ['x'], rate_limit: { requests_per_minute: 0 } },
        ],
      },
      'positive integer',
    ],
    [{ default: 'maybe', groups: [] }, '"deny" or "allow"'],
    [{ groups: {} }, 'must be an array'],
  ])('refuses %j', (policy, message) => {
    expect(() => parsePolicy(policy)).toThrow(message);
  });
});

describe('policy helpers', () => {
  const policy = parsePolicy({
    groups: [
      { name: 'first', match: ['a'], models: ['m1'] },
      { name: 'second', match: ['a', 'b'] },
    ],
  });

  test('first matching entry wins', () => {
    expect(matchGroup(policy, ['b', 'a'])?.name).toBe('first');
    expect(matchGroup(policy, ['b'])?.name).toBe('second');
    expect(matchGroup(policy, ['A'])).toBeUndefined();
  });

  test('groups claim: arrays, single strings, dotted paths', () => {
    expect(
      groupsFromClaims({ groups: ['x', 1, 'y'] } as any, 'groups')
    ).toEqual(['x', 'y']);
    expect(groupsFromClaims({ role: 'admin' }, 'role')).toEqual(['admin']);
    expect(
      groupsFromClaims({ realm_access: { roles: ['r'] } }, 'realm_access.roles')
    ).toEqual(['r']);
    expect(groupsFromClaims({}, 'realm_access.roles')).toEqual([]);
  });

  test('detects Entra ID groups overage', () => {
    expect(
      hasGroupsOverage({ _claim_names: { groups: 'src1' } } as any, 'groups')
    ).toBe(true);
    expect(hasGroupsOverage({ groups: [] } as any, 'groups')).toBe(false);
  });

  test('model patterns', () => {
    expect(modelAllowed(['gpt-4o'], 'gpt-4o')).toBe(true);
    expect(modelAllowed(['gpt-4o'], 'gpt-4o-mini')).toBe(false);
    expect(modelAllowed(['gpt-4o*'], 'gpt-4o-mini')).toBe(true);
    expect(modelAllowed(['*'], 'anything')).toBe(true);
  });

  test('checkTarget', () => {
    const g = parsePolicy({
      groups: [
        { name: 'g', match: ['x'], providers: ['openai'], models: ['m'] },
      ],
    }).groups[0];
    expect(checkTarget(g, 'OpenAI', 'm')).toBeUndefined();
    expect(checkTarget(g, 'anthropic', 'm')).toBe('provider_not_allowed');
    expect(checkTarget(g, 'openai', 'other')).toBe('model_not_allowed');
    // No model (files, batches, proxied GETs): provider only.
    expect(checkTarget(g, 'openai', undefined)).toBeUndefined();
    expect(checkTarget(undefined, 'anything', 'x')).toBeUndefined();
  });

  test('Cache-Control TTL', () => {
    expect(ttlFromCacheControl(null)).toBe(600_000);
    expect(ttlFromCacheControl('public, max-age=3600')).toBe(3_600_000);
    expect(ttlFromCacheControl('max-age=5')).toBe(60_000);
    expect(ttlFromCacheControl('max-age=9999999')).toBe(86_400_000);
    expect(ttlFromCacheControl('no-store')).toBe(60_000);
  });
});

describe('Workers path: settings from bindings, verification with WebCrypto', () => {
  const realFetch = global.fetch;
  const nav = Object.getOwnPropertyDescriptor(globalThis, 'navigator');

  beforeEach(() => {
    _resetJwksForTests();
    _resetRateLimitsForTests();
    // hono/adapter's getRuntimeKey() reads navigator.userAgent: as on
    // Cloudflare Workers, env(c) now returns the bindings (c.env).
    Object.defineProperty(globalThis, 'navigator', {
      value: { userAgent: 'Cloudflare-Workers' },
      configurable: true,
    });
  });

  afterEach(() => {
    global.fetch = realFetch;
    if (nav) Object.defineProperty(globalThis, 'navigator', nav);
  });

  test('a token is verified and removed before the handler', async () => {
    const { privateKey, publicKey } = await generateKeyPair('ES256', {
      extractable: true,
    });
    const jwk = { ...(await exportJWK(publicKey)), kid: 'w1', alg: 'ES256' };
    const fetched: string[] = [];
    global.fetch = jest.fn(async (url: any, init: any) => {
      fetched.push(String(url));
      expect(init.redirect).toBe('manual');
      return new Response(JSON.stringify({ keys: [jwk] }), {
        headers: { 'content-type': 'application/json' },
      });
    }) as any;

    const seen: Record<string, string | undefined>[] = [];
    const app = new Hono();
    app.use('*', psOidc());
    app.post('/v1/chat/completions', (c) => {
      seen.push({
        authorization: c.req.header('authorization'),
        subject: c.req.header('x-ps-auth-subject'),
        group: oidcIdentityOf(c)?.group?.name,
      });
      return c.json({ ok: true });
    });

    const bindings = {
      PS_OIDC_ENABLED: 'true',
      PS_OIDC_ISSUER: ISSUER,
      PS_OIDC_AUDIENCE: 'api://gw',
      PS_OIDC_JWKS_URL: 'https://login.example.com/tenant/keys',
      PS_OIDC_GROUPS_CLAIM: 'roles',
      PS_OIDC_POLICY: JSON.stringify({
        groups: [{ name: 'chat-users', match: ['Chat.Use'] }],
      }),
      PS_OIDC_PROVIDER_KEYS: JSON.stringify({ openai: 'sk-worker' }),
    };
    expect(process.env.PS_OIDC_ENABLED).toBeUndefined();

    const jwt = await new SignJWT({ roles: ['Chat.Use'] })
      .setProtectedHeader({ alg: 'ES256', kid: 'w1' })
      .setIssuer(ISSUER)
      .setAudience('api://gw')
      .setSubject('worker-user')
      .setExpirationTime('5m')
      .sign(privateKey);

    const init = (auth?: string) => ({
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-portkey-provider': 'openai',
        ...(auth ? { authorization: auth } : {}),
      },
      body: JSON.stringify({ model: 'm' }),
    });

    const denied = await app.request('/v1/chat/completions', init(), bindings);
    expect(denied.status).toBe(401);

    const ok = await app.request(
      '/v1/chat/completions',
      init(`Bearer ${jwt}`),
      bindings
    );
    expect(ok.status).toBe(200);
    expect(seen).toEqual([
      {
        authorization: 'Bearer sk-worker',
        subject: expect.stringMatching(/^[0-9a-f]{32}$/),
        group: 'chat-users',
      },
    ]);
    expect(fetched).toEqual(['https://login.example.com/tenant/keys']);

    // Without the bindings (process.env is empty) OIDC is off.
    const off = await app.request('/v1/chat/completions', init());
    expect(off.status).toBe(200);
  });
});
