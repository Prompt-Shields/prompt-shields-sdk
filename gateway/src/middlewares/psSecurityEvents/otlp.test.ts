/**
 * Security events as OTLP log records: config resolution, field mapping and
 * the bounded, non-blocking exporter, against a fake OTLP/HTTP receiver
 * (a real HTTP server on 127.0.0.1, reached through upstreamFetch).
 */
import { createServer, IncomingMessage, Server, ServerResponse } from 'http';
import { AddressInfo } from 'net';
import {
  buildLogsExportRequest,
  MAX_IN_FLIGHT,
  OtlpLogsExporter,
  SecurityLogsOtlpConfig,
  securityLogRecord,
  securityLogsOtlpConfig,
  SEVERITY_NUMBER,
} from './otlp';
import { defaultRegistry } from '../psMetrics/registry';
import '../psMetrics/security';
import type { SecurityEvent } from './schema';

// ---------------------------------------------------------------------------
// Fake OTLP receiver

interface Received {
  path: string;
  headers: Record<string, string | string[] | undefined>;
  body: any;
}

type Handler = (req: IncomingMessage, res: ServerResponse) => void;

async function receiver(handler?: Handler) {
  const received: Received[] = [];
  let respond: Handler =
    handler ??
    ((_req, res) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end('{}');
    });
  const server: Server = createServer((req, res) => {
    let raw = '';
    req.on('data', (d) => (raw += d));
    req.on('end', () => {
      received.push({
        path: req.url || '',
        headers: req.headers,
        body: raw ? JSON.parse(raw) : undefined,
      });
      respond(req, res);
    });
  });
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
  const { port } = server.address() as AddressInfo;
  return {
    url: `http://127.0.0.1:${port}`,
    received,
    setHandler: (h: Handler) => (respond = h),
    close: () =>
      new Promise<void>((r) => {
        server.closeAllConnections?.();
        server.close(() => r());
      }),
  };
}

// ---------------------------------------------------------------------------
// Helpers

const dropped = (reason: string) => {
  const line = `ps_gateway_telemetry_dropped_total{sink="otel_logs",reason="${reason}"}`;
  const hit = defaultRegistry
    .render()
    .split('\n')
    .find((l) => l.startsWith(line + ' '));
  return hit ? Number(hit.slice(line.length + 1)) : 0;
};

const config = (
  endpoint: string,
  over: Partial<SecurityLogsOtlpConfig> = {}
): SecurityLogsOtlpConfig => ({
  endpoint,
  headers: {},
  serviceName: 'gw-test',
  maxBatch: 10,
  flushMs: 60_000,
  maxQueue: 100,
  ...over,
});

const denyEvent = (): SecurityEvent => ({
  '@timestamp': '2026-10-20T12:00:00.123Z',
  'ps.schema_version': '1.0.0',
  'event.id': '00000000-0000-4000-8000-000000000001',
  'event.name': 'guardrail.verdict',
  'event.kind': 'alert',
  'event.category': ['intrusion_detection'],
  'event.outcome': 'failure',
  'log.logger': 'ps.security',
  'service.name': 'prompt-shields-gateway',
  vendor_product: 'Prompt Shields Gateway',
  severity: 'high',
  action: 'blocked',
  signature: 'portkey.pii',
  'ps.request_id': 'trace-1',
  'http.route': '/v1/chat/completions',
  'http.request.method': 'POST',
  'ps.business_unit': 'retail',
  'gen_ai.provider.name': 'openai',
  'ps.guardrail.hook_id': 'input_guardrail_x1',
  'ps.guardrail.hook_type': 'guardrail',
  'ps.guardrail.check_id': 'portkey.pii',
  'ps.guardrail.stage': 'input',
  'ps.guardrail.verdict': 'deny',
  'ps.guardrail.deny': true,
  'ps.guardrail.duration_ms': 12,
});

const attrs = (record: any) =>
  Object.fromEntries(
    record.attributes.map((a: any) => [
      a.key,
      a.value.stringValue ??
        (a.value.intValue !== undefined
          ? Number(a.value.intValue)
          : undefined) ??
        a.value.boolValue ??
        a.value.arrayValue?.values.map((v: any) => v.stringValue),
    ])
  );

let warn: jest.SpyInstance;
beforeEach(() => {
  warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
});
afterEach(() => warn.mockRestore());

// ---------------------------------------------------------------------------

describe('securityLogsOtlpConfig', () => {
  test('off unless PS_SECURITY_EVENTS_OTLP is true', () => {
    expect(
      securityLogsOtlpConfig({ PS_OTEL_ENDPOINT: 'http://otel:4318/v1/traces' })
    ).toBeNull();
  });

  test('explicit endpoint and headers; batching from PS_OTEL_*', () => {
    expect(
      securityLogsOtlpConfig({
        PS_SECURITY_EVENTS_OTLP: 'true',
        PS_SECURITY_EVENTS_OTLP_ENDPOINT: 'https://otel.example.com/v1/logs',
        PS_SECURITY_EVENTS_OTLP_HEADERS: 'x-token=abc',
        PS_OTEL_MAX_BATCH: '16',
        PS_OTEL_FLUSH_MS: '250',
        PS_OTEL_MAX_QUEUE: '500',
        PS_API_KEY: 'ps-key',
      })
    ).toEqual({
      endpoint: 'https://otel.example.com/v1/logs',
      headers: { 'x-token': 'abc' },
      serviceName: 'prompt-shields-gateway',
      maxBatch: 16,
      flushMs: 250,
      maxQueue: 500,
    });
  });

  test("derived from psOtel's endpoint, with its headers, never PS_API_KEY", () => {
    const cfg = securityLogsOtlpConfig({
      PS_SECURITY_EVENTS_OTLP: '1',
      PS_OTEL_ENDPOINT: 'http://otel-collector:4318/v1/traces',
      PS_OTEL_HEADERS: 'x-scope=gw',
      PS_API_KEY: 'ps-key',
    });
    expect(cfg?.endpoint).toBe('http://otel-collector:4318/v1/logs');
    expect(cfg?.headers).toEqual({ 'x-scope': 'gw' });
  });

  test('the PS collector trace ingest path is not turned into a logs URL', () => {
    expect(
      securityLogsOtlpConfig({
        PS_SECURITY_EVENTS_OTLP: 'true',
        PS_OTEL_ENDPOINT: 'https://ps.example.com/ingest/otel/v1/traces',
      })
    ).toBeNull();
  });

  test('standard OTEL_EXPORTER_OTLP_* variables', () => {
    expect(
      securityLogsOtlpConfig({
        PS_SECURITY_EVENTS_OTLP: 'true',
        OTEL_EXPORTER_OTLP_ENDPOINT: 'https://otel.example.com/',
        OTEL_EXPORTER_OTLP_HEADERS: 'authorization=Bearer%20t',
      })
    ).toMatchObject({
      endpoint: 'https://otel.example.com/v1/logs',
      headers: { authorization: 'Bearer t' },
    });
    expect(
      securityLogsOtlpConfig({
        PS_SECURITY_EVENTS_OTLP: 'true',
        OTEL_EXPORTER_OTLP_LOGS_ENDPOINT: 'https://logs.example.com/custom',
        OTEL_EXPORTER_OTLP_ENDPOINT: 'https://otel.example.com',
      })?.endpoint
    ).toBe('https://logs.example.com/custom');
  });

  test.each([
    'http://169.254.169.254/v1/logs',
    'http://metadata.google.internal/v1/logs',
    'http://10.0.0.5:4318/v1/logs',
    'file:///etc/passwd',
    'http://user:pw@otel.example.com/v1/logs',
  ])('refuses an unsafe egress target: %s', (endpoint) => {
    expect(
      securityLogsOtlpConfig({
        PS_SECURITY_EVENTS_OTLP: 'true',
        PS_SECURITY_EVENTS_OTLP_ENDPOINT: endpoint,
      })
    ).toBeNull();
  });
});

describe('log record mapping', () => {
  test('attributes, severity, event name, timestamp and body', () => {
    const event = denyEvent();
    const record = securityLogRecord(event);
    expect(record.timeUnixNano).toBe(
      `${Date.parse('2026-10-20T12:00:00.123Z')}000000`
    );
    expect(record.severityNumber).toBe(17);
    expect(record.severityText).toBe('high');
    expect(record.eventName).toBe('guardrail.verdict');
    expect(JSON.parse(record.body.stringValue)).toEqual(event);
    const a = attrs(record);
    expect(a).toMatchObject({
      'event.name': 'guardrail.verdict',
      'event.category': ['intrusion_detection'],
      'ps.schema_version': '1.0.0',
      'ps.guardrail.deny': true,
      'ps.guardrail.duration_ms': 12,
      'ps.business_unit': 'retail',
      action: 'blocked',
      signature: 'portkey.pii',
    });
    expect(a).not.toHaveProperty('@timestamp');
    expect(a).not.toHaveProperty('service.name');
  });

  test('every CIM severity has an OTel severity number', () => {
    expect(SEVERITY_NUMBER).toEqual({
      informational: 9,
      low: 13,
      medium: 15,
      high: 17,
    });
  });

  test('one resource with service.name and the ps.security scope', () => {
    const req = buildLogsExportRequest([{ x: 1 }], 'gw');
    expect(req.resourceLogs[0].resource.attributes).toEqual([
      { key: 'service.name', value: { stringValue: 'gw' } },
    ]);
    expect(req.resourceLogs[0].scopeLogs[0].scope).toEqual({
      name: 'ps.security',
      version: '1.0.0',
    });
  });
});

describe('OtlpLogsExporter against a fake receiver', () => {
  test('delivers batches to /v1/logs with the configured headers', async () => {
    const r = await receiver();
    try {
      const exporter = new OtlpLogsExporter(
        config(`${r.url}/v1/logs`, { headers: { 'x-token': 't1' } })
      );
      exporter.enqueue(securityLogRecord(denyEvent()));
      exporter.enqueue(securityLogRecord(denyEvent()));
      expect(r.received).toHaveLength(0); // waits for the batch or timer
      await exporter.flush();
      expect(r.received).toHaveLength(1);
      expect(r.received[0].path).toBe('/v1/logs');
      expect(r.received[0].headers['x-token']).toBe('t1');
      expect(r.received[0].headers['content-type']).toBe('application/json');
      const records =
        r.received[0].body.resourceLogs[0].scopeLogs[0].logRecords;
      expect(records).toHaveLength(2);
      expect(records[0].eventName).toBe('guardrail.verdict');
    } finally {
      await r.close();
    }
  });

  test('a full batch is sent without waiting for the timer', async () => {
    const r = await receiver();
    try {
      const exporter = new OtlpLogsExporter(
        config(`${r.url}/v1/logs`, { maxBatch: 2 })
      );
      exporter.enqueue(securityLogRecord(denyEvent()));
      exporter.enqueue(securityLogRecord(denyEvent()));
      for (let i = 0; i < 50 && !r.received.length; i++)
        await new Promise((res) => setTimeout(res, 10));
      expect(r.received).toHaveLength(1);
      expect(exporter.pending).toBe(0);
    } finally {
      await r.close();
    }
  });

  test('a redirect is not followed and the batch is counted as dropped', async () => {
    const target = await receiver();
    const r = await receiver((_req, res) => {
      res.writeHead(307, { location: `${target.url}/v1/logs` });
      res.end();
    });
    try {
      const before = dropped('http_error');
      const exporter = new OtlpLogsExporter(config(`${r.url}/v1/logs`));
      exporter.enqueue(securityLogRecord(denyEvent()));
      await exporter.flush();
      expect(r.received).toHaveLength(1);
      expect(target.received).toHaveLength(0);
      expect(dropped('http_error')).toBe(before + 1);
    } finally {
      await r.close();
      await target.close();
    }
  });

  test('HTTP errors and unreachable receivers are counted, never thrown', async () => {
    const r = await receiver((_req, res) => {
      res.writeHead(503);
      res.end();
    });
    const httpBefore = dropped('http_error');
    const sendBefore = dropped('send_failed');
    try {
      const exporter = new OtlpLogsExporter(config(`${r.url}/v1/logs`));
      exporter.enqueue(securityLogRecord(denyEvent()));
      exporter.enqueue(securityLogRecord(denyEvent()));
      await expect(exporter.flush()).resolves.toBeUndefined();
    } finally {
      await r.close();
    }
    // The port is closed now: connection refused.
    const gone = new OtlpLogsExporter(config(`${r.url}/v1/logs`));
    gone.enqueue(securityLogRecord(denyEvent()));
    await expect(gone.flush()).resolves.toBeUndefined();
    expect(dropped('http_error')).toBe(httpBefore + 2);
    expect(dropped('send_failed')).toBe(sendBefore + 1);
    expect(warn).toHaveBeenCalled();
  });

  test('the queue is bounded: the oldest records are dropped first', async () => {
    const r = await receiver();
    try {
      const before = dropped('queue_full');
      const exporter = new OtlpLogsExporter(
        config(`${r.url}/v1/logs`, { maxQueue: 3 })
      );
      for (let i = 0; i < 5; i++) exporter.enqueue({ seq: i });
      expect(exporter.pending).toBe(3);
      expect(dropped('queue_full')).toBe(before + 2);
      await exporter.flush();
      const records =
        r.received[0].body.resourceLogs[0].scopeLogs[0].logRecords;
      expect(records.map((x: any) => x.seq)).toEqual([2, 3, 4]);
    } finally {
      await r.close();
    }
  });

  test('drain (shutdown) waits for in-flight sends and sends what is still queued', async () => {
    const held: ServerResponse[] = [];
    const r = await receiver((_req, res) => held.push(res));
    const release = setInterval(() => {
      for (const res of held.splice(0)) res.end('{}');
    }, 20);
    try {
      const exporter = new OtlpLogsExporter(
        config(`${r.url}/v1/logs`, { maxBatch: 1, flushMs: 60_000 })
      );
      for (let i = 0; i < MAX_IN_FLIGHT + 3; i++) exporter.enqueue({ seq: i });
      // Every slot is taken: a plain flush sends nothing more.
      await exporter.flush();
      expect(exporter.pending).toBe(3);
      await exporter.drain();
      expect(exporter.pending).toBe(0);
      expect(r.received).toHaveLength(MAX_IN_FLIGHT + 3);
    } finally {
      clearInterval(release);
      for (const res of held.splice(0)) res.end('{}');
      await r.close();
    }
  });

  test('enqueue never blocks and at most MAX_IN_FLIGHT batches are sent at once', async () => {
    const held: ServerResponse[] = [];
    const r = await receiver((_req, res) => held.push(res));
    try {
      const exporter = new OtlpLogsExporter(
        config(`${r.url}/v1/logs`, { maxBatch: 1, flushMs: 5 })
      );
      const start = Date.now();
      for (let i = 0; i < MAX_IN_FLIGHT + 3; i++) exporter.enqueue({ seq: i });
      expect(Date.now() - start).toBeLessThan(50);
      for (let i = 0; i < 100 && r.received.length < MAX_IN_FLIGHT; i++)
        await new Promise((res) => setTimeout(res, 10));
      await new Promise((res) => setTimeout(res, 50));
      expect(r.received).toHaveLength(MAX_IN_FLIGHT);
      expect(exporter.pending).toBe(3);

      // Free the slots: the waiting records go out on the next timer.
      for (const res of held.splice(0)) res.end('{}');
      for (let i = 0; i < 100 && r.received.length < MAX_IN_FLIGHT + 3; i++) {
        for (const res of held.splice(0)) res.end('{}');
        await new Promise((res) => setTimeout(res, 10));
      }
      for (const res of held.splice(0)) res.end('{}');
      expect(r.received).toHaveLength(MAX_IN_FLIGHT + 3);
      expect(exporter.pending).toBe(0);
    } finally {
      for (const res of held.splice(0)) res.end('{}');
      await r.close();
    }
  });
});
