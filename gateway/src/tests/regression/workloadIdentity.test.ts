/**
 * Regression tests for OpenAI and Anthropic workload identity federation
 * (#76) through the whole app: the gateway exchanges the host's identity
 * token at the vendor token endpoint (mocked), sends the minted token
 * upstream, never puts the subject or minted token in ps-telemetry, and the
 * opt-in `api_key_fingerprint` is the service account's, so it is the same
 * before and after the tokens rotate.
 */
jest.mock('jose', () => ({}));

import { SUBJECT_1, SUBJECT_2, TOKEN_FILE } from './workloadIdentityEnv';
import { writeFileSync } from 'fs';
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
import { _resetWorkloadIdentityTokenCache } from '../../utils/vendorWorkloadIdentity';

const sha16 = (s: string) =>
  createHash('sha256').update(s).digest('hex').slice(0, 16);

const parse = (body: any) =>
  typeof body === 'string' ? JSON.parse(body) : body;

let seq = 0;
const minted: string[] = [];
function mockTokenEndpoints() {
  upstream(/^https:\/\/auth\.openai\.com\/oauth\/token$/, () => {
    const token = `oa-wif-minted-${++seq}`;
    minted.push(token);
    return json({
      access_token: token,
      token_type: 'Bearer',
      expires_in: 3600,
    });
  });
  upstream(/^https:\/\/api\.anthropic\.com\/v1\/oauth\/token$/, () => {
    const token = `sk-ant-oat01-wif-minted-${++seq}`;
    minted.push(token);
    return json({ access_token: token, token_type: 'Bearer', expires_in: 600 });
  });
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
function mockAnthropic() {
  upstream(/^https:\/\/api\.anthropic\.com\/v1\/messages$/, () =>
    json({
      id: 'msg_1',
      type: 'message',
      role: 'assistant',
      model: 'claude-haiku-4-5',
      content: [{ type: 'text', text: 'ok' }],
      stop_reason: 'end_turn',
      usage: { input_tokens: 3, output_tokens: 1 },
    })
  );
}

const openaiChat = {
  model: 'gpt-4o-mini',
  messages: [{ role: 'user', content: 'hi' }],
};
const anthropicChat = {
  model: 'claude-haiku-4-5',
  max_tokens: 16,
  messages: [{ role: 'user', content: 'hi' }],
};
const openaiWorkload = {
  'x-portkey-provider': 'openai',
  'x-portkey-openai-auth-type': 'workload',
};
const anthropicWorkload = {
  'x-portkey-config': JSON.stringify({
    provider: 'anthropic',
    anthropic_auth_type: 'workload',
  }),
};

function expectNoSecrets(event: unknown) {
  const s = JSON.stringify(event);
  for (const secret of [SUBJECT_1, SUBJECT_2, ...minted]) {
    expect(s).not.toContain(secret);
  }
}

beforeEach(() => {
  resetHarness();
  _resetWorkloadIdentityTokenCache();
  minted.length = 0;
  writeFileSync(TOKEN_FILE, SUBJECT_1);
  delete process.env.PS_TELEMETRY_CREDENTIAL_FINGERPRINT;
});
afterAll(() => {
  delete process.env.PS_TELEMETRY_CREDENTIAL_FINGERPRINT;
});

test('OpenAI: exchanges the host token, sends the minted bearer, caches it', async () => {
  mockTokenEndpoints();
  mockOpenAI();
  const res = await post(
    app,
    '/v1/chat/completions',
    openaiChat,
    openaiWorkload
  );
  expect(res.status).toBe(200);
  await post(app, '/v1/chat/completions', openaiChat, openaiWorkload);

  const exchanges = upstreamCalls.filter((c) => c.url.includes('/oauth/token'));
  expect(exchanges).toHaveLength(1);
  expect(parse(exchanges[0].body)).toMatchObject({
    grant_type: 'urn:ietf:params:oauth:grant-type:token-exchange',
    subject_token: SUBJECT_1,
    identity_provider_id: 'idp_regression',
    service_account_id: 'svc_regression',
  });
  const calls = upstreamCalls.filter((c) =>
    c.url.includes('/chat/completions')
  );
  expect(calls.map((c) => c.headers.authorization)).toEqual([
    `Bearer ${minted[0]}`,
    `Bearer ${minted[0]}`,
  ]);
  await until(() => events.length === 2, 'two events');
  // Off by default.
  expect(events[0]).not.toHaveProperty('api_key_fingerprint');
  events.forEach(expectNoSecrets);
});

test('Anthropic (config): jwt-bearer exchange, Bearer + OAuth beta, no x-api-key', async () => {
  mockTokenEndpoints();
  mockAnthropic();
  const res = await post(app, '/v1/messages', anthropicChat, anthropicWorkload);
  expect(res.status).toBe(200);
  const [exchange, call] = upstreamCalls;
  expect(exchange.url).toBe('https://api.anthropic.com/v1/oauth/token');
  expect(exchange.headers['anthropic-beta']).toBe(
    'oauth-2025-04-20,oidc-federation-2026-04-01'
  );
  expect(parse(exchange.body)).toMatchObject({
    grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer',
    assertion: SUBJECT_1,
    federation_rule_id: 'fdrl_regression',
  });
  expect(call.headers.authorization).toBe(`Bearer ${minted[0]}`);
  expect(call.headers['x-api-key']).toBeUndefined();
  expect(call.headers['anthropic-beta']).toContain('oauth-2025-04-20');
  expectNoSecrets(await telemetryEvent());
});

test('a custom host is refused before the host token is used', async () => {
  mockTokenEndpoints();
  const res = await post(app, '/v1/chat/completions', openaiChat, {
    ...openaiWorkload,
    'x-portkey-custom-host': 'https://collector.evil.example/v1',
  });
  expect(res.status).toBe(400);
  expect(upstreamCalls).toHaveLength(0);
});

describe('api_key_fingerprint (PS_TELEMETRY_CREDENTIAL_FINGERPRINT=true)', () => {
  beforeEach(() => {
    process.env.PS_TELEMETRY_CREDENTIAL_FINGERPRINT = 'true';
  });

  test('OpenAI: the service account fingerprint survives token rotation', async () => {
    mockTokenEndpoints();
    mockOpenAI();
    await post(app, '/v1/chat/completions', openaiChat, openaiWorkload);
    await until(() => events.length === 1, 'first event');
    // The projected token rotates and the cached access token expires.
    writeFileSync(TOKEN_FILE, SUBJECT_2);
    _resetWorkloadIdentityTokenCache();
    await post(app, '/v1/chat/completions', openaiChat, openaiWorkload);
    await until(() => events.length === 2, 'second event');

    const subjects = upstreamCalls
      .filter((c) => c.url.includes('/oauth/token'))
      .map((c) => parse(c.body).subject_token);
    expect(subjects).toEqual([SUBJECT_1, SUBJECT_2]);
    expect(minted[0]).not.toBe(minted[1]);

    const expected = sha16('openai-workload\nsvc_regression');
    expect(events.map((e) => e.api_key_fingerprint)).toEqual([
      expected,
      expected,
    ]);
    events.forEach(expectNoSecrets);
  });

  test('Anthropic: organization + service account fingerprint', async () => {
    mockTokenEndpoints();
    mockAnthropic();
    await post(app, '/v1/messages', anthropicChat, anthropicWorkload);
    const ev = await telemetryEvent();
    expect(ev.api_key_fingerprint).toBe(
      sha16(
        'anthropic-workload\n00000000-0000-0000-0000-00000000000a\nsvac_regression'
      )
    );
    expectNoSecrets(ev);
  });
});
