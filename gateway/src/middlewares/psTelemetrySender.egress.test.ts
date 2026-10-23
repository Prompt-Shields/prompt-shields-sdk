/**
 * PS fork (security): the collector telemetry sender's egress is SSRF-safe.
 * `PS_COLLECTOR_URL` must pass `isSafeUpstreamURL` (a refused URL is never
 * called; events drop as send_failed without retries), and sends never
 * follow a redirect.
 */
import { createServer, Server } from 'http';
import { AddressInfo } from 'net';
import { PSTelemetrySender } from './psTelemetrySender';

beforeAll(() => jest.spyOn(console, 'warn').mockImplementation(() => {}));

async function listen(server: Server): Promise<number> {
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
  return (server.address() as AddressInfo).port;
}

function close(server: Server) {
  server.closeAllConnections?.();
  server.close();
}

test.each([
  'http://169.254.169.254/ingest/events',
  'http://10.0.0.5:8000/ingest/events',
  'ftp://collector.test/ingest/events',
])(
  'a refused collector URL (%s) is never called and drops without retries',
  async (url) => {
    const calls: string[] = [];
    const sleeps: number[] = [];
    const s = new PSTelemetrySender({
      url,
      apiKey: 'k',
      maxAttempts: 3,
      fetch: (async (u: string) => {
        calls.push(u);
        return new Response('{}', { status: 200 });
      }) as any,
      sleep: async (ms) => {
        sleeps.push(ms);
      },
    });
    await s.send({ a: 1 });
    expect(calls).toHaveLength(0);
    expect(sleeps).toEqual([]);
    expect(s.stats().dropped.send_failed).toBe(1);
    expect(s.stats().sent).toBe(0);
  }
);

test('an in-cluster collector name is allowed', async () => {
  const calls: string[] = [];
  const s = new PSTelemetrySender({
    url: 'http://prompt-shields-collector:8000/ingest/events',
    apiKey: 'k',
    fetch: (async (u: string) => {
      calls.push(u);
      return new Response('{}', { status: 200 });
    }) as any,
  });
  await s.send({ a: 1 });
  expect(calls).toHaveLength(1);
  expect(s.stats().sent).toBe(1);
});

test('a redirect from the collector is not followed', async () => {
  let targetHits = 0;
  const target = createServer((req, res) => {
    targetHits++;
    req.resume();
    res.end('{}');
  });
  const redirecting = createServer((req, res) => {
    req.resume();
    res.writeHead(307, {
      location: `http://127.0.0.1:${targetPort}/ingest/events`,
    });
    res.end();
  });
  const targetPort = await listen(target);
  const port = await listen(redirecting);
  try {
    // No injected fetch: the default (upstreamFetch) is used.
    const s = new PSTelemetrySender({
      url: `http://127.0.0.1:${port}/ingest/events`,
      apiKey: 'k',
      maxAttempts: 1,
      sleep: async () => {},
    });
    await s.send({ a: 1 });
    expect(targetHits).toBe(0);
    expect(s.stats().dropped.send_failed).toBe(1);
  } finally {
    close(redirecting);
    close(target);
  }
});
