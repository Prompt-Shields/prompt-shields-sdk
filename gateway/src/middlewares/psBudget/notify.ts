/**
 * Budget threshold notifications (#29, #347). When a settle moves a counter
 * across a budget's warn share (`warn_at` x limit) or its limit, the gateway:
 *
 * - counts it in `ps_gateway_budget_threshold_crossings_total{budget,
 *   window,threshold}` (the Prometheus alert rules in
 *   deploy/prometheus/alerts.yaml page on it);
 * - writes one JSON line, `"log.logger":"ps.budget"`, with the event;
 * - with PS_BUDGET_WEBHOOK_URL, POSTs the event there, signed like the
 *   collector's webhooks when PS_BUDGET_WEBHOOK_SECRET is set
 *   (`Prompt-Shields-Signature: t=<unix>,v1=<hex HMAC-SHA256 of
 *   "<t>." + body>`), with `Prompt-Shields-Event-Id` and
 *   `Prompt-Shields-Event-Type: budget.threshold_crossed`. One attempt,
 *   5 s timeout, never blocking or failing the call; https only unless
 *   PS_BUDGET_WEBHOOK_ALLOW_HTTP=true, and the host must pass the gateway's
 *   egress check (private hosts need TRUSTED_CUSTOM_HOSTS).
 *
 * The settle is atomic (one Lua script), so with a shared Redis exactly one
 * replica (or SDK process) sees each crossing and reports it.
 */
import { isSafeUpstreamURL } from '../requestValidator';
import { upstreamFetch } from '../../utils/fetch';
import { Budget } from './config';
import { budgetThresholdCrossingsTotal } from './metrics';

export const THRESHOLD_EVENT_TYPE = 'budget.threshold_crossed';
export const WEBHOOK_TIMEOUT_MS = 5_000;

export interface BudgetThresholdEvent {
  type: typeof THRESHOLD_EVENT_TYPE;
  event_id: string;
  occurred_at: string;
  namespace: string;
  budget_id: string;
  threshold: 'warn' | 'exceeded';
  scope: string;
  /** The attribute value ('_' for global; a user budget's is a hash). */
  subject: string;
  window: string;
  period: string;
  unit: 'usd' | 'tokens';
  limit: number;
  /** The counter after the write, in `unit`. */
  spent: number;
  mode: string;
}

export interface OnCounter {
  budget: Budget;
  subject: string;
  period: string;
}

/** The thresholds crossed by a write from `before` to `after` (before < t <= after). */
export function thresholdCrossings(
  onCounter: OnCounter[],
  namespace: string,
  before: number,
  after: number,
  nowMs: number
): BudgetThresholdEvent[] {
  const out: BudgetThresholdEvent[] = [];
  if (!(after > before)) return out;
  const seen = new Set<string>();
  for (const { budget: b, subject, period } of onCounter) {
    if (seen.has(b.id)) continue;
    seen.add(b.id);
    const marks: [BudgetThresholdEvent['threshold'], number][] = [];
    if (b.warnAt < 1) marks.push(['warn', b.warnAt * b.limit]);
    marks.push(['exceeded', b.limit]);
    for (const [threshold, mark] of marks) {
      if (before < mark && mark <= after)
        out.push({
          type: THRESHOLD_EVENT_TYPE,
          event_id: crypto.randomUUID(),
          occurred_at: new Date(nowMs).toISOString(),
          namespace,
          budget_id: b.id,
          threshold,
          scope: b.scope,
          subject,
          window: b.window,
          period,
          unit: b.unit,
          limit: b.limit,
          spent: after,
          mode: b.mode,
        });
    }
  }
  return out;
}

const toHex = (buf: ArrayBuffer) =>
  [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');

/** `t=<ts>,v1=<hex>`: the collector's webhook signature format. */
export async function signPayload(
  secret: string,
  body: string,
  timestamp: number
): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw',
    enc.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign']
  );
  const mac = await crypto.subtle.sign(
    'HMAC',
    key,
    enc.encode(`${timestamp}.${body}`)
  );
  return `t=${timestamp},v1=${toHex(mac)}`;
}

const isTrue = (v?: string) =>
  ['true', '1', 'yes'].includes((v ?? '').trim().toLowerCase());

let warnedUrl: string | undefined;

/** The webhook URL when set and allowed, else undefined (logged once). */
export function webhookUrlOf(
  env: Record<string, string | undefined>
): string | undefined {
  const raw = env.PS_BUDGET_WEBHOOK_URL?.trim();
  if (!raw) return undefined;
  let reason: string | undefined;
  try {
    const url = new URL(raw);
    if (
      url.protocol !== 'https:' &&
      !(url.protocol === 'http:' && isTrue(env.PS_BUDGET_WEBHOOK_ALLOW_HTTP))
    )
      reason = 'it must be https (or set PS_BUDGET_WEBHOOK_ALLOW_HTTP=true)';
    else if (!isSafeUpstreamURL(raw))
      reason =
        'it is not an allowed egress target; list private or loopback hosts in TRUSTED_CUSTOM_HOSTS';
  } catch {
    reason = 'it is not a valid URL';
  }
  if (reason) {
    if (warnedUrl !== raw) {
      warnedUrl = raw;
      console.warn(`[PS Budget] PS_BUDGET_WEBHOOK_URL ignored: ${reason}`);
    }
    return undefined;
  }
  return raw;
}

export type Fetch = (url: string, init: RequestInit) => Promise<Response>;

async function postWebhook(
  url: string,
  event: BudgetThresholdEvent,
  secret: string | undefined,
  doFetch: Fetch
): Promise<void> {
  const body = JSON.stringify(event);
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'Prompt-Shields-Event-Id': event.event_id,
    'Prompt-Shields-Event-Type': THRESHOLD_EVENT_TYPE,
  };
  if (secret)
    headers['Prompt-Shields-Signature'] = await signPayload(
      secret,
      body,
      Math.floor(Date.now() / 1000)
    );
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), WEBHOOK_TIMEOUT_MS);
  try {
    const res = await doFetch(url, {
      method: 'POST',
      headers,
      body,
      signal: controller.signal,
    });
    if (!res.ok)
      console.warn(
        `[PS Budget] threshold webhook answered HTTP ${res.status} for ${event.budget_id}`
      );
  } catch (err) {
    console.warn(
      `[PS Budget] threshold webhook failed for ${event.budget_id}: ${(err as Error)?.message}`
    );
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Report crossings: metric, log line and (configured) webhook. Resolves when
 * the webhook attempts end; never rejects.
 */
export async function notifyThresholds(
  events: BudgetThresholdEvent[],
  env: Record<string, string | undefined>,
  doFetch: Fetch = upstreamFetch
): Promise<void> {
  if (!events.length) return;
  const url = webhookUrlOf(env);
  const secret = env.PS_BUDGET_WEBHOOK_SECRET || undefined;
  const posts: Promise<void>[] = [];
  for (const event of events) {
    budgetThresholdCrossingsTotal.inc({
      budget: event.budget_id,
      window: event.window,
      threshold: event.threshold,
    });
    try {
      console.warn(JSON.stringify({ 'log.logger': 'ps.budget', ...event }));
    } catch {
      /* logging never fails the call */
    }
    if (url) posts.push(postWebhook(url, event, secret, doFetch));
  }
  await Promise.all(posts);
}
