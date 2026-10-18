/**
 * PSTelemetrySender (#87): batch metadata and drop accounting, with an
 * injected fetch and no real timers.
 */
import {
  MAX_RETRY_AFTER_MS,
  PSTelemetrySender,
  parseRetryAfterMs,
} from './psTelemetrySender';

const ok = (body = '{}') => new Response(body, { status: 200 });

function sender(
  respond: () => Response | Promise<Response>,
  opts: Partial<ConstructorParameters<typeof PSTelemetrySender>[0]> = {}
) {
  const bodies: any[] = [];
  const sleeps: number[] = [];
  const s = new PSTelemetrySender({
    url: 'http://collector.test/ingest/events',
    apiKey: 'k',
    fetch: (async (_url: any, init: any) => {
      bodies.push(JSON.parse(init.body));
      return respond();
    }) as any,
    sleep: async (ms) => {
      sleeps.push(ms);
    },
    ...opts,
  });
  return { s, bodies, sleeps };
}

beforeAll(() => jest.spyOn(console, 'warn').mockImplementation(() => {}));

test('metadata matches the SDK shape', async () => {
  const { s, bodies } = sender(() => ok());
  await s.send({ vendor: 'openai', model: 'm', source: 'gateway' });
  await s.send({ vendor: 'openai', model: 'm', source: 'gateway' });
  expect(bodies[0]).toEqual({
    events: [{ vendor: 'openai', model: 'm', source: 'gateway' }],
    metadata: {
      client_id: s.clientId,
      seq: 1,
      enqueued_total: 1,
      dropped: {},
      sdk: 'gateway',
    },
  });
  expect(bodies[1].metadata.seq).toBe(2);
  expect(s.stats()).toMatchObject({ enqueued: 2, sent: 2, dropped_total: 0 });
});

test('no API key: nothing is sent or counted', async () => {
  const { s, bodies } = sender(() => ok(), { apiKey: '' });
  await s.send({});
  expect(bodies).toHaveLength(0);
  expect(s.stats().enqueued).toBe(0);
});

test('network errors are retried, then counted as send_failed', async () => {
  const { s, bodies, sleeps } = sender(
    () => {
      throw new Error('down');
    },
    { maxAttempts: 3, retryBaseMs: 10 }
  );
  await s.send({ a: 1 });
  expect(bodies.map((b) => b.metadata.seq)).toEqual([1, 2, 3]);
  expect(sleeps).toEqual([10, 20]);
  expect(s.stats().dropped.send_failed).toBe(1);
  expect(s.stats().sent).toBe(0);
});

test('429 honours Retry-After and counts rate_limited when it persists', async () => {
  const { s, sleeps } = sender(
    () => new Response('', { status: 429, headers: { 'retry-after': '7' } }),
    { maxAttempts: 2 }
  );
  await s.send({});
  expect(sleeps).toEqual([7000]);
  expect(s.stats().dropped.rate_limited).toBe(1);
});

test('a later batch reports earlier drops', async () => {
  let fail = true;
  const { s, bodies } = sender(
    () => (fail ? new Response('', { status: 500 }) : ok()),
    { maxAttempts: 1 }
  );
  await s.send({});
  fail = false;
  await s.send({});
  expect(bodies[1].metadata).toMatchObject({
    enqueued_total: 2,
    dropped: { send_failed: 1 },
  });
});

test('unserialisable events are dropped without a request', async () => {
  const { s, bodies } = sender(() => ok());
  await s.send({ n: BigInt(1) });
  const circular: any = {};
  circular.self = circular;
  await s.send(circular);
  expect(bodies).toHaveLength(0);
  expect(s.stats().dropped.serialization_failed).toBe(2);
  await s.send({});
  // seq is not consumed by a failed serialisation.
  expect(bodies[0].metadata).toMatchObject({
    seq: 1,
    enqueued_total: 3,
    dropped: { serialization_failed: 2 },
  });
});

test('overflow while sends are pending is counted', async () => {
  let release: () => void = () => {};
  const gate = new Promise<void>((r) => (release = r));
  const { s } = sender(
    async () => {
      await gate;
      return ok();
    },
    { maxPending: 2 }
  );
  const sends = [s.send({}), s.send({}), s.send({})];
  await Promise.resolve();
  expect(s.stats().dropped.buffer_overflow).toBe(1);
  release();
  await Promise.all(sends);
  expect(s.stats()).toMatchObject({ enqueued: 3, sent: 2, pending: 0 });
});

test('overflow while sends are failing is attributed to the failure', async () => {
  let release: () => void = () => {};
  const gate = new Promise<void>((r) => (release = r));
  let first = true;
  const { s } = sender(
    async () => {
      if (first) {
        first = false;
        return new Response('', { status: 500 });
      }
      await gate;
      return ok();
    },
    { maxPending: 1, maxAttempts: 2 }
  );
  const pending = s.send({}); // fails once, then waits on the gate
  await new Promise((r) => setTimeout(r, 0));
  await s.send({}); // overflows while the first is backing off
  expect(s.stats().dropped.send_failed).toBe(1);
  release();
  await pending;
  expect(s.stats().sent).toBe(1);
});

test('server_skipped rows in a 2xx response are counted', async () => {
  const { s } = sender(() => ok('{"skipped": 1}'));
  await s.send({});
  expect(s.stats()).toMatchObject({ sent: 0, dropped: { server_skipped: 1 } });
});

test('parseRetryAfterMs', () => {
  expect(parseRetryAfterMs('2')).toBe(2000);
  expect(parseRetryAfterMs('99999')).toBe(MAX_RETRY_AFTER_MS);
  expect(parseRetryAfterMs('nonsense')).toBeUndefined();
  expect(parseRetryAfterMs(undefined)).toBeUndefined();
  const date = new Date(Date.now() + 5000).toUTCString();
  const ms = parseRetryAfterMs(date)!;
  expect(ms).toBeGreaterThan(3000);
  expect(ms).toBeLessThanOrEqual(5000);
});
