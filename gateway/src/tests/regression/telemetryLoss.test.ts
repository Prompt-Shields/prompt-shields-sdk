/**
 * Event-loss accounting in ps-telemetry (#87), through the whole gateway app
 * and offline: every collector request carries the same `metadata` the
 * Python SDK sends (client id, per-client monotonic batch seq, cumulative
 * enqueued and dropped counts), and an event the collector never took is
 * counted as dropped and reported on the next batch.
 */
jest.mock('jose', () => ({}));

import {
  collector,
  collectorMetadata,
  events,
  json,
  post,
  resetHarness,
  until,
  upstream,
} from './harness';
import app from '../../index';
import { psTelemetrySender } from '../../middlewares/ps-telemetry';
import { DEFAULT_MAX_ATTEMPTS } from '../../middlewares/psTelemetrySender';

const OPENAI = {
  'x-portkey-provider': 'openai',
  authorization: 'Bearer sk-regression',
};

const chat = () =>
  post(
    app,
    '/v1/chat/completions',
    { model: 'gpt-4o', messages: [{ role: 'user', content: 'hi' }] },
    OPENAI
  );

beforeEach(() => {
  resetHarness();
  upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
    json({
      id: 'chatcmpl-1',
      object: 'chat.completion',
      model: 'gpt-4o-2024-08-06',
      choices: [
        {
          index: 0,
          message: { role: 'assistant', content: 'hi' },
          finish_reason: 'stop',
        },
      ],
      usage: { prompt_tokens: 3, completion_tokens: 1 },
    })
  );
});

test('each batch carries client id, monotonic seq and enqueued total', async () => {
  const before = psTelemetrySender.stats();
  await chat();
  await chat();
  await until(() => events.length === 2, 'two events');

  expect(collectorMetadata).toHaveLength(2);
  const [a, b] = collectorMetadata;
  expect(a.sdk).toBe('gateway');
  expect(a.client_id).toMatch(/^[0-9a-f]{32}$/);
  // Stable for the process.
  expect(b.client_id).toBe(a.client_id);
  expect(a.client_id).toBe(before.client_id);
  expect(b.seq).toBe(a.seq + 1);
  expect(a.enqueued_total).toBe(before.enqueued + 1);
  expect(b.enqueued_total).toBe(before.enqueued + 2);
  expect(a.dropped).toEqual({});
});

test('a send that fails on every attempt is counted and reported', async () => {
  collector(() => new Response('boom', { status: 503 }));
  await chat();
  await until(
    () => psTelemetrySender.stats().dropped.send_failed === 1,
    'send_failed drop'
  );
  // Each attempt is its own batch with a new seq.
  expect(collectorMetadata).toHaveLength(DEFAULT_MAX_ATTEMPTS);
  const seqs = collectorMetadata.map((m) => m.seq);
  expect(seqs).toEqual([...seqs].sort((x, y) => x - y));
  expect(new Set(seqs).size).toBe(DEFAULT_MAX_ATTEMPTS);

  // The collector recovers: the next batch tells it about the loss.
  resetHarness();
  upstream(/api\.openai\.com/, () => json({ model: 'gpt-4o', usage: {} }));
  await chat();
  await until(() => events.length === 1, 'event after recovery');
  expect(collectorMetadata[0].dropped).toEqual({ send_failed: 1 });
  const s = psTelemetrySender.stats();
  expect(collectorMetadata[0].enqueued_total).toBe(s.enqueued);
});

test('a retried send that succeeds is not counted as dropped', async () => {
  let calls = 0;
  collector(() =>
    ++calls === 1
      ? new Response('', { status: 429, headers: { 'retry-after': '0' } })
      : new Response('{}', { status: 200 })
  );
  const before = psTelemetrySender.stats();
  await chat();
  await until(() => psTelemetrySender.stats().sent === before.sent + 1, 'sent');
  expect(calls).toBe(2);
  expect(psTelemetrySender.stats().dropped_total).toBe(before.dropped_total);
});
