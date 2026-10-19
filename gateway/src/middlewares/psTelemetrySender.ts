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
 * Back-off (as in the SDK): a 429 sets one shared resume deadline from its
 * Retry-After (1 s without one, at most 300 s). Until then nothing is sent:
 * new events and events due for a retry wait in one queue, in the order they
 * were enqueued, still within the `maxPending` bound (overflow is counted as
 * `rate_limited`). At the deadline the queue is sent oldest first, at most
 * DRAIN_CONCURRENCY requests at a time, so the collector is not hit by every
 * waiting event at once; a 429 among them pauses the rest again. Events that
 * arrive once sending has resumed are sent at once, as before the 429, so a
 * backlog never throttles live traffic to the drain rate.
 *
 * Shutdown: shutdown(timeoutMs) stops accepting events, ends any back-off
 * (there is no later retry, as in the SDK's close()) and waits up to
 * `timeoutMs` (PS_TELEMETRY_SHUTDOWN_TIMEOUT_MS, default 5000) for pending
 * events; those still pending then are counted as `shutdown`. The Node
 * server calls it on SIGTERM/SIGINT; on edge runtimes each send is handed to
 * `waitUntil` instead and shutdown() is not used.
 *
 * Runtime-neutral: only fetch, setTimeout and crypto.randomUUID (a timer is
 * unref'd only where the runtime has unref).
 *
 * Fail-open: send() never throws, and never blocks the LLM response (callers
 * do not await it on the request path).
 */

import { telemetryDroppedTotal } from './psMetrics/security';

// Drop reasons (a superset of the SDK's DROP_REASONS):
//   buffer_overflow       too many events already waiting to be delivered
//                         while sends are succeeding
//   send_failed           network error or non-2xx response on every attempt
//                         (also used for overflow while sends are failing)
//   rate_limited          429 on the last attempt (also overflow while
//                         backing off after a 429)
//   server_skipped        a 2xx response reported rows it skipped (final)
//   shutdown              still pending when shutdown() timed out, or sent
//                         after it (the SDK's name for events still buffered
//                         at close)
//   serialization_failed  the event could not be JSON-encoded
export const DROP_REASONS = [
  'buffer_overflow',
  'send_failed',
  'rate_limited',
  'server_skipped',
  'shutdown',
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
/** Back-off after a 429 without a usable Retry-After (the SDK's 1 s). */
export const DEFAULT_RETRY_AFTER_MS = 1000;
/** How long shutdown() waits for pending events by default. */
export const DEFAULT_SHUTDOWN_TIMEOUT_MS = 5000;
/** Requests in flight at once while the back-off queue drains. */
export const DRAIN_CONCURRENCY = 8;

export interface SenderOptions {
  url: string;
  apiKey: string;
  maxPending?: number;
  maxAttempts?: number;
  retryBaseMs?: number;
  /** Default wait in shutdown() before pending events count as `shutdown`. */
  shutdownTimeoutMs?: number;
  /** Defaults to the global fetch at call time (tests replace it). */
  fetch?: typeof fetch;
  sleep?: (ms: number) => Promise<void>;
}

export interface SenderStats {
  client_id: string;
  enqueued: number;
  sent: number;
  /** Events not yet sent or dropped (in flight or queued). */
  pending: number;
  /** Of those, events waiting in the back-off queue. */
  queued: number;
  /** Time left before sends resume after a 429 (0 when not backing off). */
  retry_after_ms: number;
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
    // Never keep the process alive just to retry telemetry (Node only;
    // other runtimes have no unref).
    if (typeof t?.unref === 'function') t.unref();
  });
}

function positiveInt(raw: string | undefined, fallback: number): number {
  const n = Number(raw);
  return Number.isInteger(n) && n > 0 ? n : fallback;
}

function nonNegativeInt(raw: string | undefined, fallback: number): number {
  if (raw === undefined || raw.trim() === '') return fallback;
  const n = Number(raw);
  return Number.isInteger(n) && n >= 0 ? n : fallback;
}

/** One event on its way to the collector. */
interface Pending {
  /** Enqueue order: the back-off queue is kept sorted by it. */
  id: number;
  event: unknown;
  attempts: number;
  /** Sent, dropped or abandoned at shutdown: nothing more is counted. */
  settled: boolean;
  resolve: () => void;
}

type Outcome =
  | { ok: true; skipped: number }
  | { ok: false; reason: DropReason; final?: boolean };

export class PSTelemetrySender {
  readonly clientId: string = crypto.randomUUID().replace(/-/g, '');
  readonly shutdownTimeoutMs: number;
  private seq = 0;
  private enqueued = 0;
  private sent = 0;
  private dropped: Record<DropReason, number> = Object.fromEntries(
    DROP_REASONS.map((r) => [r, 0])
  ) as Record<DropReason, number>;
  // Why deliveries are backing up (undefined while sends succeed); overflow
  // is attributed to it, as the SDK attributes evictions.
  private backlogReason: DropReason | undefined;

  private nextId = 0;
  /** Every event not yet settled; its size is the pending count. */
  private readonly live = new Set<Pending>();
  /** Events waiting for the back-off to end, oldest first. */
  private readonly queue: Pending[] = [];
  /** Set by a 429; cleared by the drain loop at the deadline, or shutdown(). */
  private paused = false;
  private resumeAt = 0; // Date.now() before which nothing is sent
  private pauses = 0; // 429s seen, so the drain loop notices a new deadline
  private draining = false;
  private wakeDrain: (() => void) | undefined;
  private closed = false;
  private idleWaiters: Array<() => void> = [];

  private readonly maxPending: number;
  private readonly maxAttempts: number;
  private readonly retryBaseMs: number;
  private readonly sleep: (ms: number) => Promise<void>;

  constructor(private readonly opts: SenderOptions) {
    this.maxPending = opts.maxPending ?? DEFAULT_MAX_PENDING;
    this.maxAttempts = Math.max(1, opts.maxAttempts ?? DEFAULT_MAX_ATTEMPTS);
    this.retryBaseMs = opts.retryBaseMs ?? DEFAULT_RETRY_BASE_MS;
    this.shutdownTimeoutMs =
      opts.shutdownTimeoutMs ?? DEFAULT_SHUTDOWN_TIMEOUT_MS;
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
      shutdownTimeoutMs: nonNegativeInt(
        env.PS_TELEMETRY_SHUTDOWN_TIMEOUT_MS,
        DEFAULT_SHUTDOWN_TIMEOUT_MS
      ),
    });
  }

  stats(): SenderStats {
    const dropped = { ...this.dropped };
    return {
      client_id: this.clientId,
      enqueued: this.enqueued,
      sent: this.sent,
      pending: this.live.size,
      queued: this.queue.length,
      retry_after_ms: this.paused ? Math.max(0, this.resumeAt - Date.now()) : 0,
      dropped,
      dropped_total: Object.values(dropped).reduce((a, b) => a + b, 0),
    };
  }

  /** Deliver one event. Never throws; resolves once it is sent or dropped. */
  async send(event: unknown): Promise<void> {
    if (!this.opts.apiKey) return; // telemetry off: nothing is enqueued
    try {
      this.enqueued += 1;
      if (this.closed) {
        this.drop('shutdown');
        return;
      }
      if (this.live.size >= this.maxPending) {
        this.drop(this.backlogReason ?? 'buffer_overflow');
        return;
      }
      await new Promise<void>((resolve) => {
        const item: Pending = {
          id: this.nextId++,
          event,
          attempts: 0,
          settled: false,
          resolve,
        };
        this.live.add(item);
        // Behind the back-off. Once it has ended, new events go straight
        // out while the queue drains: queuing them behind it would hold live
        // traffic to the drain rate, and overflow it, for good.
        if (this.paused) this.requeue(item);
        else void this.process(item);
      });
    } catch (err) {
      console.warn('[PS Telemetry] send failed:', err);
    }
  }

  /**
   * Stop accepting events and wait up to `timeoutMs` for pending ones; those
   * still pending then are counted as `shutdown`. A 429 back-off is ended
   * first: there is no later retry. Never throws.
   */
  async shutdown(timeoutMs: number = this.shutdownTimeoutMs): Promise<void> {
    try {
      this.closed = true;
      if (this.paused) {
        this.paused = false;
        this.resumeAt = 0;
        this.wakeDrain?.();
      }
      if (this.live.size) {
        let timer: ReturnType<typeof setTimeout> | undefined;
        // Not unref'd: the process should wait (bounded) for the flush.
        const expired = new Promise<void>((r) => {
          timer = setTimeout(r, Math.max(0, timeoutMs));
        });
        const idle = new Promise<void>((r) => this.idleWaiters.push(r));
        await Promise.race([idle, expired]);
        clearTimeout(timer);
      }
      const left = [...this.live];
      if (left.length) {
        this.queue.length = 0;
        this.drop('shutdown', left.length);
        for (const item of left) this.settle(item);
      }
    } catch (err) {
      console.warn('[PS Telemetry] shutdown failed:', err);
    }
  }

  private drop(reason: DropReason, n = 1) {
    this.dropped[reason] += n;
    telemetryDroppedTotal.inc({ sink: 'ps_telemetry', reason }, n);
    console.warn(`[PS Telemetry] dropped ${n} event(s) (${reason})`);
  }

  private settle(item: Pending) {
    if (item.settled) return;
    item.settled = true;
    this.live.delete(item);
    item.resolve();
    if (!this.live.size) {
      const waiters = this.idleWaiters;
      this.idleWaiters = [];
      for (const w of waiters) w();
    }
  }

  /** Pause all sending until `delayMs` from now (the latest 429 wins). */
  private pause(delayMs: number) {
    this.paused = true;
    this.pauses += 1;
    this.resumeAt = Date.now() + delayMs;
    console.warn(
      `[PS Telemetry] rate limited; pausing sends for ${delayMs} ms`
    );
  }

  /** Put an event in the back-off queue, in enqueue order, and drain it. */
  private requeue(item: Pending) {
    let i = this.queue.length;
    while (i > 0 && this.queue[i - 1].id > item.id) i--;
    this.queue.splice(i, 0, item);
    void this.drain();
  }

  /** Resolves after `ms`, or earlier when shutdown() ends the back-off. */
  private waitForResume(ms: number): Promise<void> {
    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        if (this.wakeDrain === finish) this.wakeDrain = undefined;
        resolve();
      };
      this.wakeDrain = finish;
      this.sleep(ms).then(finish, finish);
    });
  }

  /**
   * The one loop that sends queued events: it waits out the back-off, then
   * sends them oldest first, DRAIN_CONCURRENCY at a time (a 429 among them
   * pauses it again).
   */
  private async drain(): Promise<void> {
    if (this.draining) return;
    this.draining = true;
    try {
      while (this.queue.length) {
        while (this.paused) {
          const seen = this.pauses;
          await this.waitForResume(Math.max(0, this.resumeAt - Date.now()));
          // A 429 that arrived meanwhile set a new deadline: wait for it.
          if (this.pauses === seen) this.paused = false;
        }
        const workers: Promise<void>[] = [];
        for (let i = 0; i < DRAIN_CONCURRENCY && this.queue.length; i++)
          workers.push(this.drainWorker());
        await Promise.all(workers);
      }
    } catch (err) {
      console.warn('[PS Telemetry] drain failed:', err);
    } finally {
      this.draining = false;
    }
  }

  /** Send queued events one after another until empty or paused again. */
  private async drainWorker(): Promise<void> {
    while (this.queue.length && !this.paused) {
      const item = this.queue.shift();
      if (item && !item.settled) await this.process(item);
    }
  }

  /**
   * Attempt delivery until the event is settled, or hand it to the back-off
   * queue when a 429 (its own or another event's) pauses sending.
   */
  private async process(item: Pending): Promise<void> {
    try {
      for (;;) {
        const outcome = await this.attempt(item.event);
        if (item.settled) return; // abandoned at shutdown: already counted
        if (outcome.ok) {
          this.backlogReason = undefined;
          this.sent += 1 - outcome.skipped;
          if (outcome.skipped) this.drop('server_skipped', outcome.skipped);
          return this.settle(item);
        }
        item.attempts += 1;
        if (outcome.final || item.attempts >= this.maxAttempts) {
          this.drop(outcome.reason);
          return this.settle(item);
        }
        if (this.paused) return this.requeue(item);
        await this.sleep(this.retryBaseMs * 2 ** (item.attempts - 1));
        if (item.settled) return;
        // A 429 elsewhere paused sending while this one waited.
        if (this.paused) return this.requeue(item);
      }
    } catch (err) {
      console.warn('[PS Telemetry] send failed:', err);
      if (!item.settled) {
        this.drop('send_failed');
        this.settle(item);
      }
    }
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

  /** One request for one event. Never throws; a 429 starts the back-off. */
  private async attempt(event: unknown): Promise<Outcome> {
    let body: string;
    try {
      body = this.body([event]);
    } catch {
      // Retrying cannot help: the event itself does not serialise.
      return { ok: false, reason: 'serialization_failed', final: true };
    }
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
      if (resp.ok) return { ok: true, skipped: await skippedOf(resp) };
      if (resp.status === 429) {
        this.backlogReason = 'rate_limited';
        this.pause(
          parseRetryAfterMs(resp.headers?.get?.('retry-after')) ??
            DEFAULT_RETRY_AFTER_MS
        );
        return { ok: false, reason: 'rate_limited' };
      }
      console.warn(`[PS Telemetry] send failed: ${resp.status}`);
    } catch (err) {
      console.warn('[PS Telemetry] send error:', err);
    }
    this.backlogReason = 'send_failed';
    return { ok: false, reason: 'send_failed' };
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
