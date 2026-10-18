/**
 * PSTelemetrySender (#87): batch metadata and drop accounting, the shared
 * 429 back-off and shutdown, with an injected fetch and no real timers.
 */
import {
  DEFAULT_MAX_PENDING,
  DEFAULT_SHUTDOWN_TIMEOUT_MS,
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
  jest.useFakeTimers(); // the wait is measured against Date.now()
  try {
    await s.send({});
  } finally {
    jest.useRealTimers();
  }
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

describe('shared 429 back-off (fake timers)', () => {
  beforeEach(() => jest.useFakeTimers());
  afterEach(() => jest.useRealTimers());

  /** A collector that answers from `script` (then 200) after `latencyMs`. */
  function collector(script: Response[], latencyMs = 10) {
    const seen: string[] = [];
    let inflight = 0;
    let maxInflight = 0;
    const s = new PSTelemetrySender({
      url: 'http://collector.test/ingest/events',
      apiKey: 'k',
      fetch: (async (_url: any, init: any) => {
        seen.push(JSON.parse(init.body).events[0].id);
        inflight += 1;
        maxInflight = Math.max(maxInflight, inflight);
        await new Promise((r) => setTimeout(r, latencyMs));
        inflight -= 1;
        return script.shift() ?? ok();
      }) as any,
    });
    return { s, seen, maxInflight: () => maxInflight };
  }
  const tooMany = (secs: string) =>
    new Response('', { status: 429, headers: { 'retry-after': secs } });

  test('a 429 pauses every send until the deadline, then sends in order, one at a time', async () => {
    const { s, seen, maxInflight } = collector([tooMany('5')]);
    const first = s.send({ id: 'a' });
    await jest.advanceTimersByTimeAsync(10); // a: 429
    expect(s.stats()).toMatchObject({ pending: 1, queued: 1 });
    expect(s.stats().retry_after_ms).toBeGreaterThan(4900);

    const rest = ['b', 'c', 'd'].map((id) => s.send({ id }));
    await jest.advanceTimersByTimeAsync(4980);
    expect(seen).toEqual(['a']); // nothing sent while paused
    expect(s.stats()).toMatchObject({ pending: 4, queued: 4 });

    await jest.advanceTimersByTimeAsync(20 + 4 * 10);
    await Promise.all([first, ...rest]);
    expect(seen).toEqual(['a', 'a', 'b', 'c', 'd']);
    expect(maxInflight()).toBe(1);
    expect(s.stats()).toMatchObject({
      enqueued: 4,
      sent: 4,
      pending: 0,
      queued: 0,
      retry_after_ms: 0,
      dropped_total: 0,
    });
  });

  test('a later 429 moves the shared deadline', async () => {
    const { s, seen } = collector([tooMany('2'), tooMany('3')]);
    const sends = [s.send({ id: 'a' }), s.send({ id: 'b' })];
    await jest.advanceTimersByTimeAsync(10); // a and b were in flight: both 429
    expect(seen).toEqual(['a', 'b']);
    await jest.advanceTimersByTimeAsync(2500);
    expect(seen).toEqual(['a', 'b']); // b's 3 s deadline replaced a's 2 s
    await jest.advanceTimersByTimeAsync(500 + 20);
    await Promise.all(sends);
    expect(seen).toEqual(['a', 'b', 'a', 'b']);
    expect(s.stats()).toMatchObject({ sent: 2, dropped_total: 0 });
  });

  test('a 429 while draining pauses the rest of the queue again', async () => {
    const { s, seen } = collector([tooMany('1'), ok(), tooMany('2')]);
    const sends = [s.send({ id: 'a' })];
    await jest.advanceTimersByTimeAsync(10);
    sends.push(s.send({ id: 'b' }), s.send({ id: 'c' }));
    await jest.advanceTimersByTimeAsync(1000 + 20); // a ok, b 429
    expect(seen).toEqual(['a', 'a', 'b']);
    await jest.advanceTimersByTimeAsync(1900);
    expect(seen).toEqual(['a', 'a', 'b']);
    await jest.advanceTimersByTimeAsync(100 + 20);
    await Promise.all(sends);
    expect(seen).toEqual(['a', 'a', 'b', 'b', 'c']);
    expect(s.stats()).toMatchObject({ sent: 3, dropped_total: 0 });
  });

  test('overflow while paused is counted as rate_limited', async () => {
    const { s, seen } = collector([tooMany('1')]);
    const sends = [s.send({ id: 'a' })];
    await jest.advanceTimersByTimeAsync(10);
    for (let i = 0; i < DEFAULT_MAX_PENDING + 1; i++)
      sends.push(s.send({ id: `e${i}` }));
    expect(s.stats()).toMatchObject({
      pending: DEFAULT_MAX_PENDING,
      dropped: { rate_limited: 2 },
    });
    await jest.advanceTimersByTimeAsync(1000 + DEFAULT_MAX_PENDING * 10 + 10);
    await Promise.all(sends);
    expect(seen.slice(0, 3)).toEqual(['a', 'a', 'e0']);
    expect(s.stats()).toMatchObject({
      sent: DEFAULT_MAX_PENDING,
      pending: 0,
      dropped_total: 2,
    });
  });

  test('a retry waiting on its own back-off joins a 429 pause', async () => {
    const script = [new Response('', { status: 500 }), tooMany('4')];
    const { s, seen } = collector(script);
    const a = s.send({ id: 'a' }); // 500, then waits 200 ms to retry
    await jest.advanceTimersByTimeAsync(10);
    const b = s.send({ id: 'b' }); // 429 while a waits
    await jest.advanceTimersByTimeAsync(10 + 200);
    expect(seen).toEqual(['a', 'b']); // a did not retry into the pause
    await jest.advanceTimersByTimeAsync(4000 + 20);
    await Promise.all([a, b]);
    expect(seen).toEqual(['a', 'b', 'a', 'b']);
    expect(s.stats()).toMatchObject({ sent: 2, dropped_total: 0 });
  });
});

describe('shutdown (fake timers)', () => {
  beforeEach(() => jest.useFakeTimers());
  afterEach(() => jest.useRealTimers());

  test('waits for pending sends and resolves when they finish', async () => {
    const { s } = sender(
      () => new Promise<Response>((r) => setTimeout(() => r(ok()), 100))
    );
    const send = s.send({});
    const done = jest.fn();
    s.shutdown(5000).then(done);
    await jest.advanceTimersByTimeAsync(100);
    await send;
    expect(done).toHaveBeenCalled();
    expect(s.stats()).toMatchObject({ sent: 1, dropped_total: 0 });
  });

  test('counts what is still pending at the timeout as shutdown', async () => {
    const { s, bodies } = sender(() => new Promise<Response>(() => {}), {
      shutdownTimeoutMs: 2000,
    });
    const sends = [s.send({}), s.send({})];
    const done = jest.fn();
    s.shutdown().then(done);
    await jest.advanceTimersByTimeAsync(1999);
    expect(done).not.toHaveBeenCalled();
    await jest.advanceTimersByTimeAsync(1);
    await Promise.all(sends);
    expect(done).toHaveBeenCalled();
    expect(bodies).toHaveLength(2);
    expect(s.stats()).toMatchObject({
      pending: 0,
      dropped: { shutdown: 2 },
      dropped_total: 2,
    });
    // Events after shutdown are counted, not sent.
    await s.send({});
    expect(bodies).toHaveLength(2);
    expect(s.stats()).toMatchObject({ enqueued: 3, dropped: { shutdown: 3 } });
  });

  test('ends a 429 back-off: queued events are sent at once', async () => {
    let calls = 0;
    const { s } = sender(
      () =>
        ++calls === 1
          ? new Response('', { status: 429, headers: { 'retry-after': '300' } })
          : ok(),
      // A sleep that never ends: only shutdown() can end the back-off.
      { sleep: () => new Promise(() => {}) }
    );
    const sends = [s.send({})];
    await jest.advanceTimersByTimeAsync(0);
    sends.push(s.send({}));
    expect(s.stats()).toMatchObject({ queued: 2 });
    expect(calls).toBe(1);
    const done = jest.fn();
    s.shutdown(5000).then(done);
    await jest.advanceTimersByTimeAsync(0);
    await Promise.all(sends);
    expect(done).toHaveBeenCalled();
    expect(calls).toBe(3);
    expect(s.stats()).toMatchObject({ sent: 2, dropped_total: 0 });
  });

  test('a send that completes after the timeout is not counted twice', async () => {
    let answer: (r: Response) => void = () => {};
    const { s } = sender(() => new Promise<Response>((r) => (answer = r)));
    const send = s.send({});
    await jest.advanceTimersByTimeAsync(0);
    s.shutdown(10);
    await jest.advanceTimersByTimeAsync(10);
    await send;
    answer(ok());
    await jest.advanceTimersByTimeAsync(0);
    expect(s.stats()).toMatchObject({
      sent: 0,
      dropped: { shutdown: 1 },
      dropped_total: 1,
    });
  });

  test('nothing pending: resolves without waiting', async () => {
    const { s } = sender(() => ok());
    await s.shutdown(5000);
    expect(s.stats().dropped_total).toBe(0);
  });
});

test('PS_TELEMETRY_SHUTDOWN_TIMEOUT_MS', () => {
  const env = { PS_API_KEY: 'k' };
  expect(PSTelemetrySender.fromEnv(env).shutdownTimeoutMs).toBe(
    DEFAULT_SHUTDOWN_TIMEOUT_MS
  );
  for (const [raw, want] of [
    ['250', 250],
    ['0', 0],
    ['-1', DEFAULT_SHUTDOWN_TIMEOUT_MS],
    ['soon', DEFAULT_SHUTDOWN_TIMEOUT_MS],
  ] as const)
    expect(
      PSTelemetrySender.fromEnv({
        ...env,
        PS_TELEMETRY_SHUTDOWN_TIMEOUT_MS: raw,
      }).shutdownTimeoutMs
    ).toBe(want);
});
