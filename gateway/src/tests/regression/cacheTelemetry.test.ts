/**
 * Cache telemetry (#33) through the whole gateway app, offline: each request
 * sends exactly one collector event, which carries its cache lookup
 * (`cache_status`), its attribution (X-PS-* headers) and its call id. There
 * is no separate cache-only event, and a call that fails over to another
 * target still reports one lookup.
 */
jest.mock('jose', () => ({}));

import { events, json, post, resetHarness, until, upstream } from './harness';
import app from '../../index';

const CHAT = /api\.openai\.com\/v1\/chat\/completions$/;

let served = 0;
const chatOk = (model: string) =>
  json({
    id: `chatcmpl-${++served}`,
    object: 'chat.completion',
    created: 1,
    model,
    choices: [
      {
        index: 0,
        message: { role: 'assistant', content: `answer-${served}` },
        finish_reason: 'stop',
      },
    ],
    usage: { prompt_tokens: 100, completion_tokens: 50, total_tokens: 150 },
  });

const ATTRIBUTION = {
  'x-ps-use-case': 'support-bot',
  'x-ps-business-unit': 'customer-care',
};

const cached = (extra: object = {}) => ({
  'x-portkey-config': JSON.stringify({
    provider: 'openai',
    api_key: 'sk-cache-telemetry',
    cache: { mode: 'simple' },
    ...extra,
  }),
  ...ATTRIBUTION,
});

const ask = (content: string, headers: Record<string, string>) =>
  post(
    app,
    '/v1/chat/completions',
    { model: 'gpt-4o-mini', messages: [{ role: 'user', content }] },
    headers
  );

/** Waits for `n` events, then a little longer to catch any extra one. */
async function settle(n: number) {
  await until(() => events.length >= n, `${n} events`);
  await new Promise((r) => setTimeout(r, 50));
  return [...events];
}

beforeEach(() => {
  resetHarness();
  upstream(CHAT, (call) => chatOk(call.body.model));
});

describe('one event per request', () => {
  test('a miss and a hit each send one event with cache_status and attribution', async () => {
    const miss = await ask('cache telemetry: what is 2+2?', cached());
    expect(miss.headers.get('x-portkey-cache-status')).toBe('MISS');
    const [missEvent, ...extraMiss] = await settle(1);
    expect(extraMiss).toHaveLength(0);
    expect(missEvent).toMatchObject({
      cache_status: 'MISS',
      est_tokens_saved: 0,
      cost_source: 'pricing_table',
      use_case_name: 'support-bot',
      business_unit: 'customer-care',
      call_id: miss.headers.get('x-ps-call-id'),
      tokens_in: 100,
    });

    resetHarness();
    const hit = await ask('cache telemetry: what is 2+2?', cached());
    expect(hit.headers.get('x-portkey-cache-status')).toBe('HIT');
    const [hitEvent, ...extraHit] = await settle(1);
    expect(extraHit).toHaveLength(0);
    expect(hitEvent).toMatchObject({
      cache_status: 'HIT',
      cost: 0,
      cost_source: 'cache',
      cache_saved_cost: missEvent.cost,
      use_case_name: 'support-bot',
      business_unit: 'customer-care',
      call_id: hit.headers.get('x-ps-call-id'),
    });
    expect(hitEvent.est_tokens_saved).toBeGreaterThan(0);
  });

  test('a call with the cache off carries no cache_status', async () => {
    await ask('cache telemetry: no cache', {
      'x-portkey-provider': 'openai',
      authorization: 'Bearer sk-nocache',
      ...ATTRIBUTION,
    });
    const [ev, ...extra] = await settle(1);
    expect(extra).toHaveLength(0);
    expect(ev.cache_status).toBeUndefined();
    expect(ev.est_tokens_saved).toBeUndefined();
  });

  test('a call that falls back to a second target reports one lookup', async () => {
    upstream(CHAT, (call) =>
      call.body.model === 'gpt-primary'
        ? json({ error: { message: 'down', type: 'server_error' } }, 500)
        : chatOk(call.body.model)
    );
    const res = await ask(
      'cache telemetry: fallback',
      cached({
        strategy: { mode: 'fallback' },
        targets: [
          {
            provider: 'openai',
            api_key: 'sk-cache-telemetry',
            override_params: { model: 'gpt-primary' },
          },
          { provider: 'openai', api_key: 'sk-cache-telemetry' },
        ],
      })
    );
    expect(res.status).toBe(200);
    const sent = await settle(1);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      cache_status: 'MISS',
      use_case_name: 'support-bot',
      retry_count: 1,
    });
    expect(sent[0].fallback_chain).toHaveLength(1);
  });
});
