/**
 * PS fork (security): the psOtel trace exporter's egress is SSRF-safe, like
 * the OTLP logs exporter. The endpoint must pass `isSafeUpstreamURL`, sends
 * never follow a redirect, and spans for a refused endpoint are dropped and
 * counted in ps_gateway_telemetry_dropped_total{sink="otel"}.
 */
import { createServer, Server } from 'http';
import { AddressInfo } from 'net';
import { OtelConfig, OtlpJsonExporter } from './psOtel';
import { defaultRegistry } from './psMetrics/registry';

const config = (endpoint: string): OtelConfig => ({
  endpoint,
  headers: {},
  serviceName: 'gw',
  captureContent: false,
  maxBatch: 10,
  flushMs: 60_000,
  maxQueue: 100,
});

const span = (id: string) => ({ span: { spanId: id } }) as any;

const dropped = (reason: string) => {
  const line = `ps_gateway_telemetry_dropped_total{sink="otel",reason="${reason}"}`;
  const hit = defaultRegistry
    .render()
    .split('\n')
    .find((l) => l.startsWith(line + ' '));
  return hit ? Number(hit.slice(line.length + 1)) : 0;
};

async function listen(server: Server): Promise<number> {
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
  return (server.address() as AddressInfo).port;
}

function close(server: Server) {
  server.closeAllConnections?.();
  server.close();
}

afterEach(() => jest.restoreAllMocks());

test.each([
  'http://169.254.169.254/latest/meta-data/',
  'http://10.0.0.5:4318/v1/traces',
  'http://user:pw@otel.example.com/v1/traces',
  'file:///etc/passwd',
  'not a url',
])(
  'a refused endpoint (%s) sends nothing; spans are counted',
  async (endpoint) => {
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
    const fetchFn = jest.fn(async () => ({ ok: true }) as any);
    const ex = new OtlpJsonExporter(config(endpoint), fetchFn);
    const before = dropped('unsafe_endpoint');
    ex.enqueue(span('a'));
    ex.enqueue(span('b'));
    await expect(ex.flush()).resolves.toBeUndefined();
    await ex.flush();
    expect(fetchFn).not.toHaveBeenCalled();
    expect(ex.pending).toBe(0);
    expect(dropped('unsafe_endpoint')).toBe(before + 2);
    expect(warn).toHaveBeenCalledTimes(1);
  }
);

test('an allowed endpoint is still sent to', async () => {
  const fetchFn = jest.fn(async () => ({ ok: true }) as any);
  const ex = new OtlpJsonExporter(
    config('https://otlp.example.com/v1/traces'),
    fetchFn
  );
  ex.enqueue(span('a'));
  await ex.flush();
  expect(fetchFn).toHaveBeenCalledTimes(1);
});

test('a redirect is not followed and the batch is counted as dropped', async () => {
  jest.spyOn(console, 'warn').mockImplementation(() => {});
  let targetHits = 0;
  const target = createServer((req, res) => {
    targetHits++;
    req.resume();
    res.end();
  });
  let hits = 0;
  const redirecting = createServer((req, res) => {
    hits++;
    req.resume();
    res.writeHead(307, {
      location: `http://127.0.0.1:${targetPort}/v1/traces`,
    });
    res.end();
  });
  const targetPort = await listen(target);
  const port = await listen(redirecting);
  try {
    // Default fetch: upstreamFetch. 127.0.0.1 is a default trusted host.
    const ex = new OtlpJsonExporter(
      config(`http://127.0.0.1:${port}/v1/traces`)
    );
    const before = dropped('http_error');
    ex.enqueue(span('a'));
    await ex.flush();
    expect(hits).toBe(1);
    expect(targetHits).toBe(0);
    expect(dropped('http_error')).toBe(before + 1);
  } finally {
    close(redirecting);
    close(target);
  }
});
