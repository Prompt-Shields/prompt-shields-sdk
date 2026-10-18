/**
 * Delivery of ps-telemetry events to the Prompt Shields collector, with
 * event-loss accounting (#87).
 *
 * Mirrors the Python SDK's TelemetrySender (packages/sdk/prompt_shields/
 * telemetry.py): every request body carries a `metadata` object next to its
 * events,
 *
 *   {"events": [...], "metadata": {"client_id": "...", "seq": 7,
 *    "enqueued_total": 120, "dropped": {"send_failed": 3}, "sdk": "gateway"}}
 *
 * - client_id: random, stable for the life of the process.
 * - seq: per-client monotonic batch counter, incremented for every request
 *   body sent (a retry is a new batch with a new seq, as in the SDK).
 * - enqueued_total: events handed to the sender since the process started.
 * - dropped: cumulative events that will never reach the collector, by
 *   reason (only non-zero reasons are sent).
 *
 * The collector reconciles these into GET /telemetry/loss. The gateway sends
 * one event per request, so a batch holds one event.
 *
 * Fail-open: send() never throws, and never blocks the LLM response (callers
 * do not await it on the request path).
 */

// Drop reasons (a superset of the SDK's DROP_REASONS):
//   buffer_overflow       too many events already waiting to be delivered
//                         while sends are succeeding
//   send_failed           network error or non-2xx response on every attempt
//                         (also used for overflow while sends are failing)
//   rate_limited          429 on the last attempt (also overflow while
//                         backing off after a 429)
//   server_skipped        a 2xx response reported rows it skipped (final)
//   serialization_failed  the event could not be JSON-encoded
export const DROP_REASONS = [
  'buffer_overflow',
  'send_failed',
  'rate_limited',
  'server_skipped',
  'serialization_failed',
] as const;
export type DropReason = (typeof DROP_REASONS)[number];

export const SDK_NAME = 'gateway';
/** Same bound as the SDK's MAX_BUFFER_SIZE. */
export const DEFAULT_MAX_PENDING = 1000;
export const DEFAULT_MAX_ATTEMPTS = 3;
export const DEFAULT_RETRY_BASE_MS = 200;
/** Ceiling on a server-supplied Retry-After (the SDK's 300 s). */
export const MAX_RETRY_AFTER_MS = 300_000;

export interface SenderOptions {
  url: string;
  apiKey: string;
  maxPending?: number;
  maxAttempts?: number;
  retryBaseMs?: number;
  /** Defaults to the global fetch at call time (tests replace it). */
  fetch?: typeof fetch;
  sleep?: (ms: number) => Promise<void>;
}

export interface SenderStats {
  client_id: string;
  enqueued: number;
  sent: number;
  pending: number;
  dropped: Record<DropReason, number>;
  dropped_total: number;
}

/** Milliseconds to wait from a Retry-After header (delta-seconds or HTTP-date). */
export function parseRetryAfterMs(
  value: string | null | undefined
): number | undefined {
  if (!value) return undefined;
  const trimmed = value.trim();
  let ms: number;
  if (/^\d+(\.\d+)?$/.test(trimmed)) {
    ms = parseFloat(trimmed) * 1000;
  } else {
    const when = Date.parse(trimmed);
    if (Number.isNaN(when)) return undefined;
    ms = when - Date.now();
  }
  return Math.min(Math.max(ms, 0), MAX_RETRY_AFTER_MS);
}

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    const t: any = setTimeout(resolve, ms);
    // Never keep the process alive just to retry telemetry.
    if (typeof t?.unref === 'function') t.unref();
  });
}

function positiveInt(raw: string | undefined, fallback: number): number {
  const n = Number(raw);
  return Number.isInteger(n) && n > 0 ? n : fallback;
}

export class PSTelemetrySender {
  readonly clientId: string = crypto.randomUUID().replace(/-/g, '');
  private seq = 0;
  private enqueued = 0;
  private sent = 0;
  private pending = 0;
  private dropped: Record<DropReason, number> = Object.fromEntries(
    DROP_REASONS.map((r) => [r, 0])
  ) as Record<DropReason, number>;
  // Why deliveries are backing up (undefined while sends succeed); overflow
  // is attributed to it, as the SDK attributes evictions.
  private backlogReason: DropReason | undefined;

  private readonly maxPending: number;
  private readonly maxAttempts: number;
  private readonly retryBaseMs: number;
  private readonly sleep: (ms: number) => Promise<void>;

  constructor(private readonly opts: SenderOptions) {
    this.maxPending = opts.maxPending ?? DEFAULT_MAX_PENDING;
    this.maxAttempts = Math.max(1, opts.maxAttempts ?? DEFAULT_MAX_ATTEMPTS);
    this.retryBaseMs = opts.retryBaseMs ?? DEFAULT_RETRY_BASE_MS;
    this.sleep = opts.sleep ?? defaultSleep;
  }

  /** A sender configured from PS_* environment variables. */
  static fromEnv(env: Record<string, string | undefined>): PSTelemetrySender {
    const base = (env.PS_COLLECTOR_URL || 'http://localhost:8000').replace(
      /\/+$/,
      ''
    );
    return new PSTelemetrySender({
      url: `${base}/ingest/events`,
      apiKey: env.PS_API_KEY || '',
      maxPending: positiveInt(
        env.PS_TELEMETRY_MAX_PENDING,
        DEFAULT_MAX_PENDING
      ),
      maxAttempts: positiveInt(
        env.PS_TELEMETRY_MAX_ATTEMPTS,
        DEFAULT_MAX_ATTEMPTS
      ),
      retryBaseMs: positiveInt(
        env.PS_TELEMETRY_RETRY_BASE_MS,
        DEFAULT_RETRY_BASE_MS
      ),
    });
  }

  stats(): SenderStats {
    const dropped = { ...this.dropped };
    return {
      client_id: this.clientId,
      enqueued: this.enqueued,
      sent: this.sent,
      pending: this.pending,
      dropped,
      dropped_total: Object.values(dropped).reduce((a, b) => a + b, 0),
    };
  }

  /** Deliver one event. Never throws; resolves once it is sent or dropped. */
  async send(event: unknown): Promise<void> {
    if (!this.opts.apiKey) return; // telemetry off: nothing is enqueued
    try {
      this.enqueued += 1;
      if (this.pending >= this.maxPending) {
        this.drop(this.backlogReason ?? 'buffer_overflow');
        return;
      }
      this.pending += 1;
      try {
        await this.deliver(event);
      } finally {
        this.pending -= 1;
      }
    } catch (err) {
      console.warn('[PS Telemetry] send failed:', err);
    }
  }

  private drop(reason: DropReason, n = 1) {
    this.dropped[reason] += n;
    console.warn(`[PS Telemetry] dropped ${n} event(s) (${reason})`);
  }

  /** The request body for one batch; takes the next seq only on success. */
  private body(events: unknown[]): string {
    const dropped: Record<string, number> = {};
    for (const r of DROP_REASONS)
      if (this.dropped[r]) dropped[r] = this.dropped[r];
    const seq = this.seq + 1;
    const text = JSON.stringify({
      events,
      metadata: {
        client_id: this.clientId,
        seq,
        enqueued_total: this.enqueued,
        dropped,
        sdk: SDK_NAME,
      },
    });
    this.seq = seq;
    return text;
  }

  private async deliver(event: unknown): Promise<void> {
    let reason: DropReason = 'send_failed';
    for (let attempt = 1; attempt <= this.maxAttempts; attempt++) {
      let body: string;
      try {
        body = this.body([event]);
      } catch {
        // Retrying cannot help: the event itself does not serialise.
        this.drop('serialization_failed');
        return;
      }
      let delay = this.retryBaseMs * 2 ** (attempt - 1);
      try {
        const doFetch = this.opts.fetch ?? globalThis.fetch;
        const resp = await doFetch(this.opts.url, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${this.opts.apiKey}`,
          },
          body,
        });
        if (resp.ok) {
          this.backlogReason = undefined;
          const skipped = await skippedOf(resp);
          this.sent += 1 - skipped;
          if (skipped) this.drop('server_skipped', skipped);
          return;
        }
        if (resp.status === 429) {
          reason = 'rate_limited';
          delay = parseRetryAfterMs(resp.headers?.get?.('retry-after')) ?? 1000;
        } else {
          reason = 'send_failed';
          console.warn(`[PS Telemetry] send failed: ${resp.status}`);
        }
      } catch (err) {
        reason = 'send_failed';
        console.warn('[PS Telemetry] send error:', err);
      }
      this.backlogReason = reason;
      if (attempt < this.maxAttempts) await this.sleep(delay);
    }
    this.drop(reason);
  }
}

/** Rows a 2xx response says it skipped (0 when not reported), capped at 1. */
async function skippedOf(resp: Response): Promise<number> {
  try {
    if (typeof resp.json !== 'function') return 0;
    const body: any = await resp.json();
    const n = body?.skipped;
    return Number.isInteger(n) ? Math.max(0, Math.min(n, 1)) : 0;
  } catch {
    return 0;
  }
}
