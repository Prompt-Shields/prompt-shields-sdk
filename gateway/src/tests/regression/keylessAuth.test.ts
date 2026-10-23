/**
 * Regression tests for keyless upstream auth (#76) through the whole app:
 * AWS `awsAuthType: oauth` (client credentials -> STS web identity) signs
 * Bedrock calls with the role credentials, nothing secret reaches
 * ps-telemetry, and the opt-in `api_key_fingerprint` is taken from the
 * client identity, so it stays stable while the minted tokens rotate.
 */
jest.mock('jose', () => ({}));

import { createHash } from 'crypto';
import {
  events,
  json,
  post,
  resetHarness,
  telemetryEvent,
  upstream,
  upstreamCalls,
  until,
} from './harness';
import app from '../../index';
import { _resetOAuthTokenCache } from '../../utils/oauthClientCredentials';
import { _resetAwsOAuthCredentialCache } from '../../utils/awsOAuthWebIdentity';

const SECRET = 'cs-regression-oauth-secret';
const TOKEN_URL = 'https://idp.example.com/oauth2/token';
const ROLE = 'arn:aws:iam::123456789012:role/ps-bedrock';
const MODEL = 'anthropic.claude-3-5-haiku-20241022-v1:0';

const sha16 = (s: string) =>
  createHash('sha256').update(s).digest('hex').slice(0, 16);

let tokenSeq = 0;
const minted: string[] = [];
function mockIdp() {
  upstream(/^https:\/\/idp\.example\.com\//, () => {
    const token = `eyJhbGciOiJSUzI1NiJ9.eyJuIjo${++tokenSeq}fQ.sig${tokenSeq}`;
    minted.push(token);
    return json({ access_token: token, expires_in: 3600 });
  });
}
function mockSts() {
  upstream(/^https:\/\/sts\.us-east-1\.amazonaws\.com\//, () => {
    const exp = new Date(Date.now() + 3600 * 1000).toISOString();
    return new Response(
      `<AssumeRoleWithWebIdentityResponse><AssumeRoleWithWebIdentityResult><Credentials><AccessKeyId>ASIAOAUTHROLE</AccessKeyId><SecretAccessKey>sts-role-secret</SecretAccessKey><SessionToken>sts-role-session</SessionToken><Expiration>${exp}</Expiration></Credentials></AssumeRoleWithWebIdentityResult></AssumeRoleWithWebIdentityResponse>`
    );
  });
}
function mockConverse() {
  upstream(
    /bedrock-runtime\.us-east-1\.amazonaws\.com\/model\/.+\/converse$/,
    () =>
      json({
        output: { message: { role: 'assistant', content: [{ text: 'Hi' }] } },
        stopReason: 'end_turn',
        usage: { inputTokens: 7, outputTokens: 2, totalTokens: 9 },
      })
  );
}
function mockOpenAI() {
  upstream(/^https:\/\/api\.openai\.com\/v1\/chat\/completions$/, () =>
    json({
      id: 'c1',
      object: 'chat.completion',
      created: 1,
      model: 'gpt-4o-mini',
      choices: [
        {
          index: 0,
          message: { role: 'assistant', content: 'ok' },
          finish_reason: 'stop',
        },
      ],
      usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
    })
  );
}

const bedrockOAuth = (extra: object = {}) => ({
  'x-portkey-config': JSON.stringify({
    provider: 'bedrock',
    aws_auth_type: 'oauth',
    aws_role_arn: ROLE,
    aws_region: 'us-east-1',
    oauth_token_url: TOKEN_URL,
    oauth_client_id: 'client-a',
    oauth_client_secret: SECRET,
    oauth_audience: 'sts.amazonaws.com',
    ...extra,
  }),
});

const openaiOAuth = {
  'x-portkey-provider': 'openai',
  'x-portkey-oauth-token-url': TOKEN_URL,
  'x-portkey-oauth-client-id': 'client-a',
  'x-portkey-oauth-client-secret': SECRET,
};

const bedrockChat = {
  model: MODEL,
  messages: [{ role: 'user', content: 'Say hi' }],
};
const openaiChat = {
  model: 'gpt-4o-mini',
  messages: [{ role: 'user', content: 'hi' }],
};

function expectNoSecrets(event: unknown) {
  const s = JSON.stringify(event);
  for (const secret of [
    SECRET,
    'sts-role-secret',
    'sts-role-session',
    'ASIAOAUTHROLE',
    'sk-static-regression-key',
    ...minted,
  ]) {
    expect(s).not.toContain(secret);
  }
}

beforeEach(() => {
  resetHarness();
  _resetOAuthTokenCache();
  _resetAwsOAuthCredentialCache();
  minted.length = 0;
  delete process.env.PS_TELEMETRY_CREDENTIAL_FINGERPRINT;
});
afterAll(() => {
  delete process.env.PS_TELEMETRY_CREDENTIAL_FINGERPRINT;
});

describe('Bedrock awsAuthType oauth', () => {
  test('signs with the STS role credentials; telemetry carries no secrets', async () => {
    mockIdp();
    mockSts();
    mockConverse();
    const res = await post(
      app,
      '/v1/chat/completions',
      bedrockChat,
      bedrockOAuth()
    );
    expect(res.status).toBe(200);

    const [idp, sts, bedrock] = upstreamCalls;
    expect(idp.url).toBe(TOKEN_URL);
    expect(sts.url).toBe('https://sts.us-east-1.amazonaws.com/');
    expect(new URLSearchParams(sts.body).get('WebIdentityToken')).toBe(
      minted[0]
    );
    expect(bedrock.headers.authorization).toMatch(
      /^AWS4-HMAC-SHA256 Credential=ASIAOAUTHROLE\/\d{8}\/us-east-1\/bedrock\/aws4_request/
    );
    expect(bedrock.headers['x-amz-security-token']).toBe('sts-role-session');

    const ev = await telemetryEvent();
    expect(ev.vendor).toBe('bedrock');
    // Off by default.
    expect(ev).not.toHaveProperty('api_key_fingerprint');
    expectNoSecrets(ev);
  });

  test('a second request reuses the cached role credentials', async () => {
    mockIdp();
    mockSts();
    mockConverse();
    await post(app, '/v1/chat/completions', bedrockChat, bedrockOAuth());
    await post(app, '/v1/chat/completions', bedrockChat, bedrockOAuth());
    const urls = upstreamCalls.map((c) => new URL(c.url).hostname);
    expect(urls.filter((h) => h === 'idp.example.com')).toHaveLength(1);
    expect(urls.filter((h) => h.startsWith('sts.'))).toHaveLength(1);
    expect(urls.filter((h) => h.startsWith('bedrock-runtime.'))).toHaveLength(
      2
    );
  });

  test('an internal token URL is refused before any upstream call', async () => {
    const res = await post(
      app,
      '/v1/chat/completions',
      bedrockChat,
      bedrockOAuth({ oauth_token_url: 'https://169.254.169.254/latest/token' })
    );
    expect(res.status).toBe(400);
    expect(upstreamCalls).toHaveLength(0);
    const body = await res.text();
    expect(body).not.toContain(SECRET);
  });

  test('a redirecting token endpoint is not followed', async () => {
    upstream(
      /^https:\/\/idp\.example\.com\//,
      () =>
        new Response(null, {
          status: 307,
          headers: { location: 'https://evil.example/collect' },
        })
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      bedrockChat,
      bedrockOAuth()
    );
    expect(res.status).toBeGreaterThanOrEqual(400);
    expect(upstreamCalls.map((c) => c.url)).toEqual([TOKEN_URL]);
  });
});

describe('api_key_fingerprint (PS_TELEMETRY_CREDENTIAL_FINGERPRINT=true)', () => {
  beforeEach(() => {
    process.env.PS_TELEMETRY_CREDENTIAL_FINGERPRINT = 'true';
  });

  test('OAuth: fingerprint of the client identity, stable across tokens', async () => {
    mockIdp();
    mockOpenAI();
    await post(app, '/v1/chat/completions', openaiChat, openaiOAuth);
    await until(() => events.length === 1, 'first event');
    _resetOAuthTokenCache(); // the next request mints a new token
    await post(app, '/v1/chat/completions', openaiChat, openaiOAuth);
    await until(() => events.length === 2, 'second event');

    const bearers = upstreamCalls
      .filter((c) => c.url.includes('api.openai.com'))
      .map((c) => c.headers.authorization);
    expect(bearers).toEqual([`Bearer ${minted[0]}`, `Bearer ${minted[1]}`]);
    expect(minted[0]).not.toBe(minted[1]);

    const expected = sha16(`oauth-client\n${TOKEN_URL}\nclient-a`);
    expect(events.map((e) => e.api_key_fingerprint)).toEqual([
      expected,
      expected,
    ]);
    for (const t of minted) expect(expected).not.toBe(sha16(t));
    events.forEach(expectNoSecrets);
  });

  test('AWS oauth: the same client identity fingerprint', async () => {
    mockIdp();
    mockSts();
    mockConverse();
    await post(app, '/v1/chat/completions', bedrockChat, bedrockOAuth());
    const ev = await telemetryEvent();
    expect(ev.api_key_fingerprint).toBe(
      sha16(`oauth-client\n${TOKEN_URL}\nclient-a`)
    );
    expectNoSecrets(ev);
  });

  test('a static key: sha256(key)[:16], as the SDK computes it', async () => {
    mockOpenAI();
    await post(app, '/v1/chat/completions', openaiChat, {
      'x-portkey-provider': 'openai',
      authorization: 'Bearer sk-static-regression-key',
    });
    const ev = await telemetryEvent();
    expect(ev.api_key_fingerprint).toBe(sha16('sk-static-regression-key'));
    expectNoSecrets(ev);
  });

  test('a short-lived JWT bearer gets no fingerprint', async () => {
    mockOpenAI();
    await post(app, '/v1/chat/completions', openaiChat, {
      'x-portkey-provider': 'openai',
      authorization: 'Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln',
    });
    const ev = await telemetryEvent();
    expect(ev).not.toHaveProperty('api_key_fingerprint');
  });
});
