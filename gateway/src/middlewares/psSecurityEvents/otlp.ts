/**
 * Security events (schema v1) as OTLP log records, sent to `/v1/logs`.
 *
 * Opt-in with `PS_SECURITY_EVENTS_OTLP=true`. Independent of
 * `PS_SECURITY_EVENTS_LOG` (stdout JSON lines): either, both or neither can
 * be on. Each event becomes one LogRecord:
 *
 * - `body`: the event as the same JSON string the stdout sink writes, so a
 *   backend that indexes the body (Splunk HEC, Loki) gets the v1 schema as is;
 * - `attributes`: every event field except `@timestamp` (the record's
 *   `timeUnixNano`) and `service.name` (a resource attribute), keys unchanged;
 * - `eventName`: `event.name`;
 * - `severityText`: the CIM severity; `severityNumber`: informational → INFO
 *   (9), low → WARN (13), medium → WARN3 (15), high → ERROR (17).
 *
 * Endpoint, first match wins (headers follow the endpoint they belong to;
 * `PS_SECURITY_EVENTS_OTLP_HEADERS` overrides them all):
 * 1. `PS_SECURITY_EVENTS_OTLP_ENDPOINT` (full URL);
 * 2. `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` (full URL) with
 *    `OTEL_EXPORTER_OTLP_LOGS_HEADERS` / `OTEL_EXPORTER_OTLP_HEADERS`;
 * 3. psOtel's `PS_OTEL_ENDPOINT` with `/v1/traces` replaced by `/v1/logs`,
 *    with `PS_OTEL_HEADERS`. Not the PS collector's `/ingest/otel/` path,
 *    which takes traces only, and `PS_API_KEY` is never attached;
 * 4. `OTEL_EXPORTER_OTLP_ENDPOINT` + `/v1/logs` with
 *    `OTEL_EXPORTER_OTLP_LOGS_HEADERS` / `OTEL_EXPORTER_OTLP_HEADERS`.
 * Batching reuses psOtel's `PS_OTEL_MAX_BATCH`, `PS_OTEL_FLUSH_MS` and
 * `PS_OTEL_MAX_QUEUE`.
 *
 * Egress: the endpoint must pass `isSafeUpstreamURL` (no metadata, private
 * or loopback addresses unless listed in `TRUSTED_CUSTOM_HOSTS`), and sends
 * go through `upstreamFetch`, which never follows a redirect.
 *
 * Delivery is best-effort: enqueue never blocks the request, the queue is
 * bounded (oldest dropped first), at most MAX_IN_FLIGHT batches are in
 * flight, and failed batches are not retried. Every dropped record is
 * counted in `ps_gateway_telemetry_dropped_total{sink="otel_logs"}`.
 */

import { isSafeUpstreamURL } from '../requestValidator';
import { parseHeaderList, toOtlpAttributes } from '../psOtel';
import { telemetryDroppedTotal } from '../psMetrics/security';
import { upstreamFetch } from '../../utils/fetch';
import {
  SECURITY_EVENT_SCHEMA_VERSION,
  SecurityEvent,
  SecuritySeverity,
} from './schema';

export const SINK = 'otel_logs';
/** Batches sent concurrently; the rest wait in the (bounded) queue. */
export const MAX_IN_FLIGHT = 4;
const SEND_TIMEOUT_MS = 10_000;

export interface SecurityLogsOtlpConfig {
  endpoint: string;
  headers: Record<string, string>;
  serviceName: string;
  maxBatch: number;
  flushMs: number;
  maxQueue: number;
}

const truthy = (v?: string) => /^(1|true|yes|on)$/i.test((v || '').trim());

const posInt = (v: string | undefined, dflt: number) => {
  const n = Number(v);
  return Number.isInteger(n) && n > 0 ? n : dflt;
};

const withLogsPath = (base: string) => `${base.replace(/\/+$/, '')}/v1/logs`;

/**
 * Exporter settings, or null when `PS_SECURITY_EVENTS_OTLP` is off, no
 * endpoint is configured, or the endpoint fails the egress check.
 */
export function securityLogsOtlpConfig(
  env: Record<string, string | undefined> = process.env,
  warn: (msg: string) => void = (m) => console.warn(m)
): SecurityLogsOtlpConfig | null {
  if (!truthy(env.PS_SECURITY_EVENTS_OTLP)) return null;

  const genericHeaders =
    env.OTEL_EXPORTER_OTLP_LOGS_HEADERS || env.OTEL_EXPORTER_OTLP_HEADERS;
  const psOtel = (env.PS_OTEL_ENDPOINT || '').trim();
  let endpoint = '';
  let headers: string | undefined;
  if ((env.PS_SECURITY_EVENTS_OTLP_ENDPOINT || '').trim()) {
    endpoint = env.PS_SECURITY_EVENTS_OTLP_ENDPOINT!.trim();
  } else if ((env.OTEL_EXPORTER_OTLP_LOGS_ENDPOINT || '').trim()) {
    endpoint = env.OTEL_EXPORTER_OTLP_LOGS_ENDPOINT!.trim();
    headers = genericHeaders;
  } else if (
    /\/v1\/traces\/?$/.test(psOtel) &&
    !psOtel.includes('/ingest/otel/')
  ) {
    endpoint = psOtel.replace(/\/v1\/traces\/?$/, '/v1/logs');
    headers = env.PS_OTEL_HEADERS;
  } else if ((env.OTEL_EXPORTER_OTLP_ENDPOINT || '').trim()) {
    endpoint = withLogsPath(env.OTEL_EXPORTER_OTLP_ENDPOINT!.trim());
    headers = genericHeaders;
  }
  if (!endpoint) {
    warn(
      '[PS security events] PS_SECURITY_EVENTS_OTLP is on but no logs ' +
        'endpoint is set (PS_SECURITY_EVENTS_OTLP_ENDPOINT); OTLP export is off.'
    );
    return null;
  }
  if (!isSafeUpstreamURL(endpoint)) {
    warn(
      '[PS security events] the OTLP logs endpoint is not an allowed egress ' +
        'target (list private or loopback hosts in TRUSTED_CUSTOM_HOSTS); ' +
        'OTLP export is off.'
    );
    return null;
  }
  return {
    endpoint,
    headers: parseHeaderList(env.PS_SECURITY_EVENTS_OTLP_HEADERS || headers),
    serviceName: env.OTEL_SERVICE_NAME || 'prompt-shields-gateway',
    maxBatch: posInt(env.PS_OTEL_MAX_BATCH, 64),
    flushMs: posInt(env.PS_OTEL_FLUSH_MS, 5000),
    maxQueue: posInt(env.PS_OTEL_MAX_QUEUE, 2048),
  };
}

// ---------------------------------------------------------------------------
// Mapping

/** OTel SeverityNumber for a CIM severity. */
export const SEVERITY_NUMBER: Record<SecuritySeverity, number> = {
  informational: 9, // INFO
  low: 13, // WARN
  medium: 15, // WARN3
  high: 17, // ERROR
};

const nanosOf = (iso: string) => {
  const ms = Date.parse(iso);
  return `${Number.isFinite(ms) ? ms : Date.now()}000000`;
};

/** One OTLP JSON LogRecord for a security event. */
export function securityLogRecord(event: SecurityEvent) {
  const attrs: Record<string, any> = {};
  for (const [k, v] of Object.entries(event)) {
    if (k === '@timestamp' || k === 'service.name') continue;
    attrs[k] = v;
  }
  return {
    timeUnixNano: nanosOf(event['@timestamp']),
    observedTimeUnixNano: `${Date.now()}000000`,
    severityNumber: SEVERITY_NUMBER[event.severity] ?? 0,
    severityText: event.severity,
    eventName: event['event.name'],
    body: { stringValue: JSON.stringify(event) },
    attributes: toOtlpAttributes(attrs),
  };
}

const SCOPE = { name: 'ps.security', version: SECURITY_EVENT_SCHEMA_VERSION };

/** ExportLogsServiceRequest JSON. */
export function buildLogsExportRequest(records: any[], serviceName: string) {
  return {
    resourceLogs: [
      {
        resource: {
          attributes: toOtlpAttributes({ 'service.name': serviceName }),
        },
        scopeLogs: [{ scope: SCOPE, logRecords: records }],
      },
    ],
  };
}

// ---------------------------------------------------------------------------
// Exporter

type FetchFn = (url: string, init: RequestInit) => Promise<Response>;

export class OtlpLogsExporter {
  private queue: any[] = [];
  private timer: any;
  private inFlight = 0;
  /** Sends in progress, so `drain` can wait for them. */
  private sending = new Set<Promise<void>>();
  private warned = false;

  constructor(
    readonly config: SecurityLogsOtlpConfig,
    private fetchFn: FetchFn = upstreamFetch
  ) {}

  get pending() {
    return this.queue.length;
  }

  /**
   * Queue a record; never blocks or throws. With `waitUntil` (Workers) the
   * queue is flushed now and the send kept alive past the response; on Node,
   * a full batch is sent at once, the rest after `flushMs`.
   */
  enqueue(record: any, waitUntil?: (p: Promise<unknown>) => void) {
    this.queue.push(record);
    if (this.queue.length > this.config.maxQueue) {
      const dropped = this.queue.length - this.config.maxQueue;
      this.queue.splice(0, dropped);
      telemetryDroppedTotal.inc({ sink: SINK, reason: 'queue_full' }, dropped);
    }
    if (waitUntil) waitUntil(this.flush());
    else if (this.queue.length >= this.config.maxBatch) void this.flush();
    else this.schedule();
  }

  private schedule() {
    if (this.timer || !this.queue.length) return;
    this.timer = setTimeout(() => {
      this.timer = undefined;
      void this.flush();
    }, this.config.flushMs);
    this.timer?.unref?.();
  }

  /**
   * Send queued records, up to MAX_IN_FLIGHT batches at once; what is left
   * stays queued for the next flush. Never throws.
   */
  async flush(): Promise<void> {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = undefined;
    }
    const sends: Promise<void>[] = [];
    while (this.queue.length && this.inFlight < MAX_IN_FLIGHT) {
      const p = this.send(this.queue.splice(0, this.config.maxBatch));
      this.sending.add(p);
      void p.then(() => this.sending.delete(p));
      sends.push(p);
    }
    await Promise.all(sends);
  }

  /**
   * Send everything queued and wait for every send in progress, including
   * ones started before this call (shutdown). `flush` alone sends nothing
   * while MAX_IN_FLIGHT batches are out. Never throws.
   */
  async drain(): Promise<void> {
    while (this.queue.length || this.sending.size) {
      await this.flush();
      if (this.sending.size) await Promise.race(this.sending);
    }
  }

  private async send(batch: any[]): Promise<void> {
    this.inFlight++;
    try {
      const timeout = (AbortSignal as any).timeout?.(SEND_TIMEOUT_MS);
      const res = await this.fetchFn(this.config.endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...this.config.headers },
        body: JSON.stringify(
          buildLogsExportRequest(batch, this.config.serviceName)
        ),
        ...(timeout ? { signal: timeout } : {}),
      });
      try {
        await res.body?.cancel();
      } catch {
        /* nothing to discard */
      }
      if (!res.ok) {
        telemetryDroppedTotal.inc(
          { sink: SINK, reason: 'http_error' },
          batch.length
        );
        this.warn(`HTTP ${res.status}`);
      }
    } catch (err) {
      telemetryDroppedTotal.inc(
        { sink: SINK, reason: 'send_failed' },
        batch.length
      );
      this.warn(err instanceof Error ? err.name : 'error');
    } finally {
      this.inFlight--;
      // Records that waited for a free slot go out with the next timer.
      this.schedule();
    }
  }

  private warn(detail: string) {
    if (this.warned) return;
    this.warned = true;
    console.warn(
      `[PS security events] OTLP log export failed (${detail}); further failures muted`
    );
  }
}

// ---------------------------------------------------------------------------
// Process-wide exporter (configured once, from process.env)

let exporter: OtlpLogsExporter | null | undefined;

/** The exporter, or null when OTLP export is off. */
export function securityLogsExporter(): OtlpLogsExporter | null {
  if (exporter === undefined) {
    const config =
      typeof process !== 'undefined' && process.env
        ? securityLogsOtlpConfig(process.env)
        : null;
    exporter = config ? new OtlpLogsExporter(config) : null;
  }
  return exporter;
}

/**
 * Send what is queued and wait for sends in progress, giving up after
 * `timeoutMs` (shutdown).
 */
export async function flushSecurityLogsExporter(
  timeoutMs = 3000
): Promise<void> {
  if (!exporter) return;
  let timer: any;
  await Promise.race([
    exporter.drain(),
    new Promise<void>((r) => {
      timer = setTimeout(r, timeoutMs);
    }),
  ]);
  clearTimeout(timer);
}

/** Test helper: forget the exporter (re-read on next use) or set one. */
export function _setSecurityLogsExporterForTests(
  e: OtlpLogsExporter | null | undefined
) {
  exporter = e;
}
