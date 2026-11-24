/**
 * Virtual keys and credential pools (#343) through the real gateway app:
 * the fake collector resolves `ps-vk-...` keys, the fake providers capture
 * every header the gateway sends, so the tests can prove a caller holding
 * only a virtual key completes chat, Responses and Messages calls, the pool
 * fails over on 429 before anything else changes, telemetry carries the
 * serving credential's fingerprint and the key's binding, and neither the
 * virtual key nor a provider key reaches a provider it should not, a
 * response or telemetry.
 */
jest.mock('jose', () => ({}));

import { createHash } from 'crypto';
import {
  events,
  json,
  post,
  resetHarness,
  resolveRequests,
  until,
  upstream,
  upstreamCalls,
  virtualKeyCollector,
} from './harness';
import app from '../../index';
import { defaultResolver } from '../../middlewares/psVirtualKeys/resolver';
import { poolState } from '../../middlewares/psVirtualKeys/pool';

const VK = 'ps-vk-checkout-0123456789abcdefghijklmnopqrstuvwxyz';
const OPENAI_1 = 'sk-proj-pool-one-0001';
const OPENAI_2 = 'sk-proj-pool-two-0002';
const ANTHROPIC_1 = 'sk-ant-pool-three-0003';
const TEAM = '01900000-0000-7000-8000-00000000aaaa';
const PROJECT = '01900000-0000-7000-8000-00000000bbbb';
const PRINCIPAL = '01900000-0000-7000-8000-00000000cccc';
const CHAT = /api\.openai\.com\/v1\/chat\/completions$/;
const RESPONSES = /api\.openai\.com\/v1\/responses$/;
const MESSAGES = /api\.anthropic\.com\/v1\/messages$/;

const sha16 = (s: string) =>
  createHash('sha256').update(s).digest('hex').slice(0, 16);

const cred = (id: string, provider: string, key: string) => ({
  id,
  provider,
  label: id,
  key_fingerprint: sha16(key),
  api_key: key,
});

function resolution(over: Record<string, any> = {}) {
  return {
    active: true,
    cache_ttl_seconds: 30,
    virtual_key: {
      id: 'vk-1',
      name: 'checkout',
      team_id: TEAM,
      project_id: PROJECT,
      principal_id: PRINCIPAL,
      user_id: `principal:${PRINCIPAL}`,
      allowed_models: null,
      strategy: 'round_robin',
      expires_at: null,
      ...(over.virtual_key ?? {}),
    },
    credentials: over.credentials ?? [
      cred('c1', 'openai', OPENAI_1),
      cred('c2', 'openai', OPENAI_2),
      cred('c3', 'anthropic', ANTHROPIC_1),
    ],
    unavailable_credentials: [],
  };
}

const chatOk = () =>
  json({
    id: 'chatcmpl-1',
    object: 'chat.completion',
    created: 1,
    model: 'gpt-4o-mini',
    choices: [
      {
        index: 0,
        message: { role: 'assistant', content: 'hi' },
        finish_reason: 'stop',
      },
    ],
    usage: { prompt_tokens: 3, completion_tokens: 1, total_tokens: 4 },
  });

const rateLimited = () =>
  json(
    {
      error: {
        message: 'Rate limit reached',
        type: 'requests',
        code: 'rate_limit_exceeded',
      },
    },
    429
  );

const CHAT_BODY = {
  model: 'gpt-4o-mini',
  messages: [{ role: 'user', content: 'hello' }],
};

const bearer = (call: { headers: Record<string, string> }) =>
  call.headers['authorization'];

beforeEach(() => {
  resetHarness();
  defaultResolver.clear();
  poolState.reset();
  process.env.PS_VIRTUAL_KEYS_ENABLED = 'true';
  process.env.PS_VIRTUAL_KEYS_ALLOW_HTTP = 'true';
  delete process.env.PS_VIRTUAL_KEYS_API_KEY;
});

afterAll(() => {
  delete process.env.PS_VIRTUAL_KEYS_ENABLED;
  delete process.env.PS_VIRTUAL_KEYS_ALLOW_HTTP;
});

/** Everything the gateway sent anywhere but to the resolver, as text. */
const sentText = () =>
  JSON.stringify({ upstreamCalls, events: events.map((e) => e) });

describe('a caller holding only a virtual key', () => {
  test('completes a chat call; the pool key is used in memory only', async () => {
    virtualKeyCollector(() => json(resolution()));
    upstream(CHAT, chatOk);
    const res = await post(app, '/v1/chat/completions', CHAT_BODY, {
      authorization: `Bearer ${VK}`,
      'x-portkey-provider': 'openai',
      // Caller-declared attribution is replaced by the key's binding.
      'x-ps-team': 'someone-elses-team',
    });
    expect(res.status).toBe(200);
    const text = await res.text();
    expect(text).toContain('chat.completion');
    for (const secret of [VK, OPENAI_1, OPENAI_2, ANTHROPIC_1])
      expect(text).not.toContain(secret);

    // The collector was asked with the gateway's ingest key, not the caller's.
    expect(resolveRequests).toHaveLength(1);
    expect(resolveRequests[0].body).toEqual({ key: VK });
    expect(resolveRequests[0].headers['authorization']).toBe(
      'Bearer ps-regression-key'
    );
    expect(upstreamCalls).toHaveLength(1);
    expect(bearer(upstreamCalls[0])).toBe(`Bearer ${OPENAI_1}`);
    for (const h of Object.keys(upstreamCalls[0].headers))
      expect(h.startsWith('x-ps-')).toBe(false);
    expect(JSON.stringify(upstreamCalls[0])).not.toContain(VK);

    await until(() => events.length > 0, 'telemetry event');
    const ev = events[0];
    expect(ev.team).toBe(TEAM);
    expect(ev.project).toBe(PROJECT);
    expect(ev.user_id).toBe(`principal:${PRINCIPAL}`);
    expect(ev.api_key_fingerprint).toBe(sha16(OPENAI_1));
    const evText = JSON.stringify(events);
    for (const secret of [VK, OPENAI_1, OPENAI_2])
      expect(evText).not.toContain(secret);
  });

  test('completes a Responses call and infers the only provider', async () => {
    virtualKeyCollector(() =>
      json(resolution({ credentials: [cred('c1', 'openai', OPENAI_1)] }))
    );
    upstream(RESPONSES, () =>
      json({
        id: 'resp_1',
        object: 'response',
        status: 'completed',
        model: 'gpt-4o-mini',
        output: [
          {
            type: 'message',
            role: 'assistant',
            content: [{ type: 'output_text', text: 'hi' }],
          },
        ],
        usage: { input_tokens: 3, output_tokens: 1, total_tokens: 4 },
      })
    );
    const res = await post(
      app,
      '/v1/responses',
      { model: 'gpt-4o-mini', input: 'hello' },
      { 'x-ps-virtual-key': VK }
    );
    expect(res.status).toBe(200);
    expect(((await res.json()) as any).id).toBe('resp_1');
    expect(bearer(upstreamCalls[0])).toBe(`Bearer ${OPENAI_1}`);
  });

  test('completes a Messages call with the key in x-api-key', async () => {
    virtualKeyCollector(() => json(resolution()));
    upstream(MESSAGES, () =>
      json({
        id: 'msg_1',
        type: 'message',
        role: 'assistant',
        model: 'claude-sonnet-4-5',
        content: [{ type: 'text', text: 'hi' }],
        stop_reason: 'end_turn',
        usage: { input_tokens: 3, output_tokens: 1 },
      })
    );
    const res = await post(
      app,
      '/v1/messages',
      {
        model: 'claude-sonnet-4-5',
        max_tokens: 16,
        messages: [{ role: 'user', content: 'hello' }],
      },
      {
        'x-api-key': VK,
        'x-portkey-provider': 'anthropic',
        'anthropic-version': '2023-06-01',
      }
    );
    expect(res.status).toBe(200);
    expect(((await res.json()) as any).id).toBe('msg_1');
    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].headers['x-api-key']).toBe(ANTHROPIC_1);
    expect(JSON.stringify(upstreamCalls[0])).not.toContain(VK);
  });

  test('header mode with the caller’s own key is untouched', async () => {
    upstream(CHAT, chatOk);
    const res = await post(app, '/v1/chat/completions', CHAT_BODY, {
      authorization: 'Bearer sk-caller-own-key',
      'x-portkey-provider': 'openai',
      'x-ps-team': 'declared-team',
    });
    expect(res.status).toBe(200);
    expect(resolveRequests).toHaveLength(0);
    expect(bearer(upstreamCalls[0])).toBe('Bearer sk-caller-own-key');
    await until(() => events.length > 0, 'telemetry event');
    expect(events[0].team).toBe('declared-team');
  });
});

describe('pool failover', () => {
  test('429 fails over to the next credential of the same provider and model', async () => {
    virtualKeyCollector(() => json(resolution()));
    let n = 0;
    upstream(CHAT, () => (++n === 1 ? rateLimited() : chatOk()));
    const res = await post(app, '/v1/chat/completions', CHAT_BODY, {
      authorization: `Bearer ${VK}`,
      'x-portkey-provider': 'openai',
    });
    expect(res.status).toBe(200);
    expect(upstreamCalls.map(bearer)).toEqual([
      `Bearer ${OPENAI_1}`,
      `Bearer ${OPENAI_2}`,
    ]);
    // Same provider, same model: the pool is tried before anything else.
    expect(upstreamCalls.map((c) => c.body.model)).toEqual([
      'gpt-4o-mini',
      'gpt-4o-mini',
    ]);
    await until(() => events.length > 0, 'telemetry event');
    expect(events[0].api_key_fingerprint).toBe(sha16(OPENAI_2));
    expect(sentText()).not.toContain(VK);
  });

  test('every credential limited: the last 429 is returned', async () => {
    virtualKeyCollector(() => json(resolution()));
    upstream(CHAT, rateLimited);
    const res = await post(app, '/v1/chat/completions', CHAT_BODY, {
      authorization: `Bearer ${VK}`,
      'x-portkey-provider': 'openai',
    });
    expect(res.status).toBe(429);
    expect(upstreamCalls).toHaveLength(2);
    const text = await res.text();
    for (const secret of [OPENAI_1, OPENAI_2])
      expect(text).not.toContain(secret);
  });

  test('least_recently_limited starts with the credential limited longest ago', async () => {
    virtualKeyCollector(() =>
      json(resolution({ virtual_key: { strategy: 'least_recently_limited' } }))
    );
    let n = 0;
    upstream(CHAT, () => (++n === 1 ? rateLimited() : chatOk()));
    const headers = {
      authorization: `Bearer ${VK}`,
      'x-portkey-provider': 'openai',
    };
    await post(app, '/v1/chat/completions', CHAT_BODY, headers);
    await post(app, '/v1/chat/completions', CHAT_BODY, headers);
    // Call 1: c1 (429) then c2; call 2 starts with c2 (never limited).
    expect(upstreamCalls.map(bearer)).toEqual([
      `Bearer ${OPENAI_1}`,
      `Bearer ${OPENAI_2}`,
      `Bearer ${OPENAI_2}`,
    ]);
  });

  test('round_robin rotates; a 401 marks the credential unhealthy', async () => {
    virtualKeyCollector(() => json(resolution()));
    upstream(CHAT, (call) =>
      bearer(call) === `Bearer ${OPENAI_1}`
        ? json(
            {
              error: { message: 'Incorrect API key', code: 'invalid_api_key' },
            },
            401
          )
        : chatOk()
    );
    const headers = {
      authorization: `Bearer ${VK}`,
      'x-portkey-provider': 'openai',
    };
    const first = await post(app, '/v1/chat/completions', CHAT_BODY, headers);
    expect(first.status).toBe(200);
    upstreamCalls.length = 0;
    // c1 is unhealthy: the next calls go to c2 first, whatever the rotation.
    await post(app, '/v1/chat/completions', CHAT_BODY, headers);
    await post(app, '/v1/chat/completions', CHAT_BODY, headers);
    expect(upstreamCalls.map(bearer)).toEqual([
      `Bearer ${OPENAI_2}`,
      `Bearer ${OPENAI_2}`,
    ]);
  });
});

describe('refusals', () => {
  const headers = {
    authorization: `Bearer ${VK}`,
    'x-portkey-provider': 'openai',
  };

  test('an unknown, revoked or expired key gets 401 and is cached', async () => {
    virtualKeyCollector(() =>
      json({ active: false, reason: 'revoked', cache_ttl_seconds: 30 })
    );
    const res = await post(app, '/v1/chat/completions', CHAT_BODY, headers);
    expect(res.status).toBe(401);
    expect(((await res.json()) as any).error.code).toBe('invalid_virtual_key');
    await post(app, '/v1/chat/completions', CHAT_BODY, headers);
    expect(resolveRequests).toHaveLength(1);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('the collector unreachable: 503, nothing cached, nothing sent', async () => {
    virtualKeyCollector(() => json({ detail: 'boom' }, 500));
    const res = await post(app, '/v1/chat/completions', CHAT_BODY, headers);
    expect(res.status).toBe(503);
    expect(res.headers.get('retry-after')).toBe('5');
    virtualKeyCollector(() => json(resolution()));
    upstream(CHAT, chatOk);
    expect(
      (await post(app, '/v1/chat/completions', CHAT_BODY, headers)).status
    ).toBe(200);
    expect(resolveRequests).toHaveLength(2);
  });

  test('a model outside allowed_models is refused before the provider', async () => {
    virtualKeyCollector(() =>
      json(
        resolution({ virtual_key: { allowed_models: ['gpt-4o-mini', 'o3*'] } })
      )
    );
    upstream(CHAT, chatOk);
    const denied = await post(
      app,
      '/v1/chat/completions',
      { ...CHAT_BODY, model: 'gpt-4.1' },
      headers
    );
    expect(denied.status).toBe(403);
    expect(((await denied.json()) as any).error.code).toBe(
      'virtual_key_model_not_allowed'
    );
    expect(upstreamCalls).toHaveLength(0);
    const ok = await post(
      app,
      '/v1/chat/completions',
      { ...CHAT_BODY, model: 'o3-mini' },
      headers
    );
    expect(ok.status).toBe(200);
  });

  test('allowed_models cannot be bypassed through the proxy or batches', async () => {
    virtualKeyCollector(() =>
      json(resolution({ virtual_key: { allowed_models: ['gpt-4o-mini'] } }))
    );
    upstream(CHAT, chatOk);
    for (const path of [
      '/v1/proxy/chat/completions',
      '/v1/chat/completions/',
      '/v1/batches',
    ]) {
      const res = await post(
        app,
        path,
        path === '/v1/batches'
          ? {
              input_file_id: 'file-1',
              endpoint: '/v1/chat/completions',
              completion_window: '24h',
            }
          : { ...CHAT_BODY, model: 'gpt-4.1' },
        { ...headers, 'x-portkey-provider': 'openai' }
      );
      expect([path, res.status]).toEqual([path, 403]);
      expect(((await res.json()) as any).error.code).toBe(
        'virtual_key_model_not_allowed'
      );
    }
    expect(upstreamCalls).toHaveLength(0);
  });

  test('without allowed_models the proxy still works with a pool key', async () => {
    virtualKeyCollector(() => json(resolution()));
    upstream(CHAT, chatOk);
    const res = await post(app, '/v1/proxy/chat/completions', CHAT_BODY, {
      ...headers,
      'x-portkey-provider': 'openai',
    });
    expect(res.status).toBe(200);
    expect(bearer(upstreamCalls[0])).toMatch(/^Bearer sk-proj-pool-/);
  });

  test('a provider the pool has no credential for is refused', async () => {
    virtualKeyCollector(() =>
      json(resolution({ credentials: [cred('c1', 'openai', OPENAI_1)] }))
    );
    const res = await post(app, '/v1/chat/completions', CHAT_BODY, {
      ...headers,
      'x-portkey-provider': 'mistral-ai',
    });
    expect(res.status).toBe(403);
    expect(((await res.json()) as any).error.code).toBe(
      'virtual_key_provider_not_allowed'
    );
    expect(upstreamCalls).toHaveLength(0);
  });

  test('a config or custom host cannot carry pool keys elsewhere', async () => {
    virtualKeyCollector(() => json(resolution()));
    for (const extra of <Record<string, string>[]>[
      { 'x-portkey-config': JSON.stringify({ provider: 'openai' }) },
      { 'x-portkey-custom-host': 'https://attacker.example/v1' },
      { 'x-portkey-azure-resource-name': 'evil.example/x?' },
    ]) {
      const res = await post(app, '/v1/chat/completions', CHAT_BODY, {
        ...headers,
        ...extra,
      });
      expect(res.status).toBe(400);
      expect(((await res.json()) as any).error.code).toBe(
        'virtual_key_header_mode_only'
      );
    }
    expect(resolveRequests).toHaveLength(0);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('disabled: a ps-vk bearer is passed through as before', async () => {
    process.env.PS_VIRTUAL_KEYS_ENABLED = 'false';
    upstream(CHAT, chatOk);
    await post(app, '/v1/chat/completions', CHAT_BODY, headers);
    expect(resolveRequests).toHaveLength(0);
    expect(bearer(upstreamCalls[0])).toBe(`Bearer ${VK}`);
  });

  test('misconfigured (https required for a remote collector): 500', async () => {
    delete process.env.PS_VIRTUAL_KEYS_ALLOW_HTTP;
    const res = await post(app, '/v1/chat/completions', CHAT_BODY, headers);
    expect(res.status).toBe(500);
    expect(((await res.json()) as any).error.code).toBe(
      'virtual_keys_misconfigured'
    );
    expect(resolveRequests).toHaveLength(0);
  });
});
