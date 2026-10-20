/**
 * OIDC/JWT authentication and IdP-group policy (PS_OIDC_ENABLED), through
 * the real gateway app: a local IdP (discovery + JWKS served by the fake
 * fetch) signs test tokens; a fake upstream captures every header the
 * gateway sends, so the tests can prove the IdP token never reaches it.
 */
// jose 6 ships ES modules only. The other regression suites stub it out;
// this one needs the real library, loaded with Node's own require (which
// can load ES modules from Node 20.19 / 22.12) instead of jest's.
jest.mock('jose', () =>
  (process as any).getBuiltinModule('module').createRequire(__filename)('jose')
);

import { readFileSync } from 'fs';
import { join } from 'path';
import {
  events,
  json,
  post,
  resetHarness,
  until,
  upstream,
  upstreamCalls,
} from './harness';
import app from '../../index';
import { exportJWK, generateKeyPair, SignJWT, JWK } from 'jose';
import {
  _resetJwksForTests,
  _setClockForTests,
  MIN_TTL_MS,
  REFRESH_COOLDOWN_MS,
} from '../../middlewares/psOidc/jwks';
import {
  _resetRateLimitsForTests,
  _setRateLimitClockForTests,
} from '../../middlewares/psOidc/policy';
import {
  _resetSecurityEventsForTests,
  flushSecurityEvents,
} from '../../middlewares/psSecurityEvents';

const ISSUER = 'https://idp.test/tenant-1';
const AUDIENCE = 'api://prompt-shields-gateway';
const DISCOVERY =
  /^https:\/\/idp\.test\/tenant-1\/\.well-known\/openid-configuration$/;
const JWKS_URL = 'https://idp.test/tenant-1/keys';
const JWKS = /^https:\/\/idp\.test\/tenant-1\/keys$/;
const CHAT_URL = /api\.openai\.com\/v1\/chat\/completions$/;
const GATEWAY_OPENAI_KEY = 'sk-gateway-owned-openai-key';

const POLICY = {
  default: 'deny',
  groups: [
    {
      name: 'quant-analysts',
      // An Entra ID group object id and a plain group name.
      match: ['6f1c2a3b-0000-4000-8000-00000000c0de', 'quants'],
      providers: ['openai'],
      models: ['gpt-4o-mini', 'gpt-4.1*'],
    },
    {
      name: 'limited',
      match: ['interns'],
      rate_limit: { requests_per_minute: 2 },
    },
    { name: 'everyone-else', match: ['staff'] },
  ],
};

type Key = { privateKey: CryptoKey; publicJwk: JWK };
let keyA: Key;
let keyB: Key;
let attacker: Key;
let ecKey: Key;
/** What the fake IdP currently publishes. */
let published: JWK[] = [];
let jwksCacheControl = 'max-age=300';
let clock = Date.now();

async function makeKey(alg: string, kid: string): Promise<Key> {
  const { privateKey, publicKey } = await generateKeyPair(alg, {
    extractable: true,
  });
  return {
    privateKey: privateKey as CryptoKey,
    publicJwk: { ...(await exportJWK(publicKey)), kid, alg, use: 'sig' },
  };
}

interface TokenOptions {
  key?: Key;
  kid?: string;
  alg?: string;
  iss?: string;
  aud?: string;
  sub?: string;
  expSecondsFromNow?: number;
  claims?: Record<string, unknown>;
}

async function token(o: TokenOptions = {}): Promise<string> {
  const key = o.key ?? keyA;
  const nowSec = Math.floor(Date.now() / 1000);
  return new SignJWT({
    groups: ['quants'],
    email: 'jane.doe@bank.example',
    ...o.claims,
  })
    .setProtectedHeader({
      alg: o.alg ?? (key.publicJwk.alg as string),
      kid: o.kid ?? key.publicJwk.kid,
      typ: 'JWT',
    })
    .setIssuer(o.iss ?? ISSUER)
    .setAudience(o.aud ?? AUDIENCE)
    .setSubject(o.sub ?? 'user-00u1abcd')
    .setIssuedAt(nowSec - 5)
    .setExpirationTime(nowSec + (o.expSecondsFromNow ?? 600))
    .sign(key.privateKey);
}

const b64url = (v: object) =>
  Buffer.from(JSON.stringify(v)).toString('base64url');

const chatOk = () =>
  json({
    id: 'chatcmpl-oidc',
    object: 'chat.completion',
    model: 'gpt-4o-mini',
    choices: [
      {
        index: 0,
        message: { role: 'assistant', content: 'ok' },
        finish_reason: 'stop',
      },
    ],
    usage: { prompt_tokens: 3, completion_tokens: 1, total_tokens: 4 },
  });

const chatBody = (model = 'gpt-4o-mini') => ({
  model,
  messages: [{ role: 'user', content: 'hello' }],
});

const bearer = (t: string) => ({ authorization: `Bearer ${t}` });

const idpCalls = () => upstreamCalls.filter((c) => c.url.includes('idp.test'));
const providerCalls = () =>
  upstreamCalls.filter((c) => !c.url.includes('idp.test'));

/** No header value of any upstream call contains `secret`. */
function expectNeverSent(secret: string) {
  for (const call of upstreamCalls) {
    for (const [name, value] of Object.entries(call.headers))
      expect(`${name}: ${value}`).not.toContain(secret);
    if (typeof call.body === 'string') expect(call.body).not.toContain(secret);
    else if (call.body && typeof call.body === 'object')
      expect(JSON.stringify(call.body)).not.toContain(secret);
  }
}

// Security events (stdout JSON lines), checked against the JSON Schema.
const SCHEMA = JSON.parse(
  readFileSync(
    join(__dirname, '../../../../docs/security-events.schema.json'),
    'utf8'
  )
);
function expectConforms(event: any) {
  const def = SCHEMA.$defs[String(event['event.name']).replace('.', '_')];
  expect(def).toBeDefined();
  for (const key of def.required) expect(event).toHaveProperty([key]);
  for (const [key, value] of Object.entries(event)) {
    const prop = def.properties[key];
    if (!prop) throw new Error(`field ${key} is not in the schema`);
    if ('const' in prop) expect(value).toBe(prop.const);
    if (prop.enum) expect(prop.enum).toContain(value);
    if (prop.items?.enum)
      for (const v of value as any[]) expect(prop.items.enum).toContain(v);
    if (prop.pattern) expect(value).toMatch(new RegExp(prop.pattern));
  }
}
let written: string[] = [];
let writeSpy: jest.SpyInstance;
async function securityEvents(): Promise<any[]> {
  await new Promise((r) => setImmediate(r));
  flushSecurityEvents();
  return written
    .join('')
    .split('\n')
    .filter((l) => l.includes('"ps.security"'))
    .map((l) => JSON.parse(l))
    .map((e) => {
      expectConforms(e);
      return e;
    });
}

const OIDC_ENV = [
  'PS_OIDC_ENABLED',
  'PS_OIDC_ISSUER',
  'PS_OIDC_AUDIENCE',
  'PS_OIDC_JWKS_URL',
  'PS_OIDC_ALGORITHMS',
  'PS_OIDC_CLOCK_SKEW_SECONDS',
  'PS_OIDC_GROUPS_CLAIM',
  'PS_OIDC_POLICY',
  'PS_OIDC_TELEMETRY_SUBJECT',
  'PS_OIDC_SUBJECT_SALT',
  'PS_OIDC_PROVIDER_KEYS',
  'PS_SECURITY_EVENTS_LOG',
];

function serveIdp() {
  upstream(DISCOVERY, () =>
    json({
      issuer: ISSUER,
      jwks_uri: JWKS_URL,
      id_token_signing_alg_values_supported: ['RS256'],
    })
  );
  upstream(
    JWKS,
    () =>
      new Response(JSON.stringify({ keys: published }), {
        status: 200,
        headers: {
          'content-type': 'application/json',
          'cache-control': jwksCacheControl,
        },
      })
  );
}

beforeAll(async () => {
  keyA = await makeKey('RS256', 'key-a');
  keyB = await makeKey('RS256', 'key-b');
  attacker = await makeKey('RS256', 'key-a');
  ecKey = await makeKey('ES256', 'key-ec');
});

beforeEach(() => {
  resetHarness();
  _resetJwksForTests();
  _resetRateLimitsForTests();
  _resetSecurityEventsForTests();
  clock = Date.now();
  _setClockForTests(() => clock);
  published = [keyA.publicJwk, ecKey.publicJwk];
  jwksCacheControl = 'max-age=300';
  serveIdp();
  upstream(CHAT_URL, () => chatOk());
  written = [];
  writeSpy = jest
    .spyOn(process.stdout, 'write')
    .mockImplementation((chunk: any) => {
      written.push(String(chunk));
      return true;
    });
  process.env.PS_OIDC_ENABLED = 'true';
  process.env.PS_OIDC_ISSUER = ISSUER;
  process.env.PS_OIDC_AUDIENCE = `other-api,${AUDIENCE}`;
  process.env.PS_OIDC_POLICY = JSON.stringify(POLICY);
  process.env.PS_OIDC_PROVIDER_KEYS = JSON.stringify({
    openai: GATEWAY_OPENAI_KEY,
  });
  process.env.PS_SECURITY_EVENTS_LOG = 'true';
});

afterEach(() => {
  writeSpy.mockRestore();
  for (const k of OIDC_ENV) delete process.env[k];
});

const chat = (headers: Record<string, string>, model?: string) =>
  post(app, '/v1/chat/completions', chatBody(model), {
    'x-portkey-provider': 'openai',
    ...headers,
  });

describe('off by default', () => {
  test('without PS_OIDC_ENABLED the Authorization header is the provider key, as before', async () => {
    delete process.env.PS_OIDC_ENABLED;
    const res = await chat({ authorization: 'Bearer sk-caller-key' });
    expect(res.status).toBe(200);
    expect(providerCalls()[0].headers.authorization).toBe(
      'Bearer sk-caller-key'
    );
    expect(idpCalls()).toHaveLength(0);
  });
});

describe('authentication', () => {
  test('no token: 401 with WWW-Authenticate, an auth.failure event, no upstream call', async () => {
    const res = await chat({});
    expect(res.status).toBe(401);
    expect(res.headers.get('www-authenticate')).toBe(
      'Bearer realm="prompt-shields-gateway"'
    );
    expect(((await res.json()) as any).error.code).toBe('missing_token');
    expect(upstreamCalls).toHaveLength(0);
    const [event] = await securityEvents();
    expect(event).toMatchObject({
      'event.name': 'auth.failure',
      'ps.auth.realm': 'oidc',
      'ps.auth.reason': 'missing_credentials',
      'http.response.status_code': 401,
      'http.route': '/v1/chat/completions',
    });
  });

  test('a valid token is accepted; the gateway key is used and the IdP token never reaches the provider', async () => {
    const jwt = await token();
    const res = await chat(bearer(jwt));
    expect(res.status).toBe(200);
    const [call] = providerCalls();
    expect(call.url).toMatch(CHAT_URL);
    expect(call.headers.authorization).toBe(`Bearer ${GATEWAY_OPENAI_KEY}`);
    // Neither the token nor its signature, nor the auth headers we set.
    expectNeverSent(jwt);
    expectNeverSent(jwt.split('.')[2]);
    for (const name of Object.keys(call.headers))
      expect(name.startsWith('x-ps-')).toBe(false);
  });

  test('discovery is used to find the JWKS, and keys are cached', async () => {
    await chat(bearer(await token()));
    await chat(bearer(await token()));
    expect(idpCalls().map((c) => c.url)).toEqual([
      `${ISSUER}/.well-known/openid-configuration`,
      JWKS_URL,
    ]);
  });

  test('an ES256 key from the same JWKS works', async () => {
    const res = await chat(bearer(await token({ key: ecKey })));
    expect(res.status).toBe(200);
  });

  test.each([
    ['wrong audience', () => ({ aud: 'api://someone-else' }), 'wrong_audience'],
    [
      'wrong issuer',
      () => ({ iss: 'https://evil.test/tenant-1' }),
      'wrong_issuer',
    ],
    [
      'expired beyond the clock skew',
      () => ({ expSecondsFromNow: -120 }),
      'expired',
    ],
    // Same kid as the published key A, different private key.
    [
      'signed by a key the IdP did not publish',
      () => ({ key: attacker }),
      'bad_signature',
    ],
  ] as [string, () => TokenOptions, string][])(
    'rejects a token with %s',
    async (_name, options, detail) => {
      const res = await chat(bearer(await token(options())));
      expect(res.status).toBe(401);
      expect(res.headers.get('www-authenticate')).toContain(
        'error="invalid_token"'
      );
      expect(providerCalls()).toHaveLength(0);
      const [event] = await securityEvents();
      expect(event).toMatchObject({
        'event.name': 'auth.failure',
        'ps.auth.realm': 'oidc',
        'ps.auth.reason': 'invalid_credentials',
        'ps.auth.detail': detail,
      });
    }
  );

  test('accepts a token expired by less than the clock skew (60 s)', async () => {
    const res = await chat(bearer(await token({ expSecondsFromNow: -20 })));
    expect(res.status).toBe(200);
  });

  test('PS_OIDC_CLOCK_SKEW_SECONDS=0 rejects it', async () => {
    process.env.PS_OIDC_CLOCK_SKEW_SECONDS = '0';
    const res = await chat(bearer(await token({ expSecondsFromNow: -20 })));
    expect(res.status).toBe(401);
  });

  test('rejects alg=none and HS256 without fetching any keys', async () => {
    const claims = {
      iss: ISSUER,
      aud: AUDIENCE,
      sub: 'x',
      exp: Math.floor(Date.now() / 1000) + 600,
    };
    const none = `${b64url({ alg: 'none', typ: 'JWT' })}.${b64url(claims)}.`;
    const hs = await new SignJWT(claims)
      .setProtectedHeader({ alg: 'HS256', kid: 'key-a' })
      .sign(new TextEncoder().encode('a-shared-secret-of-sufficient-length'));
    // An RS256-keyed JWKS must not be usable as an HMAC secret either.
    const hsWithPublicKey = await new SignJWT(claims)
      .setProtectedHeader({ alg: 'HS256', kid: 'key-a' })
      .sign(new TextEncoder().encode(JSON.stringify(keyA.publicJwk)));
    for (const t of [none, hs, hsWithPublicKey]) {
      const res = await chat(bearer(t));
      expect(res.status).toBe(401);
    }
    expect(idpCalls()).toHaveLength(0);
    const details = (await securityEvents()).map((e) => e['ps.auth.detail']);
    expect(details).toEqual([
      'disallowed_alg',
      'disallowed_alg',
      'disallowed_alg',
    ]);
  });

  test('rejects an algorithm outside PS_OIDC_ALGORITHMS', async () => {
    process.env.PS_OIDC_ALGORITHMS = 'RS256';
    const res = await chat(bearer(await token({ key: ecKey })));
    expect(res.status).toBe(401);
    expect((await securityEvents())[0]['ps.auth.detail']).toBe(
      'disallowed_alg'
    );
  });

  test('rejects garbage and non-Bearer credentials', async () => {
    for (const authorization of [
      'Bearer not-a-jwt',
      'Bearer a.b.c',
      'Basic dXNlcjpwYXNz',
    ]) {
      const res = await chat({ authorization });
      expect(res.status).toBe(401);
    }
  });

  test('/v1/health and / stay open', async () => {
    expect((await app.request('/v1/health')).status).toBe(200);
    expect((await app.request('/')).status).toBe(200);
  });

  test('an invalid configuration fails closed with 500', async () => {
    process.env.PS_OIDC_ALGORITHMS = 'RS256,HS256';
    const res = await chat(bearer(await token()));
    expect(res.status).toBe(500);
    expect(((await res.json()) as any).error.code).toBe('oidc_misconfigured');
    expect(upstreamCalls).toHaveLength(0);
  });
});

describe('JWKS fetching', () => {
  test('key rotation: an unknown kid refreshes the JWKS once', async () => {
    expect((await chat(bearer(await token()))).status).toBe(200);
    expect(idpCalls()).toHaveLength(2);

    // The IdP rotates to key B; the cached set only has A.
    published = [keyB.publicJwk];
    clock += REFRESH_COOLDOWN_MS + 1;
    const res = await chat(bearer(await token({ key: keyB })));
    expect(res.status).toBe(200);
    expect(idpCalls().map((c) => c.url)).toEqual([
      expect.stringContaining('openid-configuration'),
      JWKS_URL,
      JWKS_URL,
    ]);
    // A, no longer published, is refused after the refresh.
    expect((await chat(bearer(await token()))).status).toBe(401);
  });

  test('unknown kids cannot force refreshes faster than the cooldown', async () => {
    await chat(bearer(await token()));
    const before = idpCalls().length;
    for (let i = 0; i < 5; i++) {
      const res = await chat(
        bearer(await token({ key: keyB, kid: `rnd-${i}` }))
      );
      expect(res.status).toBe(401);
    }
    // At most one refresh inside the cooldown window.
    expect(idpCalls().length - before).toBeLessThanOrEqual(1);
    expect((await securityEvents()).pop()['ps.auth.detail']).toBe(
      'unknown_key'
    );
  });

  test('Cache-Control max-age decides when keys are refetched', async () => {
    jwksCacheControl = 'public, max-age=120';
    await chat(bearer(await token()));
    clock += 119_000;
    await chat(bearer(await token()));
    expect(idpCalls().filter((c) => JWKS.test(c.url))).toHaveLength(1);
    clock += 2_000;
    await chat(bearer(await token()));
    expect(idpCalls().filter((c) => JWKS.test(c.url))).toHaveLength(2);
  });

  test('max-age is clamped to at least a minute', async () => {
    jwksCacheControl = 'max-age=0';
    await chat(bearer(await token()));
    clock += MIN_TTL_MS - 1000;
    await chat(bearer(await token()));
    expect(idpCalls().filter((c) => JWKS.test(c.url))).toHaveLength(1);
  });

  test('stale keys keep working when a refresh fails', async () => {
    await chat(bearer(await token()));
    upstream(JWKS, () => json({ error: 'down' }, 500));
    clock += 3_600_000;
    expect((await chat(bearer(await token()))).status).toBe(200);
  });

  test('PS_OIDC_JWKS_URL skips discovery', async () => {
    process.env.PS_OIDC_JWKS_URL = JWKS_URL;
    expect((await chat(bearer(await token()))).status).toBe(200);
    expect(idpCalls().map((c) => c.url)).toEqual([JWKS_URL]);
  });

  test('the JWKS is not fetched from internal addresses (isSafeUpstreamURL)', async () => {
    process.env.PS_OIDC_JWKS_URL = 'https://169.254.169.254/latest/keys';
    const res = await chat(bearer(await token()));
    expect(res.status).toBe(503);
    expect(((await res.json()) as any).error.code).toBe('idp_unavailable');
    expect(upstreamCalls).toHaveLength(0);
    expect((await securityEvents())[0]).toMatchObject({
      'ps.auth.detail': 'jwks_unavailable',
      'http.response.status_code': 503,
    });
  });

  test('a redirect from the JWKS endpoint is not followed', async () => {
    upstream(
      JWKS,
      () =>
        new Response(null, {
          status: 302,
          headers: { location: 'https://evil.test/keys' },
        })
    );
    const res = await chat(bearer(await token()));
    expect(res.status).toBe(503);
    expect(upstreamCalls.some((c) => c.url.includes('evil.test'))).toBe(false);
  });

  test('discovery naming another issuer is refused', async () => {
    upstream(DISCOVERY, () =>
      json({ issuer: 'https://evil.test', jwks_uri: JWKS_URL })
    );
    const res = await chat(bearer(await token()));
    expect(res.status).toBe(503);
    expect(idpCalls().some((c) => JWKS.test(c.url))).toBe(false);
  });
});

describe('the IdP token is never forwarded upstream', () => {
  test('proxy catch-all route', async () => {
    upstream(/api\.openai\.com\/v1\/moderations$/, () => json({ ok: true }));
    const jwt = await token();
    const res = await post(
      app,
      '/v1/moderations',
      { input: 'x' },
      { 'x-portkey-provider': 'openai', ...bearer(jwt) }
    );
    expect(res.status).toBe(200);
    expect(providerCalls()).toHaveLength(1);
    expect(providerCalls()[0].headers.authorization).toBe(
      `Bearer ${GATEWAY_OPENAI_KEY}`
    );
    expectNeverSent(jwt);
  });

  test('forward_headers naming authorization, with the caller key in the config', async () => {
    const jwt = await token();
    const res = await post(app, '/v1/chat/completions', chatBody(), {
      ...bearer(jwt),
      'x-portkey-config': JSON.stringify({
        provider: 'openai',
        api_key: 'sk-from-config',
        forward_headers: ['authorization', 'Authorization'],
      }),
    });
    expect(res.status).toBe(200);
    expect(providerCalls()[0].headers.authorization).toBe(
      'Bearer sk-from-config'
    );
    expectNeverSent(jwt);
    // Config mode never gets the gateway's own key either.
    expectNeverSent(GATEWAY_OPENAI_KEY);
  });

  test('x-portkey-forward-headers on the proxy route', async () => {
    upstream(/api\.openai\.com\/v1\/moderations$/, () => json({ ok: true }));
    const jwt = await token();
    await post(
      app,
      '/v1/moderations',
      { input: 'x' },
      {
        'x-portkey-provider': 'openai',
        'x-portkey-forward-headers': 'authorization',
        ...bearer(jwt),
      }
    );
    expectNeverSent(jwt);
  });

  test('the gateway key is not sent to a caller-chosen custom host', async () => {
    upstream(/llm\.example\.com/, () => chatOk());
    const jwt = await token();
    const res = await chat({
      ...bearer(jwt),
      'x-portkey-custom-host': 'https://llm.example.com/v1',
    });
    expect(res.status).toBe(200);
    expect(providerCalls()[0].url).toContain('llm.example.com');
    expectNeverSent(GATEWAY_OPENAI_KEY);
    expectNeverSent(jwt);
  });

  test.each([
    [
      'a Hugging Face base URL',
      'huggingface',
      { 'x-portkey-huggingface-base-url': 'https://evil.example/hf' },
    ],
    [
      'an Azure AI Foundry URL',
      'azure-ai',
      { 'x-portkey-azure-foundry-url': 'https://evil.example/foundry' },
    ],
    [
      'an Azure resource name that moves the host',
      'azure-openai',
      {
        'x-portkey-azure-resource-name': 'evil.example/x?',
        'x-portkey-azure-deployment-id': 'gpt-4o-mini',
        'x-portkey-azure-api-version': '2024-10-21',
      },
    ],
    [
      'a Vertex region that moves the host',
      'vertex-ai',
      {
        'x-portkey-vertex-region': 'evil.example/#',
        'x-portkey-vertex-project-id': 'p',
      },
    ],
  ])(
    'the gateway key is not attached with %s',
    async (_what, provider, headers) => {
      const GATEWAY_KEY = `gw-key-for-${provider}`;
      process.env.PS_OIDC_PROVIDER_KEYS = JSON.stringify({
        [provider]: GATEWAY_KEY,
      });
      process.env.PS_OIDC_POLICY = JSON.stringify({
        default: 'allow',
        groups: [],
      });
      upstream(/evil\.example|azure\.com|googleapis\.com/, () => chatOk());
      const jwt = await token();
      await post(app, '/v1/chat/completions', chatBody(), {
        ...bearer(jwt),
        'x-portkey-provider': provider,
        ...headers,
      });
      expectNeverSent(GATEWAY_KEY);
      expectNeverSent(jwt);
    }
  );

  test('the gateway key is still used with plain Azure resource and deployment names', async () => {
    const AZURE_KEY = 'gw-azure-key';
    process.env.PS_OIDC_PROVIDER_KEYS = JSON.stringify({
      'azure-openai': AZURE_KEY,
    });
    process.env.PS_OIDC_POLICY = JSON.stringify({
      default: 'allow',
      groups: [],
    });
    upstream(/my-resource\.openai\.azure\.com/, () => chatOk());
    const res = await post(app, '/v1/chat/completions', chatBody(), {
      ...bearer(await token()),
      'x-portkey-provider': 'azure-openai',
      'x-portkey-azure-resource-name': 'my-resource',
      'x-portkey-azure-deployment-id': 'gpt-4o-mini',
      'x-portkey-azure-api-version': '2024-10-21',
      'x-portkey-trace-id': 'trace/with:odd#chars',
    });
    expect(res.status).toBe(200);
    const [call] = providerCalls();
    expect(new URL(call.url).host).toBe('my-resource.openai.azure.com');
    expect(call.headers['api-key']).toBe(AZURE_KEY);
  });

  test('a provider name that is an Object prototype key gets no gateway key', async () => {
    const res = await post(app, '/v1/chat/completions', chatBody(), {
      ...bearer(await token({ claims: { groups: ['staff'] } })),
      'x-portkey-provider': 'constructor',
    });
    expect(res.status).toBe(400);
    expect(providerCalls()).toHaveLength(0);
  });

  test('cached responses are scoped per user', async () => {
    const asUser = async (sub: string) =>
      chat({ ...bearer(await token({ sub })), 'x-ps-cache': 'on' });
    expect((await asUser('cache-alice')).status).toBe(200);
    expect((await asUser('cache-alice')).status).toBe(200);
    expect(providerCalls()).toHaveLength(1);
    expect((await asUser('cache-bob')).status).toBe(200);
    expect(providerCalls()).toHaveLength(2);
  });

  test('a client-supplied x-ps-auth-subject is replaced', async () => {
    const res = await chat({
      ...bearer(await token()),
      'x-ps-auth-subject': 'spoofed',
      'x-ps-auth-group': 'admins',
    });
    expect(res.status).toBe(200);
    expectNeverSent('spoofed');
  });
});

describe('group policy', () => {
  test('a user in no mapped group is denied by default (403 + policy.denied)', async () => {
    const res = await chat(bearer(await token({ claims: { groups: ['x'] } })));
    expect(res.status).toBe(403);
    expect(((await res.json()) as any).error).toMatchObject({
      type: 'permission_error',
      code: 'policy_denied',
    });
    expect(providerCalls()).toHaveLength(0);
    const [event] = await securityEvents();
    expect(event).toMatchObject({
      'event.name': 'policy.denied',
      'ps.policy.reason': 'no_matching_group',
      action: 'blocked',
      'http.response.status_code': 403,
    });
    expect(event['user.hash']).toMatch(/^[0-9a-f]{32}$/);
    expect(JSON.stringify(event)).not.toContain('jane.doe');
    expect(JSON.stringify(event)).not.toContain('user-00u1abcd');
  });

  test('Entra ID groups overage is denied with a hint', async () => {
    const res = await chat(
      bearer(
        await token({
          claims: {
            groups: undefined,
            _claim_names: { groups: 'src1' },
            _claim_sources: { src1: { endpoint: 'https://graph.example' } },
          },
        })
      )
    );
    expect(res.status).toBe(403);
    expect(((await res.json()) as any).error.message).toContain('overage');
    expect(upstreamCalls.some((c) => c.url.includes('graph'))).toBe(false);
  });

  test('default: allow lets unmapped users through without restrictions', async () => {
    process.env.PS_OIDC_POLICY = JSON.stringify({
      ...POLICY,
      default: 'allow',
    });
    const res = await chat(
      bearer(await token({ claims: { groups: [] } })),
      'gpt-4o'
    );
    expect(res.status).toBe(200);
  });

  test('without a policy every valid token is allowed', async () => {
    delete process.env.PS_OIDC_POLICY;
    const res = await chat(bearer(await token({ claims: { groups: [] } })));
    expect(res.status).toBe(200);
  });

  test('Entra ID group object ids match', async () => {
    const res = await chat(
      bearer(
        await token({
          claims: { groups: ['6f1c2a3b-0000-4000-8000-00000000c0de'] },
        })
      )
    );
    expect(res.status).toBe(200);
  });

  test('roles claim and Keycloak realm_access.roles via PS_OIDC_GROUPS_CLAIM', async () => {
    process.env.PS_OIDC_GROUPS_CLAIM = 'roles';
    let res = await chat(
      bearer(await token({ claims: { groups: [], roles: ['quants'] } }))
    );
    expect(res.status).toBe(200);
    process.env.PS_OIDC_GROUPS_CLAIM = 'realm_access.roles';
    res = await chat(
      bearer(
        await token({
          claims: { groups: [], realm_access: { roles: ['quants'] } },
        })
      )
    );
    expect(res.status).toBe(200);
  });

  test('the model allowlist (exact and prefix*)', async () => {
    const jwt = await token();
    expect((await chat(bearer(jwt), 'gpt-4.1-mini')).status).toBe(200);
    const res = await chat(bearer(jwt), 'gpt-4o');
    expect(res.status).toBe(403);
    expect(providerCalls()).toHaveLength(1);
    const events = await securityEvents();
    expect(events.pop()).toMatchObject({
      'ps.policy.reason': 'model_not_allowed',
      'ps.policy.group': 'quant-analysts',
      'gen_ai.provider.name': 'openai',
      'gen_ai.request.model': 'gpt-4o',
    });
  });

  test('the provider allowlist', async () => {
    const res = await post(app, '/v1/chat/completions', chatBody(), {
      'x-portkey-provider': 'anthropic',
      ...bearer(await token()),
    });
    expect(res.status).toBe(403);
    expect(upstreamCalls.filter((c) => c.url.includes('anthropic'))).toEqual(
      []
    );
    expect((await securityEvents()).pop()['ps.policy.reason']).toBe(
      'provider_not_allowed'
    );
  });

  test('the model is checked after override_params', async () => {
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody('gpt-4o-mini'),
      {
        ...bearer(await token()),
        'x-portkey-config': JSON.stringify({
          provider: 'openai',
          api_key: 'sk-x',
          override_params: { model: 'o3-pro' },
        }),
      }
    );
    expect(res.status).toBe(403);
    expect(providerCalls()).toHaveLength(0);
  });

  test('Azure OpenAI: the deployment is checked, not only the body model', async () => {
    process.env.PS_OIDC_POLICY = JSON.stringify({
      groups: [
        {
          name: 'staff',
          match: ['quants'],
          providers: ['azure-openai'],
          models: ['gpt-4o-mini'],
        },
      ],
    });
    upstream(/my-resource\.openai\.azure\.com/, () => chatOk());
    const azure = (deployment: string) =>
      post(app, '/v1/chat/completions', chatBody('gpt-4o-mini'), {
        ...bearer(jwt),
        'x-portkey-provider': 'azure-openai',
        'x-portkey-azure-resource-name': 'my-resource',
        'x-portkey-azure-deployment-id': deployment,
        'x-portkey-azure-api-version': '2024-10-21',
      });
    const jwt = await token();
    expect((await azure('gpt-4o-mini')).status).toBe(200);
    const res = await azure('o3-pro');
    expect(res.status).toBe(403);
    expect(providerCalls()).toHaveLength(1);
    expect((await securityEvents()).pop()).toMatchObject({
      'ps.policy.reason': 'model_not_allowed',
      'gen_ai.request.model': 'o3-pro',
    });
  });

  test('a request without a model is denied when the group restricts models', async () => {
    // The provider transform would otherwise fill in its default model.
    const res = await post(
      app,
      '/v1/chat/completions',
      { messages: [{ role: 'user', content: 'hi' }] },
      { 'x-portkey-provider': 'openai', ...bearer(await token()) }
    );
    expect(res.status).toBe(403);
    expect(providerCalls()).toHaveLength(0);
    expect((await securityEvents()).pop()['ps.policy.reason']).toBe(
      'model_not_allowed'
    );
  });

  test('realtime: the config provider is checked, not x-portkey-provider', async () => {
    const res = await app.request('/v1/realtime?model=gpt-4o-mini', {
      method: 'GET',
      headers: {
        ...bearer(await token()),
        'x-portkey-provider': 'openai',
        'x-portkey-config': JSON.stringify({
          provider: 'azure-openai',
          api_key: 'k',
        }),
      },
    });
    expect(res.status).toBe(403);
    expect((await securityEvents()).pop()).toMatchObject({
      'ps.policy.reason': 'provider_not_allowed',
      'gen_ai.provider.name': 'azure-openai',
    });
  });

  test('a fallback config skips a denied target', async () => {
    const res = await post(app, '/v1/chat/completions', chatBody('gpt-4o'), {
      ...bearer(await token()),
      'x-portkey-config': JSON.stringify({
        strategy: { mode: 'fallback' },
        targets: [
          { provider: 'anthropic', api_key: 'sk-ant' },
          {
            provider: 'openai',
            api_key: 'sk-x',
            override_params: { model: 'gpt-4o-mini' },
          },
        ],
      }),
    });
    expect(res.status).toBe(200);
    expect(providerCalls().map((c) => new URL(c.url).host)).toEqual([
      'api.openai.com',
    ]);
  });

  test('per-user rate limit: 429 with Retry-After, separate budgets per user', async () => {
    const intern = (sub: string) =>
      token({ sub, claims: { groups: ['interns'] } });
    let t = Date.UTC(2026, 0, 1, 10, 0, 5);
    _setRateLimitClockForTests(() => t);
    const alice = await intern('alice');
    expect((await chat(bearer(alice))).status).toBe(200);
    expect((await chat(bearer(alice))).status).toBe(200);
    const limited = await chat(bearer(alice));
    expect(limited.status).toBe(429);
    expect(limited.headers.get('retry-after')).toBe('55');
    expect((await chat(bearer(await intern('bob')))).status).toBe(200);
    expect(providerCalls()).toHaveLength(3);
    const rl = (await securityEvents()).find(
      (e) => e['event.name'] === 'rate_limit.hit'
    );
    expect(rl['ps.rate_limit.source']).toBe('gateway');
    t += 60_000;
    expect((await chat(bearer(alice))).status).toBe(200);
  });
});

describe('telemetry', () => {
  test('carries the hashed subject and the policy group, never the email or raw sub', async () => {
    await chat(bearer(await token()));
    await until(() => events.length > 0, 'telemetry event');
    const [event] = events;
    expect(event.user_id).toMatch(/^[0-9a-f]{32}$/);
    expect(event.user_group).toBe('quant-analysts');
    const raw = JSON.stringify(event);
    expect(raw).not.toContain('jane.doe');
    expect(raw).not.toContain('user-00u1abcd');
  });

  test('the hash is stable per user, differs between users, and changes with the salt', async () => {
    const ids: string[] = [];
    for (const [sub, salt] of [
      ['u1', undefined],
      ['u1', undefined],
      ['u2', undefined],
      ['u1', 'pepper-1'],
    ] as [string, string | undefined][]) {
      if (salt) process.env.PS_OIDC_SUBJECT_SALT = salt;
      events.length = 0;
      await chat(bearer(await token({ sub })));
      await until(() => events.length > 0, 'telemetry event');
      ids.push(events[0].user_id);
    }
    expect(ids[0]).toBe(ids[1]);
    expect(ids[2]).not.toBe(ids[0]);
    expect(ids[3]).not.toBe(ids[0]);
  });

  test('PS_OIDC_TELEMETRY_SUBJECT=raw sends sub; off sends no user id', async () => {
    process.env.PS_OIDC_TELEMETRY_SUBJECT = 'raw';
    await chat(bearer(await token()));
    await until(() => events.length > 0, 'telemetry event');
    expect(events[0].user_id).toBe('user-00u1abcd');

    events.length = 0;
    process.env.PS_OIDC_TELEMETRY_SUBJECT = 'off';
    await chat(bearer(await token()));
    await until(() => events.length > 0, 'telemetry event');
    expect(events[0].user_id).toBeUndefined();
    expect(events[0].user_group).toBe('quant-analysts');
  });
});
